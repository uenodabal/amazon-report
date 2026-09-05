#!/bin/bash
#
# launchd から呼ばれる実行ラッパー
#
# launchd はログイン時とは違う最小限の環境で動くため、
# 必要なものをここですべて明示的に用意します。
#
# 手動でも実行できます:
#     ./run_daily.sh
#     ./run_daily.sh --full
#

set -uo pipefail

# このスクリプトが置かれているフォルダへ移動する（launchdはcwdが不定のため）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

mkdir -p logs
LOG_FILE="logs/$(date +%Y-%m-%d).log"

{
  echo ""
  echo "############################################################"
  echo "# 開始: $(date '+%Y-%m-%d %H:%M:%S')"
  echo "############################################################"

  # --- 設定の読み込み ---
  if [ ! -f .env ]; then
    echo "エラー: .env が見つかりません（${SCRIPT_DIR}）"
    exit 1
  fi
  # shellcheck disable=SC1091
  source .env

  # --- 環境変数の汚染を断つ ---
  # PYTHONPATH が別バージョンの site-packages を指していると、
  # 対話シェルでは動くのに launchd では失敗する、という食い違いが起きる。
  # ここで必ず捨てて、venv だけを見るようにする。
  if [ -n "${PYTHONPATH:-}" ]; then
    echo "  PYTHONPATH を無視します（設定値: ${PYTHONPATH}）"
    unset PYTHONPATH
  fi

  # --- Pythonの決定 ---
  # 候補を順に試し、「必要なパッケージが実際に入っているもの」を選ぶ。
  # パスが存在するだけで選ぶと、空のvenvを掴んで毎回失敗する。
  REQUIRED_IMPORT="import requests, gspread, google.auth"
  CANDIDATES=(
    "$SCRIPT_DIR/venv/bin/python"
    "$HOME/.pyenv/shims/python3"
    "$(command -v python3 2>/dev/null)"
    "/usr/local/bin/python3"
    "/opt/homebrew/bin/python3"
    "/usr/bin/python3"
  )

  PYTHON=""
  for candidate in "${CANDIDATES[@]}"; do
    [ -n "$candidate" ] && [ -x "$candidate" ] || continue
    if "$candidate" -c "$REQUIRED_IMPORT" >/dev/null 2>&1; then
      PYTHON="$candidate"
      break
    fi
    echo "  スキップ: $candidate （必要なパッケージが未導入）"
  done

  if [ -z "$PYTHON" ]; then
    echo ""
    echo "エラー: 必要なパッケージが入ったPythonが見つかりません。"
    echo ""
    echo "  次のコマンドで、venvにパッケージを入れてください:"
    echo "    cd $SCRIPT_DIR"
    echo "    venv/bin/python -m pip install -r requirements.txt"
    echo ""
    echo "  確認:"
    echo "    venv/bin/python -c \"import requests, gspread, google.auth; print('OK')\""
    echo ""
    exit 1
  fi

  echo "使用するPython: $PYTHON"
  "$PYTHON" --version
  # どこのパッケージを読んでいるかを毎回記録する（環境の取り違えを検知するため）
  "$PYTHON" -c "import requests, os; print('  requests:', os.path.dirname(requests.__file__))"

  # --- 実行 ---
  # 実行中にMacがスリープすると処理が凍り、そのまま朝まで止まることがある。
  # caffeinate でアイドルスリープを抑止する（蓋を閉じた場合は対象外）。
  RUNNER=()
  if command -v caffeinate >/dev/null 2>&1; then
    RUNNER=(caffeinate -i)
    echo "  caffeinate でスリープを抑止します"
  fi

  "${RUNNER[@]}" "$PYTHON" run_all.py "$@"
  STATUS=$?

  echo "############################################################"
  echo "# 終了: $(date '+%Y-%m-%d %H:%M:%S')　終了コード=$STATUS"
  echo "############################################################"

  # --- 古いログの掃除（30日より前を削除） ---
  find logs -name "*.log" -type f -mtime +30 -delete 2>/dev/null

  exit $STATUS

} 2>&1 | tee -a "$LOG_FILE"

exit "${PIPESTATUS[0]}"
