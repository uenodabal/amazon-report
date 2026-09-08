#!/usr/bin/env python3
"""
月別シートと月次推移シートを作成する

  ・月別シート（例「2026-08」）
      ① 月間サマリー（前月比つき、原価・広告費を含む）
      ② 広告サマリー
      ③ 手数料の内訳
      ④ 売上変動の要因
      ⑤ データの充足状況
      ⑥ 日別の推移（広告・原価・利益額つき）
      ⑦ 商品別ランキング（原価・粗利率つき）
  ・月次推移シート（「月次推移」）
      月ごとの主要指標を1行ずつ並べたもの

蓄積されたrawシートを読むだけで、Amazon APIは呼びません。

原価は商品名から自動判定します（aggregate.py の PRODUCT_COST_BY_KEYWORDS）。
広告費（raw_ads_email）は税抜きで記録されているため、読み込み時に自動で
税込（×1.1）に換算しています（aggregate.py 参照）。以降このファイルで
扱う広告費・ROAS・ACOSなどはすべて税込ベースです。

使い方:
    python3 monthly.py               # 直近3ヶ月
    python3 monthly.py --months 6    # 直近6ヶ月
    python3 monthly.py --dry-run     # 書き込まずに内容を表示
"""

import argparse
import sys
from datetime import datetime

from aggregate import Data, delta_pct, pct
from common import JST, Config, log, write_rows
from sheet_format import build_requests

TREND_SHEET = "月次推移"

SUMMARY_HEADER = ["指標", "今月", "前月", "増減", "増減率%"]
DAILY_HEADER = [
    "日付", "売上", "広告費", "販売個数", "広告での販売個数",
    "セッション", "広告セッション", "転換率%", "広告転換率%",
    "手数料", "実入金額", "原価金額", "利益額",
]
PRODUCT_HEADER = [
    "順位", "ASIN", "商品名", "売上", "前月比%", "販売個数",
    "セッション", "転換率%", "手数料", "実入金額", "原価合計",
    "実入金率%", "原価率%", "Amazon粗利率%", "返品数",
]
TREND_HEADER = [
    "月", "売上", "前月比%", "販売個数", "セッション", "転換率%",
    "手数料", "実入金額", "実入金率%", "返品数", "取扱ASIN数", "データ充足",
]


def prev_month(month):
    """"2026-08" → "2026-07" """
    year, mon = int(month[:4]), int(month[5:7])
    return f"{year - 1}-12" if mon == 1 else f"{year}-{mon - 1:02d}"


def days_in_month(month):
    year, mon = int(month[:4]), int(month[5:7])
    if mon == 12:
        return 31
    from calendar import monthrange
    return monthrange(year, mon)[1]


def current_month():
    """今日が属する月。着地予想を出すかどうかの判定に使う。"""
    return datetime.now(JST).strftime("%Y-%m")


def arrow(value):
    if value == "" or value is None:
        return ""
    if value > 0:
        return "▲"
    if value < 0:
        return "▼"
    return "→"


def _line(c, p, label, key):
    """金額・件数は整数で表示する（小数点は読みにくいため）。"""
    cv, pv = int(round(c[key])), int(round(p[key]))
    diff = cv - pv
    return [label, cv, pv,
            f"{arrow(diff)} {diff:+,}" if diff else "→ 0",
            delta_pct(c[key], p[key])]


def _ratio_line(label, cv, pv, digits=2):
    cv, pv = round(cv, digits), round(pv, digits)
    diff = round(cv - pv, digits)
    return [label, cv, pv,
            f"{arrow(diff)} {diff:+}" if diff else "→ 0",
            delta_pct(cv, pv)]


def _forecast_line(label, value, days, expected_days, previous, is_current):
    """
    1日あたりの平均 × その月の日数。

    分母は「データのある日数」。月初からの経過日数で割ると、
    取得漏れの日まで分母に入って予想が実態より低く出るため。
    前月列には前月の確定実績を置き、「このペースなら前月比◯%」を読めるようにする。
    """
    if not is_current or not days:
        return None
    estimated = value / days * expected_days
    cv, pv = int(round(estimated)), int(round(previous))
    diff = cv - pv
    return [label, cv, pv,
            f"{arrow(diff)} {diff:+,}" if diff else "→ 0",
            delta_pct(estimated, previous)]


