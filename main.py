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
from typing import Optional

import pandas as pd
import requests
from binance.exceptions import BinanceAPIException, BinanceRequestException

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


def _load_live_state() -> tuple:
    """저장된 상태 로드: (first_entry_qty, tp_order_id, stop_order_id, stop_price). 재시작 시 복구. symbol 불일치면 (0.0, None, None, None)."""
    try:
        if not LIVE_STATE_FILE.exists():
            return 0.0, None, None, None
        raw = LIVE_STATE_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if data.get("symbol") != config.SYMBOL:
            return 0.0, None, None, None
        qty = float(data.get("first_entry_qty", 0) or 0)
        oid = data.get("tp_order_id")
        if oid is not None:
            oid = int(oid)
        stop_oid = data.get("stop_order_id")
        if stop_oid is not None:
            stop_oid = int(stop_oid)
        stop_pr = data.get("stop_price")
        if stop_pr is not None:
            stop_pr = float(stop_pr)
        return qty, oid, stop_oid, stop_pr
    except Exception as e:
        logger.debug("상태 파일 로드 실패 (무시): %s", e)
        return 0.0, None, None, None


def _save_live_state(
    first_entry_qty: float,
    tp_order_id: Optional[int] = None,
    stop_order_id: Optional[int] = None,
    stop_price: Optional[float] = None,
) -> None:
    """첫 진입 물량·TP/손절 주문 ID·손절가 저장 (진입 직후·재시작 시 복구)."""
    try:
        LIVE_STATE_DIR.mkdir(parents=True, exist_ok=True)
        data = {"symbol": config.SYMBOL, "first_entry_qty": first_entry_qty}
        if tp_order_id is not None:
            data["tp_order_id"] = tp_order_id
        if stop_order_id is not None:
            data["stop_order_id"] = stop_order_id
        if stop_price is not None:
            data["stop_price"] = stop_price
        LIVE_STATE_FILE.write_text(json.dumps(data, indent=0), encoding="utf-8")
    except Exception as e:
        logger.warning("상태 파일 저장 실패: %s", e)


def _load_first_entry_qty() -> float:
    """저장된 첫 진입 물량 로드 (하위 호환)."""
    qty, *_ = _load_live_state()
    return qty


def _save_first_entry_qty(first_entry_qty: float) -> None:
    """첫 진입 물량만 저장 (tp/stop order id·stop_price는 유지)."""
    _, oid, stop_oid, stop_pr = _load_live_state()
    _save_live_state(first_entry_qty, tp_order_id=oid, stop_order_id=stop_oid, stop_price=stop_pr)


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


