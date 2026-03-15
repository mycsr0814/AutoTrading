#!/bin/bash
# AWS Ubuntu 서버에서 실행: 프로젝트 디렉토리에서
# chmod +x deploy/setup_server.sh && ./deploy/setup_server.sh

set -e
cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"
VENV="$PROJECT_ROOT/.venv"
LOGS="$PROJECT_ROOT/logs"

echo "=== AutoTrading 서버 설정 (프로젝트: $PROJECT_ROOT) ==="

# 로그 디렉토리
mkdir -p "$LOGS"
echo "로그 디렉토리: $LOGS"

# Python3 + venv
if ! command -v python3 &>/dev/null; then
    echo "Python3 설치 중..."
    sudo apt-get update -qq
    sudo apt-get install -y python3 python3-venv python3-pip
fi

# 가상환경
if [ ! -d "$VENV" ]; then
    echo "가상환경 생성: $VENV"
    python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r requirements.txt
echo "의존성 설치 완료."

# .env 확인
if [ ! -f "$PROJECT_ROOT/.env" ]; then
    echo ""
    echo "⚠ .env 파일이 없습니다. 다음 내용으로 생성하세요:"
    echo "  BINANCE_API_KEY=your_key"
    echo "  BINANCE_API_SECRET=your_secret"
    echo "  BINANCE_TESTNET=true"
    echo ""
    echo "  nano $PROJECT_ROOT/.env"
    exit 1
fi
echo ".env 확인됨."

# systemd 서비스 설치 (경로를 현재 서버 경로로 치환)
SVC_FILE="$PROJECT_ROOT/deploy/autotrading.service"
TARGET="/etc/systemd/system/autotrading.service"
sed "s|/home/ubuntu/AutoTrading|$PROJECT_ROOT|g" "$SVC_FILE" | sudo tee "$TARGET" > /dev/null
sudo systemctl daemon-reload
echo "systemd 서비스 등록됨: autotrading.service"

echo ""
echo "=== 완료 ==="
echo "시작: sudo systemctl start autotrading"
echo "상태: sudo systemctl status autotrading"
echo "로그: tail -f $LOGS/autotrading.log"
echo "재부팅 시 자동 시작: sudo systemctl enable autotrading"
echo "중지: sudo systemctl stop autotrading"