# ---------------------------------------------------------------------------
# ① 月間サマリー
# ---------------------------------------------------------------------------
def build_summary(month, cur, prv):
    c, p = cur["total"], prv["total"]

    def line(label, key):
        return _line(c, p, label, key)

    def ratio_line(label, cv, pv, digits=2):
        return _ratio_line(label, cv, pv, digits)

    conv_cur, conv_prv = pct(c["units"], c["sessions"]), pct(p["units"], p["sessions"])
    rate_cur, rate_prv = pct(c["net"], c["sales"]), pct(p["net"], p["sales"])
    cogs_rate_cur, cogs_rate_prv = pct(c["cogs"], c["sales"]), pct(p["cogs"], p["sales"])
    ads_rate_cur, ads_rate_prv = pct(c["ads_cost"], c["sales"]), pct(p["ads_cost"], p["sales"])

    # 1日あたりの平均は「データがある日数」で割る。
    # 月の日数で割ると、月途中や取得漏れのある月が実態より低く出るため。
    daily_cur = c["units"] / c["traffic_days"] if c["traffic_days"] else 0.0
    daily_prv = p["units"] / p["traffic_days"] if p["traffic_days"] else 0.0

    price_cur = c["sales"] / c["units"] if c["units"] else 0.0
    price_prv = p["sales"] / p["units"] if p["units"] else 0.0

    # ---- 着地予想 ----------------------------------------------------------
    # 進行中の月だけ出す。終わった月は確定値なので、予想を並べても意味がない。
    expected = days_in_month(month)
    is_current = month == current_month()

    def forecast_line(label, value, days, previous):
        return _forecast_line(label, value, days, expected, previous, is_current)

    heading = ["■ 月間サマリー"]
    if is_current:
        heading.append(f"経過日数 {c['traffic_days']}/{expected}日")

    rows = [
        heading,
        SUMMARY_HEADER,
        line("売上", "sales"),
        forecast_line("着地予想 売上", c["sales"], c["traffic_days"], p["sales"]),
        line("販売個数", "units"),
        ratio_line("1日あたり販売個数", daily_cur, daily_prv, 1),
        forecast_line("着地予想 販売個数", c["units"], c["traffic_days"], p["units"]),
        ratio_line("平均単価", round(price_cur), round(price_prv), 0),
        line("セッション", "sessions"),
        ratio_line("転換率%", conv_cur, conv_prv),
        line("返品数", "refunded"),
        line("実入金額", "net"),
        ratio_line("実入金率%", rate_cur, rate_prv),
        line("原価合計", "cogs"),
        ratio_line("原価比率%", cogs_rate_cur, cogs_rate_prv),
        line("広告費合計", "ads_cost"),
        ratio_line("広告費比率%", ads_rate_cur, ads_rate_prv),
        forecast_line("着地予想 実入金額", c["net"], c["finance_days"], p["net"]),
        [],
    ]
    rows = [row for row in rows if row is not None]

    if is_current:
        rows.insert(
            len(rows) - 1,
            ["※ 着地予想は「データのある日数の平均 × その月の日数」。"
             "前月列は前月の確定実績です。広告費は税込（×1.1）で計算しています。"],
        )

    return rows


# ---------------------------------------------------------------------------
# ③ 手数料の内訳
# ---------------------------------------------------------------------------
def build_fee_breakdown(cur, prv):
    c, p = cur["total"], prv["total"]

    def line(label, key):
        return _line(c, p, label, key)

    def ratio_line(label, cv, pv, digits=2):
        return _ratio_line(label, cv, pv, digits)

    return [
        ["■ 手数料の内訳"],
        SUMMARY_HEADER,
        line("販売手数料（紹介料）", "referral"),
        line("FBA手数料（配送・保管）", "fba"),
        line("その他手数料", "other_fees"),
        line("プロモーション値引", "promo"),
        line("手数料合計", "fees"),
        ratio_line("対売上比%", pct(abs(c["fees"]), c["sales"]),
                   pct(abs(p["fees"]), p["sales"])),
        [],
        ["※ いずれもマイナス表示。「対売上比%」は手数料合計の絶対値を売上で割った値です。"],
        ["※ 保管料など特定商品に紐づかない費用も、その他手数料またはFBA手数料に含まれます。"],
        [],
    ]


