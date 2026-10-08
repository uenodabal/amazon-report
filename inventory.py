#!/usr/bin/env python3
"""
在庫管理シートを作る（直近N日の販売個数から発注タイミングを計算）

すでに取得済みの raw_orders（SKU→ASIN→商品名）と raw_sales_traffic（日別の
販売個数）を読み込み、商品ごと（エッセンス・ローション・ナイトクリーム）に
直近N日(既定14日)の販売個数・日販平均を集計します。Amazonへの新しい問い合わせは
行わないため、main.py・sales_traffic.py が先に実行済みであることが前提です。

リードタイム(日)・安全在庫(日)・現在庫(個)の3列は、毎回の実行で上書きしません
（シートに人が手入力した値をそのまま保持します）。初回作成時だけ、商品ごとの
既定値（DEFAULT_LEAD_TIME_DAYS / DEFAULT_SAFETY_STOCK_DAYS）で埋めます。
現在庫(個)は常に手入力を前提にしており、スクリプト側では把握できません。

発注の考え方（シンプルな発注点方式）:
    必要在庫（リードタイム+安全在庫分） = 日販平均 × (リードタイム(日) + 安全在庫(日))
    発注推奨数量 = 必要在庫 − 現在庫（0未満なら0）
    発注推奨日   = 今日 + (在庫切れまでの日数 − リードタイム(日) − 安全在庫(日))
                 ＝ 在庫が「安全在庫ぶん」を割り込む直前に、リードタイム分
                    前倒しして発注する目安日

「在庫切れまでの日数」「発注推奨日」「発注推奨数量」「状態」の4列は、Pythonで
計算した値をそのまま書き込むのではなく、スプレッドシート側の数式として書き込みます
（書き込み時にvalue_input_option="USER_ENTERED"を使用）。これにより、スクリプトの
次回実行を待たずに、シート上で「現在庫(個)」を手入力で書き換えた瞬間にこの4列が
自動で再計算されます。日販平均・リードタイム・安全在庫は同じ行の他の列を参照する
だけなので、現在庫だけを更新すれば連動して切り替わります。
（※発注時の警告色（⚠行の背景色）は、見た目の補助のためスクリプト実行時点の値で
　付けており、手入力直後ではなく次回の自動実行時に更新されます。）

注意:
  * リードタイム(日)・安全在庫(日)も、シート上で商品ごとに自由に調整できます。
  * 欠品していた期間があると、その間の販売個数が実際の需要より少なく出るため、
    日販平均が低めに出ます。欠品が続いた商品は少し余裕を見て判断してください。

使い方:
    python3 inventory.py                # 直近14日で集計
    python3 inventory.py --days 30      # 集計期間を変える
    python3 inventory.py --dry-run      # 書き込まずに内容を表示
"""

import argparse
import os
import sys
from datetime import datetime, timedelta

from aggregate import index_of, to_float, unit_cost_for
from common import JST, Config, log, read_sheet, write_rows

SHEET_NAME_DEFAULT = "在庫管理"

# aggregate.py の商品名→原価のキーワード判定を再利用して、同じキーワードで
# 商品名→表示用の商品バケット名に振り分ける（価格判定と商品の分け方を
# 常に一致させるため。キーワード自体は aggregate.PRODUCT_COST_BY_KEYWORDS 参照）。
COST_TO_PRODUCT = {1500: "ナイトクリーム", 1400: "エッセンス", 1300: "ローション"}
PRODUCTS = ["エッセンス", "ローション", "ナイトクリーム"]

# 初回作成時だけ使う既定値（以降はシートの手入力値を保持する）
# エッセンス・ローション: 発注〜入荷まで約3ヶ月、ナイトクリーム: 約4.5ヶ月
DEFAULT_LEAD_TIME_DAYS = {"エッセンス": 90, "ローション": 90, "ナイトクリーム": 135}
DEFAULT_SAFETY_STOCK_DAYS = 25  # 約3.5週間

# 列構成（「直近N日販売個数」のN以外は固定）。
# B列(販売個数)・C列(日販平均)・D列(リードタイム)・E列(安全在庫)・F列(現在庫)を
# この並びで参照する数式を組んでいるため、列の追加・入れ替えをする場合は
# build_rows() 側の数式の列アルファベットも必ず合わせて直すこと。
HEADER_OTHER = [
    "商品", "日販平均", "リードタイム(日)", "安全在庫(日)",
    "現在庫(個)", "在庫切れまでの日数", "発注推奨日", "発注推奨数量", "状態", "最終更新",
]


