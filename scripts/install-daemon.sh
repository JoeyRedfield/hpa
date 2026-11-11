#!/usr/bin/env bash
# 把 hpa 装成 macOS launchd 常驻服务：daemon（同步+流水线）+ web（手机端页面）。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
LOGDIR="$HOME/Library/Logs"

write_plist() {
  local label="$1"
  local subcmd="$2"
  local log="$LOGDIR/$label.log"
  cat > "$AGENTS/$label.plist" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$ROOT/bin/hpa</string>
    <string>$subcmd</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$log</string>
  <key>StandardErrorPath</key><string>$log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict>
</plist>
PLISTEOF
}

bootout() { launchctl bootout "gui/$UID/$1" 2>/dev/null || true; }

case "${1:-install}" in
install)
  mkdir -p "$AGENTS" "$LOGDIR"
  write_plist "com.hpa.daemon" "daemon"
  write_plist "com.hpa.web" "web"
  bootout com.hpa.daemon
  bootout com.hpa.web
  launchctl bootstrap "gui/$UID" "$AGENTS/com.hpa.daemon.plist"
  launchctl bootstrap "gui/$UID" "$AGENTS/com.hpa.web.plist"
  sleep 2
  if grep -qs "Operation not permitted" "$LOGDIR/com.hpa.daemon.log" "$LOGDIR/com.hpa.web.log"; then
    bootout com.hpa.daemon
    bootout com.hpa.web
    rm -f "$AGENTS/com.hpa.daemon.plist" "$AGENTS/com.hpa.web.plist"
    cat >&2 <<'MSG'
⚠️ install 失败：macOS 隐私保护（TCC）不让 launchd 后台进程读 ~/Desktop 和 ~/Documents，
   hpa 因此无法启动（已自动卸载，避免 crash loop）。可选做法：
   1) 在终端里前台跑：./scripts/start-foreground.sh（推荐；关掉终端前用 caffeinate -i 防止睡眠）
   2) 系统设置 → 隐私与安全性 → 完全磁盘访问权限，把运行 hpa 的程序加进去后重试 install
   3) 把仓库和笔记库移出「桌面/文稿」等受保护目录后重试 install
MSG
    exit 1
  fi
  echo "✅ 已安装并启动：com.hpa.daemon（同步+流水线）、com.hpa.web（手机端页面）"
  echo "   日志：$LOGDIR/com.hpa.daemon.log、$LOGDIR/com.hpa.web.log"
  echo "   停止：$0 uninstall"
  ;;
uninstall)
  bootout com.hpa.daemon
  bootout com.hpa.web
  rm -f "$AGENTS/com.hpa.daemon.plist" "$AGENTS/com.hpa.web.plist"
  echo "✅ 已卸载 hpa 常驻服务"
  ;;
status)
  launchctl list 2>/dev/null | grep -E "com\.hpa\.(daemon|web)" || echo "（未安装）"
  ;;
*)
  echo "用法：$0 install|uninstall|status" >&2
  exit 2
  ;;
esac
