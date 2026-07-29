#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_PATH="${CLOUDFLARED_CONFIG:-$HOME/.cloudflared/cold-rolling-system.yml}"
cd "$ROOT"

if [[ ! -x .venv/bin/python ]]; then
  echo "缺少 Python 运行环境，请先按 README 完成首次准备。"
  exit 1
fi
if [[ ! -d node_modules ]]; then
  echo "缺少前端依赖，请先运行 npm install。"
  exit 1
fi
if ! command -v cloudflared >/dev/null 2>&1; then
  echo "缺少 cloudflared，请先运行 brew install cloudflared。"
  exit 1
fi
if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "缺少隧道配置：$CONFIG_PATH"
  exit 1
fi

cleanup() {
  [[ -n "${BACKEND_PID:-}" ]] && kill "$BACKEND_PID" 2>/dev/null || true
  [[ -n "${FRONTEND_PID:-}" ]] && kill "$FRONTEND_PID" 2>/dev/null || true
  [[ -n "${CAFFEINATE_PID:-}" ]] && kill "$CAFFEINATE_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

npm run build

.venv/bin/python -m uvicorn --app-dir backend app.main:app --host 127.0.0.1 --port 8000 &
BACKEND_PID=$!
npm run start -- --host 127.0.0.1 &
FRONTEND_PID=$!
caffeinate -dimsu &
CAFFEINATE_PID=$!

for _ in {1..40}; do
  if curl --fail --silent http://127.0.0.1:8000/api/health >/dev/null \
    && curl --fail --silent http://127.0.0.1:3000 >/dev/null; then
    break
  fi
  sleep 0.25
done

curl --fail --silent http://127.0.0.1:8000/api/health >/dev/null
curl --fail --silent http://127.0.0.1:3000 >/dev/null

echo "本地服务已就绪，正在连接受保护的 Cloudflare Tunnel。"
cloudflared tunnel --config "$CONFIG_PATH" run
