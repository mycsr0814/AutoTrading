# -*- coding: utf-8 -*-
"""
백테스트 엔진 (실전 근사).
- 1봉씩 순차 처리하여 미래 참조(look-ahead) 방지.
- 수수료·슬리피지·진입/청산 체결 방식 반영.

[백테스트 오류 방지]
- 지표/신호는 해당 봉 마감 시점만 사용 (merge_asof backward).
- 동일 봉 내: 청산가 → 손절 → 익절 순 검사 (손절 우선 보수적 가정).
- 진입: FILL_ON_NEXT_BAR_OPEN 시 다음 봉에서만 체결; ENTRY_LIMIT_AT_LEVEL 시 지정가 체결 시뮬(레벨 터치 시 메이커).
"""
from dataclasses import dataclass, field
from typing import List, Optional
import pandas as pd
import numpy as np

import config
from candles import ensure_ohlcv, add_engulfing_flags
from strategy import run_signal_on_bar, StrategyState, _reset_state_after_exit


def _add_4h_trend_to_1h(df: pd.DataFrame) -> pd.DataFrame:
    """
    1h 봉 데이터에 '마지막 마감된 4h 봉' 종가·EMA50 컬럼 추가.
    추세 필터: 4h 종가 > 4h EMA50 → 롱만.
    미래 참조 없이 과거 4h 봉만 사용. 원본 봉 순서 유지.
    """
    if df is None or len(df) < 2:
        return df
    df = df.copy()
    if "open_time" not in df.columns:
        return df
    df["_dt"] = pd.to_datetime(df["open_time"], unit="ms")
    df_sorted = df.sort_values("_dt").reset_index(drop=True)
    # 4h 리샘플 (시작 시점 기준)
    resampled = df_sorted.set_index("_dt").resample("4h").agg({
        "open": "first", "high": "max", "low": "min", "close": "last"
    }).dropna(how="all")
    if resampled.empty or len(resampled) < config.TREND_EMA_PERIOD:
        df["trend_4h_close"] = np.nan
        df["trend_4h_ema50"] = np.nan
        df["atr_4h"] = np.nan
        df["trend_4h_body"] = np.nan
        df["trend_4h_upper_wick"] = np.nan
        df["trend_4h_lower_wick"] = np.nan
        df["trend_4h_low"] = np.nan
        df["trend_4h_high"] = np.nan
        df = df.drop(columns=["_dt"], errors="ignore")
        return df
    resampled["ema50"] = resampled["close"].ewm(span=config.TREND_EMA_PERIOD, adjust=False).mean()
    # 4h ATR (트레일링 스탑 거리 넓히기용)
    prev_close_4h = resampled["close"].shift(1)
    tr_4h = pd.concat([
        resampled["high"] - resampled["low"],
        (resampled["high"] - prev_close_4h).abs(),
        (resampled["low"] - prev_close_4h).abs(),
    ], axis=1).max(axis=1)
    resampled["atr_4h"] = tr_4h.rolling(window=getattr(config, "ATR_PERIOD", 14), min_periods=1).mean()
    # 4h 봉 몸통·윗꼬리·아랫꼬리 (익절 조건: 윗꼬리 > N*몸통 시 롱 익절)
    resampled["body_4h"] = (resampled["close"] - resampled["open"]).abs()
    resampled["upper_wick_4h"] = resampled["high"] - resampled[["open", "close"]].max(axis=1)
    resampled["lower_wick_4h"] = resampled[["open", "close"]].min(axis=1) - resampled["low"]
    resampled["close_time_4h"] = (resampled.index + pd.Timedelta(hours=config.TREND_TIMEFRAME_HOURS)).astype("datetime64[ns]")
    merge_right = resampled[
        ["close_time_4h", "close", "ema50", "atr_4h", "body_4h", "upper_wick_4h", "lower_wick_4h", "low", "high"]
    ].rename(columns={"close": "trend_4h_close", "ema50": "trend_4h_ema50", "low": "trend_4h_low", "high": "trend_4h_high"})
    left_key = df_sorted["_dt"].astype("datetime64[ns]")
    merged = pd.merge_asof(
        df_sorted.assign(_dt_ns=left_key),
        merge_right.sort_values("close_time_4h"),
        left_on="_dt_ns",
        right_on="close_time_4h",
        direction="backward",
    )
    # 원본 df 순서대로 매핑 (open_time 기준)
    by_ot = merged.set_index("open_time")
    df["trend_4h_close"] = df["open_time"].map(by_ot["trend_4h_close"])
    df["trend_4h_ema50"] = df["open_time"].map(by_ot["trend_4h_ema50"])
    df["atr_4h"] = df["open_time"].map(by_ot["atr_4h"])
    df["trend_4h_body"] = df["open_time"].map(by_ot["body_4h"])
    df["trend_4h_upper_wick"] = df["open_time"].map(by_ot["upper_wick_4h"])
    df["trend_4h_lower_wick"] = df["open_time"].map(by_ot["lower_wick_4h"])
    df["trend_4h_low"] = df["open_time"].map(by_ot["trend_4h_low"])
    df["trend_4h_high"] = df["open_time"].map(by_ot["trend_4h_high"])
    df = df.drop(columns=["_dt"], errors="ignore")
    return df


