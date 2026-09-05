#!/usr/bin/env python3
"""
集計ダッシュボード

蓄積されたrawデータを結合し、商品（ASIN）別の実利益・転換率と、
全体のKPIサマリーを1枚のシートにまとめます。

3つのシートを次のように突き合わせます:
  raw_orders        SKU ↔ ASIN ↔ 商品名 の対応表として使う
  raw_sales_traffic ASIN別の売上・セッション（Amazon公式の数値）
  raw_finances      SKU別の手数料と実入金額 → SKUからASINへ集約

使い方:
    python3 dashboard.py             # 直近30日
    python3 dashboard.py --days 90   # 直近90日
    python3 dashboard.py --dry-run   # 書き込まずに結果を表示

出力シート: dashboard
"""

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta

from common import JST, Config, log, read_sheet, write_rows

PRODUCT_HEADER = [
    "ASIN",
    "商品名",
    "売上",
    "販売個数",
    "セッション",
    "転換率%",
    "手数料合計",
    "実入金額",
    "実入金率%",
    "返品数",
]


def to_float(value):
    """シートから読んだ文字列を数値にする。数値でなければ0。"""
    if value is None:
        return 0.0
    text = str(value).strip().replace(",", "").replace("¥", "").replace("%", "")
    if not text or text in ("-", "—"):
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def index_of(header, *names, default=None):
    """ヘッダーから列位置を探す（候補を順に試す）。"""
    for name in names:
        if name in header:
            return header.index(name)
    return default


def pct(numerator, denominator, digits=2):
    if not denominator:
        return ""
    return round(numerator / denominator * 100, digits)


# ---------------------------------------------------------------------------
# 各シートの読み込み
# ---------------------------------------------------------------------------
def load_sku_map(cfg):
    """raw_orders から SKU → (ASIN, 商品名) の対応表を作る。"""
    rows = read_sheet(cfg, cfg.worksheet("WORKSHEET_ORDERS", "raw_orders"))
    if len(rows) < 2:
        log("  raw_orders が空です。SKUとASINの対応が取れないため、手数料は按分できません。")
        return {}

    header = [str(c) for c in rows[0]]
    i_sku = index_of(header, "sku")
    i_asin = index_of(header, "asin")
    i_name = index_of(header, "product-name")

    if i_sku is None or i_asin is None:
        log("  raw_orders に sku / asin 列が見つかりません。")
        return {}

    mapping = {}
    for row in rows[1:]:
        if len(row) <= max(i_sku, i_asin):
            continue
        sku, asin = str(row[i_sku]).strip(), str(row[i_asin]).strip()
        if not sku or not asin:
            continue
        name = str(row[i_name]).strip() if i_name is not None and len(row) > i_name else ""
        # 後から出てきた商品名のほうが新しいので上書きする
        mapping[sku] = (asin, name or mapping.get(sku, ("", ""))[1])
    log(f"  SKU→ASIN 対応: {len(mapping)} 件")
    return mapping


def load_orders_sales(cfg, since, until):
    """
    raw_orders から売上を独立に集計する（Data Kioskの数値との照合用）。
    2つの数字が大きく食い違っていれば、どちらかにデータ欠落がある。
    """
    rows = read_sheet(cfg, cfg.worksheet("WORKSHEET_ORDERS", "raw_orders"))
    if len(rows) < 2:
        return 0.0, set()

    header = [str(c) for c in rows[0]]
    i_date = index_of(header, "purchase-date")
    i_price = index_of(header, "item-price")
    i_status = index_of(header, "order-status")
    if i_date is None or i_price is None:
        return 0.0, set()

    total = 0.0
    days = set()
    for row in rows[1:]:
        if len(row) <= max(i_date, i_price):
            continue
        date = str(row[i_date])[:10]
        if not date or date < since or date > until:
            continue
        if i_status is not None and len(row) > i_status:
            if str(row[i_status]).strip() in ("Cancelled", "Canceled"):
                continue
        days.add(date)
        total += to_float(row[i_price])
    return total, days


