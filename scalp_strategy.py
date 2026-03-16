# -*- coding: utf-8 -*-
"""
ETH 15분봉 단타 전략 — EMA 정배열 + EMA50 터치 매수.

[전략 요약]
- 15분봉 상 EMA20, EMA50 정배열(EMA20 > EMA50)일 때만 롱 진입.
- 진입: 가격이 EMA50에 닿으면 매수 (해당 봉 저가 <= EMA50 터치).
- 손절: 매수가 대비 0.45% 하락 시.
- 익절: 손익비 1:1.5 (리스크 0.45% × 1.5 = 0.675% 목표).
- 백테스트: 신호는 해당 봉 마감 시점만 사용, 체결은 다음 봉 시가.
"""

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


def _get(name: str, default):
    try:
        import config
        return getattr(config, name, default)
    except Exception:
        return default


EMA_FAST = _get("SCALP_EMA_FAST", 20)
EMA_SLOW = _get("SCALP_EMA_SLOW", 50)
TOUCH_TOLERANCE = _get("SCALP_EMA50_TOUCH_TOLERANCE", 0.001)  # EMA50 대비 ±0.1% 터치
TREND_MIN_SPREAD = _get("SCALP_TREND_MIN_SPREAD_PCT", 0.001)  # 정배열 강화: EMA20 >= EMA50*(1+이값)
STOP_PCT = _get("SCALP_STOP_PCT", 0.0045)   # 0.45% 손절
RR_RATIO = _get("SCALP_RR_RATIO", 1.5)       # 익절 1:1.5
SIZE_PCT = _get("SCALP_SIZE_PCT", 0.08)
MAX_HOLD_BARS = _get("SCALP_MAX_HOLD_BARS", 24)  # 최대 보유 봉 수 (15m 기준 6시간)


def prepare_15m_for_signal(df_15: pd.DataFrame, df_1h: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """
    15m에 EMA20, EMA50 추가. 15m 종가만 사용 → 미래 참조 없음.
    df_1h는 사용하지 않음 (호환용으로 인자만 받음).
    """
    if df_15 is None or len(df_15) < EMA_SLOW + 5:
        return df_15

    df = df_15.copy()
    for c in ["open", "high", "low", "close"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    close = df["close"]
    df["ema20"] = close.ewm(span=EMA_FAST, adjust=False).mean()
    df["ema50"] = close.ewm(span=EMA_SLOW, adjust=False).mean()

    return df


@dataclass
class ScalpSignal:
    side: str
    stop_price: float
    tp_price: float
    size_pct: float
    reason: str
    # 진입가 기준 % 손절/익절 비율로 줄 경우 (백테스트에서 체결가 기준 재계산)
    stop_pct: Optional[float] = None
    rr_ratio: Optional[float] = None


def signal_at_bar(
    i: int,
    row: pd.Series,
    prev_row: Optional[pd.Series],
    df: pd.DataFrame,
    in_position: bool,
) -> Optional[ScalpSignal]:
    """
    EMA20 > EMA50(정배열)이고, 해당 봉에서 저가가 EMA50에 닿으면 롱 신호.
    손절 0.45%, 익절 1:1.5 → 체결가 확정 후 백테스트에서 stop/tp 계산하므로 stop_pct, rr_ratio 반환.
    """
    if in_position or i < EMA_SLOW:
        return None

    low = float(row["low"])
    high = float(row["high"])
    close = float(row["close"])
    ema20 = row.get("ema20")
    ema50 = row.get("ema50")

    if ema20 is None or ema50 is None or pd.isna(ema20) or pd.isna(ema50):
        return None

    ema20_val = float(ema20)
    ema50_val = float(ema50)
    if ema50_val <= 0:
        return None

    # 정배열: EMA20 > EMA50 (상승 추세). 강한 추세만: EMA20 >= EMA50*(1 + TREND_MIN_SPREAD)
    if ema20_val <= ema50_val * (1.0 + TREND_MIN_SPREAD):
        return None

    # EMA50 터치: 저가가 EMA50 또는 그 아래 약간(허용오차)까지 닿음
    ema50_hi = ema50_val * (1.0 + TOUCH_TOLERANCE)
    ema50_lo = ema50_val * (1.0 - TOUCH_TOLERANCE)
    if low > ema50_hi:
        return None
    if low < ema50_lo:
        return None  # 너무 깊이 꺾이면 제외 (선 선택적 터치만)

    # 반등 확인: 양봉(종가 > 시가)으로 EMA50 위 마감 → 꼬리만 닿고 회복
    open_ = float(row["open"])
    if close <= open_ or close <= ema50_val:
        return None

    # 직전 봉은 EMA50 위에 있었어야 함 (이번 봉에서 첫 터치 구간만)
    if prev_row is not None:
        prev_low = float(prev_row["low"])
        prev_ema50 = float(prev_row.get("ema50", 0) or 0)
        if prev_ema50 > 0 and prev_low <= prev_ema50 * (1.0 + TOUCH_TOLERANCE):
            return None

    # 롱만. 손절/익절은 체결가 기준이므로 백테스트에서 계산
    return ScalpSignal(
        side="LONG",
        stop_price=0.0,
        tp_price=0.0,
        size_pct=SIZE_PCT,
        reason="ema50_touch",
        stop_pct=STOP_PCT,
        rr_ratio=RR_RATIO,
    )
