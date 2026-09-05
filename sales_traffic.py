#!/usr/bin/env python3
"""
Amazon Data Kiosk → ASIN別 売上・セッション・転換率（日別）

セラーセントラルの「ビジネスレポート（子商品別詳細ページ売上・トラフィック）」
に相当するデータを、日付×ASINの粒度で取得します。

使い方:
    python3 sales_traffic.py                # 直近7日分
    python3 sales_traffic.py --days 30      # 直近30日分
    python3 sales_traffic.py --dry-run      # 取得のみ。書き込まない
    python3 sales_traffic.py --granularity SKU   # SKU単位で取得

注意:
  * アプリに「Brand Analytics」ロールが必要です。
  * Data Kioskは1クエリあたり1日分しか日別に分解できないため、
    日数分のクエリを順番に実行します。1日あたり1〜3分かかります。
    初回の30日分は30〜60分程度を見込んでください。
  * 当日ぶんのデータは未確定です。既定では前日までを取得します。
"""

import argparse
import json
import sys
from datetime import datetime, timedelta

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
    read_sheet,
    request_with_retry,
)

# Amazonの数値は数日かけて確定するため、直近この日数は毎回取り直す
RESTATE_DAYS = 3

DATASET = "analytics_salesAndTraffic_2024_04_24"
DK_BASE = f"{SPAPI_ENDPOINT}/dataKiosk/2023-11-15"

POLL_INTERVAL_SEC = 20
POLL_TIMEOUT_SEC = 900  # 1日分あたり15分

HEADER = [
    "date",
    "parentAsin",
    "childAsin",
    "sku",
    "unitsOrdered",
    "orderedProductSales",
    "currency",
    "totalOrderItems",
    "unitsShipped",
    "unitsRefunded",
    "refundRate",
    "sessions",
    "pageViews",
    "browserSessions",
    "mobileAppSessions",
    "buyBoxPercentage",
    "unitSessionPercentage",
]


# ---------------------------------------------------------------------------
# GraphQLクエリ
# ---------------------------------------------------------------------------
def build_query(date_str, granularity):
    """指定した1日分のASIN別売上・トラフィックを取得するクエリを組み立てる。"""
    return f"""
query SalesAndTraffic {{
  {DATASET} {{
    salesAndTrafficByAsin(
      startDate: "{date_str}"
      endDate: "{date_str}"
      aggregateBy: {granularity}
      marketplaceIds: ["{MARKETPLACE_JP}"]
    ) {{
      startDate
      endDate
      parentAsin
      childAsin
      sku
      sales {{
        orderedProductSales {{ amount currencyCode }}
        unitsOrdered
        totalOrderItems
        unitsShipped
        unitsRefunded
        refundRate
      }}
      traffic {{
        sessions
        pageViews
        browserSessions
        mobileAppSessions
        buyBoxPercentage
        unitSessionPercentage
      }}
    }}
  }}
}}
""".strip()


# ---------------------------------------------------------------------------
# Data Kiosk API
# ---------------------------------------------------------------------------
def create_query(token, query):
    response = request_with_retry(
        "POST",
        f"{DK_BASE}/queries",
        headers={"x-amz-access-token": token, "Content-Type": "application/json"},
        json={"query": query},
    )
    return response.json()["queryId"]


def wait_for_query(token, query_id):
    """
    処理完了を待つ。
    戻り値: (dataDocumentId, errorDocumentId) — データが0件の場合 dataDocumentId は None。
    """
    import time

    deadline = time.time() + POLL_TIMEOUT_SEC
    while time.time() < deadline:
        response = request_with_retry(
            "GET",
            f"{DK_BASE}/queries/{query_id}",
            headers={"x-amz-access-token": token},
        )
        info = response.json()
        status = info.get("processingStatus")

        if status == "DONE":
            return info.get("dataDocumentId"), info.get("errorDocumentId")

        if status == "CANCELLED":
            # Data Kiosk では「該当データなし」でも CANCELLED になることがある
            return None, info.get("errorDocumentId")

        if status == "FATAL":
            error_id = info.get("errorDocumentId")
            detail = ""
            if error_id:
                try:
                    detail = "\n" + fetch_document_text(token, error_id)[:800]
                except Exception:
                    pass
            raise RuntimeError(
                "クエリの処理に失敗しました（FATAL）。\n"
                "アプリに「Brand Analytics」ロールが付与されているか確認してください。"
                + detail
            )

        time.sleep(POLL_INTERVAL_SEC)

    raise TimeoutError(
        f"クエリが{POLL_TIMEOUT_SEC}秒以内に完了しませんでした（queryId={query_id}）。"
    )


def fetch_document_text(token, document_id):
    """ドキュメントのURLを取得し、本文をテキストで返す。"""
    response = request_with_retry(
        "GET",
        f"{DK_BASE}/documents/{document_id}",
        headers={"x-amz-access-token": token},
    )
    url = response.json()["documentUrl"]
    raw = request_with_retry("GET", url).content
    return decode_text(decompress_if_needed(raw))


# ---------------------------------------------------------------------------
# JSONLの解釈
# ---------------------------------------------------------------------------
def unwrap(obj):
    """
    Data KioskのJSONLは、クエリの入れ子構造をそのまま返す場合と、
    レコード単体を返す場合がある。目的のレコードに到達するまで剥がす。
    """
    seen = 0
    while isinstance(obj, dict) and "traffic" not in obj and "sales" not in obj and seen < 5:
        # 単一キーの入れ子であれば中身へ降りる
        values = list(obj.values())
        if len(values) != 1:
            break
        inner = values[0]
        if isinstance(inner, list):
            return inner  # リストならそのまま返す（呼び出し側で展開）
        obj = inner
        seen += 1
    return obj


