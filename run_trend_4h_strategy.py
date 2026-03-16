# -*- coding: utf-8 -*-
"""
ETH 4시간봉 추세 추종 서브 전략 백테스트.

아이디어 (롱 중심):
- 일봉 추세가 상승 (close_d > EMA20_d, EMA20_d > EMA50_d) 일 때만 동작.
- 4h에서 close_4h > EMA50_4h, EMA50_4h > EMA100_4h 인 구간만 진입 허용.
- 최근 N개의 4h 봉 고점(HH_N)을 상향 돌파할 때 롱 진입.
- 손절: 진입 시점 EMA50_4h 또는 최근 스윙 로우 아래.
- 익절/청산: RR 기반 1차 TP + EMA50_4h 이탈 시 전량 청산.
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

import config
from data_fetcher import load_or_fetch_1h
from backtest import _risk_metrics


@dataclass
class Trend4hTrade:
    bar_idx_entry: int
    bar_idx_exit: int
    side: str
    entry_price: float
    exit_price: float
    size_pct: float
    pnl: float
    reason: str  # "TP" | "STOP" | "EMA_BREAK" | "TIME_EXIT"


def _resample_to_4h(df_1h: pd.DataFrame) -> pd.DataFrame:
    """1시간봉 DataFrame을 4h OHLC로 리샘플."""
    if df_1h is None or len(df_1h) < 4 or "open_time" not in df_1h.columns:
        return pd.DataFrame()
    df = df_1h.copy()
    df["_dt"] = pd.to_datetime(df["open_time"], unit="ms")
    df = df.set_index("_dt")
    ohlc = df[["open", "high", "low", "close"]].resample("4h").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna(how="any")
    ohlc = ohlc.reset_index()
    ohlc["open_time"] = ohlc["_dt"].astype("int64") // 10**6  # ms
    return ohlc[["open_time", "open", "high", "low", "close"]]


def _add_daily_trend(df_1h: pd.DataFrame, df_4h: pd.DataFrame) -> pd.DataFrame:
    """
    1h 데이터에서 일봉 리샘플 후 EMA20/EMA50 계산, 그걸 4h에 매핑해서
    daily_close, daily_ema20, daily_ema50를 붙인다.
    """
    if df_1h is None or len(df_1h) < 24 or "open_time" not in df_1h.columns:
        return df_4h
    d1 = df_1h.copy()
    d1["_dt"] = pd.to_datetime(d1["open_time"], unit="ms")
    daily = d1.set_index("_dt").resample("1D").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna(how="all")
    if daily.empty:
        return df_4h
    ema20 = daily["close"].ewm(span=20, adjust=False).mean()
    ema50 = daily["close"].ewm(span=50, adjust=False).mean()
    daily = daily.assign(ema20=ema20, ema50=ema50)
    daily["_merge_key"] = (daily.index + pd.Timedelta(days=1)).astype("datetime64[ns]")

    df4 = df_4h.copy()
    df4["_dt"] = pd.to_datetime(df4["open_time"], unit="ms")
    merged = pd.merge_asof(
        df4.sort_values("_dt").assign(_dt_ns=df4["_dt"].astype("datetime64[ns]")),
        daily[["_merge_key", "close", "ema20", "ema50"]].sort_values("_merge_key"),
        left_on="_dt_ns",
        right_on="_merge_key",
        direction="backward",
    )
    merged = merged.rename(
        columns={"close_y": "daily_close", "ema20": "daily_ema20", "ema50": "daily_ema50"}
    )
    merged = merged.rename(columns={"open_x": "open", "high": "high", "low": "low", "close_x": "close"})
    return merged[["open_time", "open", "high", "low", "close", "daily_close", "daily_ema20", "daily_ema50"]]


def run_trend_4h_backtest(
    df_1h: pd.DataFrame,
    initial_capital: float = 100.0,
    n_breakout: int = 20,
    rr_tp: float = 2.0,
    position_size_pct: float = 0.2,
    max_hold_bars: int = 360,  # 4h 봉 기준, 약 60일
) -> Tuple[List[Trend4hTrade], pd.Series, float, dict]:
    """
    4h 추세 추종 전략 백테스트.
    - df_1h: 1시간봉 원본 데이터 (open_time, open, high, low, close 필요)
    - initial_capital: 시작 자본
    - n_breakout: 최근 N개 4h 봉 고점 돌파 시 진입
    - rr_tp: 손익비 (2.0 = 2R)
    - position_size_pct: 자본 대비 포지션 비율 (0.2 = 20%)
    - max_hold_bars: 최대 보유 4h 봉 수
    """
    if df_1h is None or len(df_1h) < 24:
        return [], pd.Series(dtype=float), initial_capital, {}

    df4 = _resample_to_4h(df_1h)
    if df4 is None or df4.empty:
        return [], pd.Series(dtype=float), initial_capital, {}

    df4 = _add_daily_trend(df_1h, df4)
    if df4 is None or df4.empty:
        return [], pd.Series(dtype=float), initial_capital, {}

    # 4h EMA50/100, ATR 계산
    close4 = df4["close"].astype(float)
    ema50 = close4.ewm(span=50, adjust=False).mean()
    ema100 = close4.ewm(span=100, adjust=False).mean()
    high4 = df4["high"].astype(float)
    low4 = df4["low"].astype(float)
    prev_close4 = close4.shift(1)
    tr = pd.concat(
        [high4 - low4, (high4 - prev_close4).abs(), (low4 - prev_close4).abs()], axis=1
    ).max(axis=1)
    atr = tr.rolling(window=14, min_periods=1).mean()

    df4 = df4.copy()
    df4["ema50_4h"] = ema50
    df4["ema100_4h"] = ema100
    df4["atr_4h"] = atr

    capital = initial_capital
    equity_curve: List[float] = []
    trades: List[Trend4hTrade] = []

    open_side: Optional[str] = None
    entry_price: float = 0.0
    stop_price: float = 0.0
    tp_price: float = 0.0
    entry_idx: int = -1

    for i in range(len(df4)):
        row = df4.iloc[i]
        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        ema50_i = float(row["ema50_4h"])
        ema100_i = float(row["ema100_4h"])
        atr_i = float(row["atr_4h"])

        # 일봉 추세 필터
        dc = row.get("daily_close")
        de20 = row.get("daily_ema20")
        de50 = row.get("daily_ema50")
        allow_long = True
        if dc is None or de20 is None or de50 is None:
            allow_long = False
        else:
            dcf, de20f, de50f = float(dc), float(de20), float(de50)
            if np.isnan(dcf) or np.isnan(de20f) or np.isnan(de50f):
                allow_long = False
            else:
                allow_long = (dcf > de20f) and (de20f > de50f)

        # 기존 포지션 관리 (손절/TP/EMA50 이탈/시간 만료)
        if open_side is not None:
            reason: Optional[str] = None
            exit_price: Optional[float] = None
            # 손절 / TP
            if low <= stop_price:
                exit_price = stop_price
                reason = "STOP"
            elif high >= tp_price:
                exit_price = tp_price
                reason = "TP"
            # 추세 이탈: 종가가 EMA50 아래로 내려오면 청산
            elif close < ema50_i:
                exit_price = close
                reason = "EMA_BREAK"
            # 시간 만료
            elif i - entry_idx >= max_hold_bars:
                exit_price = close
                reason = "TIME_EXIT"

            if reason is not None and exit_price is not None:
                notional = capital * position_size_pct
                pnl = notional * (exit_price - entry_price) / entry_price
                capital += pnl
                trades.append(
                    Trend4hTrade(
                        bar_idx_entry=entry_idx,
                        bar_idx_exit=i,
                        side=open_side,
                        entry_price=entry_price,
                        exit_price=exit_price,
                        size_pct=position_size_pct,
                        pnl=pnl,
                        reason=reason,
                    )
                )
                open_side = None

        # 신규 진입: 롱만, 추세 강할 때 + 최근 N봉 고점 돌파
        if open_side is None and allow_long:
            if i > n_breakout and ema50_i > 0 and ema100_i > 0 and ema50_i > ema100_i and close > ema50_i:
                window = df4.iloc[i - n_breakout : i]
                hh_n = float(window["high"].max())
                if close > hh_n:
                    # 진입
                    entry_price = close
                    # 손절: EMA50_4h 혹은 최근 N 저점 중 더 위쪽, 거기에 약간의 버퍼
                    recent_low = float(window["low"].min())
                    raw_stop = max(ema50_i, recent_low)
                    stop_price = raw_stop * 0.995  # 0.5% 버퍼
                    risk = entry_price - stop_price
                    if risk > 0 and (risk / entry_price) < 0.15:  # 리스크 15% 이내만 허용
                        tp_price = entry_price + rr_tp * risk
                        open_side = "LONG"
                        entry_idx = i

        # 자본 + 미실현 손익으로 equity 업데이트
        unrealized = 0.0
        if open_side is not None:
            notional = capital * position_size_pct
            unrealized = notional * (close - entry_price) / entry_price
        equity_curve.append(capital + unrealized)

    equity_series = pd.Series(equity_curve)
    info = {
        "n_bars_4h": len(df4),
        "first_ts": int(df4.iloc[0]["open_time"]) if "open_time" in df4.columns else None,
        "last_ts": int(df4.iloc[-1]["open_time"]) if "open_time" in df4.columns else None,
        "params": {
            "n_breakout": n_breakout,
            "rr_tp": rr_tp,
            "position_size_pct": position_size_pct,
            "max_hold_bars": max_hold_bars,
        },
    }
    return trades, equity_series, capital, info


def summarize_trend_4h(
    trades: List[Trend4hTrade], equity: pd.Series, initial: float, final: float, info: dict
) -> None:
    n = len(trades)
    n_win = sum(1 for t in trades if t.pnl > 0)
    n_loss = sum(1 for t in trades if t.pnl < 0)
    win_ratio = (n_win / n * 100) if n > 0 else 0.0
    total_pnl = sum(t.pnl for t in trades)
    avg_pnl = (total_pnl / n) if n > 0 else 0.0
    risk = _risk_metrics(equity, periods_per_year=6 * 365.0)  # 대략 4h 봉 연간 개수
    print("=== 4h 추세 추종 서브 전략 요약 ===")
    print(f"초기 자본: {initial:,.2f} → 최종 자본: {final:,.2f}  (수익률 {(final/initial - 1)*100:.2f}%)")
    print(f"거래 수: {n}  | 승: {n_win}  패: {n_loss}  승률: {win_ratio:.1f}%")
    print(f"총 PnL: {total_pnl:,.2f}  | 평균 PnL/트레이드: {avg_pnl:,.2f}")
    if not equity.empty:
        print(
            f"최대/최소 자산: {float(equity.max()):,.2f} / {float(equity.min()):,.2f}  | "
            f"최대낙폭 {risk['max_drawdown_pct']:.2f}%  | 샤프(연) {risk['sharpe_annual']:.2f}  "
            f"칼마(연) {risk['calmar_annual']:.2f}"
        )
    if info.get("first_ts") and info.get("last_ts"):
        from datetime import datetime

        start_d = datetime.utcfromtimestamp(info["first_ts"] / 1000).strftime("%Y-%m-%d")
        end_d = datetime.utcfromtimestamp(info["last_ts"] / 1000).strftime("%Y-%m-%d")
        print(f"구간: {start_d} ~ {end_d}  (4h 봉 수: {info.get('n_bars_4h', 0):,})")


def main():
    symbol = getattr(config, "SYMBOL", "ETHUSDT")
    print(f"심볼: {symbol} | 4시간봉 추세 추종 서브 전략 백테스트\n")
    for years in (5, 10):
        print("\n" + "=" * 80)
        print(f"=== {years}년 4h 추세 전략 백테스트 ===")
        df_1h = load_or_fetch_1h(years=years, symbol=symbol, force_refresh=False)
        if df_1h is None or len(df_1h) < 200:
            print("데이터 부족, 건너뜁니다.")
            continue
        initial = 100.0
        trades, equity, final_cap, info = run_trend_4h_backtest(df_1h, initial_capital=initial)
        summarize_trend_4h(trades, equity, initial, final_cap, info)


if __name__ == "__main__":
    main()