def _add_daily_trend_to_1h(df: pd.DataFrame) -> pd.DataFrame:
    """
    1h 봉에 '마지막 마감된 일봉' 종가·EMA20·EMA50·하락장저항 플래그 추가.
    일봉 추세 필터: 일봉 종가 > 일봉 EMA → 롱만.
    """
    if df is None or len(df) < 2:
        return df
    use = getattr(config, "DAILY_TREND_FILTER", False)
    bear_enabled = getattr(config, "BEAR_MARKET_RESISTANCE_ENABLED", False)
    bear_regime_enabled = getattr(config, "BEAR_REGIME_DEATH_CROSS_ENABLED", False)
    strict_filter = getattr(config, "BEAR_MARKET_STRICT_LONG_FILTER", False)
    daily_pullback = getattr(config, "DAILY_PULLBACK_LONG_ENABLED", False)
    if not use and not bear_enabled and not bear_regime_enabled and not strict_filter and not daily_pullback:
        return df
    df = df.copy()
    if "open_time" not in df.columns:
        return df
    df["_dt"] = pd.to_datetime(df["open_time"], unit="ms")
    df_sorted = df.sort_values("_dt").reset_index(drop=True)
    period = getattr(config, "DAILY_EMA_PERIOD", 20)
    period50 = getattr(config, "DAILY_EMA_50_PERIOD", 50)
    resampled = df_sorted.set_index("_dt").resample("1D").agg({
        "open": "first", "high": "max", "low": "min", "close": "last"
    }).dropna(how="all")
    if resampled.empty or len(resampled) < max(period, period50):
        df["trend_daily_close"] = np.nan
        df["trend_daily_ema"] = np.nan
        df["trend_daily_ema50"] = np.nan
        df["bear_market_resistance"] = False
        df["bear_regime"] = False
        df["bear_market_strict"] = False
        if daily_pullback:
            df["prev_daily_high"] = np.nan
            df["prev_daily_low"] = np.nan
        df = df.drop(columns=["_dt"], errors="ignore")
        return df
    resampled["ema"] = resampled["close"].ewm(span=period, adjust=False).mean()
    resampled["ema50"] = resampled["close"].ewm(span=period50, adjust=False).mean()
    # 일봉 윗꼬리 + EMA 저항: high > body_high 이고, high가 EMA 근처에서 막히고(close < EMA) 저항 확인
    body_high = resampled[["open", "close"]].max(axis=1)
    has_upper_wick = (resampled["high"] > body_high) & (resampled["high"] > 0)
    near_ema20 = (resampled["high"] - resampled["ema"]).abs() / resampled["ema"].replace(0, np.nan) <= getattr(config, "BEAR_MARKET_EMA_NEAR_PCT", 0.005)
    near_ema50 = (resampled["high"] - resampled["ema50"]).abs() / resampled["ema50"].replace(0, np.nan) <= getattr(config, "BEAR_MARKET_EMA_NEAR_PCT", 0.005)
    rejection = (near_ema20 & (resampled["close"] < resampled["ema"])) | (near_ema50 & (resampled["close"] < resampled["ema50"]))
    resistance_bar = has_upper_wick & rejection
    lookback = getattr(config, "BEAR_MARKET_LOOKBACK_DAYS", 3)
    min_days = getattr(config, "BEAR_MARKET_MIN_DAYS_WITH_WICK", 2)
    roll_count = resistance_bar.astype(int).rolling(window=lookback, min_periods=1).sum()
    resampled["bear_market_resistance"] = roll_count >= min_days if bear_enabled else False
    # 일봉 데스 크로스(EMA20 < EMA50): 연속 N일 지속 시에만 하락장으로 간주 (상승장 일시 조정 시 숏만 진입 방지)
    if bear_regime_enabled or bear_enabled:
        death_cross_bar = resampled["ema"] < resampled["ema50"]
        min_days_death = getattr(config, "BEAR_REGIME_DEATH_CROSS_MIN_DAYS", 1)
        if min_days_death <= 1:
            resampled["bear_regime"] = death_cross_bar
        else:
            # 연속 min_days_death일 이상 모두 EMA20 < EMA50 일 때만 True
            resampled["bear_regime"] = death_cross_bar.astype(int).rolling(window=min_days_death, min_periods=min_days_death).sum() >= min_days_death
    # 하락장 롱 엄격 필터: 일봉 종가가 EMA50 아래 연속 N일이면 롱 허용을 더 강하게 (4h에서 더 높은 기준)
    if strict_filter:
        strict_days = getattr(config, "BEAR_MARKET_STRICT_LONG_DAYS", 5)
        resampled["bear_market_strict"] = (resampled["close"] < resampled["ema50"]).astype(int).rolling(strict_days, min_periods=strict_days).sum() >= strict_days
    resampled["_merge_key"] = (resampled.index + pd.Timedelta(days=1)).astype("datetime64[ns]")
    merge_rename = {"close": "trend_daily_close", "ema": "trend_daily_ema", "ema50": "trend_daily_ema50"}
    if daily_pullback:
        merge_rename["high"] = "prev_daily_high"
        merge_rename["low"] = "prev_daily_low"
    need_ema50 = bear_regime_enabled or bear_enabled or strict_filter
    merge_cols = ["_merge_key", "close", "ema"] + (["high", "low"] if daily_pullback else []) + (["ema50"] if need_ema50 else []) + (["bear_regime"] if (bear_regime_enabled or bear_enabled) else []) + (["bear_market_resistance"] if bear_enabled else []) + (["bear_market_strict"] if strict_filter else [])
    merge_right = resampled[merge_cols].rename(columns=merge_rename).sort_values("_merge_key")
    left_key = df_sorted["_dt"].astype("datetime64[ns]")
    merged = pd.merge_asof(
        df_sorted.assign(_dt_ns=left_key),
        merge_right,
        left_on="_dt_ns",
        right_on="_merge_key",
        direction="backward",
    )
    by_ot = merged.set_index("open_time")
    df["trend_daily_close"] = df["open_time"].map(by_ot["trend_daily_close"])
    df["trend_daily_ema"] = df["open_time"].map(by_ot["trend_daily_ema"])
    if need_ema50:
        df["trend_daily_ema50"] = df["open_time"].map(by_ot["trend_daily_ema50"])
    else:
        df["trend_daily_ema50"] = np.nan
    if bear_regime_enabled or bear_enabled:
        _br = df["open_time"].map(by_ot["bear_regime"])
        df["bear_regime"] = _br.where(_br.notna(), other=False).astype(bool)
    else:
        df["bear_regime"] = False
    if bear_enabled:
        _bmr = df["open_time"].map(by_ot["bear_market_resistance"])
        df["bear_market_resistance"] = _bmr.where(_bmr.notna(), other=False).astype(bool)
    else:
        df["bear_market_resistance"] = False
    if strict_filter:
        _bms = df["open_time"].map(by_ot["bear_market_strict"])
        df["bear_market_strict"] = _bms.where(_bms.notna(), other=False).astype(bool)
    else:
        df["bear_market_strict"] = False
    if daily_pullback:
        df["prev_daily_high"] = df["open_time"].map(by_ot["prev_daily_high"])
        df["prev_daily_low"] = df["open_time"].map(by_ot["prev_daily_low"])
    df = df.drop(columns=["_dt"], errors="ignore")
    return df


