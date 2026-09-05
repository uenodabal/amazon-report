#!/bin/bash
#
# 毎日の自動実行を登録する（macOS launchd）
#
# 使い方:
#     ./install_launchd.sh          # 毎朝6時に設定
#     ./install_launchd.sh 9 30     # 毎朝9時30分に設定
#
# 解除:
#     ./uninstall_launchd.sh
#

set -euo pipefail

HOUR="${1:-6}"
MINUTE="${2:-0}"

LABEL="jp.exoplus.amazon-report"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

echo "作業フォルダ : $SCRIPT_DIR"
echo "実行時刻     : 毎日 ${HOUR}時${MINUTE}分"
echo ""

# --- 事前チェック ---------------------------------------------------------
if [ ! -f "$SCRIPT_DIR/.env" ]; then
  echo "エラー: .env が見つかりません。先に設定を済ませてください。"
  exit 1
fi

if [ ! -f "$SCRIPT_DIR/run_daily.sh" ]; then
  echo "エラー: run_daily.sh が見つかりません。"
  exit 1
fi

chmod +x "$SCRIPT_DIR/run_daily.sh"
mkdir -p "$HOME/Library/LaunchAgents" "$SCRIPT_DIR/logs"

# --- 既存の登録があれば解除 -----------------------------------------------
if launchctl list | grep -q "$LABEL"; then
  echo "既存の登録を解除しています…"
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
fi

# --- plistを生成 -----------------------------------------------------------
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${SCRIPT_DIR}/run_daily.sh</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>${HOUR}</integer>
        <key>Minute</key>
        <integer>${MINUTE}</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>${SCRIPT_DIR}/logs/launchd.out.log</string>

    <key>StandardErrorPath</key>
    <string>${SCRIPT_DIR}/logs/launchd.err.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>LANG</key>
        <string>ja_JP.UTF-8</string>
    </dict>

    <key>RunAtLoad</key>
    <false/>

    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
PLIST_EOF

# --- 登録 -------------------------------------------------------------------
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST"

echo ""
echo "登録しました。"
echo ""
echo "  設定ファイル : $PLIST"
echo "  ログ         : $SCRIPT_DIR/logs/"
echo ""
echo "確認方法:"
echo "  launchctl list | grep amazon-report      # 登録されているか"
echo "  ./run_daily.sh                           # 今すぐ手動で実行して動作確認"
echo "  tail -f logs/\$(date +%Y-%m-%d).log       # ログを見る"
echo ""
echo "解除:"
echo "  ./uninstall_launchd.sh"
echo ""
echo "※ Macがスリープ中は実行されず、復帰時に1回だけ実行されます。"
