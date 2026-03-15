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
LEVERAGE = 7
INTERVAL_1H = "1h"
# 바이낸스 선물 마진 모드. CROSSED=전체 잔고 공유, ISOLATED=포지션별 마진(청산 시 해당 포지션만).
MARGIN_TYPE = "CROSSED"  # "CROSSED" | "ISOLATED"
# 바이낸스 선물 최소 주문 명목가(USDT). 미만이면 주문 거부됨.
MIN_ORDER_NOTIONAL_USDT = 5.0

# --- 포지션 비중 (5년 백테스트 12% 수익률 구간 설정) ---
FIRST_ENTRY_PCT = 0.12   # 첫 진입: 자금의 12%
SECOND_ENTRY_PCT = 0.32  # 55% 눌림 시 추가: 자금의 32%
PULLBACK_20 = 0.2       # 1차 진입: 고가에서 25% 눌림/반등
PULLBACK_50 = 0.55       # 2차 진입: 55% 눌림/반등

# --- 일봉 눌림 진입 (긴 상승 추세 도중 전일이 눌렸을 때) ---
# 4h·일봉 추세 상승일 때, 전일 고·저 대비 25% 눌림 구간 터치 시 롱 1회 진입. 장악형 대신 추세+일봉 눌림만 사용 가능.
DAILY_PULLBACK_LONG_ENABLED = False  # True: 상승 추세 중 전일 범위 25% 눌림 구간 터치 시 진입 (추가 경로)
DAILY_PULLBACK_LONG_ONLY = False     # True: 위만 사용(장악형 진입 비활성화). False: 장악형 진입 + 일봉 눌림 진입 병행
DAILY_PULLBACK_LEVEL = 0.25         # 전일 고가 대비 눌림 비율. 0.25 = 고가 - 25%(고가-저가) 수준 터치 시
DAILY_PULLBACK_SIZE_PCT = 0.12      # 진입 비중 (FIRST_ENTRY_PCT와 동일 권장)
DAILY_PULLBACK_STOP_BELOW_PCT = 0.002  # 손절: 전일 저가 대비 이만큼 아래 (0.2%)

# --- 손절/익절 ---
TP_RR_RATIO = 3.0        # 손익비 1:2.5에서 1차 익절 (이득 구간 확대)
TP_FIRST_HALF = 0.4      # 1차 익절 시 포지션의 40% (잔여 60%는 연말 확정까지 유지)
# 1차 익절을 지정가로 하면 메이커 수수료만 적용 → 수수료·슬리피지 절감. True 권장.
TP_FIRST_LIMIT_ORDER = True  # True: 1차 익절가에 지정가 주문 (메이커). False: 시장가(테이커+슬리피지).
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
TREND_4H_MIN_PCT_ABOVE_EMA = 0.005  # 0.5%. 롱: 종가>=EMA*(1+이값)

# --- 일일 종료 시 청산 (EOD exit): 하루가 끝나면 포지션 청산 (익절/손실 확정) ---
EOD_EXIT_ENABLED = False  # True: 매일 첫 봉에 전일 종가로 청산. 수익일만 하려면 EOD_EXIT_PROFIT_ONLY=True
EOD_EXIT_PROFIT_ONLY = True  # True: 수익 나는 날만 EOD 청산(익절), 손실 시에는 보유.

# --- 이상적 수익 확정: 고점 대비 되돌림(give-back) 기준 (퀀트 표준, 시간 무관) ---
# 월별 데이터 분석: "거래 0 + 큰 마이너스"(고점 미확정) 구간 완화를 위해 give-back/월별 확정을 테스트했으나,
# 이 백테스트 데이터에서는 연말 확정(year)이 최종 수익률이 가장 높게 나옴. 필요 시 True + ACTIVATE/GIVEBACK 조정 후 비교.
PROFIT_LOCK_BY_GIVEBACK = False
PROFIT_GIVEBACK_ACTIVATE_PCT = 5.0   # give-back 쓸 때: 미실현 수익 이만큼(%) 나면 고점 추적
PROFIT_GIVEBACK_PCT = 4.0   # give-back 쓸 때: 고점에서 이만큼(%) 되돌리면 청산

# --- 주기별 수익 확정: 해당 구간 마감 시 수익 나 있으면 종가로 청산 (year/month/week/day 중 택1) ---
# run_backtest.py --compare-periods 로 연·월·주·일 넷 다 테스트 후 베스트로 자동 설정 가능
EOY_CLOSE_IF_PROFIT = True   # True: 아래 주기에 따라 수익 확정 사용
# run_backtest.py --compare-periods 로 연/월/주/일 비교 가능. 이 데이터에선 "year"가 최종 수익률 가장 높게 나옴.
PROFIT_LOCK_PERIOD = "year"  # "year" | "month" | "week" | "day" — 연/월/주/일 마감 시 수익이면 확정

