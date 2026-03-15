# -*- coding: utf-8 -*-
"""
바이낸스 이더리움 자동 트레이딩 봇 메인.
- 1시간봉 상승/하락 장악형 마감 후 눌림 구간에서 진입.
- 1차 익절(TP_FIRST), 트레일링 스탑, 연말 확정, 반대 장악형 청산 등 전체 주문 로직 반영.
- 테스트넷 사용: .env에 BINANCE_TESTNET=true 설정.
- 끊김/재시작 시 first_entry_qty는 data/live_state.json 에서 복구.
"""
import json
import logging
import time
import sys
from pathlib import Path

import pandas as pd

import config
from exchange import BinanceFuturesClient
from backtest import prepare_1h_df_for_signal
from strategy import run_signal_on_bar, StrategyState

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# 청산 시그널: 전량 청산 (시장가)
EXIT_FULL_ACTIONS = (
    "STOP",
    "TRAILING_STOP",
    "TREND_BREAK_EXIT",
    "EOD_EXIT",
    "EOY_PROFIT_LOCK",
    "GIVEBACK_EXIT",
    "REVERSE_ENGULF_EXIT",
)

# 끊김/재시작 시 1차 익절용 첫 진입 물량 복구
LIVE_STATE_DIR = Path(__file__).resolve().parent / "data"
LIVE_STATE_FILE = LIVE_STATE_DIR / "live_state.json"


def _load_first_entry_qty() -> float:
    """저장된 첫 진입 물량 로드 (재시작 시 복구). 포지션 없으면 0으로 초기화된 상태만 유효."""
    try:
        if not LIVE_STATE_FILE.exists():
            return 0.0
        raw = LIVE_STATE_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if data.get("symbol") != config.SYMBOL:
            return 0.0
        return float(data.get("first_entry_qty", 0) or 0)
    except Exception as e:
        logger.debug("상태 파일 로드 실패 (무시): %s", e)
        return 0.0


def _save_first_entry_qty(first_entry_qty: float) -> None:
    """첫 진입 물량 저장 (끊겨도 재시작 시 복구 가능)."""
    try:
        LIVE_STATE_DIR.mkdir(parents=True, exist_ok=True)
        data = {"symbol": config.SYMBOL, "first_entry_qty": first_entry_qty}
        LIVE_STATE_FILE.write_text(json.dumps(data, indent=0), encoding="utf-8")
    except Exception as e:
        logger.warning("상태 파일 저장 실패: %s", e)


def get_klines_for_live(client: BinanceFuturesClient, limit: int = 100) -> pd.DataFrame:
    """실시간 봇용 최근 1시간봉. 백테스트와 동일한 지표(4h/일봉 추세, ATR, 장악형) 적용."""
    raw = client.get_klines(
        symbol=config.SYMBOL,
        interval=config.INTERVAL_1H,
        limit=limit,
    )
    if not raw:
        return pd.DataFrame()
    df = pd.DataFrame(
        raw,
        columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore",
        ],
    )
    for c in ["open", "high", "low", "close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open_time"] = pd.to_numeric(df["open_time"], errors="coerce")
    df["close_time"] = pd.to_numeric(df["close_time"], errors="coerce")
    df = df[["open_time", "open", "high", "low", "close", "close_time"]].copy()
    return prepare_1h_df_for_signal(df)


def _get_position_info(client: BinanceFuturesClient) -> tuple:
    """현재 포지션 (수량, 롱여부). 없으면 (0.0, True)."""
    pos_info = client.get_position_risk(config.SYMBOL)
    for p in pos_info:
        if p.get("symbol") == config.SYMBOL:
            amt = float(p.get("positionAmt", 0))
            if amt != 0:
                return abs(amt), amt > 0
    return 0.0, True


def _close_full_position(client: BinanceFuturesClient, reason: str = "전량"):
    """포지션 전량 시장가 청산."""
    qty, is_long = _get_position_info(client)
    if qty <= 0:
        logger.debug("청산할 포지션 없음 (%s)", reason)
        return
    side = "SELL" if is_long else "BUY"
    client.create_market_order(config.SYMBOL, side, round(qty, 3), reduce_only=True)
    logger.info("[%s] 청산: %s %s", reason, side, qty)


def _close_partial_position(client: BinanceFuturesClient, pct: float, reason: str = "1차 익절"):
    """포지션의 pct(0~1) 비율만 시장가 청산."""
    qty, is_long = _get_position_info(client)
    if qty <= 0 or pct <= 0 or pct >= 1:
        return
    close_qty = round(qty * pct, 3)
    if close_qty <= 0:
        return
    side = "SELL" if is_long else "BUY"
    client.create_market_order(config.SYMBOL, side, close_qty, reduce_only=True)
    logger.info("[%s] 일부 청산 %.0f%%: %s %s", reason, pct * 100, side, close_qty)


def _close_qty(client: BinanceFuturesClient, close_qty: float, reason: str):
    """지정 수량만 시장가 청산 (백테스트 1차 익절 = 첫 진입 물량의 40%와 동일)."""
    if close_qty <= 0:
        return
    pos_qty, is_long = _get_position_info(client)
    if pos_qty <= 0:
        return
    close_qty = round(min(close_qty, pos_qty), 3)
    if close_qty <= 0:
        return
    side = "SELL" if is_long else "BUY"
    client.create_market_order(config.SYMBOL, side, close_qty, reduce_only=True)
    logger.info("[%s] 청산 수량: %s %s", reason, side, close_qty)