def header_for(days):
    h = list(HEADER_OTHER)
    h.insert(1, f"直近{days}日販売個数")
    return h


NAVY = {"red": 0.12, "green": 0.22, "blue": 0.39}
WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
EDITABLE_BG = {"red": 1.0, "green": 0.97, "blue": 0.80}
WARN_BG = {"red": 1.0, "green": 0.93, "blue": 0.93}
BAND_BG = {"red": 0.97, "green": 0.98, "blue": 0.99}
BORDER = {"style": "SOLID", "width": 1, "color": {"red": 0.80, "green": 0.84, "blue": 0.89}}


def product_bucket_for(name):
    """商品名からエッセンス/ローション/ナイトクリームのどれかを判定する。該当なければNone。"""
    cost = unit_cost_for(name)
    return COST_TO_PRODUCT.get(cost)


def to_num(value, cast=float):
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    try:
        return cast(text)
    except ValueError:
        return None


def load_asin_names(cfg):
    """raw_orders から ASIN→商品名 の対応を作る（aggregate.Data._load_orders の簡易版）。"""
    rows = read_sheet(cfg, cfg.worksheet("WORKSHEET_ORDERS", "raw_orders"))
    names = {}
    if len(rows) < 2:
        log("  raw_orders が空です。")
        return names
    h = [str(c) for c in rows[0]]
    i_asin = index_of(h, "asin")
    i_name = index_of(h, "product-name")
    if i_asin is None:
        log("  raw_orders に asin 列がありません。")
        return names
    for row in rows[1:]:
        if len(row) <= i_asin:
            continue
        asin = str(row[i_asin]).strip()
        name = str(row[i_name]).strip() if i_name is not None and len(row) > i_name else ""
        if asin and name and asin not in names:
            names[asin] = name
    log(f"  raw_orders: ASIN→商品名 {len(names)} 件")
    return names


def sum_units_by_product(cfg, names, start_date, end_date):
    """raw_sales_traffic から、期間内(両端含む)の商品別販売個数を合計する。"""
    rows = read_sheet(cfg, cfg.worksheet("WORKSHEET_SALES_TRAFFIC", "raw_sales_traffic"))
    units_by_product = {p: 0.0 for p in PRODUCTS}
    if len(rows) < 2:
        log("  raw_sales_traffic が空です。")
        return units_by_product
    h = [str(c) for c in rows[0]]
    i_date = index_of(h, "date", default=0)
    i_asin = index_of(h, "childAsin", "parentAsin", default=2)
    i_units = index_of(h, "unitsOrdered")
    unmatched = set()
    for row in rows[1:]:
        if len(row) <= max(i_date, i_asin):
            continue
        date = str(row[i_date])[:10]
        if not (start_date <= date <= end_date):
            continue
        asin = str(row[i_asin]).strip()
        bucket = product_bucket_for(names.get(asin, ""))
        if not bucket:
            if asin:
                unmatched.add(asin)
            continue
        units = to_float(row[i_units]) if i_units is not None and len(row) > i_units else 0.0
        units_by_product[bucket] += units
    if unmatched:
        log(f"  ※ 商品名が判定できなかったASINが{len(unmatched)}件あります（在庫集計の対象外): "
            + ", ".join(sorted(unmatched)[:5]) + ("…" if len(unmatched) > 5 else ""))
    return units_by_product


def load_preserved(cfg, sheet_name):
    """既存シートから、商品ごとの手入力値（リードタイム・安全在庫・現在庫）を読む。"""
    rows = read_sheet(cfg, sheet_name)
    preserved = {}
    if len(rows) < 2:
        return preserved
    header = [str(c) for c in rows[0]]
    # 「直近N日販売個数」のN（index 1）は実行時の --days で変わり得るので、
    # そこだけ除いて列構成が一致するか確認する。
    header_without_days = header[:1] + header[2:] if len(header) > 1 else header
    if header_without_days != HEADER_OTHER:
        log("  ※ 既存シートの列構成が変わっているため、手入力値は引き継げません（既定値で作成します）。")
        return preserved
    for row in rows[1:]:
        if not row or not str(row[0]).strip():
            continue
        name = str(row[0]).strip()
        preserved[name] = {
            "lead_time": to_num(row[3] if len(row) > 3 else "", cast=int),
            "safety_stock": to_num(row[4] if len(row) > 4 else "", cast=int),
            "current_stock": to_num(row[5] if len(row) > 5 else "", cast=float),
        }
    return preserved