def _close_qty_limit(
    client: BinanceFuturesClient, close_qty: float, price: float, reason: str = "1차 익절"
):
    """지정 수량을 지정가로 청산 (메이커 수수료 적용). 롱이면 매도 지정가, 숏이면 매수 지정가."""
    if close_qty <= 0 or not (price and price > 0):
        return
    pos_qty, is_long = _get_position_info(client)
    if pos_qty <= 0:
        return
    close_qty = round(min(close_qty, pos_qty), 3)
    if close_qty <= 0:
        return
    side = "SELL" if is_long else "BUY"
    client.create_limit_order(config.SYMBOL, side, close_qty, price, reduce_only=True)
    logger.info("[%s] 지정가 청산(메이커): %s %s @ %s", reason, side, close_qty, price)


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
    margin_type = getattr(config, "MARGIN_TYPE", "ISOLATED")
    try:
        client.set_margin_type(config.SYMBOL, margin_type)
        logger.info("마진 모드: %s", margin_type)
    except Exception as e:
        logger.warning("마진 모드 설정 실패 (이미 동일할 수 있음, 포지션 있으면 변경 불가): %s", e)

    min_notional = getattr(config, "MIN_ORDER_NOTIONAL_USDT", 5.0)
    balance = client.get_usdt_balance()
    min_balance_approx = min_notional / (config.FIRST_ENTRY_PCT * config.LEVERAGE)
    if balance < min_balance_approx and balance > 0:
        logger.warning(
            "USDT 잔고(%.2f)가 적어 1차 진입 시 주문이 거부될 수 있습니다. 권장: 약 %.0f USDT 이상.",
            balance, min_balance_approx,
        )

    state = StrategyState()
    last_bar_time = None
    consecutive_api_failures = 0
    # 백테스트와 동일: 1차 익절은 "첫 진입(OPEN_1) 물량의 40%"만 청산. 진입 직후 TP/손절 주문 ID·손절가 복구.
    first_entry_qty, tp_order_id, stop_order_id, stop_price = _load_live_state()
    pos_qty, _ = _get_position_info(client)
    if pos_qty <= 0:
        first_entry_qty = 0.0
        for oid in (tp_order_id, stop_order_id):
            if oid is not None:
                try:
                    client.cancel_order(config.SYMBOL, oid)
                except Exception:
                    pass
        tp_order_id = stop_order_id = stop_price = None
        _save_live_state(0.0, None, None, None)
        logger.debug("포지션 없음 → first_entry_qty·주문 ID 초기화")
    elif first_entry_qty > 0:
        logger.info("재시작: 저장된 첫 진입 물량 복구 first_entry_qty=%.3f", first_entry_qty)

    while True:
        try:
            pos_qty, is_long = _get_position_info(client)
            # 포지션 없으면 저장된 first_entry_qty·TP/손절 주문 무효 (수동 청산 등)
            if pos_qty <= 0 and (first_entry_qty > 0 or tp_order_id is not None or stop_order_id is not None):
                first_entry_qty = 0.0
                for oid in (tp_order_id, stop_order_id):
                    if oid is not None:
                        try:
                            client.cancel_order(config.SYMBOL, oid)
                        except Exception:
                            pass
                tp_order_id = stop_order_id = stop_price = None
                _save_live_state(0.0, None, None, None)
                logger.debug("포지션 없음 감지 → first_entry_qty·주문 ID 초기화")

            # 미리 걸어둔 1차 익절 지정가 주문 체결 여부 확인 (백테스트와 동일: 같은 가격·비율로만 체결)
            if tp_order_id is not None:
                try:
                    order = client.get_order(config.SYMBOL, tp_order_id)
                    status = (order.get("status") or "").upper()
                    if status == "FILLED":
                        pct = getattr(config, "TP_FIRST_HALF", 0.4)
                        first_entry_qty = round(first_entry_qty * (1.0 - pct), 3)
                        tp_order_id = None
                        _save_live_state(first_entry_qty, None, stop_order_id, stop_price)
                        logger.info("1차 익절 지정가 체결 확인 → first_entry_qty=%.3f", first_entry_qty)
                except Exception as e:
                    logger.debug("TP 주문 조회 실패 (다음 루프에서 재시도): %s", e)

            # 실전 정렬: 포지션 보유 시 거래소 손절 주문(STOP_MARKET) 등록 — 봇 중단 시에도 손절 실행
            place_stop = getattr(config, "LIVE_PLACE_STOP_ORDER", True)
            if place_stop and pos_qty > 0 and stop_price is not None and stop_price > 0:
                try:
                    open_orders = client.get_open_orders(config.SYMBOL)
                    stop_orders = [o for o in open_orders if (o.get("type") or "").upper() == "STOP_MARKET" and o.get("reduceOnly")]
                    if stop_order_id is not None:
                        # 이미 체결됐거나 수량이 바뀌었으면 취소 후 재등록
                        try:
                            o = client.get_order(config.SYMBOL, stop_order_id)
                            if (o.get("status") or "").upper() in ("FILLED", "CANCELED", "EXPIRED"):
                                stop_order_id = None
                            elif abs(float(o.get("origQty", 0)) - pos_qty) > 1e-6:
                                client.cancel_order(config.SYMBOL, stop_order_id)
                                stop_order_id = None
                        except Exception:
                            stop_order_id = None
                    if stop_order_id is None:
                        if stop_orders:
                            stop_order_id = int(stop_orders[0].get("orderId", 0))
                            if stop_order_id:
                                _save_live_state(first_entry_qty, tp_order_id, stop_order_id, stop_price)
                        else:
                            side = "SELL" if is_long else "BUY"
                            res = client.create_stop_market_order(config.SYMBOL, side, round(pos_qty, 3), stop_price, close_position=False)
                            stop_order_id = int(res.get("orderId", 0))
                            if stop_order_id:
                                _save_live_state(first_entry_qty, tp_order_id, stop_order_id, stop_price)
                                logger.info("손절 주문 등록(STOP_MARKET): %s %s @ %s orderId=%s", side, pos_qty, stop_price, stop_order_id)
                except Exception as e:
                    logger.warning("손절 주문 등록/갱신 실패 (다음 루프에서 재시도): %s", e)

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
                        min_notional = getattr(config, "MIN_ORDER_NOTIONAL_USDT", 5.0)
                        if balance < 1e-6:
                            logger.warning("USDT 잔고 없음. 진입 스킵.")
                            time.sleep(60)
                            continue
                        size_pct = detail.get("size_pct", 0.1)
                        margin = balance * size_pct
                        side = "BUY" if state.positions and state.positions[-1].side == "LONG" else "SELL"
                        # ENTRY_LIMIT_AT_LEVEL=True: 지정가 at 눌림 레벨(백테스트와 동일). False: 다음 봉 시가 체결 = 실전에서는 시장가(백테스트와 동일)
                        use_limit_at_level = getattr(config, "ENTRY_LIMIT_AT_LEVEL", False)
                        if use_limit_at_level:
                            limit_price = detail.get("limit_price")
                            price = float(limit_price) if limit_price is not None and float(limit_price) > 0 else float(detail.get("entry") or row["close"])
                            if not (price and price > 0):
                                logger.warning("유효하지 않은 가격. 진입 스킵.")
                                time.sleep(60)
                                continue
                            qty = (margin * config.LEVERAGE) / price
                            qty = round(qty, 3)
                            notional = qty * price
                            if qty <= 0 or notional < min_notional:
                                logger.warning(
                                    "주문 명목가 %.2f USDT < 최소 %.2f USDT. 진입 스킵. (잔고: %.2f USDT)",
                                    notional, min_notional, balance,
                                )
                                time.sleep(60)
                                continue
                            client.create_limit_order(config.SYMBOL, side, qty, price, reduce_only=False)
                            logger.info("진입(지정가 at 눌림레벨): %s %s @ %s", side, qty, price)
                        else:
                            # 백테스트: 다음 봉 시가에 체결 → 실전: 신호 확인 시점이 곧 다음 봉 초입이므로 시장가 = 다음 봉 시가 근사
                            qty = (margin * config.LEVERAGE) / float(row["close"])
                            qty = round(qty, 3)
                            notional = qty * float(row["close"])
                            if qty <= 0 or notional < min_notional:
                                logger.warning(
                                    "주문 명목가 %.2f USDT < 최소 %.2f USDT. 진입 스킵. (잔고: %.2f USDT)",
                                    notional, min_notional, balance,
                                )
                                time.sleep(60)
                                continue
                            client.create_market_order(config.SYMBOL, side, qty, reduce_only=False)
                            logger.info("진입(시장가, 다음 봉 시가 근사): %s %s", side, qty)
                        stop_price = float(detail.get("stop")) if detail.get("stop") is not None else None
                        if action == "OPEN_1":
                            first_entry_qty = qty
                            # 진입 직후 1차 익절 지정가 주문 (백테스트와 동일: 같은 익절가·같은 비율 → 메이커 수수료)
                            if getattr(config, "TP_FIRST_LIMIT_ORDER", False):
                                tp_price = detail.get("tp1") or detail.get("price")
                                if tp_price is not None and float(tp_price) > 0:
                                    pct = getattr(config, "TP_FIRST_HALF", 0.4)
                                    tp_qty = round(first_entry_qty * pct, 3)
                                    if tp_qty > 0 and tp_qty * float(tp_price) >= min_notional:
                                        try:
                                            res = client.create_limit_order(
                                                config.SYMBOL, "SELL", tp_qty, float(tp_price), reduce_only=True
                                            )
                                            tp_order_id = int(res.get("orderId", 0))
                                            if tp_order_id:
                                                _save_live_state(first_entry_qty, tp_order_id, stop_order_id, stop_price)
                                                logger.info("1차 익절 지정가 등록(메이커): SELL %s @ %s orderId=%s", tp_qty, tp_price, tp_order_id)
                                        except Exception as e:
                                            logger.warning("1차 익절 지정가 등록 실패 (다음 TP_FIRST 시 지정가/시장가로 처리): %s", e)
                                            _save_live_state(first_entry_qty, None, stop_order_id, stop_price)
                            else:
                                _save_live_state(first_entry_qty, None, stop_order_id, stop_price)
                        else:
                            _save_live_state(first_entry_qty, tp_order_id, stop_order_id, stop_price)
                    except Exception as e:
                        logger.exception("진입 주문 실패: %s", e)

                elif action == "TP_FIRST":
                    try:
                        pct = getattr(config, "TP_FIRST_HALF", 0.4)
                        min_notional = getattr(config, "MIN_ORDER_NOTIONAL_USDT", 5.0)
                        # 진입 직후 걸어둔 TP 지정가가 이미 체결됐을 수 있음 → 상태만 동기화 (백테스트와 동일 조건 유지)
                        if tp_order_id is not None:
                            try:
                                order = client.get_order(config.SYMBOL, tp_order_id)
                                status = (order.get("status") or "").upper()
                                if status == "FILLED":
                                    first_entry_qty = round(first_entry_qty * (1.0 - pct), 3)
                                    tp_order_id = None
                                    _save_live_state(first_entry_qty, None, stop_order_id, stop_price)
                                    logger.info("1차 익절 지정가 이미 체결됨 → first_entry_qty=%.3f", first_entry_qty)
                                # NEW/PARTIALLY_FILLED: 주문 대기 중이면 그대로 두고 다음 봉에 체결될 때 위 루프에서 반영
                            except Exception as e:
                                logger.debug("TP 주문 조회 실패: %s", e)
                            # tp_order_id 있으면 여기서 추가 주문 안 함 (이중 청산 방지)
                            continue
                        # tp_order_id 없음 (재시작 등): 1차 익절가에 지정가 또는 시장가로 처리
                        tp_price = (detail.get("tp1") or detail.get("price")) if detail else None
                        tp_price = float(tp_price) if tp_price is not None and float(tp_price) > 0 else float(row["close"])
                        to_close = round(first_entry_qty * pct, 3) if first_entry_qty > 0 else 0.0
                        if to_close > 0:
                            if tp_price > 0 and to_close * tp_price < min_notional:
                                _close_full_position(client, reason="TP_FIRST")
                                first_entry_qty = 0.0
                                _save_live_state(0.0, None, None, None)
                            elif getattr(config, "TP_FIRST_LIMIT_ORDER", False):
                                _close_qty_limit(client, to_close, tp_price, reason="TP_FIRST")
                                first_entry_qty = round(first_entry_qty * (1 - pct), 3)
                                _save_live_state(first_entry_qty, None, stop_order_id, stop_price)
                            else:
                                _close_qty(client, to_close, reason="TP_FIRST")
                                first_entry_qty = round(first_entry_qty * (1 - pct), 3)
                                _save_live_state(first_entry_qty, None, stop_order_id, stop_price)
                        else:
                            _close_partial_position(client, pct, reason="TP_FIRST")
                            logger.info("재시작 후 1차 익절: first_entry_qty 미복구 → 포지션의 %.0f%% 청산", pct * 100)
                    except Exception as e:
                        logger.exception("1차 익절 주문 실패: %s", e)

                elif action in EXIT_FULL_ACTIONS:
                    try:
                        for oid in (tp_order_id, stop_order_id):
                            if oid is not None:
                                try:
                                    client.cancel_order(config.SYMBOL, oid)
                                except Exception:
                                    pass
                        tp_order_id = stop_order_id = None
                        stop_price = None
                        _close_full_position(client, reason=action)
                        first_entry_qty = 0.0
                        _save_live_state(0.0, None, None, None)
                    except Exception as e:
                        logger.exception("청산 주문 실패 (%s): %s", action, e)

            consecutive_api_failures = 0
            time.sleep(60)
        except KeyboardInterrupt:
            logger.info("봇 종료")
            break
        except (requests.exceptions.RequestException, BinanceRequestException, BinanceAPIException) as e:
            # 네트워크/API 일시 장애: 스택트레이스 로그 스팸을 줄이고, 점진적 대기 후 재시도
            consecutive_api_failures += 1
            sleep_s = min(300, 10 * consecutive_api_failures)
            logger.warning("네트워크/API 오류: %s (연속 %d회) → %d초 후 재시도", e, consecutive_api_failures, sleep_s)
            time.sleep(sleep_s)
        except Exception as e:
            logger.exception("루프 오류: %s", e)
            time.sleep(60)


if __name__ == "__main__":
    run_live_bot()
