# -*- coding: utf-8 -*-
"""바이낸스 선물 API 래퍼: 수수료·슬리피지 반영, 재시도·오류 처리."""
import time
import logging
from typing import Optional, Dict, Any, List
from decimal import Decimal
import random

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
import requests

import config

logger = logging.getLogger(__name__)

# 재시도 설정
MAX_RETRIES = 5
RETRY_DELAY = 2
RETRY_STATUS_CODES = {418, 429, 500, 502, 503, 504}
RETRYABLE_REQUEST_EXC = (
    requests.exceptions.ReadTimeout,
    requests.exceptions.ConnectTimeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
)


def _sleep_backoff(attempt: int) -> None:
    """재시도 백오프(지수 + 지터). attempt=0부터."""
    base = RETRY_DELAY * (2 ** attempt)
    jitter = random.uniform(0, 0.3 * base)
    time.sleep(min(30.0, base + jitter))


def _retry_request(func):
    """네트워크/서버 오류 시 재시도 데코레이터."""
    def wrapper(*args, **kwargs):
        last_exc = None
        for attempt in range(MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except RETRYABLE_REQUEST_EXC as e:
                last_exc = e
                logger.warning("네트워크 타임아웃/연결 오류 재시도 %s/%s: %s", attempt + 1, MAX_RETRIES, e)
                _sleep_backoff(attempt)
                continue
            except BinanceRequestException as e:
                last_exc = e
                if hasattr(e, "status_code") and e.status_code in RETRY_STATUS_CODES:
                    logger.warning("재시도 %s/%s: %s", attempt + 1, MAX_RETRIES, e)
                    _sleep_backoff(attempt)
                    continue
                raise
            except BinanceAPIException as e:
                # -1015 Too many requests, -1003 Too many requests
                if e.code in (-1015, -1003):
                    last_exc = e
                    logger.warning("요청 제한 재시도 %s/%s", attempt + 1, MAX_RETRIES)
                    _sleep_backoff(attempt)
                    continue
                raise
        raise last_exc
    return wrapper


class BinanceFuturesClient:
    """바이낸스 USDT-M 선물 클라이언트 (수수료·슬리피지 적용)."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        testnet: Optional[bool] = None,
        fee_rate: Optional[float] = None,
        slippage_bps: Optional[float] = None,
    ):
        self._api_key = api_key or config.BINANCE_API_KEY
        self._api_secret = api_secret or config.BINANCE_API_SECRET
        self._testnet = testnet if testnet is not None else config.BINANCE_TESTNET
        self._fee = fee_rate if fee_rate is not None else config.FEE_EFFECTIVE
        self._slippage_bps = slippage_bps if slippage_bps is not None else config.SLIPPAGE_BPS
        self._client: Optional[Client] = None

    def _get_client(self) -> Client:
        if self._client is None:
            if not self._api_key or not self._api_secret:
                raise ValueError("BINANCE_API_KEY, BINANCE_API_SECRET를 .env에 설정하세요.")
            connect_timeout = getattr(config, "BINANCE_CONNECT_TIMEOUT_SEC", 3)
            read_timeout = getattr(config, "BINANCE_READ_TIMEOUT_SEC", 20)
            self._client = Client(
                self._api_key,
                self._api_secret,
                testnet=self._testnet,
                requests_params={"timeout": (connect_timeout, read_timeout)},
            )
        return self._client

    @_retry_request
    def ping(self) -> bool:
        """연결 확인."""
        c = self._get_client()
        c.futures_ping()
        return True

    @_retry_request
    def get_exchange_info(self) -> Dict[str, Any]:
        return self._get_client().futures_exchange_info()

    @_retry_request
    def set_leverage(self, symbol: str, leverage: int) -> Dict:
        return self._get_client().futures_change_leverage(symbol=symbol, leverage=leverage)

    @_retry_request
    def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> Dict:
        """마진 모드 설정. margin_type: "ISOLATED" | "CROSSED". 포지션 없을 때만 변경 가능."""
        # python-binance: futures_change_margin_type(symbol, marginType)
        return self._get_client().futures_change_margin_type(symbol=symbol, marginType=margin_type)

    def apply_slippage_buy(self, price: float, is_buy: bool = True) -> float:
        """매수 시 불리한 방향으로 슬리피지 적용 (매수면 올림)."""
        bps = self._slippage_bps / 10000.0
        return price * (1 + bps) if is_buy else price * (1 - bps)

    def apply_slippage_sell(self, price: float) -> float:
        """매도 시 불리한 방향 (내림)."""
        bps = self._slippage_bps / 10000.0
        return price * (1 - bps)

    def fee_for_notional(self, notional: float) -> float:
        """거래 금액에 대한 수수료 (포지션 진입/청산 각각 적용)."""
        return notional * self._fee

    @_retry_request
    def get_klines(
        self,
        symbol: str,
        interval: str,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 1000,
    ) -> List:
        return self._get_client().futures_klines(
            symbol=symbol,
            interval=interval,
            startTime=start_time,
            endTime=end_time,
            limit=limit,
        )

    @_retry_request
    def get_account_balance(self) -> List[Dict]:
        return self._get_client().futures_account_balance()

    @_retry_request
    def get_position_risk(self, symbol: Optional[str] = None) -> List[Dict]:
        return self._get_client().futures_position_information(symbol=symbol or config.SYMBOL)

    @_retry_request
    def create_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        reduce_only: bool = False,
    ) -> Dict:
        return self._get_client().futures_create_order(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=quantity,
            reduceOnly=reduce_only,
        )

    @_retry_request
    def create_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        reduce_only: bool = False,
        time_in_force: str = "GTC",
    ) -> Dict:
        """지정가 주문 (메이커 수수료 적용). GTC=체결될 때까지 유지."""
        return self._get_client().futures_create_order(
            symbol=symbol,
            side=side,
            type="LIMIT",
            quantity=quantity,
            price=round(price, 2),
            timeInForce=time_in_force,
            reduceOnly=reduce_only,
        )

    @_retry_request
    def create_stop_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        stop_price: float,
        close_position: bool = False,
    ) -> Dict:
        return self._get_client().futures_create_order(
            symbol=symbol,
            side=side,
            type="STOP_MARKET",
            quantity=quantity,
            stopPrice=round(stop_price, 2),
            closePosition=close_position,
        )

    @_retry_request
    def create_take_profit_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        stop_price: float,
    ) -> Dict:
        return self._get_client().futures_create_order(
            symbol=symbol,
            side=side,
            type="TAKE_PROFIT_MARKET",
            quantity=quantity,
            stopPrice=round(stop_price, 2),
        )

    @_retry_request
    def get_order(self, symbol: str, order_id: int) -> Dict:
        """주문 단건 조회 (체결 여부 확인용)."""
        return self._get_client().futures_get_order(symbol=symbol, orderId=order_id)

    @_retry_request
    def cancel_order(self, symbol: str, order_id: int) -> Dict:
        """지정가 등 주문 취소."""
        return self._get_client().futures_cancel_order(symbol=symbol, orderId=order_id)

    @_retry_request
    def get_open_orders(self, symbol: str) -> List[Dict]:
        """미체결 주문 목록."""
        return self._get_client().futures_get_open_orders(symbol=symbol)

    def get_usdt_balance(self) -> float:
        """USDT 가용 잔고."""
        for b in self.get_account_balance():
            if b.get("asset") == "USDT":
                return float(b.get("availableBalance", 0) or b.get("balance", 0))
        return 0.0
