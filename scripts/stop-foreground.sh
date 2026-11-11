#!/usr/bin/env bash
# 停止 start-foreground.sh 起的进程。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
for label in daemon web; do
  pidfile="$ROOT/state/$label.pid"
  if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    kill "$(cat "$pidfile")" && echo "· $label 已停止"
  fi
  rm -f "$pidfile"
done
