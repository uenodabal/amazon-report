#!/usr/bin/env python3
"""
毎朝の実績をSlackに自動投稿する

内容:
  ■ 今月の進捗        当月の売上・着地予想売上・広告費・着地予想広告費・CPA
  ■ 日販              直近N日間（既定7日）の日別データ（売上・広告費・個数・
                       セッション・転換率など）
  ■ 昨日の広告キャンペーン実績   キャンペーンごとの前日実績（費用・クリック数・
                       CPC・CV・CPA・ACOS）

Amazon APIは呼ばず、aggregate.py 経由でrawシートを読むだけです。
Slackへの投稿は Incoming Webhook（SLACK_WEBHOOK_URL）にJSONをPOSTするだけの
シンプルな方式です。広告費は税込（×1.1）で計算しています
（aggregate.py の AD_COST_TAX_MULTIPLIER 参照）。

使い方:
    source .env
    python3 slack_daily_report.py               # 直近7日間で投稿
    python3 slack_daily_report.py --days 10      # 日販の対象日数を変える
    python3 slack_daily_report.py --dry-run      # 投稿せず内容を表示
"""

import argparse
import sys
import unicodedata
from datetime import datetime, timedelta

from aggregate import Data, pct
from common import JST, Config, log, request_with_retry
from monthly import current_month, days_in_month

DAILY_HEADER = [
    "日付", "売上", "広告費", "個数", "広告個数",
    "Sess", "広告Sess", "転換率%", "広告転換率%",
]
CPN_HEADER = ["キャンペーン名", "費用", "クリック数", "CPC", "CV", "CPA", "ACOS%"]


# ---------------------------------------------------------------------------
# 等幅フォント（Slackのcode block）向けの表組み
# 日本語（全角）は半角の2倍の幅で表示されるため、文字数ではなく表示幅で揃える。
# ---------------------------------------------------------------------------
def _width(text):
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 1 for ch in str(text))


def _pad(text, width, align="left"):
    text = str(text)
    gap = " " * max(0, width - _width(text))
    return (gap + text) if align == "right" else (text + gap)


def render_table(header, rows):
    """先頭列は左揃え、残りは右揃えにしたシンプルな表を文字列で返す。"""
    ncols = len(header)
    all_rows = [header] + rows
    widths = [max(_width(r[i]) for r in all_rows) for i in range(ncols)]

    def fmt(row):
        cells = [
            _pad(row[i], widths[i], "left" if i == 0 else "right")
            for i in range(ncols)
        ]
        return "  ".join(cells)

    lines = [fmt(header), "  ".join("-" * widths[i] for i in range(ncols))]
    lines += [fmt(row) for row in rows]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ■ 今月の進捗
# ---------------------------------------------------------------------------
def build_progress_section(data):
    month = current_month()
    c = data.summarize(month)["total"]
    expected = days_in_month(month)

    def forecast(value, days_with_data):
        return value / days_with_data * expected if days_with_data else 0.0

    sales_forecast = forecast(c["sales"], c["traffic_days"])
    ads_forecast = forecast(c["ads_cost"], c["ads_days"])
    cpa = c["ads_cost"] / c["ads_units"] if c["ads_units"] else 0.0

    return "\n".join([
        f"売上：¥{round(c['sales']):,}",
        f"着地予想売上：¥{round(sales_forecast):,}",
        f"広告費：¥{round(c['ads_cost']):,}",
        f"着地予想広告費：¥{round(ads_forecast):,}",
        f"CPA：¥{round(cpa):,}",
    ])


# ---------------------------------------------------------------------------
# ■ 日販（直近N日間）
# ---------------------------------------------------------------------------
def _recent_dates_with_data(data, days):
    """売上 or 広告データがある日付を新しい順に集め、直近N日分だけ返す。"""
    dates = {r["date"] for r in data.traffic} | {r["date"] for r in data.ads}
    return sorted(dates, reverse=True)[:days]


