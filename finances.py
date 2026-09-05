#!/usr/bin/env python3
"""
Amazon Finances API → 注文単位の手数料内訳（商品別の実利益の算出用）

売上から販売手数料・FBA手数料・プロモーション値引きを差し引いた
「実際に入金される金額」を、注文×SKU単位で取得します。
精算レポート（月2回）を待たずに日次で採算が把握できます。

使い方:
    python3 finances.py                # 直近30日分
    python3 finances.py --days 90      # 直近90日分（最大180日）
    python3 finances.py --dry-run      # 取得のみ。書き込まない

出力シート: raw_finances
"""

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone

from common import (
    JST,
    SPAPI_ENDPOINT,
    Config,
    get_access_token,
    log,
    merge_and_write,
    request_with_retry,
)

FINANCES_PATH = f"{SPAPI_ENDPOINT}/finances/v0/financialEvents"

MAX_DAYS = 180  # APIの上限
PAGE_SLEEP_SEC = 2.5  # レート制限 0.5 req/秒 に対する余裕

HEADER = [
    "postedDate",
    "eventType",
    "amazonOrderId",
    "sku",
    "quantity",
    "商品代金",
    "税",
    "配送料",
    "その他売上",
    "プロモーション値引",
    "販売手数料",
    "FBA手数料",
    "その他手数料",
    "純額",
]

# 手数料の分類
REFERRAL_FEE_TYPES = {"Commission", "ReferralFee"}


def amount_of(node):
    """{"CurrencyAmount": 123.0} 形式から数値を取り出す。"""
    if not isinstance(node, dict):
        return 0.0
    for key in ("CurrencyAmount", "Amount", "value"):
        if key in node and node[key] is not None:
            try:
                return float(node[key])
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def classify_charges(charge_list):
    """売上側の金額を種類ごとに振り分ける。"""
    principal = tax = shipping = other = 0.0
    for charge in charge_list or []:
        value = amount_of(charge.get("ChargeAmount"))
        ctype = charge.get("ChargeType", "")
        if ctype == "Principal":
            principal += value
        elif "Tax" in ctype:
            tax += value
        elif "Shipping" in ctype:
            shipping += value
        else:
            other += value
    return principal, tax, shipping, other


def classify_fees(fee_list):
    """手数料を「販売手数料」「FBA手数料」「その他」に振り分ける（いずれも負の値）。"""
    referral = fba = other = 0.0
    for fee in fee_list or []:
        value = amount_of(fee.get("FeeAmount"))
        ftype = fee.get("FeeType", "")
        if ftype in REFERRAL_FEE_TYPES:
            referral += value
        elif "FBA" in ftype:
            fba += value
        else:
            other += value
    return referral, fba, other


def sum_promotions(promotion_list):
    total = 0.0
    for promo in promotion_list or []:
        total += amount_of(promo.get("PromotionAmount"))
    return total


def make_row(posted_date, event_type, order_id, sku, quantity, charges, fees, promo):
    principal, tax, shipping, other_sales = charges
    referral, fba, other_fees = fees
    net = principal + tax + shipping + other_sales + promo + referral + fba + other_fees
    return [
        posted_date,
        event_type,
        order_id,
        sku,
        quantity,
        round(principal, 2),
        round(tax, 2),
        round(shipping, 2),
        round(other_sales, 2),
        round(promo, 2),
        round(referral, 2),
        round(fba, 2),
        round(other_fees, 2),
        round(net, 2),
    ]


def date_part(value, fallback=""):
    """ISO8601の日時から日付部分だけ取り出す。"""
    if not value:
        return fallback
    return str(value)[:10]


# ---------------------------------------------------------------------------
# イベントの展開
# ---------------------------------------------------------------------------
def flatten_shipment_events(events, event_type, fallback_date):
    """出荷イベント／返金イベントを SKU 単位の行に展開する。"""
    rows = []
    for event in events or []:
        posted = date_part(event.get("PostedDate"), fallback_date)
        order_id = event.get("AmazonOrderId", "")

        items = event.get("ShipmentItemList") or event.get("ShipmentItemAdjustmentList") or []
        for item in items:
            rows.append(
                make_row(
                    posted,
                    event_type,
                    order_id,
                    item.get("SellerSKU", ""),
                    item.get("QuantityShipped", ""),
                    classify_charges(
                        item.get("ItemChargeList") or item.get("ItemChargeAdjustmentList")
                    ),
                    classify_fees(
                        item.get("ItemFeeList") or item.get("ItemFeeAdjustmentList")
                    ),
                    sum_promotions(
                        item.get("PromotionList") or item.get("PromotionAdjustmentList")
                    ),
                )
            )
    return rows