def build_rows(units_by_product, days, preserved, today):
    """
    シートに書き込む行を作る。

    戻り値は (rows, warn_flags)。
    rows の「在庫切れまでの日数」「発注推奨日」「発注推奨数量」「状態」は、
    Python側の計算結果の値ではなく、同じ行の他列（日販平均・リードタイム・
    安全在庫・現在庫）を参照するスプレッドシートの数式文字列にする。
    そのため、シート上で「現在庫(個)」を書き換えると、この4列はスクリプトの
    再実行を待たずに自動で再計算される。

    warn_flags は、書き込み時点（＝現在庫が preserved の値だった時点）で
    「⚠今すぐ発注」に該当するかどうかの目印で、行の警告色付けにのみ使う
    （数式そのものの計算には影響しない）。
    """
    rows = []
    warn_flags = []
    for i, product in enumerate(PRODUCTS):
        r = i + 2  # シート上の行番号（1行目はヘッダー）
        units_window = units_by_product.get(product, 0.0)
        daily_avg = units_window / days if days else 0.0

        saved = preserved.get(product, {})
        lead_time = saved.get("lead_time")
        if lead_time is None:
            lead_time = DEFAULT_LEAD_TIME_DAYS[product]
        safety_stock = saved.get("safety_stock")
        if safety_stock is None:
            safety_stock = DEFAULT_SAFETY_STOCK_DAYS
        current_stock = saved.get("current_stock")
        if current_stock is None:
            current_stock = 0

        # C/D/E/F列（この並びは固定）
        f_days_until = f'=IF(C{r}=0,"",ROUND(F{r}/C{r},1))'
        f_order_by = (
            f'=IF(C{r}=0,"（直近販売実績なし）",'
            f'TEXT(TODAY()+F{r}/C{r}-D{r}-E{r},"yyyy-mm-dd")'
            f'&IF(F{r}/C{r}-D{r}-E{r}<=0,"（超過）",""))'
        )
        f_order_qty = f'=IF(C{r}=0,"",MAX(0,ROUND(C{r}*(D{r}+E{r})-F{r},0)))'
        f_status = (
            f'=IF(C{r}=0,"販売実績なし",'
            f'IF(F{r}/C{r}-D{r}-E{r}<=0,"⚠今すぐ発注",'
            f'"あと"&INT(F{r}/C{r}-D{r}-E{r})&"日で発注目安"))'
        )

        rows.append([
            product,
            int(units_window),
            round(daily_avg, 2),
            lead_time,
            safety_stock,
            current_stock,
            f_days_until,
            f_order_by,
            f_order_qty,
            f_status,
            today.strftime("%Y-%m-%d %H:%M"),
        ])

        # 警告色付け用：数式と同じ条件を、今わかっている値だけでPython側でも判定する。
        if daily_avg > 0:
            order_by_offset = current_stock / daily_avg - lead_time - safety_stock
            warn_flags.append(order_by_offset <= 0)
        else:
            warn_flags.append(False)

    return rows, warn_flags


