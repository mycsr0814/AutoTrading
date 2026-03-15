# -*- coding: utf-8 -*-
"""트레이딩 봇 설정 (API는 .env에서 로드)"""
import os
from pathlib import Path
from dotenv import load_dotenv

# .env는 프로젝트 루트에서 로드 (GitHub 업로드 시 .env 제외)
load_dotenv(Path(__file__).resolve().parent / ".env")

# --- API (반드시 .env에 설정) ---
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
# 테스트넷 사용 시 .env에 BINANCE_TESTNET=true 설정 (실전 전 반드시 테스트넷 검증 권장)
BINANCE_TESTNET = os.getenv("BINANCE_TESTNET", "false").lower() == "true"

# --- 거래 설정 ---
SYMBOL = "ETHUSDT"
LEVERAGE = 6
INTERVAL_1H = "1h"

# --- 포지션 비중 (5년 백테스트 12% 수익률 구간 설정) ---
FIRST_ENTRY_PCT = 0.12   # 첫 진입: 자금의 12%
SECOND_ENTRY_PCT = 0.32  # 55% 눌림 시 추가: 자금의 32%
PULLBACK_20 = 0.2       # 1차 진입: 고가에서 25% 눌림/반등
PULLBACK_50 = 0.55       # 2차 진입: 55% 눌림/반등

# --- 손절/익절 ---
TP_RR_RATIO = 3.0        # 손익비 1:2.5에서 1차 익절 (이득 구간 확대)
TP_FIRST_HALF = 0.4      # 1차 익절 시 포지션의 40% (잔여 60%는 연말 확정까지 유지)
# 손절: 장악형 캔들 꼬리(저가/고가)에 타이트 (버퍼 없음)

# --- 1차 익절 후 잔여 포지션 청산 방식 (추세 길게 먹기 / 좋은 타이밍 익절) ---
# "original_stop": 잔여 50%는 기존 손절가(장악형 꼬리)까지 유지
# "trend_break_4h": 잔여 50%는 4h 종가가 EMA50 이탈할 때만 청산 → 추세 끝까지 유지
# "trailing_atr": 고점 대비 ATR 만큼 되돌림 시 청산 → 시간이 아닌 가격 기준 '좋은 타이밍' 익절
REMAINDER_EXIT_MODE = "original_stop"  # trailing_atr = ATR 트레일로 좋은 타이밍 익절 가능

# trend_break_4h / trailing_atr 사용 시: 청산 후 N봉 동안 새 셋업 무시 (과도한 재진입·수수료 방지)
COOLDOWN_BARS_AFTER_REMAINDER_EXIT = 24   # trend_break_4h/ trailing_atr 쓸 때만 권장 (0=쿨다운 없음)

# --- ATR 트레일링 (REMAINDER_EXIT_MODE == "trailing_atr" 일 때만 사용) ---
ATR_PERIOD = 14
USE_TRAILING_STOP = True
TRAILING_STOP_ATR_MULT = 3.0
TRAILING_STOP_MIN_BREAKEVEN = True
TRAILING_EXIT_ON_CLOSE = True
TRAILING_ACTIVATE_AFTER_ATR = 1.0
TRAILING_USE_4H_ATR = True

# --- 장악형 품질 필터 ---
ENGULF_BODY_RATIO_MIN = 1.2   # 현재 봉 몸통 >= 직전 봉 몸통의 N배일 때만 장악형 인정 (노이즈 제거)

# --- 추세 필터 (4시간봉 EMA) ---
TREND_EMA_PERIOD = 50    # 4h 봉 EMA 기간
TREND_TIMEFRAME_HOURS = 4  # 1h → 4h 리샘플
# 4h 종가가 EMA 대비 최소 이 거리 이상일 때만 진입 (하락장 반등에서 롱 차단). 0이면 기존처럼 종가>EMA만 사용
# 연말 확정 수익 극대화: 0.01~0.015 로 올리면 하락장(예: 2022) 롱 진입 감소 → 손절 감소, 연말 확정 기회 증가
TREND_4H_MIN_PCT_ABOVE_EMA = 0.005  # 0.5%. 롱: 종가>=EMA*(1+이값), 숏: 종가<=EMA*(1-이값). 0이면 기존 동작

