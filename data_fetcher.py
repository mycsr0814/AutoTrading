# -*- coding: utf-8 -*-
"""과거 1시간봉 데이터 수집 (백테스트용). 미래 참조 방지를 위해 봉 단위로만 사용."""
import time
import logging
from pathlib import Path
from typing import Optional, List
import pandas as pd

import config
from exchange import BinanceFuturesClient

logger = logging.getLogger(__name__)

# 바이낸스 1봉당 1회 최대 1000개
KLINES_LIMIT = 1000
# N년 = N * 365 * 24 (시간 봉 수)
HOURS_5Y = 5 * 365 * 24
HOURS_10Y = 10 * 365 * 24


def _klines_to_df(klines: List) -> pd.DataFrame:
    """바이낸스 klines 리스트를 open/high/low/close DataFrame으로."""
    if not klines:
        return pd.DataFrame(columns=["open_time", "open", "high", "low", "close", "close_time"])
    df = pd.DataFrame(
        klines,
        columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore",
        ],
    )
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["open_time"] = pd.to_numeric(df["open_time"], errors="coerce")
    df["close_time"] = pd.to_numeric(df["close_time"], errors="coerce")
    return df[["open_time", "open", "high", "low", "close", "close_time"]]


def _fetch_historical(
    symbol: str,
    interval: str,
    bars: int,
    end_time_ms: Optional[int] = None,
    client: Optional[BinanceFuturesClient] = None,
) -> pd.DataFrame:
    """
    공통 kline 수집 함수.
    - interval: "1h", "15m" 등
    - bars: 가져올 봉 개수 (예: 5년치 1h = HOURS_5Y)
    """
    if client is None:
        try:
            client = BinanceFuturesClient()
        except Exception:
            logger.warning("API 키 없음: _fetch_historical 스킵. 캐시 파일 사용 권장.")
            return pd.DataFrame()

    all_dfs: List[pd.DataFrame] = []
    end = end_time_ms
    requested = 0

    while requested < bars:
        try:
            klines = client.get_klines(
                symbol=symbol,
                interval=interval,
                end_time=end,
                limit=min(KLINES_LIMIT, bars - requested),
            )
        except Exception as e:
            logger.error("klines 요청 실패 (%s, %s): %s", symbol, interval, e)
            break

        if not klines:
            break

        df = _klines_to_df(klines)
        all_dfs.append(df)
        requested += len(df)
        # 다음 청크는 수집한 가장 과거 시점 이전
        end = int(df["open_time"].min()) - 1
        if len(klines) < KLINES_LIMIT:
            break
        time.sleep(0.2)

    if not all_dfs:
        return pd.DataFrame()

    out = pd.concat(all_dfs, ignore_index=True)
    out = out.drop_duplicates(subset=["open_time"]).sort_values("open_time").reset_index(drop=True)
    return out


def fetch_historical_1h(
    symbol: str = config.SYMBOL,
    hours: int = HOURS_5Y,
    end_time_ms: Optional[int] = None,
    client: Optional[BinanceFuturesClient] = None,
) -> pd.DataFrame:
    """
    과거 1시간봉 데이터 수집. end_time_ms 미지정 시 현재 시점 기준.
    한 번에 1000개씩 요청하여 병합 (미래 참조 없음).
    """
    return _fetch_historical(
        symbol=symbol,
        interval=config.INTERVAL_1H,
        bars=hours,
        end_time_ms=end_time_ms,
        client=client,
    )


def fetch_historical_5m(
    symbol: str = config.SYMBOL,
    bars: int = 5 * 365 * 24 * 12,  # 5년치 5m = 5*365*24*12
    end_time_ms: Optional[int] = None,
    client: Optional[BinanceFuturesClient] = None,
) -> pd.DataFrame:
    """
    과거 5분봉 데이터 수집. end_time_ms 미지정 시 현재 시점 기준.
    """
    return _fetch_historical(
        symbol=symbol,
        interval="5m",
        bars=bars,
        end_time_ms=end_time_ms,
        client=client,
    )


