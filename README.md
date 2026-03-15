# 바이낸스 이더리움 선물 자동 트레이딩 봇

1시간봉 **상승/하락 장악형** 마감 후 눌림 구간에서 진입하는 ETHUSDT 선물 전략 봇입니다.

## 요구사항

- Python 3.9+
- 바이낸스 API 키 (선물 거래 권한)

## 프로젝트 구조

| 파일 | 설명 |
|------|------|
| `main.py` | 실거래 봇 진입점. 1시간봉 마감 시 신호 확인 후 주문 |
| `backtest.py` | 백테스트 엔진. 봉 단위 순차 처리, 수수료·슬리피지 반영 |
| `strategy.py` | 진입/손절/익절 로직 (백테스트·실전 공통) |
| `config.py` | 심볼, 레버리지, 비중, 수수료 등 설정 |
| `exchange.py` | 바이낸스 선물 API 래퍼 |
| `data_fetcher.py` | 과거 1시간봉 수집 및 캐시 |
| `candles.py` | 장악형 패턴·캔들 유틸 |
| `run_backtest.py` | 5년 백테스트 실행, 주기 비교·레버리지·진입/익절 최적화 옵션 |
| `optimize_backtest.py` | 연말 확정 고정 하에 파라미터 그리드 탐색 |

## 설정

1. 가상환경 생성 및 패키지 설치:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate   # Windows
   pip install -r requirements.txt
   ```

2. API 키 설정 (GitHub 업로드 시 `.env`는 제외됨):
   - 프로젝트 루트에 `.env` 파일 생성
   - 아래 내용 입력:
     ```
     BINANCE_API_KEY=your_api_key_here
     BINANCE_API_SECRET=your_api_secret_here
     ```
   - 테스트넷 사용 시: `BINANCE_TESTNET=true` 추가

## 전략 요약

- **진입**: 1시간봉이 상승/하락 장악형으로 마감된 뒤, 다음 봉에서 눌림/반등 시  
  - 1차: 자금의 12% (config `FIRST_ENTRY_PCT`), 2차: 55% 눌림 시 추가 32% (`SECOND_ENTRY_PCT`)
- **손절**: 장악형 봉의 최저(롱)/최고(숏) 이탈 시 전액 손절
- **익절**: 손익비 1:3 도달 시 40% 1차 익절, 잔여는 추세 추종(연말 확정 등)
- **레버리지**: `config.py`의 `LEVERAGE` (기본 6배)
- **수수료·슬리피지**: `config.py`에서 반영

## 최소 잔고 (실거래)

- 바이낸스 USDT-M 선물 **주문당 최소 명목가 5 USDT** (미만 시 API 에러 -4164).
- 1차 진입이 자금의 12% × 레버리지이므로, **약 7 USDT 이상** 잔고를 권장합니다.  
  (7 × 0.12 × 6 = 5.04 USDT ≥ 5 USDT)
- **2 USDT만 있을 때**: 2 × 0.12 × 6 = 1.44 USDT 명목가로 5 USDT 미만이 되어 **진입 주문이 거부됩니다.** 실거래 가능하려면 최소 약 7 USDT 이상이 필요합니다.
- 잔고가 부족하면 진입 시 로그 경고 후 주문을 스킵합니다.

## 실행

- **백테스트** (과거 5년 1시간봉, 미래 참조 없음):
  ```bash
  python run_backtest.py
  ```
  - 최초 실행 시 바이낸스 API로 5년치 1시간봉을 받아 `data/`에 캐시합니다. API 키가 없으면 데이터가 없어 백테스트가 중단됩니다.
  - 옵션: `--compare-periods`, `--optimize-leverage`, `--compare-short-entry`, `--optimize-entry-tp`, `--apply-best` 등 (자세한 내용은 `python run_backtest.py --help`).

- **실거래 봇**:
  ```bash
  python main.py
  ```
  - 1시간봉 마감 시점마다 신호를 확인하고 주문합니다. 네트워크/서버 오류 시 재시도 로직이 포함되어 있습니다.

## 백테스트 vs 실전 코드

- **동일**: 신호 로직은 `strategy.run_signal_on_bar` 한 곳에서 처리하며, 백테스트와 실전 모두 이 함수를 사용합니다.
- **동일**: 봉 데이터 전처리(장악형, 4h/일봉 추세, ATR)는 `backtest.prepare_1h_df_for_signal`로 통일되어 있으며, 실전은 `main.get_klines_for_live`에서 동일 함수를 호출합니다.
- **동일**: `config.py`의 진입 비중·손익비·1차 익절 비율·레버리지 등이 백테스트와 실전에 공통 적용됩니다.
- **실전만**: 거래소 잔고 조회, 최소 명목가 체크, 수량 반올림(0.001), 시장가 주문 전송, 끊김 시 `data/live_state.json`으로 1차 익절용 첫 진입 물량 복구.

자세한 백테스트 검증(선행 참조 없음, 봉 내 처리 순서 등)은 `BACKTEST_VALIDATION.md`를 참고하세요.

## 주의사항

- 실거래 전 반드시 백테스트와 테스트넷·소액으로 검증하세요.
- API 키는 `.env`에만 두고 GitHub 등에 업로드하지 마세요 (`.gitignore`에 `.env` 포함됨).