def build_daily_table(data, days):
    dates = _recent_dates_with_data(data, days)
    if not dates:
        return "（データがまだありません）"

    by_day = data.daily_range(min(dates), max(dates))
    rows = []
    for date in dates:  # 新しい日付が上
        d = by_day.get(date, {})
        rows.append([
            date,
            f"{round(d.get('sales', 0)):,}",
            f"{round(d.get('ads_cost', 0)):,}",
            int(d.get("units", 0)),
            int(d.get("ads_units", 0)),
            int(d.get("sessions", 0)),
            int(d.get("ads_clicks", 0)),
            pct(d.get("units", 0), d.get("sessions", 0)),
            pct(d.get("ads_units", 0), d.get("ads_clicks", 0)),
        ])
    return render_table(DAILY_HEADER, rows)


# ---------------------------------------------------------------------------
# ■ 昨日の広告キャンペーン実績
# ---------------------------------------------------------------------------
def build_campaign_table(data):
    yesterday = (datetime.now(JST) - timedelta(days=1)).strftime("%Y-%m-%d")
    by_campaign = data.campaigns_in_range(yesterday)
    if not by_campaign:
        return yesterday, "（昨日の広告データはまだありません）"

    items = sorted(by_campaign.items(), key=lambda kv: kv[1].get("cost", 0.0), reverse=True)
    rows = []
    total = {"cost": 0.0, "clicks": 0.0, "units": 0.0, "sales": 0.0}
    for name, v in items:
        cost, clicks = v.get("cost", 0.0), v.get("clicks", 0.0)
        units, sales = v.get("units", 0.0), v.get("sales", 0.0)
        cpc = cost / clicks if clicks else 0.0
        cpa = cost / units if units else 0.0
        rows.append([
            name, f"{round(cost):,}", int(clicks), round(cpc),
            int(units), round(cpa), pct(cost, sales),
        ])
        for k in total:
            total[k] += v.get(k, 0.0)

    cpc_t = total["cost"] / total["clicks"] if total["clicks"] else 0.0
    cpa_t = total["cost"] / total["units"] if total["units"] else 0.0
    rows.append([
        "合計", f"{round(total['cost']):,}", int(total["clicks"]), round(cpc_t),
        int(total["units"]), round(cpa_t), pct(total["cost"], total["sales"]),
    ])
    return yesterday, render_table(CPN_HEADER, rows)


# ---------------------------------------------------------------------------
def build_message(data, days):
    today = datetime.now(JST).strftime("%Y-%m-%d")
    progress = build_progress_section(data)
    daily_table = build_daily_table(data, days)
    yesterday, campaign_table = build_campaign_table(data)

    return (
        f"*おはようございます。{today} の実績報告です。*\n\n"
        f"*■ 今月の進捗*\n{progress}\n\n"
        f"*■ 日販（直近{days}日間）*\n```{daily_table}```\n\n"
        f"*■ 昨日（{yesterday}）の広告キャンペーン実績*\n```{campaign_table}```"
    )


def post_to_slack(webhook_url, text):
    request_with_retry("POST", webhook_url, json={"text": text})


def main():
    parser = argparse.ArgumentParser(description="Slackへ毎朝の実績レポートを投稿します。")
    parser.add_argument("--days", type=int, default=7, help="日販テーブルの対象日数（既定: 7）")
    parser.add_argument("--dry-run", action="store_true", help="投稿せずに内容を表示")
    args = parser.parse_args()

    cfg = Config()
    if not args.dry_run:
        if not cfg.spreadsheet_id or not cfg.sa_json_path:
            raise SystemExit(
                "環境変数が設定されていません: SPREADSHEET_ID, GOOGLE_SERVICE_ACCOUNT_JSON"
            )
        if not cfg.slack_webhook_url:
            raise SystemExit(
                "環境変数が設定されていません: SLACK_WEBHOOK_URL\n"
                "SlackでIncoming Webhookを発行し、.env（またはGitHub Secrets）に設定してください。"
            )

    log("=" * 60)
    log("Slackへ実績レポートを投稿します")
    log("=" * 60)

    try:
        log("rawデータを読み込んでいます…")
        data = Data(cfg)

        text = build_message(data, args.days)

        if args.dry_run:
            log("--dry-run のため投稿は行いません。内容:")
            log("")
            print(text)
        else:
            log("Slackへ投稿しています…")
            post_to_slack(cfg.slack_webhook_url, text)
            log("投稿しました。")

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
