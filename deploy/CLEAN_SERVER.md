# AWS 서버 홈 디렉터리 정리

서버에 있는 파일을 지우고 깨끗하게 만들 때 사용하세요.  
**`.ssh` 폴더는 지우지 않아야** 같은 PEM 키로 다시 접속할 수 있습니다.

---

## 1. SSH 접속

```powershell
ssh -i "C:\Users\mycsr\amazon\popkorn009.pem" ubuntu@13.238.18.50
```

---

## 2. 서버에서 실행할 명령어

### 현재有什么 있는지 확인

```bash
cd ~
pwd
ls -la
```

### .ssh 제외하고 전부 삭제 (한 줄)

```bash
cd ~ && for f in * .[!.]*; do [ -e "$f" ] && [ "$f" != ".ssh" ] && rm -rf "$f"; done && ls -la
```

- `*`: 일반 파일/폴더  
- `.[!.]*`: `.`으로 시작하는 숨김 파일/폴더 (`.ssh`는 아래에서 제외)  
- `.ssh`는 삭제하지 않아서, 나중에도 같은 PEM으로 접속 가능합니다.

### 또는 단계별로 삭제

```bash
cd ~
# 일반 파일/폴더만 삭제
rm -rf *
# .ssh 가 아닌 숨김만 삭제 ( .bashrc, .profile, .cache 등)
rm -rf .bash_logout .bashrc .profile .cache .sudo_as_admin_success 2>/dev/null
# 확인
ls -la
```

`.ssh`만 남기고 나머지 숨김까지 다 지우려면:

```bash
cd ~
find . -maxdepth 1 ! -name '.' ! -name '..' ! -name '.ssh' -exec rm -rf {} +
ls -la
```

---

## 3. 확인

- `ls -la` 결과에 `.` , `..` , `.ssh` 만 있으면 홈이 비워진 상태입니다.
- 이후 같은 PEM으로 다시 SSH 접속해서 AutoTrading 배포하면 됩니다.
