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
from candles import add_engulfing_flags, ensure_ohlcv, pullback_level_from_low, pullback_level_from_high


@dataclass
class Position:
    """진입 포지션."""
    side: str  # "LONG" 또는 "SHORT"
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
    first_entry_done: bool = False  # 25% 눌림 1차 진입 여부 (이후 55%에서 2차 진입)
    cooldown_until_bar: int = -1    # 연말 확정 청산 후 이 봉 인덱스까지 새 셋업 무시
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
    """청산 후 전략 상태 초기화. set_cooldown=True면 연말 확정 후 COOLDOWN_BARS_AFTER_EOY 적용."""
    state.in_position = False
    state.positions = []
    state.waiting_for_pullback = False
    state.first_entry_done = False
    state.last_daily_pullback_date = None
    if set_cooldown:
        cooldown_bars = getattr(config, "COOLDOWN_BARS_AFTER_EOY", 24)
        state.cooldown_until_bar = bar_idx + cooldown_bars if cooldown_bars > 0 else -1
    else:
        state.cooldown_until_bar = -1


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

    # --- 직전 봉이 상승/하락 장악형으로 마감된 경우: 다음 봉에서 눌림/반등 대기 ---
    # 포지션 없을 때만 새 장악형으로 셋업. 쿨다운 중(연말 확정 청산 후)이면 새 셋업 무시.
    in_cooldown = state.cooldown_until_bar >= 0 and bar_idx <= state.cooldown_until_bar
    if prev_row is not None and bar_idx >= 1 and not (state.in_position and state.positions) and not in_cooldown:
        p = prev_row
        # 롱 셋업: 상승 장악형
        if p.get("bull_engulf") and not getattr(config, "DAILY_PULLBACK_LONG_ONLY", False):
            state.waiting_for_pullback = True
            state.first_entry_done = False
            state.last_engulf_bar_idx = bar_idx - 1
            state.last_engulf_low = float(p["low"])
            state.last_engulf_high = float(p["high"])
            state.last_engulf_was_bull = True
        # 숏 셋업: 하락 장악형
        elif p.get("bear_engulf"):
            state.waiting_for_pullback = True
            state.first_entry_done = False
            state.last_engulf_bar_idx = bar_idx - 1
            state.last_engulf_low = float(p["low"])
            state.last_engulf_high = float(p["high"])
            state.last_engulf_was_bull = False

    # --- 이미 포지션 있을 때: 손절 → 연말확정 → 반대 장악형 → 1차 익절 (잔여는 손절가 터치 시) ---
    if state.in_position and state.positions:
        is_long = state.positions[0].side == "LONG"

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

        # 0) 기존 손절가 터치 시 즉시 청산
        for pos in state.positions:
            if pos.side == "LONG" and low <= pos.stop_price:
                _reset_state_after_exit(state, bar_idx, set_cooldown=False)
                action = "STOP"
                detail = {"price": pos.stop_price, "reason": "stop_long"}
                return state, action, detail
            if pos.side == "SHORT" and high >= pos.stop_price:
                _reset_state_after_exit(state, bar_idx, set_cooldown=False)
                action = "STOP"
                detail = {"price": pos.stop_price, "reason": "stop_short"}
                return state, action, detail

        # 반대 장악형: 롱/숏 보유 중 반대 방향 장악형 발생 시 해당 봉 종가로 청산
        if getattr(config, "EXIT_ON_OPPOSITE_ENGULF", True):
            exit_price = float(close)
            if is_long and is_bear_engulf:
                _reset_state_after_exit(state, bar_idx, set_cooldown=False)
                action = "REVERSE_ENGULF_EXIT"
                detail = {"price": exit_price, "reason": "opposite_engulf"}
                return state, action, detail
            if (not is_long) and is_bull_engulf:
                _reset_state_after_exit(state, bar_idx, set_cooldown=False)
                action = "REVERSE_ENGULF_EXIT"
                detail = {"price": exit_price, "reason": "opposite_engulf"}
                return state, action, detail

        # 1차 익절 체크 (롱/숏)
        for pos in state.positions:
            if pos.first_tp_price is None or pos.first_tp_done:
                continue
            if pos.side == "LONG" and high >= pos.first_tp_price:
                pos.first_tp_done = True
                action = "TP_FIRST"
                detail = {"price": pos.first_tp_price, "size_pct": pos.size_pct * config.TP_FIRST_HALF}
                return state, action, detail
            if pos.side == "SHORT" and low <= pos.first_tp_price:
                pos.first_tp_done = True
                action = "TP_FIRST"
                detail = {"price": pos.first_tp_price, "size_pct": pos.size_pct * config.TP_FIRST_HALF}
                return state, action, detail

    elow = state.last_engulf_low
    ehigh = state.last_engulf_high
    # 롱/숏 공통으로 마지막 장악형 범위를 사용 (bull: low→high, bear: low→high 그대로)
    stop_long = elow
    stop_short = ehigh
    level_20_long = pullback_level_from_low(elow, ehigh, config.PULLBACK_20) if elow < ehigh else elow
    level_50_long = pullback_level_from_low(elow, ehigh, config.PULLBACK_50) if elow < ehigh else elow
    level_20_short = pullback_level_from_high(ehigh, elow, config.PULLBACK_20) if elow < ehigh else ehigh
    level_50_short = pullback_level_from_high(ehigh, elow, config.PULLBACK_50) if elow < ehigh else ehigh

    # --- 추세 필터: 롱/숏 진입 허용 여부 ---
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
            # 숏: 4h 종가가 EMA 아래에 있을 때만 허용 (롱과 대칭)
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

    if getattr(config, "BEAR_MARKET_RESISTANCE_ENABLED", False):
        br = row.get("bear_market_resistance")
        if br is not None and bool(br):
            allow_long = False  # 윗꼬리 저항 구간에서는 롱 차단

    if getattr(config, "BEAR_REGIME_DEATH_CROSS_ENABLED", False):
        if row.get("bear_regime") is not None and bool(row.get("bear_regime")):
            allow_long = False  # 데스 크로스 구간에서는 롱 차단

    # --- 롱: 25% 눌림 1차 진입, 50% 눌림 2차 진입 (4h 이평 위일 때만). 포지션 보유 중 새 장악형은 위에서 셋업 갱신 안 함. ---
    if state.waiting_for_pullback and state.last_engulf_was_bull and allow_long:
        if not state.first_entry_done and low <= level_20_long:
            vol_skip, vol_mult = _vol_filter(row)
            if not vol_skip:
                size_pct_adj = config.FIRST_ENTRY_PCT * vol_mult
                state.first_entry_done = True
                state.in_position = True
                entry_price = _entry_fill_price(close, high, low, level_20_long, is_long=True)
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
                action = "OPEN_1"
                detail = {"entry": entry_price, "stop": stop_long, "tp1": first_tp, "size_pct": size_pct_adj, "limit_price": level_20_long}
                return state, action, detail
        if state.first_entry_done and state.positions and len(state.positions) == 1 and low <= level_50_long:
            vol_skip, vol_mult = _vol_filter(row)
            if not vol_skip:
                size_pct_adj = config.SECOND_ENTRY_PCT * vol_mult
                entry_price = _entry_fill_price(close, high, low, level_50_long, is_long=True)
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
                detail = {"entry": entry_price, "stop": stop_long, "tp1": first_tp, "size_pct": size_pct_adj, "limit_price": level_50_long}
                return state, action, detail
        if not state.first_entry_done and low <= level_50_long:
            vol_skip, vol_mult = _vol_filter(row)
            if not vol_skip:
                size_pct_adj = config.FIRST_ENTRY_PCT * vol_mult
                state.first_entry_done = True
                state.in_position = True
                entry_price = _entry_fill_price(close, high, low, level_50_long, is_long=True)
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
                action = "OPEN_1"
                detail = {"entry": entry_price, "stop": stop_long, "tp1": first_tp, "size_pct": size_pct_adj, "limit_price": level_50_long}
                return state, action, detail

    # --- 숏: 하락 장악형 후 25% 반등 1차 진입, 50% 반등 2차 진입 (4h 이평 아래일 때만) ---
    if state.waiting_for_pullback and not state.last_engulf_was_bull and allow_short:
        # 1차 숏 진입: 25% 반등
        if not state.first_entry_done and high >= level_20_short and not state.in_position:
            vol_skip, vol_mult = _vol_filter(row)
            if not vol_skip:
                size_pct_adj = config.FIRST_ENTRY_PCT * vol_mult
                state.first_entry_done = True
                state.in_position = True
                entry_price = _entry_fill_price(close, high, low, level_20_short, is_long=False)
                first_tp = _rr_tp_price(entry_price, stop_short, config.TP_RR_RATIO, is_long=False)
                state.positions = [
                    Position(
                        side="SHORT",
                        entry_price=entry_price,
                        size_pct=size_pct_adj,
                        stop_price=stop_short,
                        first_tp_price=first_tp,
                        entry_bar_idx=bar_idx,
                    )
                ]
                action = "OPEN_1"
                detail = {"entry": entry_price, "stop": stop_short, "tp1": first_tp, "size_pct": size_pct_adj, "limit_price": level_20_short}
                return state, action, detail
        # 2차 숏 진입: 50% 반등
        if state.first_entry_done and state.positions and len(state.positions) == 1 and high >= level_50_short:
            vol_skip, vol_mult = _vol_filter(row)
            if not vol_skip:
                size_pct_adj = config.SECOND_ENTRY_PCT * vol_mult
                entry_price = _entry_fill_price(close, high, low, level_50_short, is_long=False)
                first_tp = _rr_tp_price(entry_price, stop_short, config.TP_RR_RATIO, is_long=False)
                state.positions.append(
                    Position(
                        side="SHORT",
                        entry_price=entry_price,
                        size_pct=size_pct_adj,
                        stop_price=stop_short,
                        first_tp_price=first_tp,
                        entry_bar_idx=bar_idx,
                    )
                )
                action = "OPEN_2"
                detail = {"entry": entry_price, "stop": stop_short, "tp1": first_tp, "size_pct": size_pct_adj, "limit_price": level_50_short}
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
                            action = "OPEN_1"
                            detail = {"entry": entry_price, "stop": stop_price, "tp1": first_tp, "size_pct": size_pct, "limit_price": level, "source": "daily_pullback"}
                            return state, action, detail

    return state, action, detail