def load_sales_traffic(cfg, since):
    """raw_sales_traffic を ASIN 単位に集計する。"""
    rows = read_sheet(cfg, cfg.worksheet("WORKSHEET_SALES_TRAFFIC", "raw_sales_traffic"))
    if len(rows) < 2:
        log("  raw_sales_traffic が空です。")
        return {}, {}

    header = [str(c) for c in rows[0]]
    idx = {
        "date": index_of(header, "date", default=0),
        "asin": index_of(header, "childAsin", "parentAsin", default=2),
        "sales": index_of(header, "orderedProductSales"),
        "units": index_of(header, "unitsOrdered"),
        "sessions": index_of(header, "sessions"),
        "refunded": index_of(header, "unitsRefunded"),
    }

    per_asin = defaultdict(lambda: defaultdict(float))
    per_day = defaultdict(lambda: defaultdict(float))

    for row in rows[1:]:
        date = str(row[idx["date"]])[:10] if len(row) > idx["date"] else ""
        if not date or date < since:
            continue
        asin = str(row[idx["asin"]]).strip() if len(row) > idx["asin"] else ""
        if not asin:
            continue

        for key in ("sales", "units", "sessions", "refunded"):
            col = idx[key]
            value = to_float(row[col]) if col is not None and len(row) > col else 0.0
            per_asin[asin][key] += value
            per_day[date][key] += value

    log(f"  売上・トラフィック: {len(per_asin)} ASIN / {len(per_day)} 日分")
    return per_asin, per_day


def load_finances(cfg, since, sku_map):
    """raw_finances を ASIN 単位に集計する（SKUからASINへ変換）。"""
    rows = read_sheet(cfg, cfg.worksheet("WORKSHEET_FINANCES", "raw_finances"))
    if len(rows) < 2:
        log("  raw_finances が空です。")
        return {}, {
            "fees": 0.0, "net": 0.0, "unmapped": 0.0, "principal": 0.0,
            "tax_shipping": 0.0, "promo": 0.0, "referral": 0.0,
            "fba": 0.0, "other_fees": 0.0,
        }, set()

    header = [str(c) for c in rows[0]]
    idx = {
        "date": index_of(header, "postedDate", default=0),
        "sku": index_of(header, "sku", default=3),
        "referral": index_of(header, "販売手数料"),
        "fba": index_of(header, "FBA手数料"),
        "other": index_of(header, "その他手数料"),
        "net": index_of(header, "純額"),
        "principal": index_of(header, "商品代金"),
        "tax": index_of(header, "税"),
        "shipping": index_of(header, "配送料"),
        "other_sales": index_of(header, "その他売上"),
        "promo": index_of(header, "プロモーション値引"),
    }

    per_asin = defaultdict(lambda: defaultdict(float))
    totals = {
        "fees": 0.0, "net": 0.0, "unmapped": 0.0,
        # 「売上→実入金」の橋渡しに使う内訳
        "principal": 0.0, "tax_shipping": 0.0, "promo": 0.0,
        "referral": 0.0, "fba": 0.0, "other_fees": 0.0,
    }
    days = set()

    for row in rows[1:]:
        date = str(row[idx["date"]])[:10] if len(row) > idx["date"] else ""
        if not date or date < since:
            continue
        days.add(date)

        def value(key):
            col = idx[key]
            return to_float(row[col]) if col is not None and len(row) > col else 0.0

        fees = value("referral") + value("fba") + value("other")
        net = value("net")
        totals["fees"] += fees
        totals["net"] += net
        totals["principal"] += value("principal")
        totals["tax_shipping"] += value("tax") + value("shipping") + value("other_sales")
        totals["promo"] += value("promo")
        totals["referral"] += value("referral")
        totals["fba"] += value("fba")
        totals["other_fees"] += value("other")

        sku = str(row[idx["sku"]]).strip() if len(row) > idx["sku"] else ""
        asin = sku_map.get(sku, ("", ""))[0]
        if not asin:
            # 保管料など注文に紐づかない費用、または対応表に無いSKU
            totals["unmapped"] += net
            continue

        per_asin[asin]["fees"] += fees
        per_asin[asin]["net"] += net

    log(f"  手数料・入金: {len(per_asin)} ASIN / {len(days)} 日分")
    if totals["unmapped"]:
        log(f"  （商品に紐づかない費用: {totals['unmapped']:,.0f}）")
    return per_asin, totals, days


