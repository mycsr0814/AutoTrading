# -*- coding: utf-8 -*-
"""
ETH 15분봉 단타 전략 백테스트 — 시타델급 설계 + 백테스트 오류 방지.

[백테스트 무결성]
- 미래 참조 금지: 지표·신호는 해당 봉 마감 시점만 사용 (merge_asof backward, df.iloc[:i+1]).
- 체결: 진입 신호 발생 봉의 "다음 봉 시가"에 체결 (동일 봉 look-ahead 제거). 슬리피지·수수료 반영.
- 동일 봉 내 순서: 1) 전 봉 신호로 인한 현재 봉 시가 진입 2) 보유 포지션 손절/익절/시간청산 3) 신규 신호 발생 시 다음 봉 체결 예약.
- 손절 우선: 동일 봉에서 손절가·익절가 동시 터치 시 손절로 처리 (보수적).
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

import config
from data_fetcher import load_or_fetch_15m, load_or_fetch_1h
from scalp_strategy import (
    prepare_15m_for_signal,
    signal_at_bar,
    MAX_HOLD_BARS,
)


@dataclass
class Scalp15mTrade:
    bar_idx_entry: int
    bar_idx_exit: int
    side: str
    entry_price: float
    exit_price: float
    size_pct: float
    pnl: float
    fee_entry: float
    fee_exit: float
    reason: str  # "TP" | "STOP" | "TIME_EXIT"


def run_scalp_15m_backtest(
    df_15: pd.DataFrame,
    df_1h: pd.DataFrame,
    initial_capital: float = 100.0,
    leverage: int = 4,
    fee_maker: float = None,
    fee_taker: float = None,
    slippage_bps: float = None,
    max_hold_bars: int = MAX_HOLD_BARS,
) -> Tuple[List[Scalp15mTrade], pd.Series, float, dict]:
    """
    15m 단타 백테스트. 오류 방지:
    - 봉 i에서 신호 발생 → bar i+1 시가에 진입 (진입가 = open_{i+1} * (1 ± slippage)), 수수료 적용.
    - 청산: 시장가 가정 → 청산가 ± 슬리피지, 테이커 수수료.
    - 동일 봉: 먼저 pending 진입 체결 → 손절/익절/시간청산 검사 → 신규 신호 시 pending 설정.
    """
    if df_15 is None or len(df_15) < 80:
        return [], pd.Series(dtype=float), initial_capital, {}

    fee_maker = fee_maker if fee_maker is not None else getattr(config, "FEE_MAKER", 0.0002)
    fee_taker = fee_taker if fee_taker is not None else getattr(config, "FEE_TAKER", 0.0004)
    slippage_bps = slippage_bps if slippage_bps is not None else getattr(config, "SLIPPAGE_BPS", 15)

    df_15 = prepare_15m_for_signal(df_15, df_1h)
    if "ema20" not in df_15.columns or "ema50" not in df_15.columns:
        return [], pd.Series(dtype=float), initial_capital, {}

    capital = initial_capital
    equity_curve: List[float] = []
    trades: List[Scalp15mTrade] = []

    in_position = False
    entry_price = 0.0
    stop_price = 0.0
    tp_price = 0.0
    entry_bar_idx = -1
    entry_fee_paid = 0.0
    size_pct = getattr(config, "SIZE_PCT", 0.08)
    try:
        from scalp_strategy import SIZE_PCT
        size_pct = SIZE_PCT
    except Exception:
        pass

    # 다음 봉 시가 체결용: 신호 발생 봉 인덱스와 신호 내용
    pending_entry: Optional[dict] = None  # {side, stop_price, tp_price, size_pct} 또는 stop_pct, rr_ratio

    for i in range(len(df_15)):
        row = df_15.iloc[i]
        prev_row = df_15.iloc[i - 1] if i >= 1 else None
        open_price = float(row["open"])
        high_bar = float(row["high"])
        low_bar = float(row["low"])
        close_price = float(row["close"])

        # ---------- 1) 전 봉에서 발생한 진입 신호 → 현재 봉 시가에 체결 (다음 봉 체결) ----------
        if pending_entry is not None and not in_position:
            side = pending_entry["side"]
            size_pct = pending_entry["size_pct"]
            slip_mult = 1.0 + (slippage_bps / 10000.0) if side == "LONG" else 1.0 - (slippage_bps / 10000.0)
            entry_fill = open_price * slip_mult
            # 체결가 기준 손절/익절: stop_pct·rr_ratio 있으면 진입가 기준으로 계산
            if pending_entry.get("stop_pct") is not None and pending_entry.get("rr_ratio") is not None:
                sp = float(pending_entry["stop_pct"])
                rr = float(pending_entry["rr_ratio"])
                stop_price = entry_fill * (1.0 - sp)
                tp_price = entry_fill + rr * (entry_fill * sp)
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

        # ---------- 2) 보유 포지션: 손절 / 익절 / 시간 만료 (동일 봉 내 손절 우선) ----------
        if in_position:
            exit_reason: Optional[str] = None
            exit_price_val: Optional[float] = None

            is_long = entry_price > 0 and (tp_price - entry_price) > 0
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
                exit_slip_mult = 1.0 - (slippage_bps / 10000.0) if is_long else 1.0 + (slippage_bps / 10000.0)
                exit_fill = exit_price_val * exit_slip_mult
                if is_long:
                    pnl = notional * (exit_fill - entry_price) / entry_price
                else:
                    pnl = notional * (entry_price - exit_fill) / entry_price
                capital -= fee_exit
                capital += pnl
                trades.append(
                    Scalp15mTrade(
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

        # ---------- 3) 포지션 없을 때만 진입 신호 산출 (해당 봉 마감 데이터만 사용) ----------
        if not in_position and pending_entry is None:
            sig = signal_at_bar(i, row, prev_row, df_15.iloc[: i + 1], in_position)
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

        # 자산 곡선 (미실현 포함)
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
    info = {"n_bars_15m": len(df_15)}
    return trades, equity_series, capital, info


def _risk_metrics(equity: pd.Series, periods_per_year: float = 35040.0) -> dict:
    """15m 봉 기준 연간 35040개."""
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


def summarize_scalp_15m(
    trades: List[Scalp15mTrade], equity: pd.Series, initial: float, final: float, info: dict
) -> None:
    n = len(trades)
    n_win = sum(1 for t in trades if t.pnl > 0)
    n_loss = sum(1 for t in trades if t.pnl <= 0)
    win_ratio = (n_win / n * 100) if n > 0 else 0.0
    total_pnl = sum(t.pnl for t in trades)
    total_fees = sum(t.fee_entry + t.fee_exit for t in trades)
    avg_pnl = (total_pnl / n) if n > 0 else 0.0
    risk = _risk_metrics(equity, periods_per_year=35040.0)
    print("=== ETH 15m 단타 전략 (시타델 스타일) 요약 ===")
    print("※ 백테스트: 다음 봉 시가 체결, 수수료·슬리피지 반영, 미래 참조 없음")
    print(f"초기 자본: {initial:,.2f} → 최종 자산: {final:,.2f}  (수익률 {(final/initial - 1)*100:.2f}%)")
    print(f"거래 수: {n}  | 승: {n_win}  패: {n_loss}  승률: {win_ratio:.1f}%")
    print(f"총 PnL: {total_pnl:,.2f}  | 총 수수료: {total_fees:,.2f}  | 평균 PnL/트레이드: {avg_pnl:,.2f}")
    if not equity.empty:
        print(
            f"최대/최소 자산: {float(equity.max()):,.2f} / {float(equity.min()):,.2f}  | "
            f"최대낙폭 {risk['max_drawdown_pct']:.2f}%  | 샤프(연) {risk['sharpe_annual']:.2f}  "
            f"칼마(연) {risk['calmar_annual']:.2f}"
        )


def main():
    symbol = getattr(config, "SYMBOL", "ETHUSDT")
    print(f"심볼: {symbol} | 15분봉 단타 전략 (시타델급 설계, 백테스트 오류 방지)\n")
    for years in (3, 5):
        print("\n" + "=" * 80)
        print(f"=== {years}년 15m 단타 백테스트 ===")
        df_15 = load_or_fetch_15m(years=years, symbol=symbol, force_refresh=False)
        df_1h = load_or_fetch_1h(years=years, symbol=symbol, force_refresh=False)
        if df_15 is None or len(df_15) < 500 or df_1h is None or len(df_1h) < 100:
            print("데이터 부족, 건너뜁니다.")
            continue
        initial = 100.0
        trades, equity, final_cap, info = run_scalp_15m_backtest(
            df_15, df_1h,
            initial_capital=initial,
            leverage=4,
            fee_maker=config.FEE_MAKER,
            fee_taker=config.FEE_TAKER,
            slippage_bps=config.SLIPPAGE_BPS,
        )
        summarize_scalp_15m(trades, equity, initial, final_cap, info)


if __name__ == "__main__":
    main()