def run_live_bot():
    """실거래 봇: 1시간봉 마감 시점에 신호 확인 후 주문 (진입·1차 익절·전량 청산)."""
    if not config.BINANCE_API_KEY or not config.BINANCE_API_SECRET:
        logger.error(".env에 BINANCE_API_KEY, BINANCE_API_SECRET를 설정하세요.")
        sys.exit(1)

    if config.BINANCE_TESTNET:
        logger.info("테스트넷 모드로 실행 (BINANCE_TESTNET=true)")
    else:
        logger.warning("실거래 모드입니다. 테스트넷 사용 시 .env에 BINANCE_TESTNET=true 로 설정하세요.")

    client = BinanceFuturesClient()
    try:
        client.ping()
    except Exception as e:
        logger.error("바이낸스 연결 실패: %s", e)
        sys.exit(1)

    try:
        client.set_leverage(config.SYMBOL, config.LEVERAGE)
    except Exception as e:
        logger.warning("레버리지 설정 실패 (이미 설정됐을 수 있음): %s", e)

    state = StrategyState()
    last_bar_time = None
    # 백테스트와 동일: 1차 익절은 "첫 진입(OPEN_1) 물량의 40%"만 청산. 끊김 시 파일에서 복구.
    first_entry_qty = _load_first_entry_qty()
    pos_qty, _ = _get_position_info(client)
    if pos_qty <= 0:
        first_entry_qty = 0.0
        _save_first_entry_qty(0.0)
        logger.debug("포지션 없음 → first_entry_qty 초기화")
    elif first_entry_qty > 0:
        logger.info("재시작: 저장된 첫 진입 물량 복구 first_entry_qty=%.3f", first_entry_qty)

    while True:
        try:
            # 포지션 없으면 저장된 first_entry_qty 무효 (수동 청산 등)
            pos_qty, _ = _get_position_info(client)
            if pos_qty <= 0 and first_entry_qty > 0:
                first_entry_qty = 0.0
                _save_first_entry_qty(0.0)
                logger.debug("포지션 없음 감지 → first_entry_qty 초기화")

            df = get_klines_for_live(client, limit=50)
            if df is None or len(df) < 3:
                logger.warning("봉 데이터 부족, 60초 후 재시도")
                time.sleep(60)
                continue

            # 마지막 봉(가장 최근 마감된 1봉) 기준으로 신호 확인
            i = len(df) - 1
            row = df.iloc[i]
            prev_row = df.iloc[i - 1] if i >= 1 else None
            current_bar_time = int(row.get("close_time", 0) or row.get("open_time", 0))

            # 이미 이 봉으로 처리했으면 스킵
            if last_bar_time == current_bar_time:
                time.sleep(60)
                continue

            state, action, detail = run_signal_on_bar(
                i, row, prev_row, df.iloc[: i + 1], state, fee_rate=config.FEE_EFFECTIVE
            )

            if action:
                logger.info("신호: %s %s", action, detail)
                last_bar_time = current_bar_time

                if action in ("OPEN_1", "OPEN_2") and detail:
                    try:
                        balance = client.get_usdt_balance()
                        size_pct = detail.get("size_pct", 0.1)
                        margin = balance * size_pct
                        qty = (margin * config.LEVERAGE) / float(row["close"])
                        qty = round(qty, 3)
                        if qty > 0:
                            side = "BUY" if state.positions and state.positions[-1].side == "LONG" else "SELL"
                            client.create_market_order(config.SYMBOL, side, qty, reduce_only=False)
                            logger.info("진입: %s %s @ ~%s", side, qty, row["close"])
                            if action == "OPEN_1":
                                first_entry_qty = qty
                                _save_first_entry_qty(first_entry_qty)
                    except Exception as e:
                        logger.exception("진입 주문 실패: %s", e)

                elif action == "TP_FIRST":
                    try:
                        pct = getattr(config, "TP_FIRST_HALF", 0.4)
                        # 백테스트와 동일: 첫 진입(OPEN_1) 물량의 pct만 청산
                        to_close = round(first_entry_qty * pct, 3) if first_entry_qty > 0 else 0.0
                        if to_close > 0:
                            _close_qty(client, to_close, reason="TP_FIRST")
                            first_entry_qty = round(first_entry_qty * (1 - pct), 3)
                            _save_first_entry_qty(first_entry_qty)
                        else:
                            _close_partial_position(client, pct, reason="TP_FIRST")
                            logger.info("재시작 후 1차 익절: first_entry_qty 미복구 → 포지션의 %.0f%% 청산", pct * 100)
                    except Exception as e:
                        logger.exception("1차 익절 주문 실패: %s", e)

                elif action in EXIT_FULL_ACTIONS:
                    try:
                        _close_full_position(client, reason=action)
                        first_entry_qty = 0.0
                        _save_first_entry_qty(0.0)
                    except Exception as e:
                        logger.exception("청산 주문 실패 (%s): %s", action, e)

            time.sleep(60)
        except KeyboardInterrupt:
            logger.info("봇 종료")
            break
        except Exception as e:
            logger.exception("루프 오류: %s", e)
            time.sleep(60)


if __name__ == "__main__":
    run_live_bot()