# --- 일일 종료 시 청산 (EOD exit): 하루가 끝나면 포지션 청산 (익절/손실 확정) ---
EOD_EXIT_ENABLED = False  # True: 매일 첫 봉에 전일 종가로 청산. 수익일만 하려면 EOD_EXIT_PROFIT_ONLY=True
EOD_EXIT_PROFIT_ONLY = True  # True: 수익 나는 날만 EOD 청산(익절), 손실 시에는 보유.

# --- 이상적 수익 확정: 고점 대비 되돌림(give-back) 기준 (퀀트 표준, 시간 무관) ---
# 포지션 고점 대비 N% 되돌리면 청산. 암호화폐는 변동성이 커서 1.5% give-back이 너무 자주 걸려 수익이 깎일 수 있음.
PROFIT_LOCK_BY_GIVEBACK = False  # True로 하면 "이상적 타이밍"이지만 이 데이터에선 연말확정(EOY)이 수익 더 좋음
PROFIT_GIVEBACK_ACTIVATE_PCT = 4.0   # give-back 쓸 때: 이만큼 수익 나면 추적 시작
PROFIT_GIVEBACK_PCT = 1.5   # give-back 쓸 때: 고점에서 이만큼 되돌리면 청산

# --- 주기별 수익 확정: 해당 구간 마감 시 수익 나 있으면 종가로 청산 (year/month/week/day 중 택1) ---
# run_backtest.py --compare-periods 로 연·월·주·일 넷 다 테스트 후 베스트로 자동 설정 가능
EOY_CLOSE_IF_PROFIT = True   # True: 아래 주기에 따라 수익 확정 사용
PROFIT_LOCK_PERIOD = "year"  # "year" | "month" | "week" | "day" — 연/월/주/일 마감 시 수익이면 확정

# --- 좋은 타이밍 익절: 반대 장악형 발생 시 청산 (시간이 아닌 신호 기반) ---
# 롱 보유 중 하락 장악형 발생 → 해당 봉 종가로 청산. 숏 보유 중 상승 장악형 발생 → 동일.
# 연말 확정 극대화: False 로 두면 반대 장악형에 안 걸리고 연말까지 보유 → EOY 확정 횟수·규모 증가 가능 (백테스트로 비교 권장)
EXIT_ON_OPPOSITE_ENGULF = True  # True: 반대 방향 장악형 나오면 잔여(또는 전량) 해당 봉 종가로 익절

# --- 일봉 추세 필터 (2022년 같은 하락장에서 롱 진입 억제) ---
# 4h만 쓰면 반등 시 롱이 잡혀 손절이 반복됨. 일봉 종가 < 일봉 EMA → 롱 차단, 일봉 종가 > 일봉 EMA → 숏 차단.
# 2022를 더 완화하려면 TREND_4H_MIN_PCT_ABOVE_EMA 를 0.01~0.02 로 올려 보세요 (전체 수익률은 줄어들 수 있음).
DAILY_TREND_FILTER = True
DAILY_EMA_PERIOD = 20    # 일봉 EMA 기간
DAILY_EMA_50_PERIOD = 50  # 일봉 EMA 50 (하락장 저항 판단용)

# --- 하락장 최적화: 일봉 EMA 20/50에 윗꼬리 저항이 반복되면 숏만 진입 ---
# 일봉상 EMA 20·50일선에 윗꼬리가 계속 저항받으면 하락 추세로 간주 → 롱 차단, 숏만 허용.
BEAR_MARKET_RESISTANCE_ENABLED = True
BEAR_MARKET_LOOKBACK_DAYS = 3      # 최근 N일 중
BEAR_MARKET_MIN_DAYS_WITH_WICK = 2  # M일 이상 윗꼬리+EMA 저항이면 숏만
BEAR_MARKET_EMA_NEAR_PCT = 0.005   # 고가가 EMA 대비 이 비율 이내면 '저항 터치'로 간주 (0.5%)

# --- 하락장 수익화: 일봉 데스 크로스 → 롱 차단은 상승장 조정에서 오탐 많아 비활성화 ---
BEAR_REGIME_DEATH_CROSS_ENABLED = False
BEAR_REGIME_DEATH_CROSS_MIN_DAYS = 5
BEAR_REGIME_SHORT_PULLBACK_PCT = 0  # 0 = 기본 25% 반등에 숏 1차 진입

