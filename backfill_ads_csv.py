#!/usr/bin/env python3
"""
Amazon Adsの過去データ（管理画面からエクスポートしたCSV）を raw_ads_email へ
一括で取り込む、1回限りの復旧・バックフィル用スクリプト。

ads_email_report.py が動き出す前の期間（メールが届いていなかった期間）は
広告データが無いため、Amazon広告管理画面から手動でエクスポートしたCSVで
その期間を埋める。

管理画面からのエクスポートは、メール添付CSVと列の「並び順」が違うことがある
（例: キャンペーンIDとキャンペーン名の順序が入れ替わっている）。そのため
位置ではなく列名でマッピングし、raw_ads_email の標準の並び順
（ads_email_report.HEADER）に揃えてから書き込む。

使い方:
    source .env
    python3 backfill_ads_csv.py 直近7月から9月レポート.csv
    python3 backfill_ads_csv.py 直近7月から9月レポート.csv --dry-run
"""

import argparse
import csv
import io
import re
import sys
from datetime import datetime

from ads_email_report import HEADER
from common import Config, log, merge_and_write

_FORCE_TEXT_RE = re.compile(r'^="?(.*?)"?$')


def read_source_csv(path):
    with open(path, "rb") as f:
        raw = f.read()

    text = None
    for encoding in ("utf-8-sig", "cp932", "shift_jis"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")

    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise RuntimeError("CSVが空でした。")

    header = [h.strip() for h in rows[0]]
    return header, rows[1:]


def normalize_rows(header, data_rows):
    """
    列名でマッピングして raw_ads_email の標準列順（HEADER）に揃える。
    エクスポート元によって列の並びが違っても正しく取り込めるようにするため、
    位置ではなく名前で対応付ける。
    """
    missing = [name for name in HEADER if name not in header]
    if missing:
        raise RuntimeError(
            "CSVに次の列が見つかりません（列名が変わっている可能性があります）: "
            + ", ".join(missing)
        )
    idx = {name: header.index(name) for name in HEADER}
    id_pos = HEADER.index("キャンペーンID")

    out = []
    for raw_row in data_rows:
        if not any(str(c).strip() for c in raw_row):
            continue
        row = [
            str(raw_row[idx[name]]).strip() if idx[name] < len(raw_row) else ""
            for name in HEADER
        ]

        # 日付: 2026/09/06 → 2026-09-06
        if row[0]:
            try:
                row[0] = datetime.strptime(row[0], "%Y/%m/%d").strftime("%Y-%m-%d")
            except ValueError:
                pass

        # キャンペーンID: ="1234567890" → 1234567890
        m = _FORCE_TEXT_RE.match(row[id_pos])
        if m:
            row[id_pos] = m.group(1)

        out.append(row)
    return out


def main():
    parser = argparse.ArgumentParser(
        description="Amazon Adsの過去データCSVを raw_ads_email へ一括取り込みします。"
    )
    parser.add_argument("csv_path", help="Amazon広告管理画面からエクスポートしたCSVファイル")
    parser.add_argument("--dry-run", action="store_true", help="取得のみ。書き込まない")
    args = parser.parse_args()

    cfg = Config()
    if not args.dry_run:
        if not cfg.sa_json_path or not cfg.spreadsheet_id:
            raise SystemExit(
                "環境変数が設定されていません: GOOGLE_SERVICE_ACCOUNT_JSON, SPREADSHEET_ID\n"
                "`source .env` を実行してから再度お試しください。"
            )
    sheet_name = cfg.worksheet("WORKSHEET_ADS_EMAIL", "raw_ads_email")

    log("=" * 60)
    log(f"Amazon Adsの過去データを取り込みます: {args.csv_path}")
    log("=" * 60)

    source_header, source_rows = read_source_csv(args.csv_path)
    if source_header != HEADER:
        log("  ※ 列の並びがメール添付CSVと異なるため、列名で対応付けます。")
        log(f"    CSVの見出し: {source_header}")

    data_rows = normalize_rows(source_header, source_rows)
    log(f"  {len(data_rows)} 行を読み取りました。")

    if not data_rows:
        log("データがありませんでした。")
        return 1

    dates = sorted(row[0] for row in data_rows if row[0])
    log(f"  期間: {dates[0]} 〜 {dates[-1]}")
    log(f"  例: {data_rows[0]}")

    if args.dry_run:
        log("--dry-run のため、スプレッドシートへの書き込みは行いません。")
        return 0

    window_start = dates[0]
    merge_and_write(
        cfg, sheet_name, HEADER, data_rows,
        date_col=0, window_start=window_start, sort_cols=[0, 2],
    )

    log("=" * 60)
    log("正常に終了しました。")
    log("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