# --- 좋은 타이밍 익절: 반대 장악형 발생 시 청산 (시간이 아닌 신호 기반) ---
# 롱 보유 중 하락 장악형 발생 → 해당 봉 종가로 청산
EXIT_ON_OPPOSITE_ENGULF = True

# --- 4h 윗꼬리 익절: 마감 4h 봉 윗꼬리 > N*몸통이면, 다음 봉에서 해당 4h 봉 저가 대비 20% 반등 시 익절 ---
TP_4H_WICK_EXIT_ENABLED = False
TP_4H_UPPER_WICK_TO_BODY_RATIO = 4.0
TP_4H_WICK_BOUNCE_PCT = 0.2

# --- 포지션 보유 중 추가 진입 (대상승장 수익 확대, 선택 사용) ---
# 1) 같은 방향 장악형이 다시 나온 뒤 눌림 시 7% 추가. 대강세에서 유리할 수 있으나 거래 수·손절 증가 가능 → run_backtest.py --compare-4h-wick-addon 으로 비교 후 결정.
ADD_ON_ENTRY_ENABLED = False  # True: 포지션 보유 중 동일 방향 장악형 후 20% 눌림 시 7% 추가
ADD_ON_ENTRY_PCT = 0.07  # 추가 진입 비중 (자금 대비)
ADD_ON_PULLBACK_PCT = 0.2  # 추가 진입 눌림 수준 (1차와 동일 20%)

# 2) 대상승장 4h 이평 눌림 추가: 4h가 EMA 위에 있을 때 가격이 EMA 근처로 눌리면 소량 추가 (선택, 보수적 설정 권장)
TREND_ADD_ON_EMA_ENABLED = False  # True 시: 롱 보유 중 강한 상승(4h>EMA 2%)일 때 EMA 터치 시 5% 추가. 실험적.
TREND_ADD_STRONG_PCT = 0.02       # 4h 종가 >= EMA*(1+이값) 일 때만 (2% 이상 위 = 강한 상승)
TREND_ADD_EMA_TOUCH_PCT = 0.005  # 저가가 EMA*(1+이값) 이하로 눌렸을 때 진입 (0.5% 이내 = EMA 터치)
TREND_ADD_SIZE_PCT = 0.05        # 추가 진입 비중 (5%)
TREND_ADD_STOP_BELOW_EMA_PCT = 0.003  # 손절: 4h EMA 대비 이만큼 아래 (0.3%)
TREND_ADD_COOLDOWN_BARS = 48     # 동일 포지션에서 N봉(48=2일)에 한 번만 추가

# --- 일봉 추세 필터 (하락장에서 롱 진입 억제) ---
# 일봉 종가 < 일봉 EMA → 롱 차단
DAILY_TREND_FILTER = True
DAILY_EMA_PERIOD = 20
DAILY_EMA_50_PERIOD = 50  # 하락장 저항 판단용

# --- 하락장: 일봉 EMA 20/50에 윗꼬리 저항 반복 시 롱 차단 ---
BEAR_MARKET_RESISTANCE_ENABLED = True
BEAR_MARKET_LOOKBACK_DAYS = 3
BEAR_MARKET_MIN_DAYS_WITH_WICK = 2
BEAR_MARKET_EMA_NEAR_PCT = 0.005

# --- 하락장: 데스 크로스 시 롱 차단 (오탐 많아 비활성화) ---
BEAR_REGIME_DEATH_CROSS_ENABLED = False
BEAR_REGIME_DEATH_CROSS_MIN_DAYS = 5

# --- 하락장: 롱만 더 엄격히 ---
BEAR_MARKET_STRICT_LONG_FILTER = True
BEAR_MARKET_STRICT_LONG_DAYS = 5
BEAR_MARKET_STRICT_LONG_PCT = 0.012

# --- 변동성 필터 (고변동 구간 진입 억제 → 하락장·폭락 시 손실 축소) ---
VOLATILITY_FILTER_ENABLED = False  # True: ATR이 최근 N봉 백분위 상위일 때 신규 진입 스킵 또는 비중 축소
VOL_ATR_LOOKBACK = 168  # ATR 백분위 계산 구간 (1h 기준 168 = 1주)
VOL_ATR_PERCENTILE_SKIP = 92.0  # ATR이 이 백분위 이상이면 신규 진입 스킵 (0=비활성)
VOL_ATR_PERCENTILE_REDUCE = 85.0  # 이 백분위 이상이면 진입 비중을 VOL_REDUCE_SIZE_PCT 배로
VOL_REDUCE_SIZE_PCT = 0.6  # 고변동 시 적용 비중 배율 (0.6 = 60%)

