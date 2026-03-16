# -*- coding: utf-8 -*-
"""
1h 레짐 + 5m 단타 백테스트 (옵션 A).

- 1h 레짐(UP/DOWN)에 따라 5m에서 지지 리버전 롱 / 저항 리버전 숏만 허용.
- 미래 참조 없음, 다음 5m 봉 시가 체결, 수수료·슬리피지 반영.
- 동일 봉: pending 진입 체결 → 손절/익절/시간청산(손절 우선) → 신규 신호 시 다음 봉 체결 예약.
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

import config
from data_fetcher import load_or_fetch_5m, load_or_fetch_1h
from regime_scalp_5m_strategy import (
    prepare_5m_for_signal,
    signal_at_bar,
    MAX_HOLD_BARS,
)


@dataclass
class Scalp5mTrade:
    bar_idx_entry: int
    bar_idx_exit: int
    side: str
    entry_price: float
    exit_price: float
    size_pct: float
    pnl: float
    fee_entry: float
    fee_exit: float
    reason: str


def run_scalp_5m_backtest(
    df_5m: pd.DataFrame,
    df_1h: pd.DataFrame,
    initial_capital: float = 100.0,
    leverage: int = 4,
    fee_maker: float = None,
    fee_taker: float = None,
    slippage_bps: float = None,
    max_hold_bars: int = MAX_HOLD_BARS,
) -> Tuple[List[Scalp5mTrade], pd.Series, float, dict]:
    if df_5m is None or len(df_5m) < 100:
        return [], pd.Series(dtype=float), initial_capital, {}

    fee_maker = fee_maker or getattr(config, "FEE_MAKER", 0.0002)
    fee_taker = fee_taker or getattr(config, "FEE_TAKER", 0.0004)
    slippage_bps = slippage_bps if slippage_bps is not None else getattr(config, "SLIPPAGE_BPS", 15)

    df_5m = prepare_5m_for_signal(df_5m, df_1h)
    if "regime" not in df_5m.columns or "atr" not in df_5m.columns:
        return [], pd.Series(dtype=float), initial_capital, {}

    capital = initial_capital
    equity_curve: List[float] = []
    trades: List[Scalp5mTrade] = []

    in_position = False
    entry_price = 0.0
    stop_price = 0.0
    tp_price = 0.0
    entry_bar_idx = -1
    entry_fee_paid = 0.0
    size_pct = 0.08
    try:
        from regime_scalp_5m_strategy import SIZE_PCT
        size_pct = SIZE_PCT
    except Exception:
        pass

    pending_entry: Optional[dict] = None

    for i in range(len(df_5m)):
        row = df_5m.iloc[i]
        prev_row = df_5m.iloc[i - 1] if i >= 1 else None
        open_price = float(row["open"])
        high_bar = float(row["high"])
        low_bar = float(row["low"])
        close_price = float(row["close"])

        # 1) 전 봉 신호 → 현재 봉 시가 체결
        if pending_entry is not None and not in_position:
            side = pending_entry["side"]
            size_pct = pending_entry["size_pct"]
            slip_mult = 1.0 + (slippage_bps / 10000.0) if side == "LONG" else 1.0 - (slippage_bps / 10000.0)
            entry_fill = open_price * slip_mult
            if pending_entry.get("stop_pct") is not None and pending_entry.get("rr_ratio") is not None:
                sp = float(pending_entry["stop_pct"])
                rr = float(pending_entry["rr_ratio"])
                if side == "LONG":
                    stop_price = entry_fill * (1.0 - sp)
                    tp_price = entry_fill + rr * (entry_fill * sp)
                else:
                    stop_price = entry_fill * (1.0 + sp)
                    tp_price = entry_fill - rr * (entry_fill * sp)
            else:
                stop_price = pending_entry["stop_price"]
                tp_price = pending_entry["tp_price"]
            notional = capital * size_pct * leverage
            fee = notional * fee_maker
            capital -= fee
            entry_price = entry_fill
            entry_bar_idx = i
            entry_fee_paid = fee
            in_position = True
            pending_entry = None

        # 2) 손절 / 익절 / 시간 만료 (손절 우선)
        if in_position:
            is_long = (tp_price - entry_price) > 0
            exit_reason: Optional[str] = None
            exit_price_val: Optional[float] = None

            if is_long:
                stop_hit = low_bar <= stop_price
                tp_hit = high_bar >= tp_price
            else:
                stop_hit = high_bar >= stop_price
                tp_hit = low_bar <= tp_price

            if stop_hit and tp_hit:
                exit_reason = "STOP"
                exit_price_val = stop_price
            elif stop_hit:
                exit_reason = "STOP"
                exit_price_val = stop_price
            elif tp_hit:
                exit_reason = "TP"
                exit_price_val = tp_price
            elif (i - entry_bar_idx) >= max_hold_bars:
                exit_reason = "TIME_EXIT"
                exit_price_val = close_price

            if exit_reason is not None and exit_price_val is not None:
                notional = capital * size_pct * leverage
                fee_exit = notional * fee_taker
                exit_slip = 1.0 - (slippage_bps / 10000.0) if is_long else 1.0 + (slippage_bps / 10000.0)
                exit_fill = exit_price_val * exit_slip
                if is_long:
                    pnl = notional * (exit_fill - entry_price) / entry_price
                else:
                    pnl = notional * (entry_price - exit_fill) / entry_price
                capital -= fee_exit
                capital += pnl
                trades.append(
                    Scalp5mTrade(
                        bar_idx_entry=entry_bar_idx,
                        bar_idx_exit=i,
                        side="LONG" if is_long else "SHORT",
                        entry_price=entry_price,
                        exit_price=exit_fill,
                        size_pct=size_pct,
                        pnl=pnl,
                        fee_entry=entry_fee_paid,
                        fee_exit=fee_exit,
                        reason=exit_reason,
                    )
                )
                entry_fee_paid = 0.0
                in_position = False

        # 3) 신규 신호 (포지션 없을 때만)
        if not in_position and pending_entry is None:
            sig = signal_at_bar(i, row, prev_row, df_5m.iloc[: i + 1], in_position)
            if sig is not None:
                pending_entry = {
                    "side": sig.side,
                    "stop_price": sig.stop_price,
                    "tp_price": sig.tp_price,
                    "size_pct": sig.size_pct,
                }
                if getattr(sig, "stop_pct", None) is not None and getattr(sig, "rr_ratio", None) is not None:
                    pending_entry["stop_pct"] = sig.stop_pct
                    pending_entry["rr_ratio"] = sig.rr_ratio

        unrealized = 0.0
        if in_position:
            notional = capital * size_pct * leverage
            is_long = (tp_price - entry_price) > 0
            if is_long:
                unrealized = notional * (close_price - entry_price) / entry_price
            else:
                unrealized = notional * (entry_price - close_price) / entry_price
        equity_curve.append(capital + unrealized)

    equity_series = pd.Series(equity_curve)
    info = {"n_bars_5m": len(df_5m)}
    return trades, equity_series, capital, info


def _risk_metrics(equity: pd.Series, periods_per_year: float = 105120.0) -> dict:
    """5m 봉 연간 약 105120개."""
    if equity is None or len(equity) < 2:
        return {"max_drawdown_pct": 0.0, "sharpe_annual": 0.0, "sortino_annual": 0.0, "calmar_annual": 0.0}
    equity = equity.astype(float)
    cummax = equity.cummax()
    dd = (equity - cummax) / cummax.replace(0, np.nan)
    max_dd = float(dd.min() * 100) if cummax.max() > 0 else 0.0
    rets = equity.pct_change().dropna()
    if len(rets) < 2:
        return {"max_drawdown_pct": max_dd, "sharpe_annual": 0.0, "sortino_annual": 0.0, "calmar_annual": 0.0}
    mean_ret = float(rets.mean())
    std_ret = float(rets.std())
    sharpe = (mean_ret / std_ret * np.sqrt(periods_per_year)) if std_ret > 0 else 0.0
    downside = rets[rets < 0]
    ds = float(downside.std()) if len(downside) > 1 else 0.0
    sortino = (mean_ret / ds * np.sqrt(periods_per_year)) if ds > 0 else (sharpe if mean_ret > 0 else 0.0)
    total_ret = float(equity.iloc[-1] / equity.iloc[0] - 1.0) if equity.iloc[0] > 0 else 0.0
    calmar = (total_ret * 100 / abs(max_dd)) if max_dd != 0 else (total_ret * 100 if total_ret >= 0 else 0.0)
    return {"max_drawdown_pct": max_dd, "sharpe_annual": sharpe, "sortino_annual": sortino, "calmar_annual": calmar}


def summarize(trades: List[Scalp5mTrade], equity: pd.Series, initial: float, final: float, info: dict) -> None:
    n = len(trades)
    n_win = sum(1 for t in trades if t.pnl > 0)
    win_ratio = (n_win / n * 100) if n > 0 else 0.0
    total_pnl = sum(t.pnl for t in trades)
    total_fees = sum(t.fee_entry + t.fee_exit for t in trades)
    risk = _risk_metrics(equity, periods_per_year=105120.0)
    print("=== 1h 레짐 + 5m 단타 (옵션 A) 요약 ===")
    print("※ 백테스트: 다음 5m 봉 시가 체결, 수수료·슬리피지 반영, 미래 참조 없음")
    print(f"초기: {initial:,.2f} → 최종: {final:,.2f}  (수익률 {(final/initial - 1)*100:.2f}%)")
    print(f"거래: {n}  | 승: {n_win}  승률: {win_ratio:.1f}%  | 총 PnL: {total_pnl:,.2f}  수수료: {total_fees:,.2f}")
    if not equity.empty:
        print(f"최대/최소 자산: {float(equity.max()):,.2f} / {float(equity.min()):,.2f}  | 최대낙폭 {risk['max_drawdown_pct']:.2f}%  | 샤프 {risk['sharpe_annual']:.2f}  칼마 {risk['calmar_annual']:.2f}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="1h 레짐 + 5m 단타 백테스트")
    parser.add_argument("--years", type=int, nargs="+", default=[1, 3], help="백테스트 연도 (기본: 1 3). 5m 데이터 많아 1년만 하면 빠름.")
    args = parser.parse_args()

    symbol = getattr(config, "SYMBOL", "ETHUSDT")
    print(f"심볼: {symbol} | 1h 레짐 + 5m 단타 (옵션 A)\n")
    for years in args.years:
        print("\n" + "=" * 80)
        print(f"=== {years}년 5m 백테스트 ===")
        df_5m = load_or_fetch_5m(years=years, symbol=symbol, force_refresh=False)
        df_1h = load_or_fetch_1h(years=years, symbol=symbol, force_refresh=False)
        if df_5m is None or len(df_5m) < 500 or df_1h is None or len(df_1h) < 100:
            print("데이터 부족, 건너뜁니다.")
            continue
        initial = 100.0
        trades, equity, final_cap, info = run_scalp_5m_backtest(
            df_5m, df_1h,
            initial_capital=initial,
            leverage=4,
            fee_maker=config.FEE_MAKER,
            fee_taker=config.FEE_TAKER,
            slippage_bps=config.SLIPPAGE_BPS,
        )
        summarize(trades, equity, initial, final_cap, info)


if __name__ == "__main__":
    main()
