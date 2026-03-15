# AutoTrading 배포 가이드 (GitHub → AWS, 단계별)

코드는 **GitHub에 올린 뒤**, AWS 서버에서 **clone**해서 쓰는 흐름입니다.  
`.env`(API 키)는 GitHub에 올리지 않고, 서버에서만 만듭니다.

---

## 흐름 요약

```
[로컬 PC]  →  GitHub 푸시  →  [GitHub]
                  ↓
[AWS 서버]  ←  git clone   ←  [GitHub]
     + 서버에서 .env 생성
     + 서비스 설치·실행
```

---

# 1단계: 로컬에서 GitHub 준비

## 1-1. Git이 설치돼 있는지 확인

PowerShell에서:

```powershell
git --version
```

`git version 2.x.x` 같이 나오면 OK. 없으면 [Git 다운로드](https://git-scm.com/download/win) 후 설치.

---

## 1-2. 프로젝트가 Git 저장소인지 확인

```powershell
cd C:\Users\mycsr\AutoTrading
git status
```

- **"fatal: not a git repository"** → 1-3으로 가서 `git init`  
- **파일 목록이 나오면** → 이미 저장소임. 1-4로.

---

## 1-3. Git 저장소로 만들기 (아직 안 했다면)

```powershell
cd C:\Users\mycsr\AutoTrading
git init
```

한 번만 하면 됩니다.

---

## 1-4. .gitignore 확인 (.env가 빠지는지)

`.gitignore`에 다음이 **반드시** 있어야 합니다 (이미 있음):

```
.env
```

이렇게 되어 있으면 `.env`는 GitHub에 올라가지 않습니다.  
그 외 `__pycache__/`, `.venv/`, `logs/` 등도 있는지 확인해 두면 좋습니다.

---

## 1-5. GitHub에서 새 저장소 만들기

1. 브라우저에서 [github.com](https://github.com) 로그인
2. 오른쪽 상단 **+** → **New repository**
3. **Repository name**: `AutoTrading` (원하는 이름)
4. **Public** 선택
5. **"Add a README file"** 등은 체크 안 해도 됨
6. **Create repository** 클릭
7. 생성된 페이지에서 **저장소 주소** 복사  
   - HTTPS: `https://github.com/내아이디/AutoTrading.git`  
   - 본인 아이디와 저장소 이름에 맞게 사용

---

## 1-6. 로컬에서 GitHub에 푸시

PowerShell에서 (저장소 주소는 본인 걸로 바꾸기):

```powershell
cd C:\Users\mycsr\AutoTrading

# 처음 한 번만: 원격 저장소 연결
git remote add origin https://github.com/내아이디/AutoTrading.git

# 파일 추가 ( .gitignore 덕분에 .env는 제외됨 )
git add .
git status
# 목록에 .env 가 없어야 함. 있으면 .gitignore 확인.

# 첫 커밋
git commit -m "Initial commit: AutoTrading bot"

# GitHub에 올리기 (main 브랜치)
git branch -M main
git push -u origin main
```

- 이미 `origin`이 있으면 `git remote add origin ...` 는 건너뛰고, `git add .` 부터 실행.
- `git push` 시 GitHub 로그인(또는 토큰) 요구되면 진행.

---

# 2단계: AWS 서버에서 코드 받기

## 2-1. SSH 접속

PowerShell:

```powershell
ssh -i "C:\Users\mycsr\amazon\popkorn009.pem" ubuntu@13.238.18.50
```

---

## 2-2. 서버에서 프로젝트 clone

서버 터미널에서 (주소는 본인 GitHub 저장소 주소로):

```bash
cd ~
git clone https://github.com/내아이디/AutoTrading.git
cd AutoTrading
ls -la
```

- `config.py`, `main.py`, `deploy/` 등이 보이면 성공.
- `.env`는 GitHub에 없으므로 여기 없어야 정상.

---

## 2-3. 서버에서 .env 만들기

API 키는 GitHub에 올리지 않고, **서버에서만** 만듭니다.

```bash
cd ~/AutoTrading
nano .env
```

아래 내용 입력 (키/시크릿은 본인 값으로):

```
BINANCE_API_KEY=여기에_본인_API키
BINANCE_API_SECRET=여기에_본인_시크릿
BINANCE_TESTNET=true
```

저장: `Ctrl+O` → Enter → `Ctrl+X`

---

## 2-4. 서버 설정 스크립트 실행 (한 번만)

```bash
cd ~/AutoTrading
chmod +x deploy/setup_server.sh
./deploy/setup_server.sh
```

- Python3, venv, 패키지 설치
- 로그 폴더 생성
- systemd 서비스 등록

`.env`가 없으면 스크립트가 안내하고 끝납니다. 2-3에서 만든 뒤 다시 실행하면 됩니다.

---

## 2-5. 봇 서비스 시작

```bash
sudo systemctl start autotrading
sudo systemctl enable autotrading
```

- `start`: 지금 바로 실행  
- `enable`: 서버 재부팅 후에도 자동 실행

---

## 2-6. 동작 확인

```bash
sudo systemctl status autotrading
tail -f ~/AutoTrading/logs/autotrading.log
```

- `status`: active (running) 이면 정상.
- `tail -f`: 로그 실시간 확인. 종료는 `Ctrl+C`.

---

# 3단계: 나중에 코드 수정했을 때

로컬에서 수정 → GitHub 푸시 → 서버에서만 pull 하면 됩니다.

**로컬 (PowerShell):**

```powershell
cd C:\Users\mycsr\AutoTrading
git add .
git commit -m "수정 내용 요약"
git push
```

**서버 (SSH 접속 후):**

```bash
cd ~/AutoTrading
git pull
sudo systemctl restart autotrading
```

- `.env`는 서버에 그대로 두면 됩니다. (Git으로 덮어쓰이지 않음)

---

# 체크리스트 요약

| 단계 | 어디서 | 할 일 |
|------|--------|--------|
| 1 | 로컬 | Git 저장소 확인/생성, .gitignore에 .env 있는지 확인 |
| 2 | GitHub | 새 저장소 생성, 주소 복사 |
| 3 | 로컬 | `git remote add origin`, `git add .`, `git commit`, `git push` |
| 4 | AWS | SSH 접속 |
| 5 | AWS | `git clone https://github.com/내아이디/AutoTrading.git` |
| 6 | AWS | `nano .env` 로 API 키·시크릿·BINANCE_TESTNET 입력 |
| 7 | AWS | `./deploy/setup_server.sh` |
| 8 | AWS | `sudo systemctl start autotrading` + `enable autotrading` |
| 9 | AWS | `tail -f ~/AutoTrading/logs/autotrading.log` 로 로그 확인 |

이 순서대로 하면 GitHub에 올린 뒤 AWS에서 clone해서 24시간 돌리는 흐름이 됩니다.
