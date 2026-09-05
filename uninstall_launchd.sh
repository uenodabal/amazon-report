#!/bin/bash
#
# 自動実行の登録を解除する
#

set -uo pipefail

LABEL="jp.exoplus.amazon-report"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null \
  || launchctl unload "$PLIST" 2>/dev/null \
  || true

if [ -f "$PLIST" ]; then
  mv "$PLIST" "${PLIST}.disabled"
  echo "解除しました（設定ファイルは ${PLIST}.disabled に退避しています）。"
else
  echo "登録は見つかりませんでした。既に解除済みです。"
fi

echo ""
echo "確認: launchctl list | grep amazon-report"
echo "（何も表示されなければ解除できています）"