def _add_atr(df: pd.DataFrame, period: int = None) -> pd.DataFrame:
    """1h 봉에 ATR 컬럼 추가 (트레일링 스탑용). 미래 참조 없이 해당 봉까지의 데이터만 사용."""
    if df is None or len(df) < 2:
        return df
    period = period or getattr(config, "ATR_PERIOD", 14)
    df = df.copy()
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(window=period, min_periods=1).mean()
    return df


@dataclass
class BacktestTrade:
    """단일 거래 기록."""
    bar_idx: int
    action: str  # OPEN_1, OPEN_2, TP_FIRST, STOP, TRAILING_STOP, TREND_BREAK_EXIT, EOD_EXIT, EOY_PROFIT_LOCK, GIVEBACK_EXIT, REVERSE_ENGULF_EXIT, FINAL_SETTLEMENT
    side: str
    price: float
    size_pct: float
    fee: float
    stop_price: Optional[float] = None
    tp_price: Optional[float] = None
    detail: Optional[dict] = None
    pnl: Optional[float] = None  # 청산 시 실현 손익(USDT). 롱/숏별 승률·수익률 집계용.


def _add_atr_percentile(df: pd.DataFrame) -> pd.DataFrame:
    """ATR 백분위(최근 N봉 내) 추가. 변동성 필터용. 미래 참조 없음."""
    if df is None or "atr" not in df.columns:
        return df
    lookback = getattr(config, "VOL_ATR_LOOKBACK", 168)
    # 현재 봉 ATR이 롤링 창 내에서 몇 % 위에 있는지 (100 = 최고)
    def _pct(s: pd.Series) -> float:
        if len(s) < 2 or pd.isna(s.iloc[-1]):
            return 50.0
        last = s.iloc[-1]
        return float((s <= last).sum() / len(s) * 100.0)
    df = df.copy()
    df["atr_pct"] = df["atr"].astype(float).rolling(window=lookback, min_periods=min(lookback, len(df))).apply(_pct, raw=False)
    return df


def prepare_1h_df_for_signal(df: pd.DataFrame) -> pd.DataFrame:
    """
    1시간봉 DataFrame에 전략 신호용 지표 추가 (백테스트/실전 공통).
    장악형 플래그, 4h/일봉 추세, ATR 등. 미래 참조 없음.
    """
    if df is None or len(df) < 2:
        return df
    df = ensure_ohlcv(df).copy()
    df = add_engulfing_flags(df)
    df = _add_4h_trend_to_1h(df)
    df = _add_daily_trend_to_1h(df)
    df = _add_atr(df)
    if getattr(config, "VOLATILITY_FILTER_ENABLED", False):
        df = _add_atr_percentile(df)
    return df.reset_index(drop=True)


def _close_all_positions(
    positions: List[dict],
    exit_price: float,
    capital: float,
    leverage: int,
    fee_rate: float,
    bar_idx: int,
    action: str,
    detail: Optional[dict],
) -> tuple:
    """전량 청산: 시장가(테이커) 수수료 + 실전 근사 청산 슬리피지 적용 후 capital 갱신."""
    if not positions:
        return capital, None
    # 실전 근사: 롱 청산(매도)=불리한 방향으로 하락, 숏 청산(매수)=불리한 방향으로 상승
    slip_bps = getattr(config, "EXIT_SLIPPAGE_BPS", None)
    if slip_bps is None:
        slip_bps = getattr(config, "SLIPPAGE_BPS", 0) or 0
    if slip_bps > 0:
        exit_side = positions[0]["side"]
        if exit_side == "LONG":
            exit_price = exit_price * (1.0 - slip_bps / 10000.0)
        else:
            exit_price = exit_price * (1.0 + slip_bps / 10000.0)
    cap_before = capital
    exit_side = positions[0]["side"]
    total_fee = 0.0
    for pos in positions:
        notional = pos["entry_capital"] * pos["size_pct"] * leverage
        fee = notional * fee_rate
        total_fee += fee
        capital -= fee
        if pos["side"] == "LONG":
            capital += notional * (exit_price - pos["entry"]) / pos["entry"]
        else:
            capital += notional * (pos["entry"] - exit_price) / pos["entry"]
    pnl = capital - cap_before
    return capital, BacktestTrade(
        bar_idx=bar_idx,
        action=action,
        side=exit_side,
        price=exit_price,
        size_pct=0,
        fee=total_fee,
        detail=detail,
        pnl=pnl,
    )