def to_row(record):
    """1レコードを表の1行に変換する。"""
    sales = record.get("sales") or {}
    traffic = record.get("traffic") or {}
    ordered = sales.get("orderedProductSales") or {}

    return [
        record.get("startDate", ""),
        record.get("parentAsin", ""),
        record.get("childAsin", "") or "",
        record.get("sku", "") or "",
        sales.get("unitsOrdered", ""),
        ordered.get("amount", ""),
        ordered.get("currencyCode", ""),
        sales.get("totalOrderItems", ""),
        sales.get("unitsShipped", ""),
        sales.get("unitsRefunded", ""),
        sales.get("refundRate", ""),
        traffic.get("sessions", ""),
        traffic.get("pageViews", ""),
        traffic.get("browserSessions", ""),
        traffic.get("mobileAppSessions", ""),
        traffic.get("buyBoxPercentage", ""),
        traffic.get("unitSessionPercentage", ""),
    ]


def parse_jsonl(text, fallback_date):
    """JSONLを行のリストへ変換する。"""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        unwrapped = unwrap(obj)
        records = unwrapped if isinstance(unwrapped, list) else [unwrapped]

        for record in records:
            if not isinstance(record, dict):
                continue
            row = to_row(record)
            if not row[0]:
                row[0] = fallback_date  # startDate が無い場合の保険
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------
def fetch_one_day(token, date_str, granularity):
    query = build_query(date_str, granularity)
    query_id = create_query(token, query)
    data_id, error_id = wait_for_query(token, query_id)

    if not data_id:
        if error_id:
            detail = fetch_document_text(token, error_id)[:400]
            log(f"    警告: {date_str} でエラー文書が返りました — {detail}")
        else:
            log(f"    {date_str}: データ0件")
        return []

    text = fetch_document_text(token, data_id)
    return parse_jsonl(text, date_str)


def main():
    parser = argparse.ArgumentParser(
        description="Data KioskからASIN別の売上・トラフィックを取得します。"
    )
    parser.add_argument("--days", type=int, default=7, help="取得する日数（既定: 7）")
    parser.add_argument(
        "--granularity",
        choices=["CHILD", "PARENT", "SKU"],
        default="CHILD",
        help="集計単位（既定: CHILD＝子ASIN単位）",
    )
    parser.add_argument("--dry-run", action="store_true", help="書き込まずに取得だけ行う")
    parser.add_argument(
        "--refetch",
        action="store_true",
        help="取得済みの日も含めてすべて取り直す",
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
    sheet_name = cfg.worksheet("WORKSHEET_SALES_TRAFFIC", "raw_sales_traffic")

    # 当日は未確定なので前日を終端にする
    end_date = (datetime.now(JST) - timedelta(days=1)).date()
    dates = [
        (end_date - timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(args.days - 1, -1, -1)
    ]

    log("=" * 60)
    log(f"ASIN別 売上・トラフィックを取得します（{dates[0]} 〜 {dates[-1]} / {args.days}日間）")
    log(f"集計単位: {args.granularity}")
    log("=" * 60)

    # --- 既に取得済みの日を調べ、必要な日だけに絞る -------------------------
    # Data Kioskは1日1クエリ（1〜3分）かかるため、ここで大きく時短できる。
    target_dates = dates
    if not args.dry_run and not args.refetch and not args.replace:
        try:
            existing = read_sheet(cfg, sheet_name)
            known = {str(r[0])[:10] for r in existing[1:]} if len(existing) > 1 else set()
        except Exception as exc:
            log(f"  （既存データを読めなかったため全日取得します: {exc}）")
            known = set()

        if known:
            # 直近RESTATE_DAYS日は確定前なので必ず取り直す
            restate_from = dates[-RESTATE_DAYS] if len(dates) >= RESTATE_DAYS else dates[0]
            target_dates = [d for d in dates if d not in known or d >= restate_from]
            skipped = len(dates) - len(target_dates)
            if skipped:
                log(f"取得済みの {skipped} 日分はスキップします（直近{RESTATE_DAYS}日は再取得）。")

    if not target_dates:
        log("取得が必要な日はありませんでした。")
        log("=" * 60)
        log("正常に終了しました。")
        log("=" * 60)
        return 0

    log(f"今回取得する日数: {len(target_dates)} 日（およそ {len(target_dates)}〜{len(target_dates) * 3} 分）")

    try:
        token = get_access_token(cfg)

        all_rows = []
        for index, date_str in enumerate(target_dates, start=1):
            log(f"[{index}/{len(target_dates)}] {date_str} を取得しています…")
            rows = fetch_one_day(token, date_str, args.granularity)
            log(f"    {len(rows)} 件")
            all_rows.extend(rows)

        log(f"合計 {len(all_rows)} 件を取得しました。")

        if args.dry_run:
            log("--dry-run のため、スプレッドシートへの書き込みは行いません。")
            for row in all_rows[:3]:
                log(f"  例: {row}")
        elif all_rows:
            merge_and_write(
                cfg,
                sheet_name,
                HEADER,
                all_rows,
                key_cols=[0],  # 日付単位で差し替える
                replace=args.replace,
                sort_cols=[0, 2],
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
