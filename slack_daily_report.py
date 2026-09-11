#!/usr/bin/env python3
"""
毎朝の実績をSlackに自動投稿する

内容:
  ■ 今月の進捗                    当月の売上・着地予想売上・広告費・着地予想広告費・CPA
  ■ 日販（直近N日間・日ごと）       売上・広告費・個数・セッション・転換率（広告転換率）
  ■ 広告キャンペーン実績（直近M日間合計）  キャンペーンごとの期間合計
                                  （費用・販売個数・CPA・Acos）

Amazon APIは呼ばず、aggregate.py 経由でrawシートを読むだけです。
Slackへの投稿は Incoming Webhook（SLACK_WEBHOOK_URL）にJSON（Block Kit）を
POSTするだけのシンプルな方式です。広告費は税込（×1.1）で計算しています
（aggregate.py の AD_COST_TAX_MULTIPLIER 参照）。

見た目について: Slackの「fields」（2列グリッド）やコードブロックのASCII表は
スマホ幅で崩れて読みにくいため使わず、通常のテキスト（1項目1行）を
Block Kitのsectionブロックに積み重ねるだけのシンプルな形にしています。

使い方:
    source .env
    python3 slack_daily_report.py                # 日販=直近3日間、キャンペーン=直近7日間合計で投稿
    python3 slack_daily_report.py --days 10       # キャンペーン集計の対象日数を変える
    python3 slack_daily_report.py --daily-days 5  # 日販の表示日数を変える
    python3 slack_daily_report.py --dry-run       # 投稿せず内容を表示
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


def _section(text):
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


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


def _recent_dates_with_data(data, days):
    """売上 or 広告データがある日付を新しい順に集め、直近N日分だけ返す。"""
    dates = {r["date"] for r in data.traffic} | {r["date"] for r in data.ads}
    return sorted(dates, reverse=True)[:days]


def _period_label(dates, days):
    return f"{min(dates)}〜{max(dates)}" if dates else f"直近{days}日間"


# ---------------------------------------------------------------------------
# ■ 日販（直近N日間・日ごと）：通常のテキストで1日ずつ
# ---------------------------------------------------------------------------
def build_daily_texts(data, dates):
    if not dates:
        return ["（データがまだありません）"]

    by_day = data.daily_range(min(dates), max(dates))
    texts = []
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
        texts.append(
            f"*{_date_label(date)}*\n"
            f"売上：¥{sales:,}\n"
            f"広告費：¥{ads_cost:,}\n"
            f"個数：{units}（広告{ads_units}）\n"
            f"セッション：{sessions}（広告{ads_sessions}）\n"
            f"転換率：{cvr:.2f}%（広告転換率：{ads_cvr:.2f}%）"
        )
    return texts


# ---------------------------------------------------------------------------
# ■ 広告キャンペーン実績（直近M日間合計）：キャンペーンごとに通常のテキストで
# ---------------------------------------------------------------------------
def build_campaign_texts(data, dates):
    if not dates:
        return ["（広告データがまだありません）"]

    by_campaign = data.campaigns_in_range(min(dates), max(dates))
    if not by_campaign:
        return ["（この期間の広告データはまだありません）"]

    items = sorted(by_campaign.items(), key=lambda kv: kv[1].get("cost", 0.0), reverse=True)
    texts = []
    total = {"cost": 0.0, "units": 0.0, "sales": 0.0}
    for name, v in items:
        cost, units, sales = v.get("cost", 0.0), v.get("units", 0.0), v.get("sales", 0.0)
        cpa = cost / units if units else 0.0
        texts.append(
            f"○ *{name}*\n"
            f"費用：¥{round(cost):,}\n"
            f"販売個数：{int(units)}\n"
            f"CPA：¥{round(cpa):,}\n"
            f"Acos：{pct(cost, sales):.2f}%"
        )
        for k in total:
            total[k] += v.get(k, 0.0)

    cpa_t = total["cost"] / total["units"] if total["units"] else 0.0
    texts.append(
        f"○ *合計*\n"
        f"費用：¥{round(total['cost']):,}\n"
        f"販売個数：{int(total['units'])}\n"
        f"CPA：¥{round(cpa_t):,}\n"
        f"Acos：{pct(total['cost'], total['sales']):.2f}%"
    )
    return texts


# ---------------------------------------------------------------------------
# Slack Block Kit の組み立てと投稿
# ---------------------------------------------------------------------------
def build_blocks(data, days, daily_days):
    today = datetime.now(JST).strftime("%Y-%m-%d")
    dates_daily = _recent_dates_with_data(data, daily_days)
    dates_campaign = _recent_dates_with_data(data, days)

    progress = build_progress_section(data)
    daily_texts = build_daily_texts(data, dates_daily)
    campaign_texts = build_campaign_texts(data, dates_campaign)

    daily_label = _period_label(dates_daily, daily_days)
    campaign_label = _period_label(dates_campaign, days)

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": f"📊 {today} の実績報告", "emoji": True}},
        _section(f"*■ 今月の進捗*\n{progress}"),
        {"type": "divider"},
        _section(f"*■ 日販（{daily_label}）*"),
        *[_section(t) for t in daily_texts],
        {"type": "divider"},
        _section(f"*■ 広告キャンペーン実績（{campaign_label}合計）*"),
        *[_section(t) for t in campaign_texts],
    ]
    fallback_text = f"{today} の実績報告です。"
    return blocks, fallback_text


def blocks_to_text(blocks):
    """--dry-run 表示・テスト用に、blocksの中身を読める文字列にする。"""
    parts = []
    for block in blocks:
        if block["type"] == "divider":
            parts.append("- - - - -")
            continue
        parts.append(block.get("text", {}).get("text", ""))
    return "\n\n".join(parts)


def post_to_slack(webhook_url, blocks, fallback_text):
    request_with_retry("POST", webhook_url, json={"text": fallback_text, "blocks": blocks})


def main():
    parser = argparse.ArgumentParser(description="Slackへ毎朝の実績レポートを投稿します。")
    parser.add_argument("--days", type=int, default=7, help="キャンペーン集計の対象日数（既定: 7）")
    parser.add_argument("--daily-days", type=int, default=3, help="日販の表示日数（既定: 3）")
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

        blocks, fallback_text = build_blocks(data, args.days, args.daily_days)

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