def fetch_historical_15m(
    symbol: str = config.SYMBOL,
    minutes: int = HOURS_5Y * 4,  # 5년치 15m = 5년 1h의 4배 봉 수
    end_time_ms: Optional[int] = None,
    client: Optional[BinanceFuturesClient] = None,
) -> pd.DataFrame:
    """
    과거 15분봉 데이터 수집. end_time_ms 미지정 시 현재 시점 기준.
    한 번에 1000개씩 요청하여 병합 (미래 참조 없음).
    """
    return _fetch_historical(
        symbol=symbol,
        interval="15m",
        bars=minutes,
        end_time_ms=end_time_ms,
        client=client,
    )


def load_or_fetch_1h(
    years: int = 5,
    cache_dir: Optional[Path] = None,
    symbol: str = config.SYMBOL,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    N년치 1시간봉 로드. cache_dir에 CSV가 있으면 로드, 없거나 force_refresh면 API로 수집 후 저장.
    years: 5 또는 10 등. 10년 = 약 87,600봉 (바이낸스 제공 한도 내).
    """
    if cache_dir is None:
        cache_dir = Path(__file__).resolve().parent / "data"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{symbol}_1h_{years}y.csv"
    hours = years * 365 * 24

    if cache_file.exists() and not force_refresh:
        try:
            df = pd.read_csv(cache_file)
            df["open_time"] = pd.to_numeric(df["open_time"], errors="coerce")
            df["close_time"] = pd.to_numeric(df["close_time"], errors="coerce")
            for c in ["open", "high", "low", "close"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            return df.sort_values("open_time").reset_index(drop=True)
        except Exception as e:
            logger.warning("캐시 로드 실패: %s", e)

    df = fetch_historical_1h(symbol=symbol, hours=hours)
    if not df.empty:
        df.to_csv(cache_file, index=False)
    return df


def load_or_fetch_5y_1h(
    cache_dir: Optional[Path] = None,
    symbol: str = config.SYMBOL,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """5년치 1시간봉 로드 (load_or_fetch_1h(5) 래퍼)."""
    return load_or_fetch_1h(years=5, cache_dir=cache_dir, symbol=symbol, force_refresh=force_refresh)


def load_or_fetch_5m(
    years: int = 5,
    cache_dir: Optional[Path] = None,
    symbol: str = config.SYMBOL,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """N년치 5분봉 로드. cache_dir에 CSV가 있으면 로드, 없거나 force_refresh면 API로 수집 후 저장."""
    if cache_dir is None:
        cache_dir = Path(__file__).resolve().parent / "data"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{symbol}_5m_{years}y.csv"
    bars = years * 365 * 24 * 12  # 1시간=12*5m

    if cache_file.exists() and not force_refresh:
        try:
            df = pd.read_csv(cache_file)
            df["open_time"] = pd.to_numeric(df["open_time"], errors="coerce")
            df["close_time"] = pd.to_numeric(df["close_time"], errors="coerce")
            for c in ["open", "high", "low", "close"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            return df.sort_values("open_time").reset_index(drop=True)
        except Exception as e:
            logger.warning("5m 캐시 로드 실패: %s", e)

    df = fetch_historical_5m(symbol=symbol, bars=bars)
    if not df.empty:
        df.to_csv(cache_file, index=False)
    return df


def load_or_fetch_15m(
    years: int = 5,
    cache_dir: Optional[Path] = None,
    symbol: str = config.SYMBOL,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    N년치 15분봉 로드. cache_dir에 CSV가 있으면 로드, 없거나 force_refresh면 API로 수집 후 저장.
    """
    if cache_dir is None:
        cache_dir = Path(__file__).resolve().parent / "data"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{symbol}_15m_{years}y.csv"
    minutes = years * 365 * 24 * 4  # 1시간=4*15m

    if cache_file.exists() and not force_refresh:
        try:
            df = pd.read_csv(cache_file)
            df["open_time"] = pd.to_numeric(df["open_time"], errors="coerce")
            df["close_time"] = pd.to_numeric(df["close_time"], errors="coerce")
            for c in ["open", "high", "low", "close"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            return df.sort_values("open_time").reset_index(drop=True)
        except Exception as e:
            logger.warning("15m 캐시 로드 실패: %s", e)

    df = fetch_historical_15m(symbol=symbol, minutes=minutes)
    if not df.empty:
        df.to_csv(cache_file, index=False)
    return df
