#!/usr/bin/env python3
"""
売上差異の診断

ダッシュボードに出る2つの売上
  ・売上（ASIN別集計・Amazon公式） … raw_sales_traffic
  ・売上（注文レポート基準・参考）   … raw_orders
が食い違うとき、その原因を実データから切り分けます。

推測ではなく、シートの中身を数えて内訳を出します。
Amazon APIは呼びません（シートを読むだけ）。

使い方:
    python3 diagnose.py             # 直近30日
    python3 diagnose.py --days 31   # 期間を指定
"""

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta

from common import JST, Config, log, read_sheet
from dashboard import index_of, to_float


def section(title):
    log("")
    log("─" * 62)
    log(f"■ {title}")
    log("─" * 62)


def money(value):
    return f"{value:>14,.0f}"


def analyze_orders(cfg, since, until):
    """raw_orders を状態・販路ごとに分解する。"""
    rows = read_sheet(cfg, cfg.worksheet("WORKSHEET_ORDERS", "raw_orders"))
    if len(rows) < 2:
        log("raw_orders が空です。main.py を実行してください。")
        return None

    h = [str(c) for c in rows[0]]
    idx = {
        "date": index_of(h, "purchase-date"),
        "price": index_of(h, "item-price"),
        "status": index_of(h, "order-status"),
        "item_status": index_of(h, "item-status"),
        "qty": index_of(h, "quantity"),
        "shipping": index_of(h, "shipping-price"),
        "giftwrap": index_of(h, "gift-wrap-price"),
        "tax": index_of(h, "item-tax"),
        "channel": index_of(h, "sales-channel"),
        "fulfillment": index_of(h, "fulfillment-channel"),
        "b2b": index_of(h, "is-business-order"),
        "asin": index_of(h, "asin"),
    }

    if idx["date"] is None or idx["price"] is None:
        log("raw_orders に purchase-date / item-price 列がありません。")
        return None

    def cell(row, key):
        col = idx[key]
        if col is None or len(row) <= col:
            return ""
        return str(row[col]).strip()

    result = {
        "by_status": defaultdict(lambda: {"count": 0, "amount": 0.0, "blank_price": 0}),
        "by_channel": defaultdict(lambda: {"count": 0, "amount": 0.0}),
        "by_fulfillment": defaultdict(lambda: {"count": 0, "amount": 0.0}),
        "shipping": 0.0,
        "giftwrap": 0.0,
        "tax": 0.0,
        "b2b_amount": 0.0,
        "total_all": 0.0,
        "dates": set(),
        "asins": set(),
        "rows": 0,
        "has_columns": {k: v is not None for k, v in idx.items()},
    }

    for row in rows[1:]:
        date = cell(row, "date")[:10]
        if not date or date < since or date > until:
            continue

        result["rows"] += 1
        result["dates"].add(date)
        if cell(row, "asin"):
            result["asins"].add(cell(row, "asin"))

        price_text = cell(row, "price")
        price = to_float(price_text)
        status = cell(row, "status") or "(不明)"

        bucket = result["by_status"][status]
        bucket["count"] += 1
        bucket["amount"] += price
        if not price_text:
            bucket["blank_price"] += 1

        channel = cell(row, "channel") or "(不明)"
        result["by_channel"][channel]["count"] += 1
        result["by_channel"][channel]["amount"] += price

        fulfillment = cell(row, "fulfillment") or "(不明)"
        result["by_fulfillment"][fulfillment]["count"] += 1
        result["by_fulfillment"][fulfillment]["amount"] += price

        result["shipping"] += to_float(cell(row, "shipping"))
        result["giftwrap"] += to_float(cell(row, "giftwrap"))
        result["tax"] += to_float(cell(row, "tax"))
        result["total_all"] += price
        if cell(row, "b2b").lower() in ("true", "1", "yes"):
            result["b2b_amount"] += price

    return result


def analyze_sales_traffic(cfg, since, until):
    rows = read_sheet(cfg, cfg.worksheet("WORKSHEET_SALES_TRAFFIC", "raw_sales_traffic"))
    if len(rows) < 2:
        log("raw_sales_traffic が空です。sales_traffic.py を実行してください。")
        return None

    h = [str(c) for c in rows[0]]
    i_date = index_of(h, "date", default=0)
    i_sales = index_of(h, "orderedProductSales")
    i_asin = index_of(h, "childAsin", "parentAsin", default=2)

    total = 0.0
    dates = set()
    asins = set()
    per_day = defaultdict(float)

    for row in rows[1:]:
        date = str(row[i_date])[:10] if len(row) > i_date else ""
        if not date or date < since or date > until:
            continue
        dates.add(date)
        value = to_float(row[i_sales]) if i_sales is not None and len(row) > i_sales else 0.0
        total += value
        per_day[date] += value
        if len(row) > i_asin and str(row[i_asin]).strip():
            asins.add(str(row[i_asin]).strip())

    return {"total": total, "dates": dates, "asins": asins, "per_day": per_day}


