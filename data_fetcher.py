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
# 5년 = 5 * 365 * 24
HOURS_5Y = 5 * 365 * 24


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
    if client is None:
        try:
            client = BinanceFuturesClient()
        except Exception:
            # API 키 없이 백테스트만 할 때는 빈 DataFrame 반환 가능하도록
            logger.warning("API 키 없음: fetch_historical_1h 스킵. 캐시 파일 사용 권장.")
            return pd.DataFrame()

    all_dfs: List[pd.DataFrame] = []
    end = end_time_ms
    requested = 0

    while requested < hours:
        try:
            klines = client.get_klines(
                symbol=symbol,
                interval=config.INTERVAL_1H,
                end_time=end,
                limit=min(KLINES_LIMIT, hours - requested),
            )
        except Exception as e:
            logger.error("klines 요청 실패: %s", e)
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


def load_or_fetch_5y_1h(
    cache_dir: Optional[Path] = None,
    symbol: str = config.SYMBOL,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    ​5년치 1시간봉 로드. cache_dir에 CSV가 있으면 로드, 없거나 force_refresh면 API로 수집 후 저장.
    """
    if cache_dir is None:
        cache_dir = Path(__file__).resolve().parent / "data"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{symbol}_1h_5y.csv"

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

    df = fetch_historical_1h(symbol=symbol, hours=HOURS_5Y)
    if not df.empty:
        df.to_csv(cache_file, index=False)
    return df
