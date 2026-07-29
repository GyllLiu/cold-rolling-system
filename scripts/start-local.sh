#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -d node_modules ]]; then
  echo "缺少 node_modules，请先运行 npm install。"
  exit 1
fi

echo "网页：http://127.0.0.1:3000"
npm run start:lan
