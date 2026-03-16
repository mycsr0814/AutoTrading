# -*- coding: utf-8 -*-
"""
1h 레짐 + 5m 단타 전략 (옵션 A).

[설계]
- 1h: EMA20, EMA50으로 레짐 분류. UP(정배열), DOWN(역배열), NEUTRAL.
- 1h → 5m: merge_asof(backward)로 각 5m 봉이 "마지막 마감된 1h" 레짐·EMA 사용 (미래 참조 없음).
- UP 레짐: 5m에서 지지(1h EMA50 밴드) 터치 + RSI 과매도 반등 → 롱만.
- DOWN 레짐: 5m에서 저항(1h EMA50 밴드) 터치 + RSI 과매수 꺾임 → 숏만.
- 손절/익절: ATR 또는 % 기반, R:R 1:2 이상. 체결가는 다음 5m 봉 시가.
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


# --- 1h 레짐 ---
REGIME_EMA_FAST = _get("REGIME_SCALP_1H_EMA_FAST", 20)
REGIME_EMA_SLOW = _get("REGIME_SCALP_1H_EMA_SLOW", 50)
REGIME_UP_MIN_PCT = _get("REGIME_SCALP_UP_MIN_PCT", 0.002)   # 종가 >= EMA50*(1+이값) → UP
REGIME_DOWN_MAX_PCT = _get("REGIME_SCALP_DOWN_MAX_PCT", 0.002)  # 종가 <= EMA50*(1-이값) → DOWN

# --- 5m 지표 ---
RSI_PERIOD = _get("REGIME_SCALP_RSI_PERIOD", 14)
RSI_OVERSOLD = _get("REGIME_SCALP_RSI_OVERSOLD", 35)
RSI_OVERBOUGHT = _get("REGIME_SCALP_RSI_OVERBOUGHT", 65)
ATR_PERIOD = _get("REGIME_SCALP_ATR_PERIOD", 14)
TOUCH_BAND_ATR = _get("REGIME_SCALP_TOUCH_BAND_ATR", 1.0)   # 지지/저항 = 1h EMA50 ± k*ATR5m

# --- 리스크 ---
STOP_ATR_MULT = _get("REGIME_SCALP_STOP_ATR_MULT", 1.0)
RR_RATIO = _get("REGIME_SCALP_RR_RATIO", 2.0)
SIZE_PCT = _get("REGIME_SCALP_SIZE_PCT", 0.08)
MAX_HOLD_BARS = _get("REGIME_SCALP_MAX_HOLD_BARS", 24)


def _add_1h_regime_to_5m(df_5m: pd.DataFrame, df_1h: pd.DataFrame) -> pd.DataFrame:
    """
    1h에서 EMA20, EMA50 계산 후 레짐(UP/DOWN/NEUTRAL) 산출.
    5m에 merge_asof(backward)로 매핑 → 각 5m 봉은 "그 시점까지 마감된 1h" 정보만 사용.
    """
    if df_1h is None or len(df_1h) < REGIME_EMA_SLOW or df_5m is None or len(df_5m) < 2:
        return df_5m

    d1 = df_1h.copy()
    d1["_dt"] = pd.to_datetime(d1["open_time"], unit="ms")
    d1["close"] = pd.to_numeric(d1["close"], errors="coerce")
    d1["ema_fast"] = d1["close"].ewm(span=REGIME_EMA_FAST, adjust=False).mean()
    d1["ema_slow"] = d1["close"].ewm(span=REGIME_EMA_SLOW, adjust=False).mean()
    # 1h 봉 마감 시점 = 시작 + 1h
    d1["_merge_key"] = d1["_dt"] + pd.Timedelta(hours=1)

    up_cond = (d1["ema_fast"] > d1["ema_slow"]) & (
        d1["close"] >= d1["ema_slow"] * (1.0 + REGIME_UP_MIN_PCT)
    )
    down_cond = (d1["ema_fast"] < d1["ema_slow"]) & (
        d1["close"] <= d1["ema_slow"] * (1.0 - REGIME_DOWN_MAX_PCT)
    )
    d1["regime"] = np.where(up_cond, "UP", np.where(down_cond, "DOWN", "NEUTRAL"))

    merge_right = d1[["_merge_key", "regime", "ema_slow"]].copy()
    merge_right = merge_right.rename(columns={"_merge_key": "_mk", "ema_slow": "ema_slow_1h"})
    merge_right = merge_right.sort_values("_mk")

    d5 = df_5m.copy()
    d5["_dt"] = pd.to_datetime(d5["open_time"], unit="ms")
    d5["_dt_ns"] = d5["_dt"].astype("datetime64[ns]")
    d5["_idx"] = np.arange(len(d5))
    left_sorted = d5.sort_values("_dt_ns")

    merged = pd.merge_asof(
        left_sorted,
        merge_right,
        left_on="_dt_ns",
        right_on="_mk",
        direction="backward",
    )
    merged = merged.sort_values("_idx")
    out = df_5m.copy()
    out["regime"] = merged["regime"].values
    out["ema_slow_1h"] = merged["ema_slow_1h"].values
    return out


def _add_rsi(df: pd.DataFrame, period: int = RSI_PERIOD) -> pd.Series:
    close = pd.to_numeric(df["close"], errors="coerce")
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(span=period, adjust=False).mean()
    avg_loss = loss.ewm(span=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


def _add_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(window=period, min_periods=1).mean()


def prepare_5m_for_signal(df_5m: pd.DataFrame, df_1h: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """5m에 1h 레짐·EMA, 5m RSI·ATR 추가. 미래 참조 없음."""
    if df_5m is None or len(df_5m) < max(RSI_PERIOD, ATR_PERIOD) + 10:
        return df_5m

    df = df_5m.copy()
    for c in ["open", "high", "low", "close"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    df["rsi"] = _add_rsi(df, RSI_PERIOD)
    df["atr"] = _add_atr(df, ATR_PERIOD)

    if df_1h is not None and len(df_1h) >= REGIME_EMA_SLOW:
        df = _add_1h_regime_to_5m(df, df_1h)
    else:
        df["regime"] = "NEUTRAL"
        df["ema_slow_1h"] = np.nan

    return df


@dataclass
class RegimeScalpSignal:
    side: str  # "LONG" | "SHORT"
    stop_price: float
    tp_price: float
    size_pct: float
    reason: str
    stop_pct: Optional[float] = None
    rr_ratio: Optional[float] = None


def signal_at_bar(
    i: int,
    row: pd.Series,
    prev_row: Optional[pd.Series],
    df: pd.DataFrame,
    in_position: bool,
) -> Optional[RegimeScalpSignal]:
    """
    UP 레짐: 지지(1h EMA50 - band) 터치 + RSI 과매도 반등 → 롱.
    DOWN 레짐: 저항(1h EMA50 + band) 터치 + RSI 과매수 꺾임 → 숏.
    손절/익절은 ATR 기반; 백테스트에서 체결가 기준 재계산용 stop_pct/rr_ratio도 반환.
    """
    if in_position or i < 1:
        return None

    regime = row.get("regime")
    if regime not in ("UP", "DOWN"):
        return None

    low = float(row["low"])
    high = float(row["high"])
    close = float(row["close"])
    open_ = float(row["open"])
    ema_slow_1h = row.get("ema_slow_1h")
    atr = row.get("atr")
    rsi = row.get("rsi")

    if pd.isna(ema_slow_1h) or pd.isna(atr) or pd.isna(rsi) or atr <= 0 or ema_slow_1h <= 0:
        return None

    ema_slow_1h = float(ema_slow_1h)
    atr_val = float(atr)
    rsi_val = float(rsi)
    band = TOUCH_BAND_ATR * atr_val

    # --- UP: 롱 at 지지 (저가가 1h EMA50 - band 근처 터치, 양봉 반등, RSI 과매도 후 상승) ---
    if regime == "UP":
        support_lo = ema_slow_1h - band
        support_hi = ema_slow_1h + band * 0.5
        if low > support_hi or low < support_lo:
            return None
        if close <= open_ or close <= ema_slow_1h:
            return None
        if prev_row is not None:
            prev_rsi = prev_row.get("rsi")
            if prev_rsi is not None and not np.isnan(prev_rsi):
                if rsi_val <= float(prev_rsi):
                    return None  # RSI 상승(반등) 필요
        if rsi_val > 55:
            return None  # 과매도~중립 구간 (RSI 35~55)

        stop_dist = STOP_ATR_MULT * atr_val
        stop_price = low - stop_dist
        if stop_price >= close or stop_price <= 0:
            return None
        risk = close - stop_price
        tp_price = close + RR_RATIO * risk
        return RegimeScalpSignal(
            side="LONG",
            stop_price=stop_price,
            tp_price=tp_price,
            size_pct=SIZE_PCT,
            reason="support_reversal",
            stop_pct=risk / close if close > 0 else None,
            rr_ratio=RR_RATIO,
        )

    # --- DOWN: 숏 at 저항 (고가가 1h EMA50 + band 근처 터치, 음봉, RSI 과매수 후 하락) ---
    if regime == "DOWN":
        resistance_lo = ema_slow_1h - band * 0.5
        resistance_hi = ema_slow_1h + band
        if high < resistance_lo or high > resistance_hi:
            return None
        if close >= open_ or close >= ema_slow_1h:
            return None
        if prev_row is not None:
            prev_rsi = prev_row.get("rsi")
            if prev_rsi is not None and not np.isnan(prev_rsi):
                if rsi_val >= float(prev_rsi):
                    return None  # RSI 하락 필요
        if rsi_val < 45:
            return None  # 과매수~중립 구간 (RSI 45~65)

        stop_dist = STOP_ATR_MULT * atr_val
        stop_price = high + stop_dist
        if stop_price <= close or stop_price <= 0:
            return None
        risk = stop_price - close
        tp_price = close - RR_RATIO * risk
        if tp_price <= 0:
            return None
        return RegimeScalpSignal(
            side="SHORT",
            stop_price=stop_price,
            tp_price=tp_price,
            size_pct=SIZE_PCT,
            reason="resistance_reversal",
            stop_pct=risk / close if close > 0 else None,
            rr_ratio=RR_RATIO,
        )

    return None