# ---------------------------------------------------------------------------
# 集計
# ---------------------------------------------------------------------------
def build_product_table(st_by_asin, fin_by_asin, sku_map):
    """ASIN別の一覧を作る。売上の大きい順。"""
    names = {}
    for asin, name in sku_map.values():
        if asin and name and asin not in names:
            names[asin] = name

    rows = []
    for asin in set(st_by_asin) | set(fin_by_asin):
        st = st_by_asin.get(asin, {})
        fin = fin_by_asin.get(asin, {})
        sales = st.get("sales", 0.0)
        units = st.get("units", 0.0)
        sessions = st.get("sessions", 0.0)
        fees = fin.get("fees", 0.0)
        net = fin.get("net", 0.0)

        rows.append([
            asin,
            names.get(asin, ""),
            round(sales),
            int(units),
            int(sessions),
            pct(units, sessions),
            round(fees),
            round(net),
            pct(net, sales) if sales else "",
            int(st.get("refunded", 0.0)),
        ])

    rows.sort(key=lambda r: r[2], reverse=True)
    return rows


def coverage_note(label, have_days, want_days, hint):
    """データが揃っている日数を明示する。欠けていれば警告文を返す。"""
    if have_days >= want_days:
        return [f"{label}のデータ", f"{have_days}/{want_days}日 ✅ 揃っています"], None
    warning = (
        f"⚠ {label}は {want_days}日中 {have_days}日分しかありません"
        f"（集計値は実際より少なく出ます）。{hint}"
    )
    return [f"{label}のデータ", f"{have_days}/{want_days}日 ⚠ 不足"], warning


def build_bridge(total_sales, t):
    """
    売上（注文ベース）から実入金額（出荷ベース）までを、
    加減算が追える形で並べる。
    """
    if not t["net"] and not t["principal"]:
        return [
            ["■ 売上から実入金までの内訳"],
            ["手数料データがまだありません。finances.py を実行してください。"],
            [],
        ]

    timing_gap = t["principal"] - total_sales

    rows = [
        ["■ 売上から実入金までの内訳"],
        ["※ 売上は「注文日」基準、入金は「出荷日」基準です。この差が下の「出荷タイミングのズレ」です。"],
        [],
        ["売上（Amazon公式・注文ベース）", round(total_sales)],
        ["  出荷タイミングのズレ", round(timing_gap)],
        ["商品代金（入金ベース）", round(t["principal"])],
        ["＋ 税・配送料・その他売上", round(t["tax_shipping"])],
        ["− プロモーション値引", round(t["promo"])],
        ["− 販売手数料", round(t["referral"])],
        ["− FBA手数料", round(t["fba"])],
        ["− その他手数料", round(t["other_fees"])],
        ["＝ 実入金額（純額）", round(t["net"])],
        [],
        ["  うち商品に紐づかない費用（保管料など）", round(t["unmapped"])],
    ]

    # 検算：内訳の合計が純額と一致するか
    calculated = (
        t["principal"] + t["tax_shipping"] + t["promo"]
        + t["referral"] + t["fba"] + t["other_fees"]
    )
    if abs(calculated - t["net"]) > 1:
        rows.append(["⚠ 内訳の合計と純額が一致しません", round(calculated - t["net"])])

    rows.append([])
    return rows


