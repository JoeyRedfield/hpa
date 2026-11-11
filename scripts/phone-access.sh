#!/usr/bin/env bash
# 用 Tailscale 把本机手机端页面暴露到 tailnet，并打印带 token 的手机 URL。
# 前提：本机已装 Tailscale 并登录；hpa web / hpa daemon 已在 8787 端口运行。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${1:-8787}"
TS="/Applications/Tailscale.app/Contents/MacOS/Tailscale"

if [ ! -x "$TS" ]; then
  echo "❌ 找不到 Tailscale：$TS" >&2
  exit 2
fi

"$TS" serve --bg "$PORT"
HOST=$("$TS" status --json | /usr/bin/python3 -c \
  'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
TOKEN=$(grep -h '^HPA_WEB_TOKEN=' "$HOME/.config/hpa/secrets.env" 2>/dev/null | cut -d= -f2- || true)
if [ -z "$TOKEN" ]; then
  TOKEN=$(cat "$ROOT/state/web-token" 2>/dev/null || true)
fi
if [ -z "$TOKEN" ]; then
  echo "⚠️ 还没有 web token：先运行一次 bin/hpa web 生成 state/web-token" >&2
fi
echo "手机访问：https://$HOST/?token=$TOKEN"