def run_backtest(
    df: pd.DataFrame,
    initial_capital: float = 10000.0,
    leverage: int = None,
    fee_rate: float = None,
    fee_entry: float = None,
) -> tuple:
    """
    봉 단위 순차 백테스트. 실전 근사 및 오류 방지:

    [미래 참조 금지]
    - 각 봉 i에서 df.iloc[:i+1]까지만 사용. 지표는 merge_asof(backward)로 해당 봉 마감 시점만 반영.

    [동일 봉 내 처리 순서] (실전과 동일한 체결 우선순위)
    1. 전 봉 신호에 의한 다음 봉 시가 진입 (pending_entry) → 포지션 추가
    2. 청산가(레버리지) 도달 시 강제 청산
    3. run_signal_on_bar() → 손절 검사(우선) → 4h윗꼬리/반대장악/추가진입/추세이탈/트레일링/1차 익절 순
    4. 진입 신호 시 fill_next_bar면 다음 봉에서 체결, 아니면 당일 체결
    5. 자산 하한선 이하 시 강제 청산, 봉 말 미청산 포지션은 종가 기준 손익 반영

    [체결 가정]
    - 진입: FILL_ON_NEXT_BAR_OPEN=True면 다음 봉 시가+진입 슬리피지. 청산: 시장가(테이커)+EXIT_SLIPPAGE_BPS.
    - 동일 봉에서 손절가·익절가 동시 터치 시 손절 우선 검사하여 손절로 처리 (보수적).
    반환: (거래 목록, 봉별 자산 시리즈, 최종 자산, info, 사용한 DataFrame)
    """
    if df is None or len(df) < 3:
        return [], pd.Series(dtype=float), initial_capital, {}, None

    leverage = leverage or config.LEVERAGE
    fee_rate = fee_rate or config.FEE_EFFECTIVE
    fee_entry = fee_entry if fee_entry is not None else getattr(config, "FEE_ENTRY", config.FEE_MAKER)
    df = prepare_1h_df_for_signal(df)

    capital = initial_capital
    state = StrategyState()
    trades: List[BacktestTrade] = []
    positions: List[dict] = []
    n_bars = len(df) - 1  # 루프 횟수 (i=1..len(df)-1)
    equity_curve: List[float] = [0.0] * n_bars  # 사전 할당
    fill_next_bar = getattr(config, "FILL_ON_NEXT_BAR_OPEN", False)
    next_bar_slip_bps = getattr(config, "FILL_NEXT_BAR_SLIPPAGE_BPS", 5) or 0
    fee_maker = getattr(config, "FEE_MAKER", config.FEE_MAKER)
    pending_entry: Optional[dict] = None  # {side, size_pct, stop, tp1, action, bar_idx_signal, detail, limit_price}

    for i in range(1, len(df)):
        row = df.iloc[i]
        prev_row = df.iloc[i - 1]
        low_bar = float(row["low"])
        high_bar = float(row["high"])
        close_price = float(row["close"])
        open_price = float(row["open"])

        # 실전 근사: 전 봉에서 발생한 진입 신호를 현재 봉에서 체결 (동일 봉 look-ahead 제거)
        # ENTRY_LIMIT_AT_LEVEL: 지정가 at 눌림 레벨 시뮬 — 봉 내 가격이 limit_price 터치 시에만 해당 가격에 메이커 체결, 미터치 시 다음 봉까지 대기
        if pending_entry is not None:
            side = pending_entry["side"]
            size_pct = pending_entry["size_pct"]
            limit_price = pending_entry.get("limit_price")
            entry_limit_at_level = getattr(config, "ENTRY_LIMIT_AT_LEVEL", False)
            do_fill = True
            entry_fill = open_price
            fee_entry_use = fee_entry
            if entry_limit_at_level and limit_price is not None and limit_price > 0:
                touched = (side == "LONG" and low_bar <= limit_price) or (side == "SHORT" and high_bar >= limit_price)
                if touched:
                    entry_fill = float(limit_price)
                    fee_entry_use = fee_maker
                else:
                    do_fill = False
            else:
                if next_bar_slip_bps and side == "LONG":
                    entry_fill = open_price * (1.0 + next_bar_slip_bps / 10000.0)
                elif next_bar_slip_bps and side == "SHORT":
                    entry_fill = open_price * (1.0 - next_bar_slip_bps / 10000.0)
            if do_fill:
                notional = capital * size_pct * leverage
                fee = notional * fee_entry_use
                capital -= fee
                positions.append({
                    "entry": entry_fill,
                    "size_pct": size_pct,
                    "entry_capital": capital,
                    "stop": pending_entry["stop"],
                    "tp1": pending_entry["tp1"],
                    "side": side,
                    "tp1_done": False,
                })
                trades.append(
                    BacktestTrade(
                        bar_idx=i,
                        action=pending_entry["action"],
                        side=side,
                        price=entry_fill,
                        size_pct=size_pct,
                        fee=fee,
                        stop_price=pending_entry.get("stop"),
                        tp_price=pending_entry.get("tp1"),
                        detail=pending_entry.get("detail"),
                    )
                )
                pending_entry = None

        # 실거래와 동일: 청산은 "손절보다 가격이 먼저 도달했을 때"만. 로스컷(손절)이 청산가보다 먼저 있으면 손절로 나감.
        # 롱: 가격 하락 시 더 위에 있는 쪽(진입가에 가까운 쪽)을 먼저 침 → 청산가 > 손절가 이면 청산, 아니면 손절.
        # 숏: 가격 상승 시 더 아래 있는 쪽을 먼저 침 → 청산가 < 손절가 이면 청산, 아니면 손절.
        liq_price_enabled = getattr(config, "BACKTEST_LIQUIDATION_PRICE_ENABLED", False)
        if liq_price_enabled and positions:
            stop_price = positions[0].get("stop")  # 동일 셋업이면 손절가는 동일
            liq_triggered = False
            exit_price_liq = None
            exit_side_liq = None
            for pos in positions:
                entry = pos["entry"]
                side = pos["side"]
                stop_p = pos.get("stop")
                if stop_p is None:
                    stop_p = stop_price
                # 거래소 청산가 근사: 롱 = 진입가 대비 1/레버리지 하락, 숏 = 1/레버리지 상승
                if side == "LONG":
                    liq_p = entry * (1.0 - 1.0 / leverage)
                    # 청산가가 손절가보다 위(진입가에 가까움)일 때만 청산. 그렇지 않으면 이 봉에서는 손절이 먼저 터짐.
                    if low_bar <= liq_p and (stop_p is None or liq_p > stop_p):
                        liq_triggered = True
                        exit_price_liq = liq_p if exit_price_liq is None else max(exit_price_liq, liq_p)
                        exit_side_liq = "LONG"
                else:
                    liq_p = entry * (1.0 + 1.0 / leverage)
                    if high_bar >= liq_p and (stop_p is None or liq_p < stop_p):
                        liq_triggered = True
                        exit_price_liq = liq_p if exit_price_liq is None else min(exit_price_liq, liq_p)
                        exit_side_liq = "SHORT"
            if liq_triggered and exit_price_liq is not None:
                cap_before_liq = capital
                exit_side_liq = exit_side_liq or (positions[0]["side"])
                for pos in positions:
                    notional = pos["entry_capital"] * pos["size_pct"] * leverage
                    fee = notional * fee_rate
                    capital -= fee
                    if pos["side"] == "LONG":
                        capital += notional * (exit_price_liq - pos["entry"]) / pos["entry"]
                    else:
                        capital += notional * (pos["entry"] - exit_price_liq) / pos["entry"]
                capital = max(capital, initial_capital * getattr(config, "BACKTEST_LIQUIDATION_PCT", 0.05))
                positions = []
                _reset_state_after_exit(state, i, set_cooldown=False)
                trades.append(
                    BacktestTrade(
                        bar_idx=i,
                        action="LIQUIDATION",
                        side=exit_side_liq,
                        price=exit_price_liq,
                        size_pct=0,
                        fee=0,
                        detail={"reason": "liquidation_price_hit", "leverage": leverage},
                        pnl=capital - cap_before_liq,
                    )
                )
                equity_curve[i - 1] = capital
                continue

        # look-ahead 방지: i번째 봉까지만 전달 (df.iloc[:i+1]). 전략은 미래 봉/미래 지표 참조 불가.
        state, action, detail = run_signal_on_bar(
            i, row, prev_row, df.iloc[: i + 1], state, fee_rate=fee_rate
        )

        if action in ("OPEN_1", "OPEN_2", "OPEN_ADD", "OPEN_TREND_ADD"):
            if detail:
                entry_price = detail.get("entry", close_price)
                stop_price = detail.get("stop")
                tp_price = detail.get("tp1")
                size_pct = detail.get("size_pct", 0.1)
            else:
                entry_price = close_price
                stop_price = None
                tp_price = None
                size_pct = 0.1
            side = state.positions[-1].side if state.positions else "LONG"
            if fill_next_bar:
                pending_entry = {
                    "side": side,
                    "size_pct": size_pct,
                    "stop": stop_price,
                    "tp1": tp_price,
                    "action": action,
                    "bar_idx_signal": i,
                    "detail": detail,
                    "limit_price": detail.get("limit_price") if detail else None,
                }
            else:
                notional = capital * size_pct * leverage
                fee = notional * fee_entry
                capital -= fee
                trades.append(
                    BacktestTrade(
                        bar_idx=i,
                        action=action,
                        side=side,
                        price=entry_price,
                        size_pct=size_pct,
                        fee=fee,
                        stop_price=stop_price,
                        tp_price=tp_price,
                        detail=detail,
                    )
                )
                positions.append({
                    "entry": entry_price,
                    "size_pct": size_pct,
                    "entry_capital": capital,
                    "stop": stop_price,
                    "tp1": tp_price,
                    "side": side,
                    "tp1_done": False,
                })

        elif action == "TP_FIRST":
            cap_before_tp = capital
            tp_fee = 0.0
            close_pct = detail.get("close_pct", config.TP_FIRST_HALF) if detail else config.TP_FIRST_HALF
            # 1차 익절 지정가(TP_FIRST_LIMIT_ORDER)면 메이커 수수료만, 슬리피지 없음(체결가=익절가 그대로)
            use_tp_limit = getattr(config, "TP_FIRST_LIMIT_ORDER", False)
            tp_fee_rate = config.FEE_MAKER if use_tp_limit else fee_rate
            if detail and positions:
                tp_price = detail.get("price", close_price)  # 지정가면 정확히 이 가격에 체결
                for pos in positions:
                    if not pos.get("tp1_done"):
                        pos["tp1_done"] = True
                        notional = pos["entry_capital"] * pos["size_pct"] * leverage * close_pct
                        fee = notional * tp_fee_rate
                        tp_fee = fee
                        capital -= fee
                        if pos["side"] == "LONG":
                            capital += notional * (tp_price - pos["entry"]) / pos["entry"]
                        else:
                            capital += notional * (pos["entry"] - tp_price) / pos["entry"]
                        pos["size_pct"] *= (1 - close_pct)
                        break
                if close_pct >= 1.0:
                    positions = []
                    _reset_state_after_exit(state, i, set_cooldown=False)
            trades.append(
                BacktestTrade(
                    bar_idx=i,
                    action=action,
                    side=state.positions[0].side if state.positions else "LONG",
                    price=detail.get("price", close_price) if detail else close_price,
                    size_pct=detail.get("size_pct", 0) if detail else 0,
                    fee=tp_fee,
                    detail=detail,
                    pnl=capital - cap_before_tp,
                )
            )

        elif action in (
            "STOP",
            "TRAILING_STOP",
            "TREND_BREAK_EXIT",
            "EOD_EXIT",
            "EOY_PROFIT_LOCK",
            "GIVEBACK_EXIT",
            "REVERSE_ENGULF_EXIT",
            "TP_4H_WICK_EXIT",
        ):
            exit_price = (detail.get("price", close_price) if detail else close_price)
            capital, trade = _close_all_positions(
                positions, exit_price, capital, leverage, fee_rate, i, action, detail
            )
            positions = []  # strategy.run_signal_on_bar에서 이미 state 초기화됨
            if trade:
                trades.append(trade)

        # 미실현 손익 (포지션 유지 중)
        total_unrealized = 0.0
        for pos in positions:
            notional = pos["entry_capital"] * pos["size_pct"] * leverage
            if pos["side"] == "LONG":
                total_unrealized += notional * (close_price - pos["entry"]) / pos["entry"]
            else:
                total_unrealized += notional * (pos["entry"] - close_price) / pos["entry"]
        current_equity = capital + total_unrealized

        # 청산 시뮬레이션 (레버리지 백테스트 오류 방지: 자산이 초기자본의 N% 이하로 떨어지면 강제 청산)
        liq_enabled = getattr(config, "BACKTEST_LIQUIDATION_ENABLED", False)
        liq_pct = getattr(config, "BACKTEST_LIQUIDATION_PCT", 0.05)
        if liq_enabled and positions and current_equity < initial_capital * liq_pct:
            cap_before_liq = capital
            exit_side = positions[0]["side"]
            for pos in positions:
                notional = pos["entry_capital"] * pos["size_pct"] * leverage
                fee = notional * fee_rate
                capital -= fee
                if pos["side"] == "LONG":
                    capital += notional * (close_price - pos["entry"]) / pos["entry"]
                else:
                    capital += notional * (pos["entry"] - close_price) / pos["entry"]
            capital = max(capital, initial_capital * liq_pct)  # 남은 자산을 최소 수준으로 제한
            positions = []
            _reset_state_after_exit(state, i, set_cooldown=False)
            trades.append(
                BacktestTrade(
                    bar_idx=i,
                    action="LIQUIDATION",
                    side=exit_side,
                    price=close_price,
                    size_pct=0,
                    fee=0,
                    detail={"reason": "equity_below_threshold", "equity_pct": current_equity / initial_capital},
                    pnl=capital - cap_before_liq,
                )
            )
            total_unrealized = 0.0

        equity_curve[i - 1] = capital + total_unrealized

    # 미청산 포지션을 마지막 봉 종가로 정산 (연도별 거래수에 반영되도록 trades에 기록)
    last_bar_idx = len(df) - 1
    close_price = float(df.iloc[-1]["close"])
    if positions:
        cap_before_final = capital
        exit_side = positions[0]["side"]
        for pos in positions:
            notional = pos["entry_capital"] * pos["size_pct"] * leverage
            fee = notional * fee_rate
            capital -= fee
            if pos["side"] == "LONG":
                capital += notional * (close_price - pos["entry"]) / pos["entry"]
            else:
                capital += notional * (pos["entry"] - close_price) / pos["entry"]
        trades.append(
            BacktestTrade(
                bar_idx=last_bar_idx,
                action="FINAL_SETTLEMENT",
                side=exit_side,
                price=close_price,
                size_pct=0,
                fee=0,
                detail={"reason": "end_of_backtest"},
                pnl=capital - cap_before_final,
            )
        )

    equity_series = pd.Series(equity_curve) if equity_curve else pd.Series(dtype=float)
    info = {
        "n_bars": len(df),
        "first_ts": int(df.iloc[0]["open_time"]) if "open_time" in df.columns else None,
        "last_ts": int(df.iloc[-1]["open_time"]) if "open_time" in df.columns else None,
    }
    return trades, equity_series, capital, info, df