def flatten_service_fee_events(events, fallback_date):
    """サービス手数料（保管料など、注文に紐づかない費用）を行に展開する。"""
    rows = []
    for event in events or []:
        referral, fba, other = classify_fees(event.get("FeeList"))
        reason = event.get("FeeReason") or event.get("FeeDescription") or "サービス手数料"
        rows.append(
            make_row(
                date_part(event.get("PostedDate"), fallback_date),
                f"手数料（{reason}）",
                event.get("AmazonOrderId", ""),
                event.get("SellerSKU", ""),
                "",
                (0.0, 0.0, 0.0, 0.0),
                (referral, fba, other),
                0.0,
            )
        )
    return rows


def collect_rows(financial_events, fallback_date):
    rows = []
    rows += flatten_shipment_events(
        financial_events.get("ShipmentEventList"), "出荷", fallback_date
    )
    rows += flatten_shipment_events(
        financial_events.get("RefundEventList"), "返金", fallback_date
    )
    rows += flatten_shipment_events(
        financial_events.get("ShipmentEventAdjustmentList"), "出荷調整", fallback_date
    )
    rows += flatten_service_fee_events(
        financial_events.get("ServiceFeeEventList"), fallback_date
    )
    return rows


# ---------------------------------------------------------------------------
# 取得
# ---------------------------------------------------------------------------
def fetch_financial_events(token, start, end):
    """期間内の財務イベントをページングしながらすべて取得する。"""
    rows = []
    next_token = None
    page = 0
    fallback_date = end.strftime("%Y-%m-%d")

    while True:
        page += 1
        if next_token:
            params = {"NextToken": next_token, "MaxResultsPerPage": 100}
        else:
            # 必ずUTCへ変換してから "Z" を付ける。
            # JSTのまま "Z" を付けると9時間未来の時刻として解釈され、
            # "should be no later than 2 minutes from now" で400になる。
            params = {
                "PostedAfter": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "PostedBefore": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "MaxResultsPerPage": 100,
            }

        response = request_with_retry(
            "GET",
            FINANCES_PATH,
            headers={"x-amz-access-token": token},
            params=params,
        )
        body = response.json()
        payload = body.get("payload", body)  # payload でラップされる場合とされない場合がある
        events = payload.get("FinancialEvents") or {}

        page_rows = collect_rows(events, fallback_date)
        rows.extend(page_rows)
        log(f"  {page}ページ目: {len(page_rows)} 件（累計 {len(rows)} 件）")

        next_token = payload.get("NextToken") or body.get("NextToken")
        if not next_token:
            break

        time.sleep(PAGE_SLEEP_SEC)  # レート制限（0.5 req/秒）に配慮

    return rows


def main():
    parser = argparse.ArgumentParser(
        description="Finances APIから注文単位の手数料内訳を取得します。"
    )
    parser.add_argument("--days", type=int, default=30, help="取得する日数（既定: 30、最大180）")
    parser.add_argument("--dry-run", action="store_true", help="書き込まずに取得だけ行う")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="蓄積せず、シートを全置換する（過去データは失われます）",
    )
    args = parser.parse_args()

    if args.days < 1:
        raise SystemExit("--days は1以上を指定してください。")
    if args.days > MAX_DAYS:
        log(f"※ --days の上限は {MAX_DAYS} です。{MAX_DAYS} 日として実行します。")
        args.days = MAX_DAYS

    cfg = Config()
    cfg.validate(need_sheets=not args.dry_run)
    sheet_name = cfg.worksheet("WORKSHEET_FINANCES", "raw_finances")

    # PostedAfter/Before は「リクエストの2分以上前」である必要がある
    end = datetime.now(JST) - timedelta(minutes=5)
    start = end - timedelta(days=args.days)

    log("=" * 60)
    log(f"手数料・入金内訳を取得します（{start:%Y-%m-%d} 〜 {end:%Y-%m-%d} / {args.days}日間）")
    log("=" * 60)

    try:
        token = get_access_token(cfg)
        log("財務イベントを取得しています…")
        rows = fetch_financial_events(token, start, end)

        # 日付・注文IDの順に整列
        rows.sort(key=lambda r: (r[0], r[2]))
        log(f"合計 {len(rows)} 件を取得しました。")

        if rows:
            net_total = sum(r[13] for r in rows)
            fee_total = sum(r[10] + r[11] + r[12] for r in rows)
            log(f"  期間の純額合計: {net_total:,.0f}")
            log(f"  うち手数料合計: {fee_total:,.0f}")

        if args.dry_run:
            log("--dry-run のため、スプレッドシートへの書き込みは行いません。")
            for row in rows[:3]:
                log(f"  例: {row}")
        else:
            merge_and_write(
                cfg,
                sheet_name,
                HEADER,
                rows,
                date_col=0,  # postedDate
                window_start=start.strftime("%Y-%m-%d"),
                replace=args.replace,
                sort_cols=[0, 2],
            )

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
