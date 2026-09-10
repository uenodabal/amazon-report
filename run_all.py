#!/usr/bin/env python3
"""
全スクリプトを順番に実行する（自動実行の入口）

launchd から毎日呼び出されることを想定しています。
1つのジョブが失敗しても残りは実行し、最後に結果をまとめて報告します。
実行結果はスプレッドシートの `_log` シートにも1行ずつ記録されるため、
ターミナルを見ていなくても「動いたか」「何件取れたか」を確認できます。

使い方:
    python3 run_all.py              # 通常実行（日次運用向け）
    python3 run_all.py --full       # 長めの期間で取り込む（初回・復旧用）
    python3 run_all.py --dry-run    # 書き込まずに実行
"""

import argparse
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

from common import JST, Config, append_row, log

# 1ジョブあたりの上限。これを超えたら強制終了して次へ進む。
# sales_traffic は1日1〜3分かかるため、30日分でも収まる長さにしてある。
JOB_TIMEOUT_SEC = 7200  # 2時間

LOG_SHEET = "_log"
LOG_HEADER = ["実行日時", "ジョブ", "結果", "所要秒", "詳細"]

# (ジョブ名, スクリプト, 通常時の日数, --full 時の日数)
# 精算レポートは日数ではなく「件数」を指定するため、引数名を別扱いにする（下記 JOB_ARG）
JOBS = [
    ("注文レポート", "main.py", 30, 90),
    ("ASIN別売上", "sales_traffic.py", 3, 30),
    ("手数料・入金内訳", "finances.py", 30, 180),
    # 精算レポートは90日より前を取得できないため、--full でも6件（約3ヶ月分）が上限
    ("精算レポート", "settlement.py", 3, 6),
    # Amazon Adsの自動送信メール（毎朝8:45〜9:00頃着）を取り込む。
    # 本体の実行時刻（毎朝7時台）より後に届くため、この時間の実行では
    # 前日分のメールを拾うことになる。最新の当日分は ADS_ONLY_JOBS の
    # 追い上げ実行（毎朝9:20頃）で取り込む。
    ("広告データ（メール取込）", "ads_email_report.py", 3, 7),
    # 最後にrawデータを集計して月別シートと月次推移を作る
    ("月別シート・月次推移", "monthly.py", 3, 6),
    # キャンペーン×日付ごとの実績一覧（日別広告実績シート）。全期間を作り直す。
    ("日別広告実績", "ads_daily_sheet.py", 400, 3650),
]

# 広告メールが届いた後に行う追い上げ実行用（毎朝9:20頃）。
# 広告データを取り込み直してから、月別シートと日別広告実績を再生成し、
# 最後にSlackへ実績レポートを投稿する（この時点でようやく前日分の広告データが
# 揃うため、Slack投稿はこの追い上げ実行だけで行う。7時の本体実行では行わない）。
ADS_ONLY_JOBS = [
    ("広告データ（メール取込）", "ads_email_report.py", 3, 7),
    ("月別シート・月次推移", "monthly.py", 3, 6),
    ("日別広告実績", "ads_daily_sheet.py", 400, 3650),
    ("Slack日次レポート", "slack_daily_report.py", 7, 7),
]

# スクリプトごとの数量オプション名（既定は --days）
JOB_ARG = {
    "settlement.py": "--count",
    "monthly.py": "--months",
    "ads_email_report.py": "--search-days",
    "ads_daily_sheet.py": "--days",
}