def _risk_metrics(equity: pd.Series, periods_per_year: float = 8760.0) -> dict:
    """
    자산 곡선에서 최대 낙폭(MaxDD), 샤프, 소르티노, 칼마 비율 계산.
    periods_per_year: 1시간봉 기준 8760.
    """
    if equity is None or len(equity) < 2:
        return {"max_drawdown_pct": 0.0, "sharpe_annual": 0.0, "sortino_annual": 0.0, "calmar_annual": 0.0}
    equity = equity.astype(float)
    cummax = equity.cummax()
    drawdown_pct = (equity - cummax) / cummax.replace(0, np.nan)
    max_dd_pct = float(drawdown_pct.min() * 100) if cummax.max() > 0 else 0.0
    rets = equity.pct_change().dropna()
    if len(rets) < 2:
        return {"max_drawdown_pct": max_dd_pct, "sharpe_annual": 0.0, "sortino_annual": 0.0, "calmar_annual": 0.0}
    mean_ret = float(rets.mean())
    std_ret = float(rets.std())
    sharpe = (mean_ret / std_ret * np.sqrt(periods_per_year)) if std_ret > 0 else 0.0
    downside = rets[rets < 0]
    downside_std = float(downside.std()) if len(downside) > 1 else 0.0
    sortino = (mean_ret / downside_std * np.sqrt(periods_per_year)) if downside_std > 0 else (sharpe if mean_ret > 0 else 0.0)
    total_ret = float(equity.iloc[-1] / equity.iloc[0] - 1.0) if equity.iloc[0] > 0 else 0.0
    calmar = (total_ret * 100 / abs(max_dd_pct)) if max_dd_pct != 0 else (total_ret * 100 if total_ret >= 0 else 0.0)
    return {"max_drawdown_pct": max_dd_pct, "sharpe_annual": sharpe, "sortino_annual": sortino, "calmar_annual": calmar}