# --- 수수료·슬리피지 (백테스트/실거래 공통) ---
# 바이낸스 선물: 메이커 ~0.02%, 테이커 ~0.04%, 왕복 기준
FEE_MAKER = 0.0002
FEE_TAKER = 0.0004
# 진입: 지정가(메이커) 가정 → 메이커 수수료만 (슬리피지 없음)
FEE_ENTRY = FEE_MAKER
# 청산: 시장가(테이커) 가정 → 테이커 + 슬리피지. 1차 익절은 TP_FIRST_LIMIT_ORDER=True면 지정가(메이커) 사용.
SLIPPAGE_BPS = 15        # 0.15% 슬리피지 (보수적 가정)
# 백테스트 청산 시 가격 슬리피지 (롱 매도 시 불리 반영). 1차 익절 지정가 시에는 적용 안 함.
EXIT_SLIPPAGE_BPS = 15
FEE_EFFECTIVE = FEE_TAKER + (SLIPPAGE_BPS / 10000)

# --- 백테스트 (실전 근사) ---
# 오류 방지: 미래 참조 없음(봉 단위 순차 처리), 동일 봉 내 손절 우선 검사, 청산가/자산하한 시뮬레이션.
BACKTEST_YEARS = 5
# True: 진입 시 해당 봉 종가/중간가로 체결 가정 (현실적). False: 봉 내 최저/최고 근처 체결 가정 (낙관적).
CONSERVATIVE_FILL = True
# True: CONSERVATIVE_FILL일 때 해당 봉 (고가+저가)/2 로 체결 가정 (종가보다 유리한 경우 많음)
FILL_USE_MID = True
# 실전 근사: True면 진입 신호 발생 봉의 "다음 봉 시가"에 체결 (신호 확인 후 주문 → 다음 봉 체결). 슬리피지는 FILL_NEXT_BAR_SLIPPAGE_BPS 적용.
FILL_ON_NEXT_BAR_OPEN = True
FILL_NEXT_BAR_SLIPPAGE_BPS = 5  # 다음 봉 시가 체결 시 추가 슬리피지 (bps). 0이면 시가 그대로.
# 진입 체결 방식. True: 지정가 at 눌림 레벨(봉 내 터치 시에만 체결, 보수적). False: 다음 봉 시가에 체결 — 백테스트=다음 봉 open+슬리피지, 실전=시장가(다음 봉 초입 근사).
ENTRY_LIMIT_AT_LEVEL = False
# 실전: 진입 시 거래소에 손절 주문(STOP_MARKET) 등록 여부. True 권장(봇 중단 시에도 손절 실행).
LIVE_PLACE_STOP_ORDER = True
# 레버리지 백테스트 시 청산 시뮬레이션 (실거래에 가깝게).
# 1) 청산가 도달: 거래소처럼 "진입가 대비 1/레버리지 만큼 역방향"이면 청산. 손절보다 먼저 검사 (실전은 청산가 먼저 도달 가능).
BACKTEST_LIQUIDATION_PRICE_ENABLED = True
# 2) 자산 하한: 위 청산가로도 청산 안 나와도, 자산이 초기자본의 N% 이하로 떨어지면 강제 청산 (이중 안전장치).
BACKTEST_LIQUIDATION_ENABLED = True
BACKTEST_LIQUIDATION_PCT = 0.05  # 자산이 초기자본의 5% 이하가 되면 청산
# 동일 봉 내 손절/익절 순서: 실전 불가능하므로 "보수적" 가정. 손절이 익절보다 먼저 체결된 것으로 간주 (이미 엔진에서 적용).
# 과적합 방지: 파라미터 최적화 후 반드시 기간 분할(예: 3년 학습 → 1년 검증) 또는 walk-forward로 검증 권장.

# ========== 연말 확정(PROFIT_LOCK_PERIOD=year) 기반 수익 극대화 추천 ==========
# 아래 값들은 백테스트로 비교해 보며 조합하세요. 연말 확정을 유지한 채 수익을 끌어올리는 방향입니다.
#
# 1) 진입 품질 강화 → 손절 감소 → 자본 보존 → 연말 확정 시 더 큰 포지션
#    TREND_4H_MIN_PCT_ABOVE_EMA = 0.01   # 0.005 → 0.01 (1%). 하락장 반등 롱 감소
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
