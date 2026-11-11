#!/usr/bin/env bash
# 在终端里后台启动 daemon + web（不经过 launchd，绕开 macOS 对后台进程的 TCC 限制）。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOGDIR="$HOME/Library/Logs"
mkdir -p "$ROOT/state" "$LOGDIR"
start() {
  local label="$1"
  shift
  if [ -f "$ROOT/state/$label.pid" ] && kill -0 "$(cat "$ROOT/state/$label.pid")" 2>/dev/null; then
    echo "· $label 已在运行（pid $(cat "$ROOT/state/$label.pid")）"
    return
  fi
  nohup "$@" >> "$LOGDIR/com.hpa.$label.log" 2>&1 &
  echo $! > "$ROOT/state/$label.pid"
  echo "· $label 已启动（pid $!，日志 $LOGDIR/com.hpa.$label.log）"
}
start daemon "$ROOT/bin/hpa" daemon
start web "$ROOT/bin/hpa" web
sleep 1
echo "页面：http://127.0.0.1:8787/（首次带 ?token=…，见 state/web-token 或 secrets.env）"
echo "停止：./scripts/stop-foreground.sh"
