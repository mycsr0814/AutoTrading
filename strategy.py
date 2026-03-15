# -*- coding: utf-8 -*-
"""
진입/손절/익절 로직.
- 장악형(몸통만 감쌈) 마감 후, 25% 눌림 시 1차 진입, 50% 눌림 시 2차 진입(10% 비중). 포지션 보유 중 새로 발생한 장악형은 셋업으로 쓰지 않음.
- 손절: 장악형 봉 최저/고가 이탈 시 전액 손절.
- 익절: 손익비 1:2.5 도달 시 50% 익절, 잔여 50%는 트레일링 스탑(ATR 배수)으로 청산.
- 추세 필터: 4h 종가 > 4h EMA50 → 롱만, 4h 종가 < 4h EMA50 → 숏만.
백테스트 시 틱/봉 단위로만 판단하여 미래 참조 없음.
"""
from dataclasses import dataclass, field
from typing import Optional, Tuple
import pandas as pd
import numpy as np

import config
from candles import add_engulfing_flags, ensure_ohlcv, pullback_level_from_low


@dataclass
class Position:
    """진입 포지션 (롱 기준)."""
    side: str  # "LONG" or "SHORT"
    entry_price: float
    size_pct: float   # 자금 대비 비중 (0.1 = 10%, 0.3 = 30%)
    stop_price: float
    first_tp_price: Optional[float] = None  # 1차 익절가 (손익비 config.TP_RR_RATIO)
    first_tp_done: bool = False
    entry_bar_idx: int = -1


@dataclass
class StrategyState:
    """전략 상태 (봉 단위로 갱신, 미래 데이터 미사용)."""
    in_position: bool = False
    positions: list = field(default_factory=list)  # List[Position]
    last_engulf_bar_idx: int = -1
    last_engulf_low: float = 0.0
    last_engulf_high: float = 0.0
    last_engulf_was_bull: bool = False
    waiting_for_pullback: bool = False
    first_entry_done: bool = False  # 25% 눌림 1차 진입 여부 (이후 50%에서 2차 진입)
    # 1차 익절 후 트레일링 스탑
    trailing_stop_active: bool = False
    trailing_stop_price: float = 0.0
    trailing_extreme: float = 0.0  # 롱: 고점 추적, 숏: 저점 추적
    cooldown_until_bar: int = -1    # 잔여 포지션 청산 후 이 봉 인덱스까지 새 셋업 무시
    max_unrealized_pct: float = 0.0  # 포지션 고점(최대 미실현 수익률, 가격 기준 %) — give-back 수익 확정용
    last_daily_wick_short_date: Optional[object] = None  # 일봉 윗꼬리+음봉 숏 진입한 날짜 (중복 진입 방지)


def _rr_tp_price(entry: float, stop: float, rr: float, is_long: bool) -> float:
    """손익비 rr일 때 1차 익절가."""
    risk = abs(entry - stop)
    if is_long:
        return entry + risk * rr
    return entry - risk * rr


def _entry_fill_price(close: float, high: float, low: float, level: float, is_long: bool) -> float:
    """진입 체결가: CONSERVATIVE_FILL이면 종가 또는 (고+저)/2, 아니면 레벨 근처."""
    conservative = getattr(config, "CONSERVATIVE_FILL", True)
    use_mid = getattr(config, "FILL_USE_MID", False)
    if not conservative:
        return min(close, level) if is_long else max(close, level)
    if use_mid:
        return (high + low) / 2.0
    return float(close)


