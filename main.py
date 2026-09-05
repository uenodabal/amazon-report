#!/usr/bin/env python3
"""
Amazon SP-API 注文レポート → Googleスプレッドシート

日本マーケットプレイスの注文レポートを取得し、指定したGoogleスプレッドシートの
ワークシートへ書き込みます。同じ期間で何度実行しても結果が同じになるよう、
毎回シートを洗い替えします。

使い方:
    python3 main.py                # 直近30日分
    python3 main.py --days 7       # 直近7日分
    python3 main.py --dry-run      # 取得のみ。スプレッドシートには書き込まない
"""

import argparse
import csv
import io
import sys
import time
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

# 注文レポートの日付列（purchase-date）の位置。ヘッダー名から自動検出する。
DATE_COLUMN_NAME = "purchase-date"

# 購入者の個人情報を含まない注文レポート
REPORT_TYPE = "GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL"

POLL_INTERVAL_SEC = 30
POLL_TIMEOUT_SEC = 1800  # 30分


def create_report(token, start_time, end_time):
    """レポート生成をリクエストし、reportId を返す。"""
    log(f"レポート生成をリクエストしています（{start_time:%Y-%m-%d} 〜 {end_time:%Y-%m-%d}）…")
    response = request_with_retry(
        "POST",
        f"{SPAPI_ENDPOINT}/reports/2021-06-30/reports",
        headers={"x-amz-access-token": token, "Content-Type": "application/json"},
        json={
            "reportType": REPORT_TYPE,
            "marketplaceIds": [MARKETPLACE_JP],
            "dataStartTime": start_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "dataEndTime": end_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    report_id = response.json()["reportId"]
    log(f"  受け付けられました。reportId={report_id}")
    return report_id


def wait_for_report(token, report_id):
    """レポートが DONE になるまで待ち、reportDocumentId を返す。"""
    log("レポートの生成を待っています（数分〜十数分かかります）…")
    deadline = time.time() + POLL_TIMEOUT_SEC
    waited = 0

    while time.time() < deadline:
        response = request_with_retry(
            "GET",
            f"{SPAPI_ENDPOINT}/reports/2021-06-30/reports/{report_id}",
            headers={"x-amz-access-token": token},
        )
        info = response.json()
        status = info.get("processingStatus")

        if status == "DONE":
            log(f"  生成が完了しました（待機 {waited}秒）。")
            return info["reportDocumentId"]

        if status == "CANCELLED":
            raise RuntimeError(
                "レポートがキャンセルされました。対象期間にデータが存在しない可能性があります。"
            )

        if status == "FATAL":
            raise RuntimeError(
                "レポートの生成に失敗しました（FATAL）。"
                "権限（ロール）が不足しているか、期間の指定が不正な可能性があります。"
            )

        log(f"  状態: {status} — {POLL_INTERVAL_SEC}秒後に再確認します")
        time.sleep(POLL_INTERVAL_SEC)
        waited += POLL_INTERVAL_SEC

    raise TimeoutError(
        f"レポート生成が{POLL_TIMEOUT_SEC}秒以内に完了しませんでした。"
        "時間をおいて再実行してください。"
    )


def download_report(token, document_id):
    """レポート本体をダウンロードし、行のリスト（1行目はヘッダー）として返す。"""
    log("レポートをダウンロードしています…")
    response = request_with_retry(
        "GET",
        f"{SPAPI_ENDPOINT}/reports/2021-06-30/documents/{document_id}",
        headers={"x-amz-access-token": token},
    )
    doc = response.json()

    raw = request_with_retry("GET", doc["url"]).content
    if doc.get("compressionAlgorithm") == "GZIP":
        raw = decompress_if_needed(raw)

    text = decode_text(raw)
    rows = list(csv.reader(io.StringIO(text), delimiter="\t"))
    rows = [r for r in rows if any(cell.strip() for cell in r)]  # 空行を除去
    log(f"  {len(rows) - 1 if rows else 0} 件の注文明細を取得しました。")
    return rows


def main():
    parser = argparse.ArgumentParser(
        description="Amazon SP-API の注文レポートをGoogleスプレッドシートへ取り込みます。"
    )
    parser.add_argument("--days", type=int, default=30, help="取得する日数（既定: 30）")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="取得のみ行い、スプレッドシートには書き込まない",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="蓄積せず、シートを全置換する（過去データは失われます）",
    )
    args = parser.parse_args()

    if args.days < 1:
        raise SystemExit("--days は1以上を指定してください。")

    cfg = Config()
    cfg.validate(need_sheets=not args.dry_run)
    sheet_name = cfg.worksheet("WORKSHEET_ORDERS", "raw_orders")

    end_time = datetime.now(JST)
    start_time = end_time - timedelta(days=args.days)

    log("=" * 60)
    log(f"注文レポートの取得を開始します（直近 {args.days} 日）")
    log("=" * 60)

    try:
        token = get_access_token(cfg)
        report_id = create_report(token, start_time, end_time)
        document_id = wait_for_report(token, report_id)
        rows = download_report(token, document_id)

        if args.dry_run:
            log("--dry-run のため、スプレッドシートへの書き込みは行いません。")
            if rows:
                log(f"  列: {', '.join(rows[0][:8])} …")
                for row in rows[1:4]:
                    log(f"  例: {row[:5]}")
        elif rows:
            header = rows[0]
            try:
                date_col = header.index(DATE_COLUMN_NAME)
            except ValueError:
                date_col = 2  # 通常は3列目が purchase-date
                log(f"  ※ 列「{DATE_COLUMN_NAME}」が見つからないため {date_col + 1} 列目を日付として扱います。")

            merge_and_write(
                cfg,
                sheet_name,
                header,
                rows[1:],
                date_col=date_col,
                window_start=start_time.strftime("%Y-%m-%d"),
                replace=args.replace,
                sort_cols=[date_col],
            )
        else:
            log("取得できたデータがありませんでした。既存シートは変更しません。")

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