def main():
    parser = argparse.ArgumentParser(description="2つの売上の差異を診断します。")
    parser.add_argument("--days", type=int, default=30, help="診断期間（既定: 30日）")
    args = parser.parse_args()

    cfg = Config()
    if not cfg.spreadsheet_id or not cfg.sa_json_path:
        raise SystemExit(
            "環境変数が設定されていません: SPREADSHEET_ID, GOOGLE_SERVICE_ACCOUNT_JSON"
        )

    until = (datetime.now(JST) - timedelta(days=1)).strftime("%Y-%m-%d")
    since = (datetime.now(JST) - timedelta(days=args.days)).strftime("%Y-%m-%d")

    log("=" * 62)
    log(f"売上差異の診断（{since} 〜 {until}）")
    log("=" * 62)

    orders = analyze_orders(cfg, since, until)
    st = analyze_sales_traffic(cfg, since, until)

    if orders is None or st is None:
        return 1

    # --- ① 対象期間のデータ量 ---------------------------------------------
    section("データの範囲")
    log(f"注文レポート     : {orders['rows']:,} 行 / {len(orders['dates'])} 日 / {len(orders['asins'])} ASIN")
    log(f"ASIN別売上       : {len(st['dates'])} 日 / {len(st['asins'])} ASIN")

    missing_days = orders["dates"] - st["dates"]
    if missing_days:
        lost = "、".join(sorted(missing_days)[:10])
        more = f" ほか{len(missing_days) - 10}日" if len(missing_days) > 10 else ""
        log("")
        log(f"⚠ 注文はあるがASIN別売上が無い日が {len(missing_days)} 日あります: {lost}{more}")
        log("  → `python3 sales_traffic.py --days {}` で埋めてください。".format(args.days))
        log("  これが差異の主因である可能性が高いです。")

    # --- ② 注文の状態別内訳 ------------------------------------------------
    section("注文レポートの状態別内訳（item-price 合計）")
    log(f"{'状態':<22}{'件数':>8}{'金額':>16}{'価格が空欄':>12}")
    for status, data in sorted(orders["by_status"].items(), key=lambda x: -x[1]["amount"]):
        blank = f"{data['blank_price']}件" if data["blank_price"] else "-"
        log(f"{status:<22}{data['count']:>8,}{money(data['amount'])}{blank:>12}")

    pending = orders["by_status"].get("Pending", {"count": 0, "blank_price": 0})
    if pending["blank_price"]:
        log("")
        log(f"⚠ 未発送(Pending)で価格が空欄の行が {pending['blank_price']} 件あります。")
        log("  Amazonは決済確定前の金額を出さないため、注文レポート基準の売上が")
        log("  その分だけ小さく出ます。ASIN別売上（公式）には計上されています。")

    cancelled = sum(
        d["amount"] for s, d in orders["by_status"].items()
        if s in ("Cancelled", "Canceled")
    )

    # --- ③ 販路・配送 ------------------------------------------------------
    section("販路別／配送方法別")
    for label, key in (("販路", "by_channel"), ("配送", "by_fulfillment")):
        for name, data in sorted(orders[key].items(), key=lambda x: -x[1]["amount"]):
            log(f"{label} {name:<26}{data['count']:>7,}件{money(data['amount'])}")

    # --- ④ 差額の内訳 ------------------------------------------------------
    section("差額の内訳")

    orders_valid = sum(
        d["amount"] for s, d in orders["by_status"].items()
        if s not in ("Cancelled", "Canceled")
    )
    diff = orders_valid - st["total"]

    log(f"ASIN別集計（Amazon公式）        {money(st['total'])}")
    log(f"注文レポート基準（キャンセル除く）{money(orders_valid)}")
    log(f"{'差額':<32}{money(diff)}")
    log("")
    log("差額を説明しうる要素:")
    log(f"  未発送で価格が空欄        {pending['blank_price']:>6} 件（金額不明・公式には計上済み）")
    log(f"  キャンセル分（除外済み）  {money(cancelled)}")
    log(f"  配送料（未集計）          {money(orders['shipping'])}")
    log(f"  ギフト包装料（未集計）    {money(orders['giftwrap'])}")
    log(f"  消費税（未集計）          {money(orders['tax'])}")
    log(f"  うちB2B注文               {money(orders['b2b_amount'])}")

    if missing_days:
        covered = sum(v for d, v in st["per_day"].items())
        log("")
        log(f"  ⚠ ASIN別売上が {len(missing_days)} 日欠けているため、")
        log(f"     公式側の {money(covered)} は不完全な数字です。")

    # --- ⑤ 結論 ------------------------------------------------------------
    section("診断")
    if missing_days:
        log("最大の原因は【ASIN別売上のデータ欠落】です。")
        log(f"まず `python3 sales_traffic.py --days {args.days}` で埋めてから、")
        log("もう一度この診断を実行してください。")
    elif pending["blank_price"]:
        log("最大の原因は【未発送注文の価格が空欄】であることです。")
        log("これは仕様であり、異常ではありません。")
        log("出荷が進めば注文レポート側の金額も埋まり、差は縮まります。")
    elif abs(diff) > max(orders_valid, st["total"]) * 0.02:
        log("データの欠落や未発送では説明しきれない差が残っています。")
        log("上の販路別・配送方法別の内訳を確認してください。")
        log("マルチチャネル出荷や他販路の注文が混ざっている可能性があります。")
    else:
        log("2つの売上はほぼ一致しています（差は2%以内）。")
        log("残る差は税・配送料の扱いによるもので、正常な範囲です。")

    log("")
    log("※ 対外的な売上としては【ASIN別集計（Amazon公式）】を使ってください。")
    log("  セラーセントラルのビジネスレポートと一致する数字です。")
    log("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
