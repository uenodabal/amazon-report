#!/usr/bin/env python3
"""
日別広告実績シートを作成する（キャンペーンごとに表を分けて、日別実績を並べる）

raw_ads_email に蓄積された全期間のデータから、キャンペーンごとに表を分けて
（■キャンペーン名 の見出し＋日付ごとの実績表＋そのキャンペーンの合計行）
シート「日別広告実績」へ毎回全置換で書き込みます（dashboard.py と同じ
「派生データなので作り直す」方式）。Amazon APIは呼ばず、aggregate.py 経由で
raw_ads_email を読むだけです。

以前はキャンペーンをまたいで日付順に1つの表へ並べていましたが、キャンペーンが
入り交じって読みにくいため、キャンペーンごとに独立した表に分けています。
表の並び順は費用の多いキャンペーン順（月別シートのキャンペーン別内訳と同じ）、
各表の中は新しい日付が上に来る順です。

費用は raw_ads_email の記載額（税抜き）を税込（×1.1）に換算したものを使います
（aggregate.py の AD_COST_TAX_MULTIPLIER 参照）。

使い方:
    python3 ads_daily_sheet.py               # 全期間を書き込む
    python3 ads_daily_sheet.py --days 90     # 直近90日分だけに絞る
    python3 ads_daily_sheet.py --dry-run     # 書き込まずに内容を表示
"""

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta

from aggregate import Data, pct
from common import JST, Config, log, write_rows
from monthly import _campaign_metrics_row
from sheet_format import build_requests

SHEET_NAME = "日別広告実績"

# キャンペーン名の列は持たない（■キャンペーン名 の見出しで示すため）。
HEADER = [
    "日付", "インプレッション", "費用", "CPM", "クリック数", "CPC",
    "CTR%", "CV（購入数）", "CVR%", "CPA", "ROAS", "ACOS%",
]


def _daily_row(r):
    """1日分の実績を1行にする（見出し行の列構成に合わせる）。"""
    impressions, clicks, cost = r["impressions"], r["clicks"], r["cost"]
    sales, units = r["sales"], r["units"]
    cpm = cost / impressions * 1000 if impressions else 0.0
    cpc = cost / clicks if clicks else 0.0
    cpa = cost / units if units else 0.0
    roas = round(sales / cost, 2) if cost else 0.0
    return [
        r["date"], int(impressions), round(cost), round(cpm),
        int(clicks), round(cpc), pct(clicks, impressions), int(units),
        pct(units, clicks), round(cpa), roas, pct(cost, sales),
    ]


def build_rows(data, since=None):
    rows = [
        [SHEET_NAME],
        ["更新", datetime.now(JST).strftime("%Y-%m-%d %H:%M")],
        [],
    ]

    ads_rows = data.ads_all_rows()
    if since:
        ads_rows = [r for r in ads_rows if r["date"] >= since]

    if not ads_rows:
        rows.append(["広告データがまだありません"])
        rows.append([])
        rows.append(["※ 費用は税込（レポート記載額×1.1）で計算しています。"
                     "CV＝広告経由の商品購入数。ACOS＝費用÷売上。"])
        return rows

    # キャンペーンごとにまとめる
    by_campaign = defaultdict(list)
    totals = defaultdict(lambda: defaultdict(float))
    for r in ads_rows:
        by_campaign[r["campaign"]].append(r)
        b = totals[r["campaign"]]
        b["impressions"] += r["impressions"]
        b["clicks"] += r["clicks"]
        b["cost"] += r["cost"]
        b["sales"] += r["sales"]
        b["units"] += r["units"]

    # 費用の多いキャンペーン順（月別シートのキャンペーン別内訳と同じ並び）
    campaigns = sorted(by_campaign, key=lambda name: totals[name]["cost"], reverse=True)

    for campaign in campaigns:
        campaign_rows = sorted(by_campaign[campaign], key=lambda r: r["date"], reverse=True)
        rows.append([f"■ {campaign}"])
        rows.append(HEADER)
        for r in campaign_rows:
            rows.append(_daily_row(r))
        rows.append(_campaign_metrics_row("合計", totals[campaign]))
        rows.append([])

    rows.append(["※ 費用は税込（レポート記載額×1.1）で計算しています。"
                 "CV＝広告経由の商品購入数。ACOS＝費用÷売上。"
                 "各表の「合計」行は、そのキャンペーンの対象期間全体の合計です。"])
    return rows


def main():
    parser = argparse.ArgumentParser(description="日別広告実績シートを作成します。")
    parser.add_argument("--days", type=int, default=None,
                         help="直近N日分だけに絞る（既定: 全期間）")
    parser.add_argument("--dry-run", action="store_true", help="書き込まずに内容を表示")
    args = parser.parse_args()

    cfg = Config()
    if not args.dry_run:
        if not cfg.spreadsheet_id or not cfg.sa_json_path:
            raise SystemExit(
                "環境変数が設定されていません: SPREADSHEET_ID, GOOGLE_SERVICE_ACCOUNT_JSON"
            )

    log("=" * 60)
    log("日別広告実績シートを作成します")
    log("=" * 60)

    try:
        log("rawデータ（raw_ads_email）を読み込んでいます…")
        data = Data(cfg)

        since = None
        if args.days:
            since = (datetime.now(JST) - timedelta(days=args.days)).strftime("%Y-%m-%d")
            log(f"  直近{args.days}日（{since}以降）に絞ります。")

        rows = build_rows(data, since=since)
        log(f"  {len(rows) - 3} 行を書き込みます。")

        if args.dry_run:
            for row in rows[:40]:
                log(f"    {row}")
        else:
            write_rows(cfg, SHEET_NAME, rows, formatter=build_requests)

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