# ---------------------------------------------------------------------------
# ④ 売上変動の要因（前月＝100の指数）
# ---------------------------------------------------------------------------
def build_sales_factors(cur, prv):
    # 売上 ＝ セッション × 転換率 × 平均単価 という恒等式が成り立つため、
    # 「今月÷前月」の指数にすると、要因が正確に掛け算で分解できる。
    # 増減率（±%）だと足し引きが合わず、どれが効いたのか読み取りにくい。
    c, p = cur["total"], prv["total"]
    rows = []
    if not (p["sales"] and p["sessions"] and p["units"]):
        return rows

    conv_cur, conv_prv = pct(c["units"], c["sessions"]), pct(p["units"], p["sessions"])
    price_cur = c["sales"] / c["units"] if c["units"] else 0.0
    price_prv = p["sales"] / p["units"] if p["units"] else 0.0

    def index_of(current, previous):
        return round(current / previous * 100, 1) if previous else ""

    sales_i = index_of(c["sales"], p["sales"])
    session_i = index_of(c["sessions"], p["sessions"])
    conv_i = index_of(conv_cur, conv_prv)
    price_i = index_of(price_cur, price_prv)

    rows += [
        ["■ 売上変動の要因（前月＝100）"],
        ["売上指数", sales_i],
        ["　うち セッション指数", session_i],
        ["　うち 転換率指数", conv_i],
        ["　うち 平均単価指数", price_i],
    ]

    if all(isinstance(v, float) for v in (sales_i, session_i, conv_i, price_i)):
        calculated = round(session_i * conv_i * price_i / 10000, 1)
        rows.append(["検算（3指数の積）", calculated])
        if abs(calculated - sales_i) > max(sales_i * 0.02, 1):
            rows.append(["⚠ 検算が売上指数と一致しません。データに欠落がある可能性があります。"])

        # 100からの離れ具合で主因を判定する
        gaps = {
            "セッション": abs(session_i - 100),
            "転換率": abs(conv_i - 100),
            "平均単価": abs(price_i - 100),
        }
        top = max(gaps, key=gaps.get)
        second = sorted(gaps, key=gaps.get, reverse=True)[1]
        hints = {
            "セッション": "広告・検索順位・在庫切れを確認",
            "転換率": "価格・レビュー・画像・カート獲得を確認",
            "平均単価": "値引き・クーポン・商品構成の変化を確認",
        }
        if gaps[top] >= gaps[second] * 1.5:
            rows.append(["主因", f"{top}の変動（{hints[top]}）"])
        else:
            rows.append(["主因", f"{top}と{second}の両方"])

    rows.append(["※ 100を超えれば前月より増、下回れば減。"
                 "3つの指数を掛けると売上指数になります（÷10000）。"])
    rows.append([])
    return rows


# ---------------------------------------------------------------------------
# ⑤ データの充足状況
# ---------------------------------------------------------------------------
def build_data_completeness(month, cur):
    c = cur["total"]
    expected = days_in_month(month)

    rows = [["■ データの充足状況"]]
    rows.append(["ASIN別売上", f"{c['traffic_days']}/{expected}日",
                 "✅" if c["traffic_days"] >= expected else "⚠ 不足"])
    rows.append(["手数料・入金", f"{c['finance_days']}/{expected}日",
                 "✅" if c["finance_days"] >= expected else "⚠ 不足"])
    if c["traffic_days"] < expected:
        rows.append([f"⚠ 売上データが{expected - c['traffic_days']}日分足りません。"
                     f"集計値は実際より少なく出ます。"])
    rows.append([])
    return rows