def _yearly_performance(
    df: pd.DataFrame,
    equity: pd.Series,
    trades: List[BacktestTrade],
    initial: float,
) -> List[dict]:
    """
    봉별 시각 정보와 equity 시리즈로 연도별 수익률·거래 수 계산.
    equity[k] = (df 인덱스 k+1 봉 처리 후) 자산.
    """
    if df is None or "open_time" not in df.columns or equity.empty or len(equity) < 2:
        return []
    df = df.copy()
    df["_year"] = pd.to_datetime(df["open_time"], unit="ms").dt.year
    years = sorted(df["_year"].unique())
    result = []
    for year in years:
        bar_indices = df.index[df["_year"] == year].tolist()
        if not bar_indices:
            continue
        j_min, j_max = min(bar_indices), max(bar_indices)
        # 해당 연도 시작 시점 자산: 직전 봉 처리 후 자산 (j_min-1 봉 처리 후)
        if j_min <= 1:
            start_equity = initial
        else:
            start_equity = float(equity.iloc[j_min - 2])
        # 해당 연도 마지막 봉 처리 후 자산
        end_equity = float(equity.iloc[j_max - 1])
        ret_pct = (end_equity / start_equity - 1) * 100 if start_equity > 0 else 0.0
        # 해당 연도에 발생한 거래 수 (bar_idx는 df 행 인덱스와 동일)
        trades_in_year = [t for t in trades if 0 <= t.bar_idx < len(df) and df.iloc[t.bar_idx]["_year"] == year]
        result.append({
            "year": year,
            "start_equity": start_equity,
            "end_equity": end_equity,
            "return_pct": ret_pct,
            "n_trades": len(trades_in_year),
        })
    return result


