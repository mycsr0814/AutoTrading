#!/bin/bash
# AWS 서버 홈 디렉터리 정리 (로그인용 .ssh는 유지)
# SSH 접속 후: chmod +x deploy/clean_server_home.sh && ./deploy/clean_server_home.sh
# 또는 아래 명령어를 서버에서 직접 실행

set -e
HOME_DIR="${HOME:-/home/ubuntu}"
cd "$HOME_DIR"

echo "=== $HOME_DIR 내용 삭제 ( .ssh 제외 ) ==="
echo "현재 내용:"
ls -la

echo ""
read -p "위 항목 중 .ssh 를 제외하고 모두 삭제할까요? [y/N] " -r
if [[ ! $REPLY =~ ^[yY]$ ]]; then
    echo "취소됨."
    exit 0
fi

# .ssh 제외하고 홈 아래 모든 파일/디렉터리 삭제
for item in "$HOME_DIR"/*; do
    [ -e "$item" ] && rm -rf "$item" && echo "삭제: $item"
done
for item in "$HOME_DIR"/.[!.]*; do
    [ -e "$item" ] && [ "$(basename "$item")" != ".ssh" ] && rm -rf "$item" && echo "삭제: $item"
done

echo ""
echo "정리 완료. 남은 항목:"
ls -la
