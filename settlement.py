#!/usr/bin/env python3
"""
Amazon 精算レポート → 入金明細（銀行入金との照合用）

Amazonが精算サイクル（通常2週間）ごとに自動生成する精算レポートを取得します。

【重要】精算レポートは createReport でリクエストできません。
Amazonが自動生成したものを getReports で探して取りに行く必要があります。
ここを createReport で実装しようとすると
「Request for report type ... is not allowed at this time」で必ず失敗します。

使い方:
    python3 settlement.py              # 直近3件の精算レポート
    python3 settlement.py --count 6    # 直近6件（約3ヶ月分）
    python3 settlement.py --dry-run    # 取得のみ

出力シート: raw_settlement
"""

import argparse
import csv
import io
import sys
from datetime import datetime, timedelta, timezone

from common import (
    JST,
    MARKETPLACE_JP,
    SPAPI_ENDPOINT,
    Config,
    decode_text,
    decompress_if_needed,
    get_access_token,
    log,
    merge_and_write,
    request_with_retry,
)

REPORT_TYPE = "GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE_V2"
# 旧形式。V2が1件も見つからない場合のフォールバックとして参照する。
LEGACY_REPORT_TYPE = "GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE"

# getReports の createdSince は「90日より前」を受け付けない
# （HTTP 400 InvalidInput: "... is more than 90 days old"）。
# 境界でのずれを避けるため89日を上限とする。
MAX_SINCE_DAYS = 89


def list_settlement_reports(token, report_type, since):
    """Amazonが生成済みの精算レポート一覧を取得する（新しい順）。"""
    params = {
        "reportTypes": report_type,
        "marketplaceIds": MARKETPLACE_JP,
        "pageSize": 100,
        # UTCへ変換してから "Z" を付ける（JSTのままだと9時間ずれる）
        "createdSince": since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    response = request_with_retry(
        "GET",
        f"{SPAPI_ENDPOINT}/reports/2021-06-30/reports",
        headers={"x-amz-access-token": token},
        params=params,
    )
    reports = response.json().get("reports", [])
    done = [r for r in reports if r.get("processingStatus") == "DONE"]
    done.sort(key=lambda r: r.get("dataEndTime") or r.get("createdTime") or "", reverse=True)
    return done


def download_settlement(token, report):
    """1件の精算レポートをダウンロードし、行のリストで返す（1行目はヘッダー）。"""
    response = request_with_retry(
        "GET",
        f"{SPAPI_ENDPOINT}/reports/2021-06-30/documents/{report['reportDocumentId']}",
        headers={"x-amz-access-token": token},
    )
    doc = response.json()
    raw = request_with_retry("GET", doc["url"]).content
    if doc.get("compressionAlgorithm") == "GZIP":
        raw = decompress_if_needed(raw)

    text = decode_text(raw)
    rows = list(csv.reader(io.StringIO(text), delimiter="\t"))
    return [r for r in rows if any(cell.strip() for cell in r)]


def main():
    parser = argparse.ArgumentParser(
        description="Amazonが生成済みの精算レポートを取得します。"
    )
    parser.add_argument(
        "--count", type=int, default=3, help="取得する精算レポートの件数（既定: 3）"
    )
    parser.add_argument(
        "--since-days",
        type=int,
        default=MAX_SINCE_DAYS,
        help=f"何日前までさかのぼって探すか（既定・上限とも {MAX_SINCE_DAYS}）",
    )
    parser.add_argument("--dry-run", action="store_true", help="書き込まずに取得だけ行う")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="蓄積せず、シートを全置換する（過去データは失われます）",
    )
    args = parser.parse_args()

    # Amazon側の制約でこれ以上は遡れない
    if args.since_days > MAX_SINCE_DAYS:
        log(
            f"※ --since-days は {MAX_SINCE_DAYS} が上限です"
            f"（Amazonの仕様。90日より前のレポートは一覧に出ません）。"
            f"{MAX_SINCE_DAYS} として実行します。"
        )
        args.since_days = MAX_SINCE_DAYS

    cfg = Config()
    cfg.validate(need_sheets=not args.dry_run)
    sheet_name = cfg.worksheet("WORKSHEET_SETTLEMENT", "raw_settlement")

    since = datetime.now(JST) - timedelta(days=args.since_days)

    log("=" * 60)
    log(f"精算レポートを取得します（直近 {args.count} 件）")
    log("=" * 60)

    try:
        token = get_access_token(cfg)

        log("生成済みの精算レポートを探しています…")
        reports = list_settlement_reports(token, REPORT_TYPE, since)

        if not reports:
            log("  V2形式が見つからないため、旧形式も確認します…")
            reports = list_settlement_reports(token, LEGACY_REPORT_TYPE, since)

        if not reports:
            log("")
            log("精算レポートが1件も見つかりませんでした。次の可能性があります。")
            log("  ・アプリに「Finance and Accounting」ロールが付与されていない")
            log("  ・まだ最初の精算サイクルが完了していない（通常2週間ごと）")
            log(f"  ・直近{args.since_days}日以内に精算が発生していない")
            log("")
            log("※ Amazonの仕様上、90日より前の精算レポートは取得できません。")
            return 1

        log(f"  {len(reports)} 件見つかりました。新しい順に {args.count} 件を取得します。")

        header = None
        all_rows = []
        for index, report in enumerate(reports[: args.count], start=1):
            period = f"{report.get('dataStartTime','')[:10]} 〜 {report.get('dataEndTime','')[:10]}"
            log(f"[{index}/{min(args.count, len(reports))}] {period} をダウンロード中…")
            rows = download_settlement(token, report)
            if not rows:
                log("    空でした。")
                continue
            if header is None:
                header = rows[0] + ["_取込日時"]
            stamp = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
            all_rows.extend([r + [stamp] for r in rows[1:]])
            log(f"    {len(rows) - 1} 行")

        log(f"合計 {len(all_rows)} 行を取得しました。")

        if args.dry_run:
            log("--dry-run のため、スプレッドシートへの書き込みは行いません。")
            if header:
                log(f"  列: {', '.join(header[:8])} …")
        elif header:
            try:
                key_col = header.index("settlement-id")
            except ValueError:
                key_col = 0  # 精算レポートの1列目は settlement-id
            merge_and_write(
                cfg,
                sheet_name,
                header,
                all_rows,
                key_cols=[key_col],  # 精算IDごとに差し替える
                replace=args.replace,
                sort_cols=[key_col],
            )
        else:
            log("書き込むデータがありませんでした。")

        log("=" * 60)
        log("正常に終了しました。")
        log("=" * 60)

    except Exception as exc:
        log("=" * 60)
        log(f"エラーで終了しました: {exc}")
        log("=" * 60)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