# 수익률이 안 좋은 연도만 월별·일별 상세 출력 (22, 25, 26년)
YEARS_WITH_DETAILED_OUTPUT = (2022, 2025, 2026)


def _monthly_performance(
    df: pd.DataFrame,
    equity: pd.Series,
    trades: List[BacktestTrade],
    initial: float,
) -> List[dict]:
    """
    봉별 시각으로 연·월별 수익률·자산 계산.
    equity[k] = (df 인덱스 k+1 봉 처리 후) 자산.
    """
    if df is None or "open_time" not in df.columns or equity.empty or len(equity) < 2:
        return []
    df = df.copy()
    dt = pd.to_datetime(df["open_time"], unit="ms")
    df["_year"] = dt.dt.year
    df["_month"] = dt.dt.month
    result = []
    for (year, month), grp in df.groupby(["_year", "_month"], sort=True):
        bar_indices = grp.index.tolist()
        if not bar_indices:
            continue
        j_min, j_max = min(bar_indices), max(bar_indices)
        if j_min <= 1:
            start_equity = initial
        else:
            start_equity = float(equity.iloc[j_min - 2])
        end_equity = float(equity.iloc[j_max - 1])
        ret_pct = (end_equity / start_equity - 1) * 100 if start_equity > 0 else 0.0
        trades_in_period = [
            t for t in trades
            if 0 <= t.bar_idx < len(df)
            and df.iloc[t.bar_idx]["_year"] == year
            and df.iloc[t.bar_idx]["_month"] == month
        ]
        result.append({
            "year": year,
            "month": month,
            "year_month": f"{year}-{month:02d}",
            "start_equity": start_equity,
            "end_equity": end_equity,
            "return_pct": ret_pct,
            "n_trades": len(trades_in_period),
        })
    return result


def _daily_performance(
    df: pd.DataFrame,
    equity: pd.Series,
    trades: List[BacktestTrade],
    initial: float,
    year: int,
) -> List[dict]:
    """
    특정 연도에 한해 일별 수익률·자산 계산.
    equity[k] = (df 인덱스 k+1 봉 처리 후) 자산.
    """
    if df is None or "open_time" not in df.columns or equity.empty or len(equity) < 2:
        return []
    df = df.copy()
    dt = pd.to_datetime(df["open_time"], unit="ms")
    df["_year"] = dt.dt.year
    df["_date"] = dt.dt.date
    grp_year = df[df["_year"] == year]
    if grp_year.empty:
        return []
    result = []
    for date_val, grp in grp_year.groupby("_date", sort=True):
        bar_indices = grp.index.tolist()
        if not bar_indices:
            continue
        j_min, j_max = min(bar_indices), max(bar_indices)
        if j_min <= 1:
            start_equity = initial
        else:
            start_equity = float(equity.iloc[j_min - 2])
        end_equity = float(equity.iloc[j_max - 1])
        ret_pct = (end_equity / start_equity - 1) * 100 if start_equity > 0 else 0.0
        trades_in_period = [
            t for t in trades
            if 0 <= t.bar_idx < len(df)
            and df.iloc[t.bar_idx]["_year"] == year
            and df.iloc[t.bar_idx]["_date"] == date_val
        ]
        result.append({
            "date": date_val,
            "start_equity": start_equity,
            "end_equity": end_equity,
            "return_pct": ret_pct,
            "n_trades": len(trades_in_period),
        })
    return result


