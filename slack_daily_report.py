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
Slackへの投稿は Incoming Webhook（SLACK_WEBHOOK_URL）にJSON（Block Kit）を
POSTするだけのシンプルな方式です。広告費は税込（×1.1）で計算しています
（aggregate.py の AD_COST_TAX_MULTIPLIER 参照）。

見た目について: 等幅フォントのASCII表（コードブロック）は、スマホの画面幅では
セルの途中で強制的に折り返されてしまい非常に読みにくいため使っていません。
代わりに、1件（1日・1キャンペーン）ずつ改行で区切った文章形式にして、
画面幅に応じて自然に折り返されるようにしています。

使い方:
    source .env
    python3 slack_daily_report.py               # 直近7日間で投稿
    python3 slack_daily_report.py --days 10      # 日販の対象日数を変える
    python3 slack_daily_report.py --dry-run      # 投稿せず内容を表示
"""

import argparse
import sys
from datetime import datetime, timedelta

from aggregate import Data, pct
from common import JST, Config, log, request_with_retry
from monthly import current_month, days_in_month

JP_WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]


def _date_label(date_str):
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{dt.month}/{dt.day}（{JP_WEEKDAYS[dt.weekday()]}）"


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
# ■ 日販（直近N日間）：1日1ブロック（日付＋2行）の文章形式
# ---------------------------------------------------------------------------
def _recent_dates_with_data(data, days):
    """売上 or 広告データがある日付を新しい順に集め、直近N日分だけ返す。"""
    dates = {r["date"] for r in data.traffic} | {r["date"] for r in data.ads}
    return sorted(dates, reverse=True)[:days]


def build_daily_lines(data, days):
    dates = _recent_dates_with_data(data, days)
    if not dates:
        return "（データがまだありません）"

    by_day = data.daily_range(min(dates), max(dates))
    blocks = []
    for date in dates:  # 新しい日付が上
        d = by_day.get(date, {})
        sales = round(d.get("sales", 0))
        ads_cost = round(d.get("ads_cost", 0))
        units = int(d.get("units", 0))
        ads_units = int(d.get("ads_units", 0))
        sessions = int(d.get("sessions", 0))
        ads_sessions = int(d.get("ads_clicks", 0))
        cvr = pct(d.get("units", 0), d.get("sessions", 0))
        ads_cvr = pct(d.get("ads_units", 0), d.get("ads_clicks", 0))
        blocks.append(
            f"*{_date_label(date)}*\n"
            f"売上 ¥{sales:,}　広告費 ¥{ads_cost:,}　個数 {units}（広告{ads_units}）\n"
            f"Sess {sessions}（広告{ads_sessions}）　転換率 {cvr:.2f}%（広告{ads_cvr:.2f}%）"
        )
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# ■ 昨日の広告キャンペーン実績：1キャンペーン1ブロックの文章形式
# ---------------------------------------------------------------------------
def build_campaign_lines(data):
    yesterday = (datetime.now(JST) - timedelta(days=1)).strftime("%Y-%m-%d")
    by_campaign = data.campaigns_in_range(yesterday)
    if not by_campaign:
        return yesterday, "（昨日の広告データはまだありません）"

    items = sorted(by_campaign.items(), key=lambda kv: kv[1].get("cost", 0.0), reverse=True)
    blocks = []
    total = {"cost": 0.0, "clicks": 0.0, "units": 0.0, "sales": 0.0}
    for name, v in items:
        cost, clicks = v.get("cost", 0.0), v.get("clicks", 0.0)
        units, sales = v.get("units", 0.0), v.get("sales", 0.0)
        cpc = cost / clicks if clicks else 0.0
        cpa = cost / units if units else 0.0
        blocks.append(
            f"• *{name}*\n"
            f"　費用 ¥{round(cost):,}　クリック {int(clicks)}　CPC ¥{round(cpc):,}　"
            f"CV {int(units)}　CPA ¥{round(cpa):,}　ACOS {pct(cost, sales):.2f}%"
        )
        for k in total:
            total[k] += v.get(k, 0.0)

    cpc_t = total["cost"] / total["clicks"] if total["clicks"] else 0.0
    cpa_t = total["cost"] / total["units"] if total["units"] else 0.0
    blocks.append(
        f"*合計*\n"
        f"　費用 ¥{round(total['cost']):,}　クリック {int(total['clicks'])}　CPC ¥{round(cpc_t):,}　"
        f"CV {int(total['units'])}　CPA ¥{round(cpa_t):,}　ACOS {pct(total['cost'], total['sales']):.2f}%"
    )
    return yesterday, "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Slack Block Kit の組み立てと投稿
# ---------------------------------------------------------------------------
def build_blocks(data, days):
    today = datetime.now(JST).strftime("%Y-%m-%d")
    progress = build_progress_section(data)
    daily_lines = build_daily_lines(data, days)
    yesterday, campaign_lines = build_campaign_lines(data)

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": f"📊 {today} の実績報告", "emoji": True}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*■ 今月の進捗*\n{progress}"}},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*■ 日販（直近{days}日間）*\n{daily_lines}"}},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn",
                   "text": f"*■ 昨日（{yesterday}）の広告キャンペーン実績*\n{campaign_lines}"}},
    ]
    fallback_text = f"{today} の実績報告です。"
    return blocks, fallback_text


def blocks_to_text(blocks):
    """--dry-run 表示・テスト用に、blocksの中身を読める文字列にする。"""
    parts = []
    for block in blocks:
        text = block.get("text", {}).get("text", "")
        parts.append(text)
    return "\n\n".join(parts)


def post_to_slack(webhook_url, blocks, fallback_text):
    request_with_retry("POST", webhook_url, json={"text": fallback_text, "blocks": blocks})


def main():
    parser = argparse.ArgumentParser(description="Slackへ毎朝の実績レポートを投稿します。")
    parser.add_argument("--days", type=int, default=7, help="日販の対象日数（既定: 7）")
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

        blocks, fallback_text = build_blocks(data, args.days)

        if args.dry_run:
            log("--dry-run のため投稿は行いません。内容:")
            log("")
            print(blocks_to_text(blocks))
        else:
            log("Slackへ投稿しています…")
            post_to_slack(cfg.slack_webhook_url, blocks, fallback_text)
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
