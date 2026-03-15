# AWS 서버 24시간 실행 가이드

Ubuntu 서버에서 봇을 백그라운드 서비스로 돌리는 방법입니다.

**처음 배포라면:** GitHub에 먼저 올리고 AWS에서 clone하는 **전체 단계**는 **`DEPLOY_FULL.md`**를 보세요.

---

## 1. 프로젝트를 서버로 복사

### 방법 A: Windows에서 SCP로 올리기 (로컬에 코드 있을 때)

PowerShell에서 **프로젝트 폴더가 있는 디렉터리**로 이동한 뒤:

```powershell
# AutoTrading 폴더 전체 복사 (.env 제외하고 올린 뒤 서버에서 .env 직접 만들기 권장)
scp -i "C:\Users\mycsr\amazon\popkorn009.pem" -r .\AutoTrading ubuntu@13.238.18.50:~/
```

`.env`는 보안상 서버에 직접 만드는 것을 권장합니다.

### 방법 B: Git 사용 (저장소가 있을 때)

서버에서:

```bash
cd ~
git clone https://github.com/your-username/AutoTrading.git
cd AutoTrading
```

---

## 2. 서버에 SSH 접속

```powershell
ssh -i "C:\Users\mycsr\amazon\popkorn009.pem" ubuntu@13.238.18.50
```

---

## 3. 서버에서 .env 만들기

```bash
cd ~/AutoTrading
nano .env
```

아래처럼 입력 (테스트넷 권장):

```
BINANCE_API_KEY=여기에_키
BINANCE_API_SECRET=여기에_시크릿
BINANCE_TESTNET=true
```

저장: `Ctrl+O` → Enter → `Ctrl+X`

---

## 4. 한 번만 실행: 서버 설정 스크립트

```bash
cd ~/AutoTrading
chmod +x deploy/setup_server.sh
./deploy/setup_server.sh
```

- Python3, venv, 의존성 설치
- 로그 디렉터리 생성
- systemd 서비스 등록

`.env`가 없으면 스크립트가 안내 메시지 후 종료됩니다. 3번에서 만든 뒤 다시 실행하면 됩니다.

---

## 5. 봇 시작 / 중지 / 상태

```bash
# 시작 (24시간 실행)
sudo systemctl start autotrading

# 재부팅 후에도 자동 시작
sudo systemctl enable autotrading

# 상태 확인
sudo systemctl status autotrading

# 실시간 로그
tail -f ~/AutoTrading/logs/autotrading.log

# 중지
sudo systemctl stop autotrading
```

---

## 6. 정리

| 작업           | 명령어 |
|----------------|--------|
| 시작           | `sudo systemctl start autotrading` |
| 중지           | `sudo systemctl stop autotrading` |
| 상태           | `sudo systemctl status autotrading` |
| 로그 보기      | `tail -f ~/AutoTrading/logs/autotrading.log` |
| 재부팅 시 자동 시작 | `sudo systemctl enable autotrading` |

서비스는 `Restart=always`로 설정되어 있어, 봇이 죽으면 약 30초 후 자동 재시작됩니다.  
끊김/재시작 시 `first_entry_qty`는 `data/live_state.json`에서 복구됩니다.