# --- 하락장 대응: 롱만 더 엄격히 (숏 로직 변경 없음) ---
# 일봉 종가가 EMA50 아래로 N일 연속일 때 = 명확한 하락장. 이때는 롱 허용을 더 강하게: 4h 종가가 EMA 대비 STRICT_LONG_PCT 이상일 때만 롱.
# → 하락장 반등 롱 감소, 상승장은 영향 적음.
BEAR_MARKET_STRICT_LONG_FILTER = True
BEAR_MARKET_STRICT_LONG_DAYS = 5    # 연속 N일 종가 < EMA50 이면 '하락장'으로 간주
BEAR_MARKET_STRICT_LONG_PCT = 0.012 # 하락장일 때 롱 허용: 4h 종가 >= EMA*(1+1.2%). 기본 0.5%보다 강하게
# 대하락장(위와 동일: 종가<EMA50 연속 5일)에서 반등 시 숏을 더 일찍: 1차 숏 진입을 20% 반등에 (기본 25%). 0이면 기본값 사용.
BEAR_MARKET_STRICT_SHORT_PULLBACK_PCT = 0.20

# --- 숏 진입 방식 비교: 일봉 윗꼬리 저항 + 다음 일봉 음봉(EMA20 아래) 시 숏 ---
# True: 기존(장악형+반등) + 이 룰 추가. False: 기존만. 비교 시 False/True 각각 돌려서 수익률 비교.
# 조건: 전일 일봉에 윗꼬리 저항(윗꼬리 있고 종가<EMA20), 당일 일봉 음봉·종가<EMA20 → 다음날 첫 1h봉에 숏 진입, 손절=당일 일봉 고가.
DAILY_WICK_BEAR_SHORT_ENABLED = False  # True로 켜서 기존 대비 비교

# --- 수수료·슬리피지 (백테스트/실거래 공통) ---
# 바이낸스 선물: 메이커 ~0.02%, 테이커 ~0.04%, 왕복 기준
FEE_MAKER = 0.0002
FEE_TAKER = 0.0004
# 시장가 주문 가정 시 테이커 + 슬리피지
SLIPPAGE_BPS = 5         # 0.05% 슬리피지
FEE_EFFECTIVE = FEE_TAKER + (SLIPPAGE_BPS / 10000)

# --- 백테스트 ---
BACKTEST_YEARS = 5
# True: 진입 시 해당 봉 종가/중간가로 체결 가정 (현실적). False: 봉 내 최저/최고 근처 체결 가정 (낙관적).
CONSERVATIVE_FILL = True
# True: CONSERVATIVE_FILL일 때 해당 봉 (고가+저가)/2 로 체결 가정 (종가보다 유리한 경우 많음)
FILL_USE_MID = True

# ========== 연말 확정(PROFIT_LOCK_PERIOD=year) 기반 수익 극대화 추천 ==========
# 아래 값들은 백테스트로 비교해 보며 조합하세요. 연말 확정을 유지한 채 수익을 끌어올리는 방향입니다.
#
# 1) 진입 품질 강화 → 손절 감소 → 자본 보존 → 연말 확정 시 더 큰 포지션
#    TREND_4H_MIN_PCT_ABOVE_EMA = 0.01   # 0.005 → 0.01 (1%). 하락장 반등 롱·상승장 반등 숏 감소
#    ENGULF_BODY_RATIO_MIN = 1.35        # 1.2 → 1.35. 더 명확한 장악형만 진입
#
# 2) 반대 장악형 익절 끄기 → 연말까지 보유 비율 증가 → EOY 확정 횟수·규모 증가 가능
#    EXIT_ON_OPPOSITE_ENGULF = False
#
# 3) 1차 익절 비율 조정 (둘 중 하나만 테스트)
#    TP_FIRST_HALF = 0.4   # 잔여 60%를 연말까지 끌고 가서 EOY 확정 기회 확대
#    TP_FIRST_HALF = 0.6   # 1차에서 더 많이 확정 → 손절당할 때 손실 축소
#
# 4) 손익비 소폭 확대 (1차 익절 시 수익 확대)
#    TP_RR_RATIO = 3.0     # 2.5 → 3.0. 익절 시 수익 커짐, 도달 전 손절 가능성은 소폭 증가
#
# 5) 포지션 비중 소폭 상향 (리스크 허용 시, 좋은 해에서 수익 확대)
#    FIRST_ENTRY_PCT = 0.12   # 0.10 → 0.12
#    SECOND_ENTRY_PCT = 0.32  # 0.30 → 0.32