# ---------------------------------------------------------------------------
# ② 広告サマリー（月間サマリーと手数料の内訳の間に配置）
# ---------------------------------------------------------------------------
def build_ads_summary(month, cur, prv):
    c, p = cur["total"], prv["total"]
    expected = days_in_month(month)
    is_current = month == current_month()

    def line(label, key):
        return _line(c, p, label, key)

    def ratio_line(label, cv, pv, digits=2):
        return _ratio_line(label, cv, pv, digits)

    def forecast_line(label, value, days, previous):
        return _forecast_line(label, value, days, expected, previous, is_current)

    roas_cur = round(c["ads_sales"] / c["ads_cost"], 2) if c["ads_cost"] else 0.0
    roas_prv = round(p["ads_sales"] / p["ads_cost"], 2) if p["ads_cost"] else 0.0
    acos_cur, acos_prv = pct(c["ads_cost"], c["ads_sales"]), pct(p["ads_cost"], p["ads_sales"])
    price_cur = c["ads_sales"] / c["ads_units"] if c["ads_units"] else 0.0
    price_prv = p["ads_sales"] / p["ads_units"] if p["ads_units"] else 0.0
    conv_cur, conv_prv = pct(c["ads_units"], c["ads_clicks"]), pct(p["ads_units"], p["ads_clicks"])
    share_cur, share_prv = pct(c["ads_cost"], c["sales"]), pct(p["ads_cost"], p["sales"])
    cpa_cur = c["ads_cost"] / c["ads_units"] if c["ads_units"] else 0.0
    cpa_prv = p["ads_cost"] / p["ads_units"] if p["ads_units"] else 0.0

    rows = [
        ["■ 広告サマリー"],
        SUMMARY_HEADER,
        line("広告費合計", "ads_cost"),
        forecast_line("着地予想 広告費", c["ads_cost"], c["ads_days"], p["ads_cost"]),
        line("広告経由売上", "ads_sales"),
        ratio_line("ROAS", roas_cur, roas_prv),
        ratio_line("ACOS%", acos_cur, acos_prv),
        line("販売個数（広告経由）", "ads_units"),
        forecast_line("着地予想 販売個数（広告経由）", c["ads_units"], c["ads_days"], p["ads_units"]),
        ratio_line("平均単価（広告経由）", round(price_cur), round(price_prv), 0),
        ratio_line("CPA（広告費÷販売個数）", round(cpa_cur), round(cpa_prv), 0),
        line("セッション（広告）", "ads_clicks"),
        ratio_line("転換率%（広告）", conv_cur, conv_prv),
        ratio_line("広告費の対全体売上比%", share_cur, share_prv),
    ]
    rows = [row for row in rows if row is not None]

    if not c["ads_days"] and not p["ads_days"]:
        rows.append(["※ この月の広告データがまだありません"
                     "（ads_email_report.py の稼働開始前、または未取得の月）。"])
    elif is_current:
        rows.append(["※ 着地予想は「データのある日数の平均 × その月の日数」。"
                     "広告費は税込（×1.1）で計算しています。"])
    rows.append([])
    return rows


# ---------------------------------------------------------------------------
# ⑥ 日別の推移
# ---------------------------------------------------------------------------
def build_daily(cur):
    rows = [["■ 日別の推移"], DAILY_HEADER]
    by_day = cur["by_day"]
    for date in sorted(by_day):
        d = by_day[date]
        net = d.get("net", 0)
        ads_cost = d.get("ads_cost", 0)
        cogs = d.get("cogs", 0)
        rows.append([
            date,
            round(d.get("sales", 0)),
            round(ads_cost),
            int(d.get("units", 0)),
            int(d.get("ads_units", 0)),
            int(d.get("sessions", 0)),
            int(d.get("ads_clicks", 0)),
            pct(d.get("units", 0), d.get("sessions", 0)),
            pct(d.get("ads_units", 0), d.get("ads_clicks", 0)),
            round(d.get("fees", 0)),
            round(net),
            round(cogs),
            round(net - ads_cost - cogs),
        ])

    t = cur["total"]
    t_net, t_ads_cost, t_cogs = t["net"], t["ads_cost"], t["cogs"]
    rows.append([
        "合計", round(t["sales"]), round(t_ads_cost), int(t["units"]), int(t["ads_units"]),
        int(t["sessions"]), int(t["ads_clicks"]),
        pct(t["units"], t["sessions"]), pct(t["ads_units"], t["ads_clicks"]),
        round(t["fees"]), round(t_net), round(t_cogs),
        round(t_net - t_ads_cost - t_cogs),
    ])
    rows.append([])
    rows.append(["※ 利益額 ＝ 実入金額 － 広告費 － 原価。広告費は税込（×1.1）で計算しています。"])
    rows.append([])
    return rows


# ---------------------------------------------------------------------------
# ⑦ 商品別ランキング
# ---------------------------------------------------------------------------
def build_products(cur, prv, names):
    prev_sales = {asin: v.get("sales", 0.0) for asin, v in prv["by_asin"].items()}

    items = []
    for asin, v in cur["by_asin"].items():
        items.append((asin, v))
    items.sort(key=lambda x: x[1].get("sales", 0.0), reverse=True)

    rows = [["■ 商品別ランキング（売上順）"], PRODUCT_HEADER]
    for rank, (asin, v) in enumerate(items, start=1):
        sales = v.get("sales", 0.0)
        net = v.get("net", 0.0)
        cogs = v.get("cogs", 0.0)
        rows.append([
            rank,
            asin,
            names.get(asin, ""),
            round(sales),
            delta_pct(sales, prev_sales.get(asin, 0.0)),
            int(v.get("units", 0)),
            int(v.get("sessions", 0)),
            pct(v.get("units", 0), v.get("sessions", 0)),
            round(v.get("fees", 0)),
            round(net),
            round(cogs),
            pct(net, sales) if sales else "",
            pct(cogs, sales) if sales else "",
            pct(net - cogs, sales) if sales else "",
            int(v.get("refunded", 0)),
        ])

    if cur["total"]["unmapped"]:
        rows.append([])
        rows.append(["商品に紐づかない費用（保管料など）", round(cur["total"]["unmapped"])])
    rows.append([])
    rows.append(["※ 原価は商品名から自動判定（エッセンス/美容液=1,400円、"
                 "クリアローション/化粧水=1,300円、ナイトクリーム=1,500円/個）。"
                 "Amazon粗利率＝（実入金額－原価合計）÷売上。"])
    rows.append([])
    return rows


