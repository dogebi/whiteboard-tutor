#!/usr/bin/env bash
# MOA 화이트보드 튜터 서버 실행 (.env 는 이 디렉터리에 둔다)
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi
export HTTPS_PROXY="${HTTPS_PROXY:-http://127.0.0.1:1099}"
export HTTP_PROXY="${HTTP_PROXY:-http://127.0.0.1:1099}"
exec python3 server/moa_server.py