def inventory_formatter(sheet_id, rows, warn_flags):
    """見出し・入力列の色付け・警告行の色付けをする最小限の書式。"""
    width = len(rows[0])

    requests = [
        {"repeatCell": {
            "range": {"sheetId": sheet_id},
            "cell": {"userEnteredFormat": {}},
            "fields": "userEnteredFormat",
        }},
        {"updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "gridProperties": {"hideGridlines": True}},
            "fields": "gridProperties.hideGridlines",
        }},
        {"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": width},
            "cell": {"userEnteredFormat": {
                "backgroundColor": NAVY,
                "textFormat": {"bold": True, "foregroundColor": WHITE},
                "horizontalAlignment": "CENTER",
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
        }},
        # 手入力列（リードタイム・安全在庫・現在庫）に「編集してください」の色
        {"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": len(rows) + 1,
                      "startColumnIndex": 3, "endColumnIndex": 6},
            "cell": {"userEnteredFormat": {"backgroundColor": EDITABLE_BG}},
            "fields": "userEnteredFormat.backgroundColor",
        }},
        # 最終更新列は日時として読みやすい表示形式に（USER_ENTERED で自動変換されても崩れないように）
        {"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": len(rows) + 1,
                      "startColumnIndex": width - 1, "endColumnIndex": width},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE_TIME", "pattern": "yyyy-mm-dd hh:mm"}}},
            "fields": "userEnteredFormat.numberFormat",
        }},
    ]

    for row_index in range(1, len(rows) + 1):
        is_warn = warn_flags[row_index - 1] if row_index - 1 < len(warn_flags) else False
        if is_warn:
            requests.append({"repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": row_index, "endRowIndex": row_index + 1,
                          "startColumnIndex": 0, "endColumnIndex": width},
                "cell": {"userEnteredFormat": {"backgroundColor": WARN_BG, "textFormat": {"bold": True}}},
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }})
        elif row_index % 2 == 0:
            requests.append({"repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": row_index, "endRowIndex": row_index + 1,
                          "startColumnIndex": 0, "endColumnIndex": width},
                "cell": {"userEnteredFormat": {"backgroundColor": BAND_BG}},
                "fields": "userEnteredFormat.backgroundColor",
            }})

    requests.append({"updateBorders": {
        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": len(rows) + 1,
                  "startColumnIndex": 0, "endColumnIndex": width},
        "innerHorizontal": BORDER, "innerVertical": BORDER,
        "top": BORDER, "bottom": BORDER, "left": BORDER, "right": BORDER,
    }})
    requests.append({"updateDimensionProperties": {
        "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
        "properties": {"pixelSize": 130}, "fields": "pixelSize",
    }})
    requests.append({"updateDimensionProperties": {
        "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 7, "endIndex": 8},
        "properties": {"pixelSize": 150}, "fields": "pixelSize",
    }})
    requests.append({"updateDimensionProperties": {
        "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 9, "endIndex": 10},
        "properties": {"pixelSize": 170}, "fields": "pixelSize",
    }})
    return requests


def main():
    parser = argparse.ArgumentParser(description="在庫管理シートを作成・更新します。")
    parser.add_argument("--days", type=int, default=14, help="集計対象の日数（既定: 14）")
    parser.add_argument("--dry-run", action="store_true", help="書き込まずに内容を表示")
    args = parser.parse_args()

    if args.days < 1:
        raise SystemExit("--days は1以上を指定してください。")

    cfg = Config()
    # このスクリプトはAmazon側へは問い合わせず、既存シートを読むだけなので
    # LWA(Amazon)の認証情報は不要。Googleスプレッドシート側だけ確認する。
    missing = [
        name for name, value in [
            ("GOOGLE_SERVICE_ACCOUNT_JSON", cfg.sa_json_path),
            ("SPREADSHEET_ID", cfg.spreadsheet_id),
        ] if not value
    ]
    if missing:
        raise SystemExit(
            "環境変数が設定されていません: " + ", ".join(missing) + "\n"
            "`source .env` を実行してから再度お試しください。"
        )
    if not os.path.exists(cfg.sa_json_path):
        raise SystemExit(f"サービスアカウントのJSONファイルが見つかりません: {cfg.sa_json_path}")

    sheet_name = cfg.worksheet("WORKSHEET_INVENTORY", SHEET_NAME_DEFAULT)
    today = datetime.now(JST).date()
    end_date = (today - timedelta(days=1)).strftime("%Y-%m-%d")  # 前日まで確定
    start_date = (today - timedelta(days=args.days)).strftime("%Y-%m-%d")

    log("=" * 60)
    log(f"在庫管理シートを更新します（{start_date}〜{end_date} / 直近{args.days}日の販売個数を集計）")
    log("=" * 60)

    try:
        preserved = load_preserved(cfg, sheet_name)
        names = load_asin_names(cfg)
        units_by_product = sum_units_by_product(cfg, names, start_date, end_date)
        rows, warn_flags = build_rows(units_by_product, args.days, preserved, today)
        header = header_for(args.days)

        if args.dry_run:
            log("--dry-run のため、スプレッドシートへの書き込みは行いません。")
            for row in rows:
                log(f"  {row}")
        else:
            formatter = lambda sheet_id, all_rows: inventory_formatter(sheet_id, all_rows, warn_flags)
            write_rows(
                cfg, sheet_name, [header] + rows,
                formatter=formatter, value_input_option="USER_ENTERED",
            )
            log(f"{len(rows)}件を書き込みました。")

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
