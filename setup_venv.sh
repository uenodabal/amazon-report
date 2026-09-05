#!/bin/bash
#
# venv を作り直す
#
# ネイティブ拡張（cryptography など）が正しく動くPythonを自動で選びます。
# Xcode同梱の Python 3.8 は、EOLかつ拡張モジュールが壊れやすいため避けます。
#
# 使い方:
#     ./setup_venv.sh
#

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# 汚染された環境変数を無視する（別バージョンのsite-packagesを見に行くのを防ぐ）
unset PYTHONPATH

MIN_VERSION=310   # 3.10 以上を要求する

echo "============================================================"
echo "venv を作り直します"
echo "============================================================"
echo ""
echo "使えるPythonを探しています…"

# --- 候補を集める -----------------------------------------------------------
CANDIDATES=()
for base in /opt/homebrew/bin /usr/local/bin /usr/bin; do
  for name in python3.13 python3.12 python3.11 python3.10 python3; do
    [ -x "$base/$name" ] && CANDIDATES+=("$base/$name")
  done
done
# Homebrew が /usr/local/bin へ symlink を作らない場合に備え、実体も見る
for cellar in /opt/homebrew/opt /usr/local/opt; do
  for version in 3.13 3.12 3.11 3.10; do
    binary="$cellar/python@$version/bin/python$version"
    [ -x "$binary" ] && CANDIDATES+=("$binary")
  done
done
# python.org の公式インストーラ（Xcode不要・ビルド不要）の導入先
for version in 3.13 3.12 3.11 3.10; do
  binary="/Library/Frameworks/Python.framework/Versions/$version/bin/python$version"
  [ -x "$binary" ] && CANDIDATES+=("$binary")
done
if [ -d "$HOME/.pyenv/versions" ]; then
  for dir in "$HOME/.pyenv/versions"/*; do
    [ -x "$dir/bin/python3" ] && CANDIDATES+=("$dir/bin/python3")
  done
fi

# --- 一番新しいものを選ぶ ---------------------------------------------------
BEST=""
BEST_VERSION=0
for candidate in "${CANDIDATES[@]}"; do
  version=$("$candidate" -c 'import sys; print("%d%02d" % sys.version_info[:2])' 2>/dev/null) || continue
  label=$("$candidate" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)

  if [ "$version" -lt "$MIN_VERSION" ]; then
    echo "  スキップ: $candidate （$label — 古すぎます）"
    continue
  fi
  echo "  候補:     $candidate （${label}）"
  if [ "$version" -gt "$BEST_VERSION" ]; then
    BEST="$candidate"
    BEST_VERSION="$version"
  fi
done

if [ -z "$BEST" ]; then
  echo ""
  echo "エラー: Python 3.10 以上が見つかりませんでした。"
  echo ""
  echo "  どちらかの方法で新しいPythonを入れてください:"
  echo ""
  echo "    A. pyenv（すでにお使いの場合）"
  echo "         pyenv install 3.12.7"
  echo ""
  echo "    B. Homebrew"
  echo "         brew install python@3.12"
  echo ""
  echo "  その後、もう一度 ./setup_venv.sh を実行してください。"
  exit 1
fi

echo ""
echo "選択: $BEST （$("$BEST" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')）"
echo ""

# --- 作り直す ---------------------------------------------------------------
if [ -d venv ]; then
  echo "既存の venv を削除しています…"
  rm -rf venv || { echo "エラー: venv を削除できませんでした。"; exit 1; }
fi

echo "venv を作成しています…"
"$BEST" -m venv venv || { echo "エラー: venv の作成に失敗しました。"; exit 1; }

echo "pip を更新しています…"
venv/bin/python -m pip install --upgrade pip --quiet

echo "パッケージを導入しています…"
venv/bin/python -m pip install -r requirements.txt --quiet || {
  echo "エラー: パッケージの導入に失敗しました。"
  exit 1
}

# --- 検証 -------------------------------------------------------------------
echo ""
echo "動作を確認しています…"
venv/bin/python - <<'CHECK'
import importlib
import os
import sys

failures = []
for name in ("requests", "gspread", "google.auth"):
    try:
        # __import__ だと google.auth のような名前空間パッケージで
        # 親パッケージが返り、__file__ が None になる。
        module = importlib.import_module(name)
        path = getattr(module, "__file__", None)
        where = os.path.dirname(path) if path else "(名前空間パッケージ)"
        print(f"  OK  {name:14s} {where}")
    except Exception as exc:
        print(f"  NG  {name:14s} {exc}")
        failures.append(name)

# cryptography はネイティブ拡張を含むため、ここが通れば環境として健全
try:
    import cryptography
    print(f"  OK  {'cryptography':14s} {cryptography.__version__}")
except Exception as exc:
    print(f"  NG  cryptography  {exc}")
    failures.append("cryptography")

print()
print(f"  Python {sys.version.split()[0]}")
sys.exit(1 if failures else 0)
CHECK

if [ $? -ne 0 ]; then
  echo ""
  echo "============================================================"
  echo "一部のパッケージが読み込めませんでした。"
  echo "上の NG の行を貼って相談してください。"
  echo "============================================================"
  exit 1
fi

echo ""
echo "============================================================"
echo "完了しました。"
echo ""
echo "  次はこれを実行してください（8月末以降の抜けが埋まります）:"
echo "    ./run_daily.sh --full"
echo "============================================================"