def run_job(name, script, amount, dry_run):
    """
    1ジョブを別プロセスで実行し、(成功したか, 所要秒, 要約) を返す。

    子プロセスの出力は溜め込まず、届いた行から順に流す。
    溜め込むと実行中のログが空のままになり、
    「動いているのか固まっているのか」が判別できなくなるため。
    """
    option = JOB_ARG.get(script, "--days")
    command = [sys.executable, script, option, str(amount)]
    if dry_run:
        command.append("--dry-run")

    log("")
    log("─" * 60)
    log(f"▶ {name}（{script} {option} {amount}）")
    log("─" * 60)

    # 子プロセス側の出力バッファリングを止める（そのままだと行が遅れて届く）
    env = dict(os.environ, PYTHONUNBUFFERED="1")

    started = time.time()
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )

    # 出力が一切ない状態で固まっても抜けられるよう、時間で強制終了する
    timed_out = {"hit": False}

    def kill_on_timeout():
        timed_out["hit"] = True
        process.kill()

    watchdog = threading.Timer(JOB_TIMEOUT_SEC, kill_on_timeout)
    watchdog.start()

    lines = []
    try:
        for line in process.stdout:
            line = line.rstrip()
            print(line, flush=True)
            lines.append(line)
        process.wait()
    finally:
        watchdog.cancel()

    elapsed = int(time.time() - started)
    output = "\n".join(lines)

    if timed_out["hit"]:
        log(f"✗ {name}: タイムアウト（{elapsed}秒）")
        return False, elapsed, f"タイムアウト（{JOB_TIMEOUT_SEC // 60}分）"

    if process.returncode == 0:
        summary = extract_summary(output)
        log(f"✓ {name}: 成功（{elapsed}秒）{summary}")
        return True, elapsed, summary or "成功"

    detail = extract_error(output)
    log(f"✗ {name}: 失敗（{elapsed}秒） {detail}")
    return False, elapsed, detail


def extract_summary(output):
    """出力から件数の記述を拾う。"""
    for line in reversed(output.splitlines()):
        if "件を書き込みました" in line or "件を取得しました" in line:
            return line.split("]", 1)[-1].strip()
    return ""


def extract_error(output):
    """出力からエラー行を拾う。"""
    for line in reversed(output.splitlines()):
        if "エラーで終了しました" in line:
            return line.split("エラーで終了しました:", 1)[-1].strip()[:300]
    tail = [l for l in output.rstrip().splitlines() if l.strip()]
    return tail[-1][:300] if tail else "不明なエラー"


def main():
    parser = argparse.ArgumentParser(description="全スクリプトを順番に実行します。")
    parser.add_argument(
        "--full",
        action="store_true",
        help="長めの期間で取り込む（初回や、実行が数日途切れた後の復旧用）",
    )
    parser.add_argument(
        "--ads-only",
        action="store_true",
        help="広告データの取込と月次シートの再生成のみ実行（広告メール到着後の追い上げ実行用）",
    )
    parser.add_argument("--dry-run", action="store_true", help="書き込まずに実行")
    args = parser.parse_args()

    cfg = Config()
    cfg.validate(need_sheets=not args.dry_run)

    jobs = ADS_ONLY_JOBS if args.ads_only else JOBS

    started_at = datetime.now(JST)
    log("=" * 60)
    log(f"自動取得を開始します（{started_at:%Y-%m-%d %H:%M:%S}）")
    if args.ads_only:
        log("モード: --ads-only（広告データの追い上げ実行）")
    elif args.full:
        log("モード: --full（長期間の取り込み）")
    log("=" * 60)

    results = []
    for name, script, days, full_days in jobs:
        ok, elapsed, detail = run_job(
            name, script, full_days if args.full else days, args.dry_run
        )
        results.append((name, ok, elapsed, detail))

        if not args.dry_run:
            append_row(
                cfg,
                LOG_SHEET,
                [
                    datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
                    name,
                    "成功" if ok else "失敗",
                    elapsed,
                    detail,
                ],
                header=LOG_HEADER,
            )

    # --- まとめ -------------------------------------------------------------
    total = int((datetime.now(JST) - started_at).total_seconds())
    succeeded = sum(1 for _, ok, _, _ in results if ok)

    log("")
    log("=" * 60)
    log(f"完了しました（合計 {total}秒 / 成功 {succeeded}/{len(results)}）")
    for name, ok, elapsed, detail in results:
        mark = "✓" if ok else "✗"
        log(f"  {mark} {name}: {detail}（{elapsed}秒）")
    log("=" * 60)

    # 1つでも失敗していれば異常終了（launchdのログで検知できるように）
    return 0 if succeeded == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