def print_backtest_summary(
    trades: List[BacktestTrade],
    equity: pd.Series,
    final_capital: float,
    initial: float,
    info: Optional[dict] = None,
    df: Optional[pd.DataFrame] = None,
):
    """백테스트 결과 요약 출력. df가 주어지면 연도별 성과를 함께 출력."""
    opens = sum(1 for t in trades if t.action in ("OPEN_1", "OPEN_2"))
    open_adds = sum(1 for t in trades if t.action == "OPEN_ADD")
    stops = sum(1 for t in trades if t.action == "STOP")
    liquidations = sum(1 for t in trades if t.action == "LIQUIDATION")
    trail_stops = sum(1 for t in trades if t.action == "TRAILING_STOP")
    trend_exits = sum(1 for t in trades if t.action == "TREND_BREAK_EXIT")
    eod_exits = sum(1 for t in trades if t.action == "EOD_EXIT")
    eoy_locks = sum(1 for t in trades if t.action == "EOY_PROFIT_LOCK")
    giveback_exits = sum(1 for t in trades if t.action == "GIVEBACK_EXIT")
    reverse_engulf = sum(1 for t in trades if t.action == "REVERSE_ENGULF_EXIT")
    tp_4h_wick = sum(1 for t in trades if t.action == "TP_4H_WICK_EXIT")
    final_settlements = sum(1 for t in trades if t.action == "FINAL_SETTLEMENT")
    tps = sum(1 for t in trades if t.action == "TP_FIRST")
    print("=== 백테스트 요약 ===")
    print(f"초기 자본: {initial:,.2f} USDT  →  최종 자산: {final_capital:,.2f} USDT  (수익률 {(final_capital/initial - 1)*100:.2f}%)")
    print(f"거래: 진입 {opens}회, 1차 익절 {tps}회, 손절 {stops}회, 연말확정 {eoy_locks}회, 종료정산 {final_settlements}회  (총 {len(trades)}건)")
    close_events = stops + liquidations + trail_stops + trend_exits + eod_exits + eoy_locks + giveback_exits + reverse_engulf + tp_4h_wick + final_settlements
    if close_events + tps > 0:
        win_ratio = tps / (close_events + tps) * 100
        print(f"익절 비율: {win_ratio:.1f}%")

    # 롱/숏별 청산 통계: 승률, 누적 손익(USDT), 초기자본 대비 수익률
    close_actions = (
        "TP_FIRST", "STOP", "TRAILING_STOP", "TREND_BREAK_EXIT", "EOD_EXIT", "EOY_PROFIT_LOCK",
        "GIVEBACK_EXIT", "REVERSE_ENGULF_EXIT", "TP_4H_WICK_EXIT", "LIQUIDATION", "FINAL_SETTLEMENT",
    )
    by_side = {}
    for t in trades:
        if t.action in close_actions and getattr(t, "pnl", None) is not None:
            side = t.side
            if side not in by_side:
                by_side[side] = {"n": 0, "wins": 0, "pnl_sum": 0.0}
            by_side[side]["n"] += 1
            if t.pnl > 0:
                by_side[side]["wins"] += 1
            by_side[side]["pnl_sum"] += t.pnl
    print("\n=== 롱/숏별 청산 성과 ===")
    print(f"{'구분':<6} {'청산':>6} {'승':>4} {'승률':>6} {'누적손익(USDT)':>14} {'초기대비':>10}")
    print("-" * 52)
    for side in ("LONG", "SHORT"):
        d = by_side.get(side, {"n": 0, "wins": 0, "pnl_sum": 0.0})
        n = d["n"]
        wins = d["wins"]
        wr = (wins / n * 100) if n else 0
        pnl_sum = d["pnl_sum"]
        ret_pct = (pnl_sum / initial * 100) if initial and initial > 0 else 0
        print(f"{side:<6} {n:>6} {wins:>4} {wr:>5.1f}% {pnl_sum:>+13,.2f} {ret_pct:>+9.2f}%")
    if by_side.get("SHORT", {}).get("n", 0) == 0:
        print("  (롱 전용 전략)")

    if not equity.empty:
        risk = _risk_metrics(equity, periods_per_year=8760.0)
        print(f"최대/최소 자산: {float(equity.max()):,.2f} / {float(equity.min()):,.2f} USDT  |  최대낙폭 {risk['max_drawdown_pct']:.2f}%  |  샤프 {risk['sharpe_annual']:.2f}  칼마 {risk['calmar_annual']:.2f}")
    if info and info.get("first_ts") and info.get("last_ts"):
        from datetime import datetime
        start_d = datetime.utcfromtimestamp(info["first_ts"] / 1000).strftime("%Y-%m-%d")
        end_d = datetime.utcfromtimestamp(info["last_ts"] / 1000).strftime("%Y-%m-%d")
        print(f"구간: {start_d} ~ {end_d} ({info.get('n_bars', 0):,}봉)")

    # 연도별 성과
    if df is not None and not equity.empty:
        yearly = _yearly_performance(df, equity, trades, initial)
        if yearly:
            print("\n=== 연도별 성과 ===")
            print(f"{'연도':<6} {'연초자산':>12} {'연말자산':>12} {'연수익률':>10} {'거래수':>8}")
            print("-" * 52)
            for row in yearly:
                print(
                    f"{row['year']:<6} "
                    f"{row['start_equity']:>11,.2f} "
                    f"{row['end_equity']:>11,.2f} "
                    f"{row['return_pct']:>9.2f}% "
                    f"{row['n_trades']:>8}"
                )

        # 수익률이 안 좋은 연도(22, 25, 26년)만 월별·일별 상세 출력
        monthly = _monthly_performance(df, equity, trades, initial)
        monthly_detail = [r for r in monthly if r["year"] in YEARS_WITH_DETAILED_OUTPUT]
        if monthly_detail:
            print("\n=== 연도별 월별 자산·수익률 (수익률 저조 연도: 2022, 2025, 2026년) ===")
            from itertools import groupby
            for year, rows in groupby(monthly_detail, key=lambda r: r["year"]):
                rows = list(rows)
                print(f"\n  --- {year}년 ---")
                print(f"  {'월':<4} {'월초자산':>12} {'월말자산':>12} {'월수익률':>10} {'거래수':>6}")
                print("  " + "-" * 48)
                for r in rows:
                    print(
                        f"  {r['month']:>2}월 "
                        f"{r['start_equity']:>11,.2f} "
                        f"{r['end_equity']:>11,.2f} "
                        f"{r['return_pct']:>9.2f}% "
                        f"{r['n_trades']:>6}"
                    )
