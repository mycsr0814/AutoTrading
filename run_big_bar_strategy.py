# -*- coding: utf-8 -*-
"""
1시간봉 장대양/음봉 + 되돌림(리버전) 전략 백테스트.

아이디어 (롱 기준):
- 1시간봉에서 시가 대비 -2% 이상 하락한 "장대 음봉" 발생 (몸통 기준).
- 그 장대 음봉의 고가/저가 범위 중 아래쪽 60% 지점까지 되돌림이 나왔을 때
  롱 진입 (단기 반등 리버전).

이 스크립트는 기존 main/strategy 를 건드리지 않고,
별도의 단순 백테스트를 통해 성과(승률, 기대 수익률, MDD 등)를 보는 용도이다.
"""
from dataclasses import dataclass
from typing import Optional, List, Tuple

import numpy as np
import pandas as pd

import config
from data_fetcher import load_or_fetch_1h
from backtest import prepare_1h_df_for_signal, _risk_metrics


@dataclass
class ReversionTrade:
    bar_idx_entry: int
    bar_idx_exit: int
    side: str
    entry_price: float
    exit_price: float
    size_pct: float
    pnl: float
    reason: str  # "TP" | "STOP" | "TIME_EXIT"


def _find_big_bars(df: pd.DataFrame, pct_threshold: float = 0.02, body_ratio_min: float = 0.6) -> pd.Series:
    """
    장대 양/음봉 플래그 반환.
    - pct_threshold: 시가 대비 절대 변동률 (예: 0.02 = 2%)
    - body_ratio_min: 몸통 / (고-저) 최소 비율 (꼬리만 긴 봉 제거)
    """
    o = df["open"].astype(float)
    c = df["close"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    body = (c - o).abs()
    rng = (h - l).replace(0, np.nan)
    body_ratio = (body / rng).fillna(0.0)
    pct_move = (c - o) / o.replace(0, np.nan)
    big_down = (pct_move <= -pct_threshold) & (body_ratio >= body_ratio_min)
    big_up = (pct_move >= pct_threshold) & (body_ratio >= body_ratio_min)
    return pd.Series(big_down.values, index=df.index, name="big_down"), pd.Series(
        big_up.values, index=df.index, name="big_up"
    )


def run_big_bar_reversion_backtest(
    df: pd.DataFrame,
    initial_capital: float = 100.0,
    pct_threshold: float = 0.02,
    body_ratio_min: float = 0.6,
    pullback_level_pct: float = 0.60,
    stop_buffer_pct: float = 0.003,
    rr_tp: float = 2.0,
    position_risk_pct: float = 0.1,
    max_hold_bars: int = 24,
) -> Tuple[List[ReversionTrade], pd.Series, float, dict]:
    """
    장대 양/음봉 + 되돌림 리버전 전략 단순 백테스트.

    - initial_capital: 시작 자본 (USDT)
    - pct_threshold: 장대봉 기준 (시가 대비 2% 이상 등)
    - body_ratio_min: 몸통/전체 범위 최소 비율
    - pullback_level_pct: 장대봉 범위 중 되돌림 비율 (0.60 = 60%)
    - stop_buffer_pct: 저가/고가 대비 손절 여유 (0.003 = 0.3%)
    - rr_tp: 손절 폭의 몇 배에서 TP (2.0 = 2R)
    - position_risk_pct: 자본 대비 포지션 크기 (고정 비율)
    - max_hold_bars: 최대 보유 시간 (1h 봉 단위)
    """
    if df is None or len(df) < 3:
        return [], pd.Series(dtype=float), initial_capital, {}

    # 기존 전략과 동일한 지표(4h/일봉 추세, ATR 등) 사용
    df = prepare_1h_df_for_signal(df)
    big_down, big_up = _find_big_bars(df, pct_threshold=pct_threshold, body_ratio_min=body_ratio_min)

    capital = initial_capital
    equity_curve: List[float] = []
    trades: List[ReversionTrade] = []

    # 현재 보유 포지션 (단일 포지션 가정)
    open_side: Optional[str] = None  # "LONG" or "SHORT"
    entry_price: float = 0.0
    stop_price: float = 0.0
    tp_price: float = 0.0
    entry_bar_idx: int = -1
    position_size_pct: float = 0.0

    # 최근 장대봉 정보 (롱/숏 각각 별도로)
    last_big_down_idx: Optional[int] = None
    last_big_down_high: float = 0.0
    last_big_down_low: float = 0.0
    last_big_up_idx: Optional[int] = None
    last_big_up_high: float = 0.0
    last_big_up_low: float = 0.0

    for i in range(len(df)):
        row = df.iloc[i]
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])

        # 추세 필터: 기존 4h/일봉 추세와 동일하게 롱/숏 허용 여부 판단
        trend_close = row.get("trend_4h_close")
        trend_ema = row.get("trend_4h_ema50")
        allow_long = True
        allow_short = True
        if trend_close is not None and trend_ema is not None:
            tc, te = float(trend_close), float(trend_ema)
            if not (np.isnan(tc) or np.isnan(te)) and te > 0:
                min_pct_above = getattr(config, "TREND_4H_MIN_PCT_ABOVE_EMA", 0)
                allow_long = (tc >= te * (1 + min_pct_above)) if min_pct_above else (tc > te)
                if getattr(config, "BEAR_MARKET_STRICT_LONG_FILTER", False) and row.get("bear_market_strict"):
                    strict_pct = getattr(config, "BEAR_MARKET_STRICT_LONG_PCT", 0.012)
                    allow_long = tc >= te * (1 + strict_pct)
                if min_pct_above:
                    allow_short = tc <= te * (1 - min_pct_above)
                else:
                    allow_short = tc < te
        if getattr(config, "DAILY_TREND_FILTER", False):
            dc, de = row.get("trend_daily_close"), row.get("trend_daily_ema")
            if dc is not None and de is not None:
                dcf, def_ = float(dc), float(de)
                if not (np.isnan(dcf) or np.isnan(def_)):
                    allow_long = allow_long and (dcf > def_)
                    allow_short = allow_short and (dcf < def_)

        # 1) 기존 포지션 청산 로직 (손절/TP/시간 만료)
        if open_side is not None:
            reason: Optional[str] = None
            exit_price: Optional[float] = None
            # 손절/익절 가격 우선
            if open_side == "LONG":
                if low <= stop_price:
                    exit_price = stop_price
                    reason = "STOP"
                elif high >= tp_price:
                    exit_price = tp_price
                    reason = "TP"
            else:
                if high >= stop_price:
                    exit_price = stop_price
                    reason = "STOP"
                elif low <= tp_price:
                    exit_price = tp_price
                    reason = "TP"
            # 시간 만료
            if reason is None and i - entry_bar_idx >= max_hold_bars:
                exit_price = close
                reason = "TIME_EXIT"

            if reason is not None and exit_price is not None:
                # 단순 비례 PnL (레버리지 1, 수수료 무시 또는 config를 써도 됨)
                notional = capital * position_size_pct
                if open_side == "LONG":
                    pnl = notional * (exit_price - entry_price) / entry_price
                else:
                    pnl = notional * (entry_price - exit_price) / entry_price
                capital += pnl
                trades.append(
                    ReversionTrade(
                        bar_idx_entry=entry_bar_idx,
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

        # 2) 장대봉 탐지 (이 봉이 장대 양/음봉인지 기록)
        if bool(big_down.iloc[i]):
            last_big_down_idx = i
            last_big_down_high = high
            last_big_down_low = low
        if bool(big_up.iloc[i]):
            last_big_up_idx = i
            last_big_up_high = high
            last_big_up_low = low

        # 3) 신규 진입 (기존 포지션 없을 때만)
        if open_side is None:
            # 롱: 장대 음봉 후 60% 되돌림
            if last_big_down_idx is not None and i > last_big_down_idx and allow_long:
                H = last_big_down_high
                L = last_big_down_low
                if H > L:
                    level_long = H - pullback_level_pct * (H - L)
                    if low <= level_long <= high:
                        # 진입
                        entry_price = level_long
                        stop_price = L * (1.0 - stop_buffer_pct)
                        risk = entry_price - stop_price
                        if risk > 0:
                            tp_price = entry_price + rr_tp * risk
                            open_side = "LONG"
                            entry_bar_idx = i
                            position_size_pct = position_risk_pct

            # 숏: 장대 양봉 후 60% 되돌림 (대칭 구조)
            if open_side is None and last_big_up_idx is not None and i > last_big_up_idx and allow_short:
                H = last_big_up_high
                L = last_big_up_low
                if H > L:
                    level_short = L + pullback_level_pct * (H - L)
                    if low <= level_short <= high:
                        entry_price = level_short
                        stop_price = H * (1.0 + stop_buffer_pct)
                        risk = stop_price - entry_price
                        if risk > 0:
                            tp_price = entry_price - rr_tp * risk
                            open_side = "SHORT"
                            entry_bar_idx = i
                            position_size_pct = position_risk_pct

        # 4) 현재 자본 + 미실현 손익으로 equity 업데이트
        unrealized = 0.0
        if open_side is not None:
            notional = capital * position_size_pct
            if open_side == "LONG":
                unrealized = notional * (close - entry_price) / entry_price
            else:
                unrealized = notional * (entry_price - close) / entry_price
        equity_curve.append(capital + unrealized)

    equity_series = pd.Series(equity_curve)
    info = {
        "n_bars": len(df),
        "first_ts": int(df.iloc[0]["open_time"]) if "open_time" in df.columns else None,
        "last_ts": int(df.iloc[-1]["open_time"]) if "open_time" in df.columns else None,
        "params": {
            "pct_threshold": pct_threshold,
            "body_ratio_min": body_ratio_min,
            "pullback_level_pct": pullback_level_pct,
            "stop_buffer_pct": stop_buffer_pct,
            "rr_tp": rr_tp,
            "position_risk_pct": position_risk_pct,
            "max_hold_bars": max_hold_bars,
        },
    }
    return trades, equity_series, capital, info


def summarize_trades(trades: List[ReversionTrade], equity: pd.Series, initial: float, final: float, info: dict) -> None:
    """간단한 요약 출력."""
    n = len(trades)
    n_win = sum(1 for t in trades if t.pnl > 0)
    n_loss = sum(1 for t in trades if t.pnl < 0)
    win_ratio = (n_win / n * 100) if n > 0 else 0.0
    total_pnl = sum(t.pnl for t in trades)
    avg_pnl = (total_pnl / n) if n > 0 else 0.0
    risk = _risk_metrics(equity, periods_per_year=8760.0)

    print("=== 장대양/음봉 리버전 전략 요약 ===")
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
        print(f"구간: {start_d} ~ {end_d}  (봉 수: {info.get('n_bars', 0):,})")


def _grid_search(df: pd.DataFrame, years: int) -> None:
    """여러 파라미터 조합 그리드 탐색 후 표 형태로 요약."""
    initial = 100.0
    pct_threshold_list = [0.02, 0.03, 0.04]  # 2%, 3%, 4%
    pullback_list = [0.5, 0.6, 0.7]  # 50%, 60%, 70%
    rr_list = [1.5, 2.0, 2.5]
    hold_list = [12, 24, 48]  # 12h, 24h, 48h

    rows = []
    for pct_th in pct_threshold_list:
        for pl in pullback_list:
            for rr in rr_list:
                for hold in hold_list:
                    trades, equity, final_cap, info = run_big_bar_reversion_backtest(
                        df,
                        initial_capital=initial,
                        pct_threshold=pct_th,
                        body_ratio_min=0.6,
                        pullback_level_pct=pl,
                        stop_buffer_pct=0.003,
                        rr_tp=rr,
                        position_risk_pct=0.1,
                        max_hold_bars=hold,
                    )
                    n_trades = len(trades)
                    total_pnl = sum(t.pnl for t in trades)
                    ret_pct = (final_cap / initial - 1) * 100 if initial > 0 else 0.0
                    risk = _risk_metrics(equity, periods_per_year=8760.0)
                    rows.append(
                        {
                            "pct_th": pct_th,
                            "pullback": pl,
                            "rr": rr,
                            "hold": hold,
                            "final": final_cap,
                            "ret_pct": ret_pct,
                            "max_dd": risk["max_drawdown_pct"],
                            "sharpe": risk["sharpe_annual"],
                            "calmar": risk["calmar_annual"],
                            "n_trades": n_trades,
                        }
                    )

    df_res = pd.DataFrame(rows)
    print("\n" + "=" * 80)
    print(f"=== {years}년 파라미터 그리드 탐색 결과 (장대양/음봉 리버전) ===")
    # 수익률 기준 상위 몇 개만 출력
    top = df_res.sort_values("ret_pct", ascending=False).head(15)
    print(
        f"{'pct':>4} {'pull':>4} {'RR':>4} {'hold':>5} {'최종자산':>10} {'수익률':>8} "
        f"{'MDD':>8} {'샤프':>6} {'칼마':>6} {'거래수':>6}"
    )
    print("-" * 80)
    for _, r in top.iterrows():
        print(
            f"{r['pct_th']*100:>4.1f} {r['pull']*100:>4.0f} {r['rr']:>4.1f} {int(r['hold']):>5} "
            f"{r['final']:>10.2f} {r['ret_pct']:>8.2f}% {r['max_dd']:>8.2f}% "
            f"{r['sharpe']:>6.2f} {r['calmar']:>6.2f} {int(r['n_trades']):>6}"
        )


def main():
    # 기본: 5년 + 10년 각각 테스트 (기본 파라미터 + 그리드 탐색)
    symbol = getattr(config, "SYMBOL", "ETHUSDT")
    print(f"심볼: {symbol}  |  1시간봉 장대양/음봉 리버전 전략 백테스트\n")

    for years in (5, 10):
        print("\n" + "=" * 80)
        print(f"=== {years}년 기본 파라미터 백테스트 ===")
        df = load_or_fetch_1h(years=years, symbol=symbol, force_refresh=False)
        if df is None or len(df) < 100:
            print("데이터 부족, 건너뜁니다.")
            continue
        initial = 100.0
        trades, equity, final_cap, info = run_big_bar_reversion_backtest(df, initial_capital=initial)
        summarize_trades(trades, equity, initial, final_cap, info)
        _grid_search(df, years)


if __name__ == "__main__":
    main()