def run_signal_on_bar(
    bar_idx: int,
    row: pd.Series,
    prev_row: Optional[pd.Series],
    df: pd.DataFrame,
    state: StrategyState,
    fee_rate: float = config.FEE_EFFECTIVE,
) -> Tuple[StrategyState, Optional[str], Optional[dict]]:
    """
    단일 봉(마감된 row)에 대해 신호 산출.
    - df는 bar_idx 이전(포함)까지만 사용 (미래 참조 없음).
    - 반환: (새 state, 액션 "OPEN_1"|"OPEN_2"|"STOP"|"TP_FIRST"|"TRAILING_STOP"|None, 액션 상세 dict)
    """
    action = None
    detail = None
    low, high, close = float(row["low"]), float(row["high"]), float(row["close"])
    is_bull_engulf = row.get("bull_engulf", False)
    is_bear_engulf = row.get("bear_engulf", False)

    # --- 직전 봉이 상승/하락 장악형으로 마감된 경우: 다음 봉에서 눌림 대기 ---
    # 포지션 없을 때만 새 장악형으로 셋업 (포지션 있는 동안 새 셋업으로 덮어쓰면 중복 진입·과대 수익 발생)
    # 쿨다운 중이면 새 셋업 무시 (잔여 청산 후 과도한 재진입 방지)
    cooldown_bars = getattr(config, "COOLDOWN_BARS_AFTER_REMAINDER_EXIT", 0)
    in_cooldown = cooldown_bars > 0 and bar_idx <= state.cooldown_until_bar
    if prev_row is not None and bar_idx >= 1 and not (state.in_position and state.positions) and not in_cooldown:
        p = prev_row
        if p.get("bull_engulf"):
            state.waiting_for_pullback = True
            state.first_entry_done = False
            state.last_engulf_bar_idx = bar_idx - 1
            state.last_engulf_low = float(p["low"])
            state.last_engulf_high = float(p["high"])
            state.last_engulf_was_bull = True
        elif p.get("bear_engulf"):
            state.waiting_for_pullback = True
            state.first_entry_done = False
            state.last_engulf_bar_idx = bar_idx - 1
            state.last_engulf_low = float(p["low"])
            state.last_engulf_high = float(p["high"])
            state.last_engulf_was_bull = False

    # --- 이미 포지션 있을 때: 손절 → 고점대비 되돌림(give-back) → 연말확정 → EOD → 4h 추세이탈 → 트레일링 → 1차 익절 ---
    if state.in_position and state.positions:
        remainder_mode = getattr(config, "REMAINDER_EXIT_MODE", "original_stop")
        any_tp_done = any(getattr(p, "first_tp_done", False) for p in state.positions)
        is_long = state.positions[0].side == "LONG"
        breakeven = min(p.entry_price for p in state.positions) if is_long else max(p.entry_price for p in state.positions)

        # 가중 평균 미실현 수익률 (가격 기준, %)
        total_pct = sum(getattr(p, "size_pct", 0.1) for p in state.positions)
        if total_pct > 0:
            if is_long:
                current_unrealized_pct = 100.0 * sum(
                    getattr(p, "size_pct", 0.1) * (close - p.entry_price) / p.entry_price for p in state.positions
                ) / total_pct
            else:
                current_unrealized_pct = 100.0 * sum(
                    getattr(p, "size_pct", 0.1) * (p.entry_price - close) / p.entry_price for p in state.positions
                ) / total_pct
        else:
            current_unrealized_pct = 0.0

        # 이상적 수익 확정: 고점 대비 되돌림(give-back) 시 청산 (퀀트 표준, 시간 무관)
        if getattr(config, "PROFIT_LOCK_BY_GIVEBACK", False):
            state.max_unrealized_pct = max(state.max_unrealized_pct, current_unrealized_pct)
            activate = getattr(config, "PROFIT_GIVEBACK_ACTIVATE_PCT", 2.0)
            giveback = getattr(config, "PROFIT_GIVEBACK_PCT", 1.5)
            if state.max_unrealized_pct >= activate and (state.max_unrealized_pct - current_unrealized_pct) >= giveback:
                peak_pct = state.max_unrealized_pct
                state.in_position = False
                state.positions = []
                state.waiting_for_pullback = False
                state.first_entry_done = False
                state.trailing_stop_active = False
                state.trailing_extreme = 0.0
                state.trailing_stop_price = 0.0
                state.max_unrealized_pct = 0.0
                cooldown_bars = getattr(config, "COOLDOWN_BARS_AFTER_REMAINDER_EXIT", 0)
                if cooldown_bars > 0:
                    state.cooldown_until_bar = bar_idx + cooldown_bars
                action = "GIVEBACK_EXIT"
                detail = {"price": close, "reason": "giveback_from_peak", "peak_pct": peak_pct}
                return state, action, detail

        # 주기별 수익 확정: 연/월/주 마감 시 수익 나 있으면 해당 구간 마지막 봉 종가로 청산
        if getattr(config, "EOY_CLOSE_IF_PROFIT", False) and prev_row is not None:
            cur_dt = pd.to_datetime(row["open_time"], unit="ms")
            prev_dt = pd.to_datetime(prev_row["open_time"], unit="ms")
            period = getattr(config, "PROFIT_LOCK_PERIOD", "year").lower()
            boundary_crossed = False
            if period == "year":
                boundary_crossed = cur_dt.year != prev_dt.year
            elif period == "month":
                boundary_crossed = (cur_dt.year, cur_dt.month) != (prev_dt.year, prev_dt.month)
            elif period == "week":
                cur_iso = cur_dt.isocalendar()
                prev_iso = prev_dt.isocalendar()
                boundary_crossed = (cur_iso[0], cur_iso[1]) != (prev_iso[0], prev_iso[1])
            elif period == "day":
                boundary_crossed = cur_dt.date() != prev_dt.date()
            if boundary_crossed:
                period_end_price = float(prev_row["close"])
                unrealized = 0.0
                for pos in state.positions:
                    notional_pct = getattr(pos, "size_pct", 0.1)
                    if pos.side == "LONG":
                        unrealized += notional_pct * (period_end_price - pos.entry_price) / pos.entry_price
                    else:
                        unrealized += notional_pct * (pos.entry_price - period_end_price) / pos.entry_price
                if unrealized > 0:
                    state.in_position = False
                    state.positions = []
                    state.waiting_for_pullback = False
                    state.first_entry_done = False
                    state.trailing_stop_active = False
                    state.trailing_extreme = 0.0
                    state.trailing_stop_price = 0.0
                    cooldown_bars = getattr(config, "COOLDOWN_BARS_AFTER_REMAINDER_EXIT", 0)
                    if cooldown_bars > 0:
                        state.cooldown_until_bar = bar_idx + cooldown_bars
                    action = "EOY_PROFIT_LOCK"
                    detail = {"price": period_end_price, "reason": f"period_profit_lock_{period}"}
                    return state, action, detail

        # 일일 종료 시 청산: 날짜가 바뀐 첫 봉에서 전일 종가로 청산. PROFIT_ONLY면 수익일 때만 청산.
        if getattr(config, "EOD_EXIT_ENABLED", False) and prev_row is not None:
            cur_dt = pd.to_datetime(row["open_time"], unit="ms")
            prev_dt = pd.to_datetime(prev_row["open_time"], unit="ms")
            if cur_dt.date() != prev_dt.date():
                eod_price = float(prev_row["close"])
                profit_only = getattr(config, "EOD_EXIT_PROFIT_ONLY", False)
                if profit_only:
                    # 포지션 합계 기준 미실현 손익이 양수일 때만 EOD 청산 (익절)
                    unrealized = 0.0
                    for pos in state.positions:
                        notional_pct = getattr(pos, "size_pct", 0.1)
                        if pos.side == "LONG":
                            unrealized += notional_pct * (eod_price - pos.entry_price) / pos.entry_price
                        else:
                            unrealized += notional_pct * (pos.entry_price - eod_price) / pos.entry_price
                    if unrealized <= 0:
                        # 손실 중이면 EOD 청산 안 함
                        pass
                    else:
                        state.in_position = False
                        state.positions = []
                        state.waiting_for_pullback = False
                        state.first_entry_done = False
                        state.trailing_stop_active = False
                        state.trailing_extreme = 0.0
                        state.trailing_stop_price = 0.0
                        action = "EOD_EXIT"
                        detail = {"price": eod_price, "reason": "eod_profit_take"}
                        return state, action, detail
                else:
                    state.in_position = False
                    state.positions = []
                    state.waiting_for_pullback = False
                    state.first_entry_done = False
                    state.trailing_stop_active = False
                    state.trailing_extreme = 0.0
                    state.trailing_stop_price = 0.0
                    action = "EOD_EXIT"
                    detail = {"price": eod_price, "reason": "eod_close"}
                    return state, action, detail

        use_trailing = (remainder_mode == "trailing_atr" and getattr(config, "USE_TRAILING_STOP", True))

        # 0) 기존 손절가 터치 시 즉시 청산 (4h/트레일보다 우선)
        for pos in state.positions:
            if pos.side == "LONG" and low <= pos.stop_price:
                state.in_position = False
                state.positions = []
                state.waiting_for_pullback = False
                state.first_entry_done = False
                state.trailing_stop_active = False
                state.trailing_extreme = 0.0
                state.trailing_stop_price = 0.0
                # STOP 시 쿨다운 없음 (original_stop 12% 동작 유지)
                action = "STOP"
                detail = {"price": pos.stop_price, "reason": "stop_long"}
                return state, action, detail
            if pos.side == "SHORT" and high >= pos.stop_price:
                state.in_position = False
                state.positions = []
                state.waiting_for_pullback = False
                state.first_entry_done = False
                state.trailing_stop_active = False
                state.trailing_extreme = 0.0
                state.trailing_stop_price = 0.0
                # STOP 시 쿨다운 없음 (original_stop 12% 동작 유지)
                action = "STOP"
                detail = {"price": pos.stop_price, "reason": "stop_short"}
                return state, action, detail

        # 0-2) 좋은 타이밍 익절: 반대 방향 장악형 발생 시 해당 봉 종가로 청산 (시간이 아닌 신호 기반)
        if getattr(config, "EXIT_ON_OPPOSITE_ENGULF", False):
            exit_price = float(close)  # 현재 봉(장악형 봉) 종가로 청산
            do_exit = False
            if is_long and is_bear_engulf:
                do_exit = True
            elif not is_long and is_bull_engulf:
                do_exit = True
            if do_exit:
                state.in_position = False
                state.positions = []
                state.waiting_for_pullback = False
                state.first_entry_done = False
                state.trailing_stop_active = False
                state.trailing_extreme = 0.0
                state.trailing_stop_price = 0.0
                if cooldown_bars > 0:
                    state.cooldown_until_bar = bar_idx + cooldown_bars
                action = "REVERSE_ENGULF_EXIT"
                detail = {"price": exit_price, "reason": "opposite_engulf"}
                return state, action, detail

        # 1) 4h 추세 이탈 시 잔여 포지션 청산 (추세 끝까지 보유 후 한 번에 정리)
        if remainder_mode == "trend_break_4h" and any_tp_done:
            tc, te = row.get("trend_4h_close"), row.get("trend_4h_ema50")
            if tc is not None and te is not None:
                tcf, tef = float(tc), float(te)
                if not (np.isnan(tcf) or np.isnan(tef)):
                    trend_broke = (is_long and tcf < tef) or (not is_long and tcf > tef)
                    if trend_broke:
                        state.in_position = False
                        state.positions = []
                        state.waiting_for_pullback = False
                        state.first_entry_done = False
                        state.trailing_stop_active = False
                        state.trailing_extreme = 0.0
                        state.trailing_stop_price = 0.0
                        if cooldown_bars > 0:
                            state.cooldown_until_bar = bar_idx + cooldown_bars
                        action = "TREND_BREAK_EXIT"
                        detail = {"price": close, "reason": "trend_break_4h"}
                        return state, action, detail

        atr_1h = row.get("atr")
        atr_1h_val = float(atr_1h) if atr_1h is not None and not (isinstance(atr_1h, float) and np.isnan(atr_1h)) else 0.0
        atr_4h = row.get("atr_4h")
        atr_4h_val = float(atr_4h) if atr_4h is not None and not (isinstance(atr_4h, float) and np.isnan(atr_4h)) else 0.0
        use_4h_atr = getattr(config, "TRAILING_USE_4H_ATR", False)
        atr_val = atr_4h_val if (use_4h_atr and atr_4h_val > 0) else atr_1h_val
        trail_mult = getattr(config, "TRAILING_STOP_ATR_MULT", 2.0)
        exit_on_close = getattr(config, "TRAILING_EXIT_ON_CLOSE", True)
        activate_after_atr = getattr(config, "TRAILING_ACTIVATE_AFTER_ATR", 0.0)

        # 2) 1차 익절 후 트레일링 스탑: 수익이 ATR N배 이상 나온 뒤에만 활성화 (추세 길게 먹기)
        if use_trailing and any_tp_done and not state.trailing_stop_active and atr_val > 0:
            if activate_after_atr <= 0:
                do_activate = True
            else:
                if is_long:
                    do_activate = high >= breakeven + atr_val * activate_after_atr
                else:
                    do_activate = low <= breakeven - atr_val * activate_after_atr
            if do_activate:
                state.trailing_stop_active = True
                state.trailing_extreme = high if is_long else low
                if is_long:
                    state.trailing_stop_price = state.trailing_extreme - atr_val * trail_mult
                else:
                    state.trailing_stop_price = state.trailing_extreme + atr_val * trail_mult

        # 트레일링 스탑 갱신 및 청산 체크 (USE_TRAILING_STOP=True일 때만)
        if use_trailing and state.trailing_stop_active:
            if is_long:
                state.trailing_extreme = max(state.trailing_extreme, high)
                state.trailing_stop_price = state.trailing_extreme - atr_val * trail_mult
                if getattr(config, "TRAILING_STOP_MIN_BREAKEVEN", True):
                    state.trailing_stop_price = max(state.trailing_stop_price, breakeven)
                hit = (close <= state.trailing_stop_price) if exit_on_close else (low <= state.trailing_stop_price)
                if hit:
                    state.in_position = False
                    state.positions = []
                    state.waiting_for_pullback = False
                    state.first_entry_done = False
                    state.trailing_stop_active = False
                    if cooldown_bars > 0:
                        state.cooldown_until_bar = bar_idx + cooldown_bars
                    action = "TRAILING_STOP"
                    detail = {"price": state.trailing_stop_price, "reason": "trail_long"}
                    state.trailing_extreme = 0.0
                    state.trailing_stop_price = 0.0
                    return state, action, detail
            else:
                state.trailing_extreme = min(state.trailing_extreme, low)
                state.trailing_stop_price = state.trailing_extreme + atr_val * trail_mult
                if getattr(config, "TRAILING_STOP_MIN_BREAKEVEN", True):
                    state.trailing_stop_price = min(state.trailing_stop_price, breakeven)
                hit = (close >= state.trailing_stop_price) if exit_on_close else (high >= state.trailing_stop_price)
                if hit:
                    state.in_position = False
                    state.positions = []
                    state.waiting_for_pullback = False
                    state.first_entry_done = False
                    state.trailing_stop_active = False
                    if cooldown_bars > 0:
                        state.cooldown_until_bar = bar_idx + cooldown_bars
                    action = "TRAILING_STOP"
                    detail = {"price": state.trailing_stop_price, "reason": "trail_short"}
                    state.trailing_extreme = 0.0
                    state.trailing_stop_price = 0.0
                    return state, action, detail

        # 1차 익절 체크 (손절은 위에서 이미 처리)
        for pos in state.positions:
            if pos.side == "LONG":
                if pos.first_tp_price is not None and not pos.first_tp_done and high >= pos.first_tp_price:
                    pos.first_tp_done = True
                    action = "TP_FIRST"
                    detail = {"price": pos.first_tp_price, "size_pct": pos.size_pct * config.TP_FIRST_HALF}
                    return state, action, detail
            else:  # SHORT
                if pos.first_tp_price is not None and not pos.first_tp_done and low <= pos.first_tp_price:
                    pos.first_tp_done = True
                    action = "TP_FIRST"
                    detail = {"price": pos.first_tp_price, "size_pct": pos.size_pct * config.TP_FIRST_HALF}
                    return state, action, detail

    elow = state.last_engulf_low
    ehigh = state.last_engulf_high
    # 손절: 장악형 캔들 꼬리에 타이트 (롱=저가, 숏=고가)
    stop_long = elow
    stop_short = ehigh
    level_20 = pullback_level_from_low(elow, ehigh, config.PULLBACK_20) if elow < ehigh else elow
    level_50 = pullback_level_from_low(elow, ehigh, config.PULLBACK_50) if elow < ehigh else elow

    # --- 추세 필터: 4h 종가 > 4h EMA50 → 롱만, 4h 종가 < 4h EMA50 → 숏만 ---
    trend_close = row.get("trend_4h_close")
    trend_ema = row.get("trend_4h_ema50")
    allow_long = True
    allow_short = True
    if trend_close is not None and trend_ema is not None:
        tc, te = float(trend_close), float(trend_ema)
        if not (np.isnan(tc) or np.isnan(te)) and te > 0:
            min_pct = getattr(config, "TREND_4H_MIN_PCT_ABOVE_EMA", 0)
            if min_pct <= 0:
                allow_long = tc > te
                allow_short = tc < te
            else:
                allow_long = tc >= te * (1 + min_pct)
                allow_short = tc <= te * (1 - min_pct)
            # 하락장(종가 < EMA50 연속 N일)일 때 롱만 더 엄격: 4h 종가가 EMA 대비 STRICT_LONG_PCT 이상일 때만 롱
            if getattr(config, "BEAR_MARKET_STRICT_LONG_FILTER", False) and row.get("bear_market_strict"):
                strict_pct = getattr(config, "BEAR_MARKET_STRICT_LONG_PCT", 0.012)
                allow_long = tc >= te * (1 + strict_pct)
    # --- 일봉 추세 필터 (하락장 롱 진입 억제): 일봉 종가 > 일봉 EMA → 롱 허용, 일봉 종가 < 일봉 EMA → 숏 허용 ---
    if getattr(config, "DAILY_TREND_FILTER", False):
        dc, de = row.get("trend_daily_close"), row.get("trend_daily_ema")
        if dc is not None and de is not None:
            dcf, def_ = float(dc), float(de)
            if not (np.isnan(dcf) or np.isnan(def_)):
                allow_long = allow_long and (dcf > def_)
                allow_short = allow_short and (dcf < def_)

    # --- 하락장 최적화: 일봉 EMA 20/50에 윗꼬리 저항이 반복되면 숏만 진입 (롱 차단) ---
    if getattr(config, "BEAR_MARKET_RESISTANCE_ENABLED", False):
        br = row.get("bear_market_resistance")
        if br is not None and bool(br):
            allow_long = False  # 저항 확인 구간에서는 롱 차단, 숏만 허용

    # --- 하락장 수익화: 일봉 데스 크로스(EMA20 < EMA50) 시 숏만 진입 ---
    if getattr(config, "BEAR_REGIME_DEATH_CROSS_ENABLED", False):
        if row.get("bear_regime") is not None and bool(row.get("bear_regime")):
            allow_long = False  # 하락 추세 구간에서는 롱 차단, 숏만 허용

    # --- 숏 비교용: 일봉 윗꼬리 저항(전일) + 당일 음봉·EMA20 아래 → 다음날 첫 1h봉에 숏 진입 (손절=당일 일봉 고가) ---
    if (
        getattr(config, "DAILY_WICK_BEAR_SHORT_ENABLED", False)
        and not state.in_position
        and not state.waiting_for_pullback
        and allow_short
        and row.get("daily_wick_bear_short_signal")
        and prev_row is not None
    ):
        cur_date = pd.to_datetime(row["open_time"], unit="ms").date()
        prev_date = pd.to_datetime(prev_row["open_time"], unit="ms").date()
        is_first_bar_of_day = cur_date != prev_date
        stop_high = row.get("trend_daily_high")
        if is_first_bar_of_day and stop_high is not None and not (isinstance(stop_high, float) and np.isnan(stop_high)) and float(stop_high) > 0 and (state.last_daily_wick_short_date is None or state.last_daily_wick_short_date != cur_date):
            stop_short = float(stop_high)
            entry_price = _entry_fill_price(close, high, low, close, is_long=False)
            if entry_price < stop_short:  # 숏이면 진입가 < 손절가 정상
                first_tp = _rr_tp_price(entry_price, stop_short, config.TP_RR_RATIO, is_long=False)
                state.in_position = True
                state.last_daily_wick_short_date = cur_date
                state.positions = [
                    Position(
                        side="SHORT",
                        entry_price=entry_price,
                        size_pct=config.FIRST_ENTRY_PCT,
                        stop_price=stop_short,
                        first_tp_price=first_tp,
                        entry_bar_idx=bar_idx,
                    )
                ]
                state.max_unrealized_pct = 0.0
                action = "OPEN_1"
                detail = {"entry": entry_price, "stop": stop_short, "tp1": first_tp, "size_pct": config.FIRST_ENTRY_PCT, "source": "daily_wick_bear"}
                return state, action, detail

    # --- 롱: 25% 눌림 1차 진입, 50% 눌림 2차 진입 (4h 이평 위일 때만). 포지션 보유 중 새 장악형은 위에서 셋업 갱신 안 함. ---
    if state.waiting_for_pullback and state.last_engulf_was_bull and allow_long:
        if not state.first_entry_done and low <= level_20:
            state.first_entry_done = True
            state.in_position = True
            entry_price = _entry_fill_price(close, high, low, level_20, is_long=True)
            first_tp = _rr_tp_price(entry_price, stop_long, config.TP_RR_RATIO, is_long=True)
            state.positions = [
                Position(
                    side="LONG",
                    entry_price=entry_price,
                    size_pct=config.FIRST_ENTRY_PCT,
                    stop_price=stop_long,
                    first_tp_price=first_tp,
                    entry_bar_idx=bar_idx,
                )
            ]
            state.max_unrealized_pct = 0.0
            action = "OPEN_1"
            detail = {"entry": entry_price, "stop": stop_long, "tp1": first_tp, "size_pct": config.FIRST_ENTRY_PCT}
            return state, action, detail
        if state.first_entry_done and state.positions and len(state.positions) == 1 and low <= level_50:
            entry_price = _entry_fill_price(close, high, low, level_50, is_long=True)
            first_tp = _rr_tp_price(entry_price, stop_long, config.TP_RR_RATIO, is_long=True)
            state.positions.append(
                Position(
                    side="LONG",
                    entry_price=entry_price,
                    size_pct=config.SECOND_ENTRY_PCT,
                    stop_price=stop_long,
                    first_tp_price=first_tp,
                    entry_bar_idx=bar_idx,
                )
            )
            action = "OPEN_2"
            detail = {"entry": entry_price, "stop": stop_long, "tp1": first_tp, "size_pct": config.SECOND_ENTRY_PCT}
            return state, action, detail
        if not state.first_entry_done and low <= level_50:
            state.first_entry_done = True
            state.in_position = True
            entry_price = _entry_fill_price(close, high, low, level_50, is_long=True)
            first_tp = _rr_tp_price(entry_price, stop_long, config.TP_RR_RATIO, is_long=True)
            state.positions = [
                Position(
                    side="LONG",
                    entry_price=entry_price,
                    size_pct=config.FIRST_ENTRY_PCT,
                    stop_price=stop_long,
                    first_tp_price=first_tp,
                    entry_bar_idx=bar_idx,
                )
            ]
            state.max_unrealized_pct = 0.0
            action = "OPEN_1"
            detail = {"entry": entry_price, "stop": stop_long, "tp1": first_tp, "size_pct": config.FIRST_ENTRY_PCT}
            return state, action, detail

    # --- 숏: 25% 반등 1차 진입, 50% 반등 2차 진입 (4h 이평 아래일 때만). 대하락장(종가<EMA50 5일)이면 반등 더 일찍(20%)에 1차 숏 ---
    if state.waiting_for_pullback and not state.last_engulf_was_bull and allow_short:
        level_20_s = ehigh - config.PULLBACK_20 * (ehigh - elow) if ehigh > elow else ehigh
        level_50_s = ehigh - config.PULLBACK_50 * (ehigh - elow) if ehigh > elow else ehigh
        strict_pullback = getattr(config, "BEAR_MARKET_STRICT_SHORT_PULLBACK_PCT", 0) or 0
        use_strict_pullback = row.get("bear_market_strict") and strict_pullback > 0
        level_first_s = (ehigh - strict_pullback * (ehigh - elow)) if (ehigh > elow and use_strict_pullback) else level_20_s
        if not state.first_entry_done and high >= level_first_s:
            state.first_entry_done = True
            state.in_position = True
            entry_price = _entry_fill_price(close, high, low, level_first_s, is_long=False)
            first_tp = _rr_tp_price(entry_price, stop_short, config.TP_RR_RATIO, is_long=False)
            state.positions = [
                Position(
                    side="SHORT",
                    entry_price=entry_price,
                    size_pct=config.FIRST_ENTRY_PCT,
                    stop_price=stop_short,
                    first_tp_price=first_tp,
                    entry_bar_idx=bar_idx,
                )
            ]
            state.max_unrealized_pct = 0.0
            action = "OPEN_1"
            detail = {"entry": entry_price, "stop": stop_short, "tp1": first_tp, "size_pct": config.FIRST_ENTRY_PCT}
            return state, action, detail
        if state.first_entry_done and state.positions and len(state.positions) == 1 and high >= level_50_s:
            entry_price = _entry_fill_price(close, high, low, level_50_s, is_long=False)
            first_tp = _rr_tp_price(entry_price, stop_short, config.TP_RR_RATIO, is_long=False)
            state.positions.append(
                Position(
                    side="SHORT",
                    entry_price=entry_price,
                    size_pct=config.SECOND_ENTRY_PCT,
                    stop_price=stop_short,
                    first_tp_price=first_tp,
                    entry_bar_idx=bar_idx,
                )
            )
            action = "OPEN_2"
            detail = {"entry": entry_price, "stop": stop_short, "tp1": first_tp, "size_pct": config.SECOND_ENTRY_PCT}
            return state, action, detail
        if not state.first_entry_done and high >= level_50_s:
            state.first_entry_done = True
            state.in_position = True
            entry_price = _entry_fill_price(close, high, low, level_50_s, is_long=False)
            first_tp = _rr_tp_price(entry_price, stop_short, config.TP_RR_RATIO, is_long=False)
            state.positions = [
                Position(
                    side="SHORT",
                    entry_price=entry_price,
                    size_pct=config.FIRST_ENTRY_PCT,
                    stop_price=stop_short,
                    first_tp_price=first_tp,
                    entry_bar_idx=bar_idx,
                )
            ]
            state.max_unrealized_pct = 0.0
            action = "OPEN_1"
            detail = {"entry": entry_price, "stop": stop_short, "tp1": first_tp, "size_pct": config.FIRST_ENTRY_PCT}
            return state, action, detail

    return state, action, detail