def build_month_sheet(month, cur, prv, names):
    year, mon = month[:4], int(month[5:7])
    return (
        [
            [f"{year}年{mon}月の実績"],
            ["更新", datetime.now(JST).strftime("%Y-%m-%d %H:%M")],
            [],
        ]
        + build_summary(month, cur, prv)
        + build_ads_summary(month, cur, prv)
        + build_fee_breakdown(cur, prv)
        + build_sales_factors(cur, prv)
        + build_data_completeness(month, cur)
        + build_daily(cur)
        + build_products(cur, prv, names)
    )


# ---------------------------------------------------------------------------
# 月次推移シート
# ---------------------------------------------------------------------------
def build_trend(data, months):
    rows = [
        ["月次推移"],
        ["更新", datetime.now(JST).strftime("%Y-%m-%d %H:%M")],
        [],
        TREND_HEADER,
    ]
    previous_sales = None
    for month in months:  # 古い順
        t = data.summarize(month)["total"]
        expected = days_in_month(month)
        rows.append([
            month,
            round(t["sales"]),
            delta_pct(t["sales"], previous_sales) if previous_sales is not None else "",
            int(t["units"]),
            int(t["sessions"]),
            pct(t["units"], t["sessions"]),
            round(t["fees"]),
            round(t["net"]),
            pct(t["net"], t["sales"]) if t["sales"] else "",
            int(t["refunded"]),
            t["asins"],
            f"{t['traffic_days']}/{expected}日"
            + ("" if t["traffic_days"] >= expected else " ⚠"),
        ])
        previous_sales = t["sales"]

    rows.append([])
    rows.append(["※ 「データ充足」に ⚠ が付く月は、集計値が実際より少なく出ています。"])
    rows.append(["   sales_traffic.py で該当月を取得すると埋まります。"])
    return rows


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="月別シートと月次推移シートを作成します。")
    parser.add_argument("--months", type=int, default=3, help="作成する月数（既定: 3）")
    parser.add_argument("--dry-run", action="store_true", help="書き込まずに内容を表示")
    args = parser.parse_args()

    cfg = Config()
    if not cfg.spreadsheet_id or not cfg.sa_json_path:
        raise SystemExit(
            "環境変数が設定されていません: SPREADSHEET_ID, GOOGLE_SERVICE_ACCOUNT_JSON"
        )

    log("=" * 60)
    log(f"月別シートを作成します（直近 {args.months} ヶ月）")
    log("=" * 60)

    try:
        log("rawデータを読み込んでいます…")
        data = Data(cfg)

        available = data.months_available()
        if not available:
            log("")
            log("集計できるデータがありません。")
            log("先に main.py / sales_traffic.py / finances.py を実行してください。")
            return 1

        targets = available[: args.months]          # 新しい順
        log(f"対象の月: {', '.join(targets)}")

        names = data.asin_names()

        for month in targets:
            log("")
            log(f"── {month} ──")
            cur = data.summarize(month)
            prv = data.summarize(prev_month(month))
            sheet_rows = build_month_sheet(month, cur, prv, names)

            t = cur["total"]
            log(f"  売上 {t['sales']:,.0f} / 個数 {int(t['units']):,} / "
                f"実入金 {t['net']:,.0f} / 原価 {t['cogs']:,.0f} / "
                f"広告費 {t['ads_cost']:,.0f} / {len(cur['by_asin'])} ASIN")

            if args.dry_run:
                for row in sheet_rows[:14]:
                    log(f"    {row}")
            else:
                write_rows(cfg, month, sheet_rows, formatter=build_requests)

        # --- 月次推移（古い順に並べる）------------------------------------
        log("")
        log("── 月次推移 ──")
        trend_months = sorted(available[: max(args.months, 12)])
        trend_rows = build_trend(data, trend_months)
        if args.dry_run:
            for row in trend_rows[3:]:
                log(f"    {row}")
        else:
            write_rows(cfg, TREND_SHEET, trend_rows, formatter=build_requests)

        log("")
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
