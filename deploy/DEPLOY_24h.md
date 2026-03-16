# AWS 서버에서 24시간 봇 실행 가이드

GitHub에 푸시한 뒤, SSH로 서버 접속한 상태에서 아래 순서대로 진행하세요.

---

## 1단계: 저장소 클론 (최초 1회)

```bash
cd ~
git clone https://github.com/본인계정/AutoTrading.git
cd AutoTrading
```

> 이미 폴더가 있으면: `cd ~/AutoTrading` 후 `git pull` 로 최신 코드만 받으면 됩니다.

---

## 2단계: 서버 설정 스크립트 실행

```bash
chmod +x deploy/setup_server.sh
./deploy/setup_server.sh
```

- Python3, venv, `requirements.txt` 의존성 설치
- `.env` 없으면 안내 후 종료됨 → 3단계에서 `.env` 만들고 다시 실행

---

## 3단계: .env 파일 생성

```bash
nano .env
```

아래 내용을 넣고, `your_key` / `your_secret` 를 실제 바이낸스 API 키로 바꾸세요.

```env
BINANCE_API_KEY=your_key
BINANCE_API_SECRET=your_secret
BINANCE_TESTNET=true
```

- **테스트넷**: `BINANCE_TESTNET=true` 유지 (실거래 전 반드시 테스트넷으로 검증 권장)
- **실거래**: 검증 후 `BINANCE_TESTNET=false` 로 변경

저장: `Ctrl+O` → Enter → `Ctrl+X`

`.env` 만든 뒤 다시 설정 스크립트 실행:

```bash
./deploy/setup_server.sh
```

---

## 4단계: 봇 서비스 시작

```bash
sudo systemctl start autotrading
```

상태 확인:

```bash
sudo systemctl status autotrading
```

`active (running)` 이면 정상입니다.

---

## 5단계: 재부팅 후에도 자동 실행 (선택)

서버가 재부팅돼도 봇이 자동으로 켜지게 하려면:

```bash
sudo systemctl enable autotrading
```

---

## 자주 쓰는 명령어

| 명령어 | 설명 |
|--------|------|
| `sudo systemctl status autotrading` | 서비스 상태 확인 |
| `tail -f logs/autotrading.log` | 로그 실시간 보기 (종료: Ctrl+C) |
| `sudo systemctl stop autotrading` | 봇 중지 |
| `sudo systemctl start autotrading` | 봇 다시 시작 |
| `sudo systemctl restart autotrading` | 봇 재시작 (코드/설정 변경 후) |

---

## 코드/설정 수정 후 반영

```bash
cd ~/AutoTrading
git pull
sudo systemctl restart autotrading
```

---

## 문제 발생 시

1. **의존성 오류**: `./deploy/setup_server.sh` 다시 실행
2. **API 연결 실패**: `.env` 의 키/시크릿·테스트넷 여부 확인
3. **권한 오류**: 서비스는 `User=ubuntu` 로 동작하므로 `~/AutoTrading` 과 `~/AutoTrading/logs` 가 ubuntu 소유인지 확인  
   - `ls -la ~/AutoTrading`
   - 필요 시: `sudo chown -R ubuntu:ubuntu ~/AutoTrading`
