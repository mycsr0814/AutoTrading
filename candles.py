# -*- coding: utf-8 -*-
"""캔들 패턴 감지 (상승/하락 장악형). 미래 참조 없이 마감된 봉만 사용."""
import pandas as pd

try:
    import config
except ImportError:
    config = None


def _body_low(open_price: float, close_price: float) -> float:
    """몸통(시가~종가)의 낮은 쪽."""
    return min(open_price, close_price)


def _body_high(open_price: float, close_price: float) -> float:
    """몸통(시가~종가)의 높은 쪽."""
    return max(open_price, close_price)


def _body_ratio_ok(prev_bl: float, prev_bh: float, curr_bl: float, curr_bh: float, min_ratio: float) -> bool:
    """현재 봉 몸통이 직전 봉 몸통의 min_ratio배 이상인지 (강한 장악만 인정)."""
    if min_ratio is None or min_ratio <= 0:
        return True
    prev_body = prev_bh - prev_bl
    curr_body = curr_bh - curr_bl
    if prev_body <= 0:
        return True
    return curr_body >= min_ratio * prev_body


def is_bullish_engulfing(prev: pd.Series, curr: pd.Series) -> bool:
    """
    상승 장악형: 직전 봉이 음봉, 현재 봉이 양봉이며
    현재 봉의 **몸통**이 직전 봉의 **몸통**만 완전히 감쌈 (꼬리/심지 제외).
    config.ENGULF_BODY_RATIO_MIN 이상이면 몸통 비율 조건 추가 (노이즈 제거).
    """
    prev_bear = prev["close"] < prev["open"]
    curr_bull = curr["close"] > curr["open"]
    prev_bl = _body_low(float(prev["open"]), float(prev["close"]))
    prev_bh = _body_high(float(prev["open"]), float(prev["close"]))
    curr_bl = _body_low(float(curr["open"]), float(curr["close"]))
    curr_bh = _body_high(float(curr["open"]), float(curr["close"]))
    curr_body_engulfs_prev = curr_bl <= prev_bl and curr_bh >= prev_bh
    min_ratio = getattr(config, "ENGULF_BODY_RATIO_MIN", None) if config else None
    strength_ok = _body_ratio_ok(prev_bl, prev_bh, curr_bl, curr_bh, min_ratio)
    return bool(prev_bear and curr_bull and curr_body_engulfs_prev and strength_ok)


def is_bearish_engulfing(prev: pd.Series, curr: pd.Series) -> bool:
    """
    하락 장악형: 직전 봉이 양봉, 현재 봉이 음봉이며
    현재 봉의 **몸통**이 직전 봉의 **몸통**만 완전히 감쌈 (꼬리/심지 제외).
    config.ENGULF_BODY_RATIO_MIN 이상이면 몸통 비율 조건 추가.
    """
    prev_bull = prev["close"] > prev["open"]
    curr_bear = curr["close"] < curr["open"]
    prev_bl = _body_low(float(prev["open"]), float(prev["close"]))
    prev_bh = _body_high(float(prev["open"]), float(prev["close"]))
    curr_bl = _body_low(float(curr["open"]), float(curr["close"]))
    curr_bh = _body_high(float(curr["open"]), float(curr["close"]))
    curr_body_engulfs_prev = curr_bh <= prev_bh and curr_bl >= prev_bl
    min_ratio = getattr(config, "ENGULF_BODY_RATIO_MIN", None) if config else None
    strength_ok = _body_ratio_ok(prev_bl, prev_bh, curr_bl, curr_bh, min_ratio)
    return bool(prev_bull and curr_bear and curr_body_engulfs_prev and strength_ok)


def ensure_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """바이낸스 klines 컬럼명을 open/high/low/close로 통일."""
    if df is None or df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "close_time"])
    d = df.copy()
    renames = {
        "Open": "open", "High": "high", "Low": "low", "Close": "close",
        "open_time": "open_time", "close_time": "close_time",
    }
    for k, v in renames.items():
        if k in d.columns and v not in d.columns:
            d = d.rename(columns={k: v})
    for col in ["open", "high", "low", "close"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return d


def add_engulfing_flags(df: pd.DataFrame) -> pd.DataFrame:
    """
    마감된 봉만 사용하여 상승/하락 장악형 플래그 추가.
    i번째 행 = i번째 봉이 마감된 시점이므로, i-1(직전), i(현재)만 사용 → 미래 참조 없음.
    """
    df = ensure_ohlcv(df).copy()
    if len(df) < 2:
        df["bull_engulf"] = False
        df["bear_engulf"] = False
        return df

    bull = []
    bear = []
    for i in range(1, len(df)):
        prev = df.iloc[i - 1]
        curr = df.iloc[i]
        bull.append(is_bullish_engulfing(prev, curr))
        bear.append(is_bearish_engulfing(prev, curr))
    df = df.iloc[1:].copy()
    df = df.reset_index(drop=True)
    df["bull_engulf"] = bull
    df["bear_engulf"] = bear
    return df


def pullback_level_from_low(low: float, high: float, pct: float) -> float:
    """봉 범위의 pct(0~1)만큼 아래에서의 가격. 0.2면 20% 눌림 수준 (롱 1차 진입)."""
    r = high - low
    return low + (1.0 - pct) * r if r > 0 else low


def pullback_level_from_high(high: float, low: float, pct: float) -> float:
    """봉 범위의 pct(0~1)만큼 위에서의 가격. 0.2면 20% 반등 수준 (숏 1차 진입). 롱과 대칭."""
    r = high - low
    return high - pct * r if r > 0 else high
