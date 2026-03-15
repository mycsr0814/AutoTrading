# -*- coding: utf-8 -*-
"""
진입/손절/익절 로직.
- 장악형(몸통만 감쌈) 마감 후, 25% 눌림 시 1차 진입, 50% 눌림 시 2차 진입(10% 비중). 포지션 보유 중 새로 발생한 장악형은 셋업으로 쓰지 않음.
- 손절: 장악형 봉 최저/고가 이탈 시 전액 손절.
- 익절: 손익비 1:2.5 도달 시 50% 익절, 잔여 50%는 트레일링 스탑(ATR 배수)으로 청산.
- 추세 필터: 4h 종가 > 4h EMA50 → 롱만.
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
    """진입 포지션 (롱 전용)."""
    side: str  # "LONG"
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
    trailing_extreme: float = 0.0  # 고점 추적 (트레일링 스탑용)
    cooldown_until_bar: int = -1    # 잔여 포지션 청산 후 이 봉 인덱스까지 새 셋업 무시
    max_unrealized_pct: float = 0.0  # 포지션 고점(최대 미실현 수익률) — give-back 수익 확정용
    # 포지션 보유 중 같은 방향 장악형 시 추가 진입용 (ADD_ON)
    add_on_engulf_low: float = 0.0
    add_on_engulf_high: float = 0.0
    last_trend_add_bar_idx: int = -1  # 대상승장 4h 이평 눌림 추가 진입 시 쿨다운용
    last_daily_pullback_date: Optional[object] = None  # 일봉 눌림 진입한 날짜 (하루 1회)


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


def _vol_filter(row: pd.Series) -> Tuple[bool, float]:
    """
    변동성 필터: (진입 스킵 여부, 비중 배율 0~1).
    스킵이면 (True, 0), 축소면 (False, VOL_REDUCE_SIZE_PCT), 정상 (False, 1.0).
    """
    if not getattr(config, "VOLATILITY_FILTER_ENABLED", False):
        return False, 1.0
    pct = row.get("atr_pct")
    if pct is None or (isinstance(pct, float) and np.isnan(pct)):
        return False, 1.0
    pct = float(pct)
    skip_th = getattr(config, "VOL_ATR_PERCENTILE_SKIP", 92.0) or 0
    reduce_th = getattr(config, "VOL_ATR_PERCENTILE_REDUCE", 85.0) or 0
    if skip_th > 0 and pct >= skip_th:
        return True, 0.0
    if reduce_th > 0 and pct >= reduce_th:
        return False, getattr(config, "VOL_REDUCE_SIZE_PCT", 0.6)
    return False, 1.0


def _reset_state_after_exit(state: StrategyState, bar_idx: int, set_cooldown: bool = False) -> None:
    """청산 후 전략 상태 초기화. 롱/숏 공통. set_cooldown=True면 COOLDOWN_BARS_AFTER_REMAINDER_EXIT 적용."""
    state.in_position = False
    state.positions = []
    state.waiting_for_pullback = False
    state.first_entry_done = False
    state.trailing_stop_active = False
    state.trailing_extreme = 0.0
    state.trailing_stop_price = 0.0
    state.add_on_engulf_low = 0.0
    state.add_on_engulf_high = 0.0
    state.last_trend_add_bar_idx = -1
    state.last_daily_pullback_date = None
    state.max_unrealized_pct = 0.0
    if set_cooldown:
        cooldown_bars = getattr(config, "COOLDOWN_BARS_AFTER_REMAINDER_EXIT", 0)
        state.cooldown_until_bar = bar_idx + cooldown_bars if cooldown_bars > 0 else -1


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
        if p.get("bull_engulf") and not getattr(config, "DAILY_PULLBACK_LONG_ONLY", False):
            state.waiting_for_pullback = True
            state.first_entry_done = False
            state.last_engulf_bar_idx = bar_idx - 1
            state.last_engulf_low = float(p["low"])
            state.last_engulf_high = float(p["high"])
            state.last_engulf_was_bull = True

    # --- 포지션 보유 중 같은 방향 장악형이 나오면 추가 진입 후보로 기록 (ADD_ON, 롱만) ---
    if state.in_position and state.positions and prev_row is not None and bar_idx >= 1 and getattr(config, "ADD_ON_ENTRY_ENABLED", False):
        p = prev_row
        if state.positions[0].side == "LONG" and p.get("bull_engulf"):
            state.add_on_engulf_low = float(p["low"])
            state.add_on_engulf_high = float(p["high"])

    # --- 이미 포지션 있을 때: 손절 → 4h윗꼬리 익절 → 고점대비 되돌림(give-back) → 연말확정 → EOD → 4h 추세이탈 → 트레일링 → 추가진입 → 1차 익절 ---
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
                _reset_state_after_exit(state, bar_idx, set_cooldown=True)
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
                    _reset_state_after_exit(state, bar_idx, set_cooldown=True)
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
                        _reset_state_after_exit(state, bar_idx, set_cooldown=False)
                        action = "EOD_EXIT"
                        detail = {"price": eod_price, "reason": "eod_profit_take"}
                        return state, action, detail
                else:
                    _reset_state_after_exit(state, bar_idx, set_cooldown=False)
                    action = "EOD_EXIT"
                    detail = {"price": eod_price, "reason": "eod_close"}
                    return state, action, detail

        use_trailing = (remainder_mode == "trailing_atr" and getattr(config, "USE_TRAILING_STOP", True))

        # 0) 기존 손절가 터치 시 즉시 청산 (4h/트레일보다 우선)
        for pos in state.positions:
            if pos.side == "LONG" and low <= pos.stop_price:
                _reset_state_after_exit(state, bar_idx, set_cooldown=False)
                action = "STOP"
                detail = {"price": pos.stop_price, "reason": "stop_long"}
                return state, action, detail

        # 0-1) 4h 윗꼬리 익절: 마감 4h 봉 윗꼬리 > N*몸통이면, 다음 캔들에서 저가 대비 20% 반등 시 익절
        if getattr(config, "TP_4H_WICK_EXIT_ENABLED", False):
            body_4h = row.get("trend_4h_body")
            upper_4h = row.get("trend_4h_upper_wick")
            lower_4h = row.get("trend_4h_lower_wick")
            low_4h = row.get("trend_4h_low")
            high_4h = row.get("trend_4h_high")
            if body_4h is not None and upper_4h is not None and lower_4h is not None and low_4h is not None and high_4h is not None:
                b = float(body_4h)
                uw = float(upper_4h)
                lw = float(lower_4h)
                l4 = float(low_4h)
                h4 = float(high_4h)
                ratio = getattr(config, "TP_4H_UPPER_WICK_TO_BODY_RATIO", 4.0)
                bounce_pct = getattr(config, "TP_4H_WICK_BOUNCE_PCT", 0.2)
                if not (np.isnan(b) or np.isnan(uw) or np.isnan(lw) or np.isnan(l4) or np.isnan(h4)) and b > 0 and h4 > l4:
                    if uw > ratio * b:
                        bounce_level = l4 + bounce_pct * (h4 - l4)
                        if high >= bounce_level:
                            _reset_state_after_exit(state, bar_idx, set_cooldown=(cooldown_bars > 0))
                            action = "TP_4H_WICK_EXIT"
                            detail = {"price": close, "reason": "4h_wick_bounce_long"}
                            return state, action, detail

        # 0-2) 좋은 타이밍 익절: 롱 보유 중 하락 장악형 발생 시 해당 봉 종가로 청산
        if getattr(config, "EXIT_ON_OPPOSITE_ENGULF", False):
            exit_price = float(close)
            if is_long and is_bear_engulf:
                _reset_state_after_exit(state, bar_idx, set_cooldown=(cooldown_bars > 0))
                action = "REVERSE_ENGULF_EXIT"
                detail = {"price": exit_price, "reason": "opposite_engulf"}
                return state, action, detail

        # 0-2.5) 대상승장 4h 이평 눌림 추가: 4h가 EMA 위에 있을 때 가격이 EMA 근처로 눌리면 소량 추가
        if getattr(config, "TREND_ADD_ON_EMA_ENABLED", False) and is_long:
            tc, te = row.get("trend_4h_close"), row.get("trend_4h_ema50")
            if tc is not None and te is not None:
                tcf, tef = float(tc), float(te)
                if not (np.isnan(tcf) or np.isnan(tef)) and tef > 0:
                    strong_pct = getattr(config, "TREND_ADD_STRONG_PCT", 0.012)
                    touch_pct = getattr(config, "TREND_ADD_EMA_TOUCH_PCT", 0.005)
                    cooldown = getattr(config, "TREND_ADD_COOLDOWN_BARS", 24)
                    if tcf >= tef * (1 + strong_pct) and low <= tef * (1 + touch_pct):
                        if state.last_trend_add_bar_idx < 0 or (bar_idx - state.last_trend_add_bar_idx) >= cooldown:
                            vol_skip, vol_mult = _vol_filter(row)
                            if not vol_skip:
                                size_add = getattr(config, "TREND_ADD_SIZE_PCT", 0.05) * vol_mult
                                stop_below = getattr(config, "TREND_ADD_STOP_BELOW_EMA_PCT", 0.003)
                                stop_price = tef * (1 - stop_below)
                                entry_price = _entry_fill_price(close, high, low, tef, is_long=True)
                                first_tp = _rr_tp_price(entry_price, stop_price, config.TP_RR_RATIO, is_long=True)
                                state.positions.append(
                                    Position(
                                        side="LONG",
                                        entry_price=entry_price,
                                        size_pct=size_add,
                                        stop_price=stop_price,
                                        first_tp_price=first_tp,
                                        entry_bar_idx=bar_idx,
                                    )
                                )
                                state.last_trend_add_bar_idx = bar_idx
                                action = "OPEN_TREND_ADD"
                                detail = {"entry": entry_price, "stop": stop_price, "tp1": first_tp, "size_pct": size_add, "limit_price": tef, "reason": "trend_ema_touch"}
                                return state, action, detail

        # 0-3) 포지션 보유 중 같은 방향 장악형 후 눌림/반등 시 7% 추가 진입 (OPEN_ADD)
        if getattr(config, "ADD_ON_ENTRY_ENABLED", False) and state.add_on_engulf_high > state.add_on_engulf_low:
            add_pct = getattr(config, "ADD_ON_PULLBACK_PCT", 0.2)
            elow, ehigh = state.add_on_engulf_low, state.add_on_engulf_high
            vol_skip, vol_mult = _vol_filter(row)
            if not vol_skip:
                add_on_pct = getattr(config, "ADD_ON_ENTRY_PCT", 0.07) * vol_mult
                if is_long:
                    level_add = pullback_level_from_low(elow, ehigh, add_pct)
                    if low <= level_add:
                        entry_price = _entry_fill_price(close, high, low, level_add, is_long=True)
                        first_tp = _rr_tp_price(entry_price, elow, config.TP_RR_RATIO, is_long=True)
                        state.positions.append(
                            Position(
                                side="LONG",
                                entry_price=entry_price,
                                size_pct=add_on_pct,
                                stop_price=elow,
                                first_tp_price=first_tp,
                                entry_bar_idx=bar_idx,
                            )
                        )
                        state.add_on_engulf_low = 0.0
                        state.add_on_engulf_high = 0.0
                        action = "OPEN_ADD"
                        detail = {"entry": entry_price, "stop": elow, "tp1": first_tp, "size_pct": add_on_pct, "limit_price": level_add}
                        return state, action, detail

        # 1) 4h 추세 이탈 시 잔여 포지션 청산 (추세 끝까지 보유 후 한 번에 정리)
        if remainder_mode == "trend_break_4h" and any_tp_done:
            tc, te = row.get("trend_4h_close"), row.get("trend_4h_ema50")
            if tc is not None and te is not None:
                tcf, tef = float(tc), float(te)
                if not (np.isnan(tcf) or np.isnan(tef)):
                    trend_broke = tcf < tef  # 롱만: 4h 종가가 EMA 아래로 이탈
                    if trend_broke:
                        _reset_state_after_exit(state, bar_idx, set_cooldown=(cooldown_bars > 0))
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
            do_activate = (activate_after_atr <= 0) or (high >= breakeven + atr_val * activate_after_atr)
            if do_activate:
                state.trailing_stop_active = True
                state.trailing_extreme = high
                state.trailing_stop_price = state.trailing_extreme - atr_val * trail_mult

        # 트레일링 스탑 갱신 및 청산 체크 (롱만)
        if use_trailing and state.trailing_stop_active:
            state.trailing_extreme = max(state.trailing_extreme, high)
            state.trailing_stop_price = state.trailing_extreme - atr_val * trail_mult
            if getattr(config, "TRAILING_STOP_MIN_BREAKEVEN", True):
                state.trailing_stop_price = max(state.trailing_stop_price, breakeven)
            hit = (close <= state.trailing_stop_price) if exit_on_close else (low <= state.trailing_stop_price)
            if hit:
                _reset_state_after_exit(state, bar_idx, set_cooldown=(cooldown_bars > 0))
                action = "TRAILING_STOP"
                detail = {"price": state.trailing_stop_price, "reason": "trail_long"}
                state.trailing_extreme = 0.0
                state.trailing_stop_price = 0.0
                return state, action, detail

        # 1차 익절 체크 (롱만)
        for pos in state.positions:
            if pos.first_tp_price is not None and not pos.first_tp_done and high >= pos.first_tp_price:
                pos.first_tp_done = True
                action = "TP_FIRST"
                detail = {"price": pos.first_tp_price, "size_pct": pos.size_pct * config.TP_FIRST_HALF}
                return state, action, detail

    elow = state.last_engulf_low
    ehigh = state.last_engulf_high
    stop_long = elow
    level_20 = pullback_level_from_low(elow, ehigh, config.PULLBACK_20) if elow < ehigh else elow
    level_50 = pullback_level_from_low(elow, ehigh, config.PULLBACK_50) if elow < ehigh else elow

    # --- 추세 필터: 4h 종가 > 4h EMA50 일 때만 롱 진입 ---
    trend_close = row.get("trend_4h_close")
    trend_ema = row.get("trend_4h_ema50")
    allow_long = True
    if trend_close is not None and trend_ema is not None:
        tc, te = float(trend_close), float(trend_ema)
        if not (np.isnan(tc) or np.isnan(te)) and te > 0:
            min_pct_above = getattr(config, "TREND_4H_MIN_PCT_ABOVE_EMA", 0)
            allow_long = (tc >= te * (1 + min_pct_above)) if min_pct_above else (tc > te)
            if getattr(config, "BEAR_MARKET_STRICT_LONG_FILTER", False) and row.get("bear_market_strict"):
                strict_pct = getattr(config, "BEAR_MARKET_STRICT_LONG_PCT", 0.012)
                allow_long = tc >= te * (1 + strict_pct)
    if getattr(config, "DAILY_TREND_FILTER", False):
        dc, de = row.get("trend_daily_close"), row.get("trend_daily_ema")
        if dc is not None and de is not None:
            dcf, def_ = float(dc), float(de)
            if not (np.isnan(dcf) or np.isnan(def_)):
                allow_long = allow_long and (dcf > def_)

    if getattr(config, "BEAR_MARKET_RESISTANCE_ENABLED", False):
        br = row.get("bear_market_resistance")
        if br is not None and bool(br):
            allow_long = False  # 윗꼬리 저항 구간에서는 롱 차단

    if getattr(config, "BEAR_REGIME_DEATH_CROSS_ENABLED", False):
        if row.get("bear_regime") is not None and bool(row.get("bear_regime")):
            allow_long = False  # 데스 크로스 구간에서는 롱 차단

    # --- 롱: 25% 눌림 1차 진입, 50% 눌림 2차 진입 (4h 이평 위일 때만). 포지션 보유 중 새 장악형은 위에서 셋업 갱신 안 함. ---
    if state.waiting_for_pullback and state.last_engulf_was_bull and allow_long:
        if not state.first_entry_done and low <= level_20:
            vol_skip, vol_mult = _vol_filter(row)
            if not vol_skip:
                size_pct_adj = config.FIRST_ENTRY_PCT * vol_mult
                state.first_entry_done = True
                state.in_position = True
                entry_price = _entry_fill_price(close, high, low, level_20, is_long=True)
                first_tp = _rr_tp_price(entry_price, stop_long, config.TP_RR_RATIO, is_long=True)
                state.positions = [
                    Position(
                        side="LONG",
                        entry_price=entry_price,
                        size_pct=size_pct_adj,
                        stop_price=stop_long,
                        first_tp_price=first_tp,
                        entry_bar_idx=bar_idx,
                    )
                ]
                state.max_unrealized_pct = 0.0
                action = "OPEN_1"
                detail = {"entry": entry_price, "stop": stop_long, "tp1": first_tp, "size_pct": size_pct_adj, "limit_price": level_20}
                return state, action, detail
        if state.first_entry_done and state.positions and len(state.positions) == 1 and low <= level_50:
            vol_skip, vol_mult = _vol_filter(row)
            if not vol_skip:
                size_pct_adj = config.SECOND_ENTRY_PCT * vol_mult
                entry_price = _entry_fill_price(close, high, low, level_50, is_long=True)
                first_tp = _rr_tp_price(entry_price, stop_long, config.TP_RR_RATIO, is_long=True)
                state.positions.append(
                    Position(
                        side="LONG",
                        entry_price=entry_price,
                        size_pct=size_pct_adj,
                        stop_price=stop_long,
                        first_tp_price=first_tp,
                        entry_bar_idx=bar_idx,
                    )
                )
                action = "OPEN_2"
                detail = {"entry": entry_price, "stop": stop_long, "tp1": first_tp, "size_pct": size_pct_adj, "limit_price": level_50}
                return state, action, detail
        if not state.first_entry_done and low <= level_50:
            vol_skip, vol_mult = _vol_filter(row)
            if not vol_skip:
                size_pct_adj = config.FIRST_ENTRY_PCT * vol_mult
                state.first_entry_done = True
                state.in_position = True
                entry_price = _entry_fill_price(close, high, low, level_50, is_long=True)
                first_tp = _rr_tp_price(entry_price, stop_long, config.TP_RR_RATIO, is_long=True)
                state.positions = [
                    Position(
                        side="LONG",
                        entry_price=entry_price,
                        size_pct=size_pct_adj,
                        stop_price=stop_long,
                        first_tp_price=first_tp,
                        entry_bar_idx=bar_idx,
                    )
                ]
                state.max_unrealized_pct = 0.0
                action = "OPEN_1"
                detail = {"entry": entry_price, "stop": stop_long, "tp1": first_tp, "size_pct": size_pct_adj, "limit_price": level_50}
                return state, action, detail

    # --- 롱: 긴 상승 추세 도중 전일 일봉 눌림 구간 터치 시 1회 진입 (장악형 대신 또는 병행) ---
    if (
        getattr(config, "DAILY_PULLBACK_LONG_ENABLED", False)
        and not state.in_position
        and allow_long
    ):
        ph = row.get("prev_daily_high")
        pl = row.get("prev_daily_low")
        if ph is not None and pl is not None:
            ph, pl = float(ph), float(pl)
            if not (np.isnan(ph) or np.isnan(pl)) and ph > pl:
                cur_date = pd.to_datetime(row["open_time"], unit="ms").date()
                if state.last_daily_pullback_date != cur_date:
                    level_pct = getattr(config, "DAILY_PULLBACK_LEVEL", 0.25) or 0.25
                    level = ph - level_pct * (ph - pl)
                    if low <= level <= high:
                        vol_skip, vol_mult = _vol_filter(row)
                        if not vol_skip:
                            stop_below = getattr(config, "DAILY_PULLBACK_STOP_BELOW_PCT", 0.002) or 0.002
                            stop_price = pl * (1.0 - stop_below)
                            size_pct = (getattr(config, "DAILY_PULLBACK_SIZE_PCT", 0.12) or 0.12) * vol_mult
                            entry_price = _entry_fill_price(close, high, low, level, is_long=True)
                            first_tp = _rr_tp_price(entry_price, stop_price, config.TP_RR_RATIO, is_long=True)
                            state.in_position = True
                            state.last_daily_pullback_date = cur_date
                            state.positions = [
                                Position(
                                    side="LONG",
                                    entry_price=entry_price,
                                    size_pct=size_pct,
                                    stop_price=stop_price,
                                    first_tp_price=first_tp,
                                    entry_bar_idx=bar_idx,
                                )
                            ]
                            state.max_unrealized_pct = 0.0
                            action = "OPEN_1"
                            detail = {"entry": entry_price, "stop": stop_price, "tp1": first_tp, "size_pct": size_pct, "limit_price": level, "source": "daily_pullback"}
                            return state, action, detail

    return state, action, detail