def build_summary(st_by_day, fin_totals, since, until, days, orders_sales, fin_days):
    total_sales = sum(d.get("sales", 0.0) for d in st_by_day.values())
    total_units = sum(d.get("units", 0.0) for d in st_by_day.values())
    total_sessions = sum(d.get("sessions", 0.0) for d in st_by_day.values())
    total_refunded = sum(d.get("refunded", 0.0) for d in st_by_day.values())

    st_row, st_warn = coverage_note(
        "ASIN別売上", len(st_by_day), days,
        "`python3 sales_traffic.py --days {}` で埋められます。".format(days),
    )
    fin_row, fin_warn = coverage_note(
        "手数料・入金", len(fin_days), days,
        "`python3 finances.py --days {}` で埋められます。".format(days),
    )

    warnings = [w for w in (st_warn, fin_warn) if w]

    rows = [
        ["集計期間", f"{since} 〜 {until}（{days}日間）"],
        ["更新日時", datetime.now(JST).strftime("%Y-%m-%d %H:%M")],
        [],
        ["■ データの充足状況"],
        st_row,
        fin_row,
    ]
    for w in warnings:
        rows.append([w])
    rows.append([])

    rows += [
        ["■ 売上の照合"],
        ["売上（ASIN別集計・Amazon公式）", round(total_sales)],
        ["売上（注文レポート基準・参考）", round(orders_sales)],
    ]
    if orders_sales and total_sales:
        diff = orders_sales - total_sales
        rows.append(["差額", round(diff)])
        if abs(diff) > orders_sales * 0.1:
            rows.append([
                "⚠ 2つの売上が10%以上ずれています。"
                "ASIN別売上のデータが不足している可能性が高いです。"
            ])
    elif orders_sales and not total_sales:
        rows.append([
            "⚠ ASIN別売上が0です。sales_traffic.py をまだ実行していないか、"
            "対象期間のデータが未取得です。"
        ])
    rows.append([])

    rows += build_bridge(total_sales, fin_totals)

    rows += [
        ["■ 全体サマリー"],
        ["売上", round(total_sales)],
        ["販売個数", int(total_units)],
        ["セッション数", int(total_sessions)],
        ["転換率%", pct(total_units, total_sessions)],
        ["返品数", int(total_refunded)],
        ["返品率%", pct(total_refunded, total_units)],
        [],
        ["手数料合計", round(fin_totals["fees"])],
        ["実入金額（純額）", round(fin_totals["net"])],
        ["実入金率%", pct(fin_totals["net"], total_sales) if total_sales else ""],
        ["  ※ 実入金額 ≠ 売上−手数料。内訳は上の「売上から実入金までの内訳」を参照"],
        [],
        ["1セッションあたり売上", round(total_sales / total_sessions, 1) if total_sessions else ""],
        ["平均単価", round(total_sales / total_units) if total_units else ""],
        [],
    ]
    return rows, warnings


def main():
    parser = argparse.ArgumentParser(description="rawデータを集計してダッシュボードを作ります。")
    parser.add_argument("--days", type=int, default=30, help="集計期間（既定: 30日）")
    parser.add_argument("--dry-run", action="store_true", help="書き込まずに結果を表示")
    args = parser.parse_args()

    if args.days < 1:
        raise SystemExit("--days は1以上を指定してください。")

    cfg = Config()
    # このスクリプトはAmazon APIを呼ばないため、スプレッドシートの設定だけ確認する
    if not cfg.spreadsheet_id or not cfg.sa_json_path:
        raise SystemExit(
            "環境変数が設定されていません: SPREADSHEET_ID, GOOGLE_SERVICE_ACCOUNT_JSON\n"
            "`source .env` を実行してから再度お試しください。"
        )

    until = (datetime.now(JST) - timedelta(days=1)).strftime("%Y-%m-%d")
    since = (datetime.now(JST) - timedelta(days=args.days)).strftime("%Y-%m-%d")

    log("=" * 60)
    log(f"ダッシュボードを作成します（{since} 〜 {until}）")
    log("=" * 60)

    try:
        log("rawデータを読み込んでいます…")
        sku_map = load_sku_map(cfg)
        st_by_asin, st_by_day = load_sales_traffic(cfg, since)
        fin_by_asin, fin_totals, fin_days = load_finances(cfg, since, sku_map)
        orders_sales, orders_days = load_orders_sales(cfg, since, until)
        log(f"  注文レポート基準の売上: {orders_sales:,.0f}（{len(orders_days)} 日分）")

        if not st_by_asin and not fin_by_asin:
            log("")
            log("集計できるデータがありませんでした。")
            log("先に main.py / sales_traffic.py / finances.py を実行してください。")
            return 1

        summary, warnings = build_summary(
            st_by_day, fin_totals, since, until, args.days, orders_sales, fin_days
        )
        products = build_product_table(st_by_asin, fin_by_asin, sku_map)

        sheet_rows = summary + [["■ 商品別（売上の大きい順）"], PRODUCT_HEADER] + products

        log("")
        for line in summary:
            if len(line) == 2:
                log(f"  {line[0]}: {line[1]}")
        log(f"  商品数: {len(products)}")

        if warnings:
            log("")
            log("─" * 60)
            for w in warnings:
                log(w)
            log("─" * 60)

        if args.dry_run:
            log("")
            log("--dry-run のため書き込みません。上位5商品:")
            for row in products[:5]:
                log(f"  {row}")
        else:
            # 派生データなので毎回作り直す
            write_rows(cfg, cfg.worksheet("WORKSHEET_DASHBOARD", "dashboard"), sheet_rows)

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
