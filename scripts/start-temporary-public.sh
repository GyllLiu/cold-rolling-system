#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -x .venv/bin/python ]]; then
  echo "缺少 Python 运行环境，请先按 README 完成首次准备。"
  exit 1
fi
if [[ ! -d node_modules ]]; then
  echo "缺少前端依赖，请先运行 npm install。"
  exit 1
fi
for command in cloudflared caddy openssl curl; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "缺少运行命令：$command"
    exit 1
  fi
done

PUBLIC_USERNAME="${PUBLIC_USERNAME:-operator}"
PUBLIC_PASSWORD="${PUBLIC_PASSWORD:-$(openssl rand -hex 10)}"
PUBLIC_PASSWORD_HASH="$(caddy hash-password --algorithm bcrypt --plaintext "$PUBLIC_PASSWORD")"
export PUBLIC_USERNAME PUBLIC_PASSWORD_HASH

cleanup() {
  [[ -n "${BACKEND_PID:-}" ]] && kill "$BACKEND_PID" 2>/dev/null || true
  [[ -n "${FRONTEND_PID:-}" ]] && kill "$FRONTEND_PID" 2>/dev/null || true
  [[ -n "${CADDY_PID:-}" ]] && kill "$CADDY_PID" 2>/dev/null || true
  [[ -n "${CAFFEINATE_PID:-}" ]] && kill "$CAFFEINATE_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

npm run build

.venv/bin/python -m uvicorn --app-dir backend app.main:app --host 127.0.0.1 --port 8000 &
BACKEND_PID=$!
npm run start -- --host 127.0.0.1 &
FRONTEND_PID=$!
caddy run --config scripts/Caddyfile.temporary --adapter caddyfile &
CADDY_PID=$!
caffeinate -dimsu &
CAFFEINATE_PID=$!

for _ in {1..60}; do
  if curl --fail --silent --user "$PUBLIC_USERNAME:$PUBLIC_PASSWORD" http://127.0.0.1:8080/api/health >/dev/null \
    && curl --fail --silent --user "$PUBLIC_USERNAME:$PUBLIC_PASSWORD" http://127.0.0.1:8080/ >/dev/null; then
    break
  fi
  sleep 0.25
done

curl --fail --silent --user "$PUBLIC_USERNAME:$PUBLIC_PASSWORD" http://127.0.0.1:8080/api/health >/dev/null
curl --fail --silent --user "$PUBLIC_USERNAME:$PUBLIC_PASSWORD" http://127.0.0.1:8080/ >/dev/null

echo ""
echo "临时外网访问已启用密码保护"
echo "登录用户名：$PUBLIC_USERNAME"
echo "登录密码：$PUBLIC_PASSWORD"
echo "正在申请临时 HTTPS 地址，请保持此窗口运行。"
echo ""

cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8080
