# 바이낸스 이더리움 자동 트레이딩 봇

1시간봉 **상승/하락 장악형** 마감 후 눌림 구간에서 진입하는 선물 전략 봇입니다.

## 요구사항

- Python 3.9+
- 바이낸스 API 키 (선물 거래 권한)

## 설정

1. 가상환경 생성 및 패키지 설치:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate   # Windows
   pip install -r requirements.txt
   ```

2. API 키 설정 (GitHub 업로드 시 `.env`는 제외됨):
   - 프로젝트 루트에 `.env` 파일 생성
   - `.env.example`을 참고해 아래 내용 입력:
     ```
     BINANCE_API_KEY=your_api_key_here
     BINANCE_API_SECRET=your_api_secret_here
     ```
   - 테스트넷 사용 시: `BINANCE_TESTNET=true` 추가

## 전략 요약

- **진입**: 1시간봉이 상승 장악형(또는 하락 장악형)으로 마감된 뒤, 다음 봉 형성 중  
  - 롱: 양봉 대비 20% 눌림 시 자금의 10% 진입, 50% 눌림 시 추가 30%  
  - 숏: 동일 로직 반대
- **손절**: 장악형 봉의 최저(롱)/최고(숏) 이탈 시 전액 손절
- **익절**: 손익비 1:1.2 도달 시 50% 익절, 나머지는 추세 추종
- **레버리지**: 10배
- **수수료·슬리피지**: `config.py`에서 반영

## 실행

- **백테스트** (과거 5년 1시간봉, 미래 참조 없음):
  ```bash
  python run_backtest.py
  ```
  - 최초 실행 시 바이낸스 API로 5년치 1시간봉을 받아 `data/`에 캐시합니다. API 키가 없으면 데이터가 없어 백테스트가 중단됩니다.

- **실거래 봇**:
  ```bash
  python main.py
  ```
  - 1시간봉 마감 시점마다 신호를 확인하고 주문합니다. 네트워크/서버 오류 시 재시도 로직이 포함되어 있습니다.

## 주의사항

- 실거래 전 반드시 백테스트와 소액으로 검증하세요.
- API 키는 `.env`에만 두고 GitHub 등에 업로드하지 마세요 (`.gitignore`에 `.env` 포함됨).
