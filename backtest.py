# -*- coding: utf-8 -*-
"""
백테스트 엔진.
- 1봉씩 순차 처리하여 미래 참조(look-ahead) 방지.
- 수수료·슬리피지 적용.
"""
from dataclasses import dataclass, field
from typing import List, Optional
import pandas as pd
import numpy as np

import config
from candles import ensure_ohlcv, add_engulfing_flags
from strategy import run_signal_on_bar, StrategyState


def _add_4h_trend_to_1h(df: pd.DataFrame) -> pd.DataFrame:
    """
    1h 봉 데이터에 '마지막 마감된 4h 봉' 종가·EMA50 컬럼 추가.
    추세 필터: 4h 종가 > 4h EMA50 → 롱만, 4h 종가 < 4h EMA50 → 숏만.
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
    resampled["close_time_4h"] = (resampled.index + pd.Timedelta(hours=config.TREND_TIMEFRAME_HOURS)).astype("datetime64[ns]")
    merge_right = resampled[["close_time_4h", "close", "ema50", "atr_4h"]].rename(
        columns={"close": "trend_4h_close", "ema50": "trend_4h_ema50"}
    )
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
    df = df.drop(columns=["_dt"], errors="ignore")
    return df


def _add_daily_trend_to_1h(df: pd.DataFrame) -> pd.DataFrame:
    """
    1h 봉에 '마지막 마감된 일봉' 종가·EMA20·EMA50·하락장저항 플래그 추가.
    일봉 추세 필터: 일봉 종가 > 일봉 EMA → 롱만, 일봉 종가 < 일봉 EMA → 숏만.
    하락장 최적화: 일봉상 EMA 20/50에 윗꼬리가 반복 저항받으면 bear_market_resistance=True → 숏만 진입.
    """
    if df is None or len(df) < 2:
        return df
    use = getattr(config, "DAILY_TREND_FILTER", False)
    bear_enabled = getattr(config, "BEAR_MARKET_RESISTANCE_ENABLED", False)
    bear_regime_enabled = getattr(config, "BEAR_REGIME_DEATH_CROSS_ENABLED", False)
    strict_filter = getattr(config, "BEAR_MARKET_STRICT_LONG_FILTER", False)
    wick_short_enabled = getattr(config, "DAILY_WICK_BEAR_SHORT_ENABLED", False)
    if not use and not bear_enabled and not bear_regime_enabled and not strict_filter and not wick_short_enabled:
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
        df["trend_daily_high"] = np.nan
        df["daily_wick_bear_short_signal"] = False
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
    # 일봉 윗꼬리 저항(전일) + 당일 음봉·EMA20 아래 → 다음날 첫 1h봉에 숏 시그널
    if wick_short_enabled:
        prev_body_high = resampled[["open", "close"]].shift(1).max(axis=1)
        prev_upper_wick = resampled["high"].shift(1) > prev_body_high
        prev_below_ema = resampled["close"].shift(1) < resampled["ema"].shift(1)
        curr_bearish = resampled["close"] < resampled["open"]
        curr_below_ema = resampled["close"] < resampled["ema"]
        resampled["daily_wick_bear_short_signal"] = prev_upper_wick & prev_below_ema & curr_bearish & curr_below_ema
    resampled["_merge_key"] = (resampled.index + pd.Timedelta(days=1)).astype("datetime64[ns]")
    merge_rename = {"close": "trend_daily_close", "ema": "trend_daily_ema", "ema50": "trend_daily_ema50", "high": "trend_daily_high"}
    need_ema50 = bear_regime_enabled or bear_enabled or strict_filter
    merge_cols = ["_merge_key", "close", "ema"] + (["high"] if wick_short_enabled else []) + (["ema50"] if need_ema50 else []) + (["bear_regime"] if (bear_regime_enabled or bear_enabled) else []) + (["bear_market_resistance"] if bear_enabled else []) + (["bear_market_strict"] if strict_filter else []) + (["daily_wick_bear_short_signal"] if wick_short_enabled else [])
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
        df["bear_regime"] = df["open_time"].map(by_ot["bear_regime"]).fillna(False)
    else:
        df["bear_regime"] = False
    if bear_enabled:
        df["bear_market_resistance"] = df["open_time"].map(by_ot["bear_market_resistance"]).fillna(False)
    else:
        df["bear_market_resistance"] = False
    if strict_filter:
        df["bear_market_strict"] = df["open_time"].map(by_ot["bear_market_strict"]).fillna(False)
    else:
        df["bear_market_strict"] = False
    if wick_short_enabled:
        df["trend_daily_high"] = df["open_time"].map(by_ot["trend_daily_high"])
        df["daily_wick_bear_short_signal"] = df["open_time"].map(by_ot["daily_wick_bear_short_signal"]).fillna(False)
    else:
        df["trend_daily_high"] = np.nan
        df["daily_wick_bear_short_signal"] = False
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
    return df.reset_index(drop=True)


def run_backtest(
    df: pd.DataFrame,
    initial_capital: float = 10000.0,
    leverage: int = None,
    fee_rate: float = None,
) -> tuple:
    """
    봉 단위 순차 백테스트. 각 봉에서 그 봉의 OHLC만 사용 (미래 데이터 미사용).
    반환: (거래 목록, 일별/봉별 손익 시리즈, 최종 자산, info, 사용한 DataFrame)
    """
    if df is None or len(df) < 3:
        return [], pd.Series(dtype=float), initial_capital, {}, None

    leverage = leverage or config.LEVERAGE
    fee_rate = fee_rate or config.FEE_EFFECTIVE
    df = prepare_1h_df_for_signal(df)

    capital = initial_capital
    state = StrategyState()
    trades: List[BacktestTrade] = []
    # 포지션: (진입가, 비중(자금대비), 손절가, 1차TP가, 1차TP청산여부)
    positions: List[dict] = []
    equity_curve = []

    for i in range(1, len(df)):
        row = df.iloc[i]
        prev_row = df.iloc[i - 1]
        # 현재 시점에서는 i번째 봉까지만 존재 (i+1 이후 미사용)
        state, action, detail = run_signal_on_bar(
            i, row, prev_row, df.iloc[: i + 1], state, fee_rate=fee_rate
        )

        close_price = float(row["close"])

        if action == "OPEN_1" or action == "OPEN_2":
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
            notional = capital * size_pct * leverage
            fee = notional * fee_rate
            capital -= fee
            trades.append(
                BacktestTrade(
                    bar_idx=i,
                    action=action,
                    side=state.positions[-1].side if state.positions else "LONG",
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
                "side": state.positions[-1].side,
                "tp1_done": False,
            })

        elif action == "TP_FIRST":
            tp_fee = 0.0
            if detail and positions:
                tp_price = detail.get("price", close_price)
                for pos in positions:
                    if not pos.get("tp1_done"):
                        pos["tp1_done"] = True
                        close_pct = config.TP_FIRST_HALF
                        notional = pos["entry_capital"] * pos["size_pct"] * leverage * close_pct
                        fee = notional * fee_rate
                        tp_fee = fee
                        capital -= fee
                        if pos["side"] == "LONG":
                            capital += notional * (tp_price - pos["entry"]) / pos["entry"]
                        else:
                            capital += notional * (pos["entry"] - tp_price) / pos["entry"]
                        pos["size_pct"] *= (1 - close_pct)
                        break
            trades.append(
                BacktestTrade(
                    bar_idx=i,
                    action=action,
                    side=state.positions[0].side if state.positions else "LONG",
                    price=detail.get("price", close_price) if detail else close_price,
                    size_pct=detail.get("size_pct", 0) if detail else 0,
                    fee=tp_fee,
                    detail=detail,
                )
            )

        elif action == "STOP":
            stop_price = detail.get("price", close_price) if detail else close_price
            exit_side = positions[0]["side"] if positions else "LONG"
            for pos in positions:
                notional = pos["entry_capital"] * pos["size_pct"] * leverage
                fee = notional * fee_rate
                capital -= fee
                if pos["side"] == "LONG":
                    capital += notional * (stop_price - pos["entry"]) / pos["entry"]
                else:
                    capital += notional * (pos["entry"] - stop_price) / pos["entry"]
            positions = []
            trades.append(
                BacktestTrade(
                    bar_idx=i,
                    action=action,
                    side=exit_side,
                    price=stop_price,
                    size_pct=0,
                    fee=0,
                    detail=detail,
                )
            )

        elif action == "TRAILING_STOP":
            trail_price = detail.get("price", close_price) if detail else close_price
            exit_side = positions[0]["side"] if positions else "LONG"
            for pos in positions:
                notional = pos["entry_capital"] * pos["size_pct"] * leverage
                fee = notional * fee_rate
                capital -= fee
                if pos["side"] == "LONG":
                    capital += notional * (trail_price - pos["entry"]) / pos["entry"]
                else:
                    capital += notional * (pos["entry"] - trail_price) / pos["entry"]
            positions = []
            trades.append(
                BacktestTrade(
                    bar_idx=i,
                    action=action,
                    side=exit_side,
                    price=trail_price,
                    size_pct=0,
                    fee=0,
                    detail=detail,
                )
            )

        elif action == "TREND_BREAK_EXIT":
            exit_price = detail.get("price", close_price) if detail else close_price
            exit_side = positions[0]["side"] if positions else "LONG"
            for pos in positions:
                notional = pos["entry_capital"] * pos["size_pct"] * leverage
                fee = notional * fee_rate
                capital -= fee
                if pos["side"] == "LONG":
                    capital += notional * (exit_price - pos["entry"]) / pos["entry"]
                else:
                    capital += notional * (pos["entry"] - exit_price) / pos["entry"]
            positions = []
            trades.append(
                BacktestTrade(
                    bar_idx=i,
                    action=action,
                    side=exit_side,
                    price=exit_price,
                    size_pct=0,
                    fee=0,
                    detail=detail,
                )
            )

        elif action == "EOD_EXIT":
            eod_price = detail.get("price", close_price) if detail else close_price
            exit_side = positions[0]["side"] if positions else "LONG"
            for pos in positions:
                notional = pos["entry_capital"] * pos["size_pct"] * leverage
                fee = notional * fee_rate
                capital -= fee
                if pos["side"] == "LONG":
                    capital += notional * (eod_price - pos["entry"]) / pos["entry"]
                else:
                    capital += notional * (pos["entry"] - eod_price) / pos["entry"]
            positions = []
            trades.append(
                BacktestTrade(
                    bar_idx=i,
                    action=action,
                    side=exit_side,
                    price=eod_price,
                    size_pct=0,
                    fee=0,
                    detail=detail,
                )
            )

        elif action == "EOY_PROFIT_LOCK":
            eoy_price = detail.get("price", close_price) if detail else close_price
            exit_side = positions[0]["side"] if positions else "LONG"
            for pos in positions:
                notional = pos["entry_capital"] * pos["size_pct"] * leverage
                fee = notional * fee_rate
                capital -= fee
                if pos["side"] == "LONG":
                    capital += notional * (eoy_price - pos["entry"]) / pos["entry"]
                else:
                    capital += notional * (pos["entry"] - eoy_price) / pos["entry"]
            positions = []
            trades.append(
                BacktestTrade(
                    bar_idx=i,
                    action=action,
                    side=exit_side,
                    price=eoy_price,
                    size_pct=0,
                    fee=0,
                    detail=detail,
                )
            )

        elif action == "GIVEBACK_EXIT":
            gb_price = detail.get("price", close_price) if detail else close_price
            exit_side = positions[0]["side"] if positions else "LONG"
            for pos in positions:
                notional = pos["entry_capital"] * pos["size_pct"] * leverage
                fee = notional * fee_rate
                capital -= fee
                if pos["side"] == "LONG":
                    capital += notional * (gb_price - pos["entry"]) / pos["entry"]
                else:
                    capital += notional * (pos["entry"] - gb_price) / pos["entry"]
            positions = []
            trades.append(
                BacktestTrade(
                    bar_idx=i,
                    action=action,
                    side=exit_side,
                    price=gb_price,
                    size_pct=0,
                    fee=0,
                    detail=detail,
                )
            )

        elif action == "REVERSE_ENGULF_EXIT":
            exit_price = detail.get("price", close_price) if detail else close_price
            exit_side = positions[0]["side"] if positions else "LONG"
            for pos in positions:
                notional = pos["entry_capital"] * pos["size_pct"] * leverage
                fee = notional * fee_rate
                capital -= fee
                if pos["side"] == "LONG":
                    capital += notional * (exit_price - pos["entry"]) / pos["entry"]
                else:
                    capital += notional * (pos["entry"] - exit_price) / pos["entry"]
            positions = []
            trades.append(
                BacktestTrade(
                    bar_idx=i,
                    action=action,
                    side=exit_side,
                    price=exit_price,
                    size_pct=0,
                    fee=0,
                    detail=detail,
                )
            )

        # 미실현 손익 (포지션 유지 중)
        total_unrealized = 0.0
        for pos in positions:
            notional = pos["entry_capital"] * pos["size_pct"] * leverage
            if pos["side"] == "LONG":
                total_unrealized += notional * (close_price - pos["entry"]) / pos["entry"]
            else:
                total_unrealized += notional * (pos["entry"] - close_price) / pos["entry"]
        equity_curve.append(capital + total_unrealized)

    # 미청산 포지션을 마지막 봉 종가로 정산 (연도별 거래수에 반영되도록 trades에 기록)
    last_bar_idx = len(df) - 1
    close_price = float(df.iloc[-1]["close"])
    if positions:
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
            )
        )

    equity_series = pd.Series(equity_curve) if equity_curve else pd.Series(dtype=float)
    info = {
        "n_bars": len(df),
        "first_ts": int(df.iloc[0]["open_time"]) if "open_time" in df.columns else None,
        "last_ts": int(df.iloc[-1]["open_time"]) if "open_time" in df.columns else None,
    }
    return trades, equity_series, capital, info, df


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
    stops = sum(1 for t in trades if t.action == "STOP")
    trail_stops = sum(1 for t in trades if t.action == "TRAILING_STOP")
    trend_exits = sum(1 for t in trades if t.action == "TREND_BREAK_EXIT")
    eod_exits = sum(1 for t in trades if t.action == "EOD_EXIT")
    eoy_locks = sum(1 for t in trades if t.action == "EOY_PROFIT_LOCK")
    giveback_exits = sum(1 for t in trades if t.action == "GIVEBACK_EXIT")
    reverse_engulf = sum(1 for t in trades if t.action == "REVERSE_ENGULF_EXIT")
    final_settlements = sum(1 for t in trades if t.action == "FINAL_SETTLEMENT")
    tps = sum(1 for t in trades if t.action == "TP_FIRST")
    print("=== 백테스트 요약 ===")
    print(f"초기 자본: {initial:,.2f} USDT")
    print(f"최종 자산: {final_capital:,.2f} USDT")
    print(f"수익률: {(final_capital/initial - 1)*100:.2f}%")
    print(f"거래 이벤트(진입+익절+손절) 합계: {len(trades)}")
    print(f"  - 진입: {opens}, 1차 익절: {tps}, 손절: {stops}, 트레일: {trail_stops}, 4h이탈: {trend_exits}, 일종료: {eod_exits}, 연말확정: {eoy_locks}, 고점되돌림: {giveback_exits}, 반대장악: {reverse_engulf}, 종료정산: {final_settlements}")
    close_events = stops + trail_stops + trend_exits + eod_exits + eoy_locks + giveback_exits + reverse_engulf + final_settlements
    if close_events > 0:
        win_ratio = tps / (close_events + tps) * 100 if (close_events + tps) > 0 else 0
        print(f"  - 1차 익절 vs 청산: 익절 {tps}회 / 손절 {stops}회 / 트레일 {trail_stops}회 / 4h이탈 {trend_exits}회 / 일종료 {eod_exits}회 / 연말확정 {eoy_locks}회 / 고점되돌림 {giveback_exits}회 / 반대장악 {reverse_engulf}회 / 종료정산 {final_settlements}회 -> 익절 비율 약 {win_ratio:.1f}%")
    if not equity.empty:
        print(f"최대 자산: {equity.max():,.2f} USDT")
        print(f"최소 자산: {equity.min():,.2f} USDT")
    if info and info.get("first_ts") and info.get("last_ts"):
        from datetime import datetime
        start_d = datetime.utcfromtimestamp(info["first_ts"] / 1000).strftime("%Y-%m-%d")
        end_d = datetime.utcfromtimestamp(info["last_ts"] / 1000).strftime("%Y-%m-%d")
        print(f"백테스트 구간: {start_d} ~ {end_d} (총 {info.get('n_bars', 0):,}봉)")
    fill_note = "해당 봉 (고+저)/2 체결" if getattr(config, "FILL_USE_MID", False) else "해당 봉 종가 체결"
    print(f"(진입가: CONSERVATIVE_FILL=True → {fill_note} 가정)")

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
            print("  ※ 거래수=해당 연도 발생한 진입·익절·손절·트레일·4h이탈·종료정산 이벤트 수. 0=해당 연도엔 청산/진입 없이 포지션만 유지(자산은 미실현 손익으로 변동).")
