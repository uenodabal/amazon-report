#!/usr/bin/env python3
"""
Amazon Ads「直近7日レポート自動送信」メール → Googleスプレッドシート

Amazon Adsの管理画面から毎朝 no-reply@ads.amazon.com が自動送信してくる
レポートメール（Gmail: uenoissei117@gmail.com 宛）をIMAPで取得し、
指定したGoogleスプレッドシートのワークシートへ書き込みます。

このメールにCSVファイルは添付されておらず、本文中の「レポートをダウンロード」
リンク（Amazonのクリック計測リンク越しに、期限付きの署名付きS3 URLを指す）を
たどってCSVを取得する方式です。ログイン等の追加認証は不要です
（署名付きURL自体に48時間ほどの有効期限があり、その範囲内で完結します）。

広告APIのアクセス許可（LWAスコープ）がAmazon側でまだ有効化されていないため、
それが完了するまでの「つなぎ」として、既にAmazon Ads側で自動送信されている
このメールを読み取って代用します。API連携（ads_report.py）が使えるようになった
段階で、そちらへ切り替えることを想定しています。

事前準備:
  1. Googleアカウントで2段階認証を有効化（設定済み）
  2. https://myaccount.google.com/apppasswords でアプリパスワードを発行
  3. .env に GMAIL_ADDRESS / GMAIL_APP_PASSWORD を設定

使い方:
    python3 ads_email_report.py                # 直近3日以内に届いたメールから最新の1通を処理
    python3 ads_email_report.py --search-days 7  # 検索範囲を広げる（復旧用）
    python3 ads_email_report.py --dry-run        # 取得のみ。スプレッドシートには書き込まない
"""

import argparse
import csv
import email
import imaplib
import io
import os
import re
import sys
import urllib.parse
from datetime import datetime, timedelta
from email.header import decode_header

from common import JST, Config, log, merge_and_write, request_with_retry

IMAP_HOST = "imap.gmail.com"

# Amazon Ads の自動送信メール
SENDER_ADDRESS = "no-reply@ads.amazon.com"

# メールのCSVレポートの列（このままシートの見出しにする）
HEADER = [
    "日付", "キャンペーン名", "キャンペーンID", "キャンペーン予算額",
    "キャンペーンの配信ステータス", "キャンペーン開始日", "予算の通貨",
    "インプレッション", "クリック数", "CTR", "CPC", "合計費用",
    "合計費用（調整済み）", "商品購入数", "売上", "注文された商品点数",
    "購入単価", "購入率", "クリックによる購入率", "ROAS",
]

# Excelの強制テキスト形式 ="1234567890" を素の値に戻す
_FORCE_TEXT_RE = re.compile(r'^="?(.*?)"?$')

# メール本文（HTML）中のリンク抽出
_HREF_RE = re.compile(r'href=[\'"]?([^\'" >]+)')

# Amazon Adsのクリック計測リンク: https://fe.r.ads.amazon.com/CL0/<エンコードされた実URL>/<連番>/<追跡ID>/<ハッシュ>
_CLICK_WRAP_RE = re.compile(r'^https://[^/]+/CL\d+/(.+?)/\d+/[^/]+/[^/]+$')


class GmailConfig:
    """Gmail IMAP専用の認証情報。SP-API/広告APIとは別管理。"""

    def __init__(self):
        self.address = os.environ.get("GMAIL_ADDRESS", "")
        self.app_password = os.environ.get("GMAIL_APP_PASSWORD", "")

    def validate(self):
        missing = [
            name
            for name, value in [
                ("GMAIL_ADDRESS", self.address),
                ("GMAIL_APP_PASSWORD", self.app_password),
            ]
            if not value
        ]
        if missing:
            raise SystemExit(
                "環境変数が設定されていません: " + ", ".join(missing) + "\n"
                "`source .env` を実行してから再度お試しください。\n"
                "GMAIL_APP_PASSWORD は https://myaccount.google.com/apppasswords "
                "で発行したGoogleアプリパスワード（2段階認証が必要）です。"
            )


def _decode_mime_words(value):
    """メールヘッダー（件名・添付ファイル名）のMIMEエンコードをデコードする。"""
    if not value:
        return ""
    parts = decode_header(value)
    decoded = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            decoded += text.decode(enc or "utf-8", errors="replace")
        else:
            decoded += text
    return decoded


def find_latest_report_email(imap, search_days):
    """直近 search_days 日以内に届いた、Amazon Adsからのメールのうち最新のものを探す。"""
    since = (datetime.now(JST) - timedelta(days=search_days)).strftime("%d-%b-%Y")
    log(f"メールを検索しています（差出人: {SENDER_ADDRESS}, {since} 以降）…")

    typ, data = imap.search(None, f'(FROM "{SENDER_ADDRESS}" SINCE {since})')
    if typ != "OK":
        raise RuntimeError(f"IMAP検索に失敗しました: {typ} {data}")

    ids = data[0].split()
    if not ids:
        return None

    # UIDはおおむね到着順に増えていくため、最後の1件が最新
    latest_id = ids[-1]
    log(f"  {len(ids)} 件見つかりました。最新の1件を処理します。")
    return latest_id


def fetch_message(imap, msg_id):
    """メール本文（RFC822）を取得し、emailメッセージとしてパースして返す。"""
    typ, data = imap.fetch(msg_id, "(RFC822)")
    if typ != "OK":
        raise RuntimeError(f"メールの取得に失敗しました: {typ} {data}")
    return email.message_from_bytes(data[0][1])


def _decode_click_wrapped(href):
    """
    Amazonのクリック計測リンク（.../CL0/<エンコードされた実URL>/連番/追跡ID/ハッシュ）から、
    実際のリンク先URLを取り出す。計測リンクの形式でなければ None を返す。

    実URL部分は「/」「?」「&」やリンク先自身が持つ「%」を1回だけURLエンコードした形で
    埋め込まれているため、1回 unquote するだけで正しいURL（署名付きクエリ文字列を含む）に戻る。
    """
    m = _CLICK_WRAP_RE.match(href)
    if not m:
        return None
    return urllib.parse.unquote(m.group(1))


def find_report_url(msg):
    """メール本文（text/html）から、CSVレポートのダウンロードURLを探す。見つからなければ None。"""
    for part in msg.walk():
        if part.get_content_type() != "text/html":
            continue

        payload = part.get_payload(decode=True)
        charset = part.get_content_charset() or "utf-8"
        html = payload.decode(charset, errors="replace")

        for href in _HREF_RE.findall(html):
            for candidate in (href, _decode_click_wrapped(href)):
                if not candidate:
                    continue
                path = candidate.split("?", 1)[0]
                if path.lower().endswith(".csv"):
                    return candidate

        break  # text/htmlパートは通常1つだけ

    return None


def download_report_csv(msg):
    """メールから『レポートをダウンロード』リンクを見つけ、CSVをダウンロードして返す。"""
    subject = _decode_mime_words(msg.get("Subject", ""))
    log(f"  件名: {subject}")

    url = find_report_url(msg)
    if not url:
        raise RuntimeError(
            "メール本文から『レポートをダウンロード』リンクが見つかりませんでした。"
            "Amazon側でメールの形式が変わった可能性があります（件名: "
            f"{subject}）。"
        )

    log("  レポートをダウンロードしています…")
    response = request_with_retry("GET", url)
    return response.content


def parse_csv_rows(raw_bytes):
    """CSVバイト列をパースし、シート書き込み用の行リストに変換する。"""
    # 日本語Windows環境で作られたCSVはBOM付きUTF-8かCP932であることが多い
    text = None
    for encoding in ("utf-8-sig", "cp932", "shift_jis"):
        try:
            text = raw_bytes.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw_bytes.decode("utf-8", errors="replace")

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise RuntimeError("CSVが空でした。")

    header = [h.strip() for h in rows[0]]
    if header != HEADER:
        log("  ※ 想定と異なる列構成のCSVです。列の順番が変わっている可能性があります。")
        log(f"    受信した見出し: {header}")

    data_rows = []
    for raw_row in rows[1:]:
        if not any(str(c).strip() for c in raw_row):
            continue
        row = list(raw_row)

        # 日付: 2026/09/06 → 2026-09-06（他のシートとの表記統一・日付比較のため）
        if row and row[0]:
            try:
                row[0] = datetime.strptime(row[0].strip(), "%Y/%m/%d").strftime("%Y-%m-%d")
            except ValueError:
                pass

        # キャンペーンID: ="1234567890" → 1234567890（Excelの強制テキスト形式を解除）
        if len(row) > 2 and row[2]:
            m = _FORCE_TEXT_RE.match(row[2].strip())
            if m:
                row[2] = m.group(1)

        data_rows.append(row)

    return data_rows


def main():
    parser = argparse.ArgumentParser(
        description="Amazon Adsの自動送信メール（CSV）をGoogleスプレッドシートへ取り込みます。"
    )
    parser.add_argument(
        "--search-days", type=int, default=3,
        help="何日前までのメールを検索対象にするか（既定: 3。実行が数日途切れた場合の復旧用）",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="取得のみ行い、スプレッドシートには書き込まない"
    )
    args = parser.parse_args()

    cfg = Config()
    if not args.dry_run:
        if not cfg.sa_json_path or not cfg.spreadsheet_id:
            raise SystemExit(
                "環境変数が設定されていません: GOOGLE_SERVICE_ACCOUNT_JSON, SPREADSHEET_ID\n"
                "`source .env` を実行してから再度お試しください。"
            )
        if not os.path.exists(cfg.sa_json_path):
            raise SystemExit(f"サービスアカウントのJSONファイルが見つかりません: {cfg.sa_json_path}")

    gmail_cfg = GmailConfig()
    gmail_cfg.validate()
    sheet_name = cfg.worksheet("WORKSHEET_ADS_EMAIL", "raw_ads_email")

    log("=" * 60)
    log("Amazon Ads自動送信メールの取り込みを開始します")
    log("=" * 60)

    try:
        log(f"Gmailに接続しています（{gmail_cfg.address}）…")
        imap = imaplib.IMAP4_SSL(IMAP_HOST)
        imap.login(gmail_cfg.address, gmail_cfg.app_password)
        imap.select("INBOX", readonly=True)

        try:
            msg_id = find_latest_report_email(imap, args.search_days)
            if msg_id is None:
                log(f"  直近 {args.search_days} 日以内に該当メールが見つかりませんでした。")
                log("  （届くのが遅れているだけの可能性があります。翌日以降のレポートで自動的に補完されます）")
                return 0

            msg = fetch_message(imap, msg_id)
            raw_csv = download_report_csv(msg)
            log(f"  {len(raw_csv):,} バイトのCSVを取得しました。")
        finally:
            imap.logout()

        data_rows = parse_csv_rows(raw_csv)
        log(f"  {len(data_rows)} 件の行を読み取りました。")

        if args.dry_run:
            log("--dry-run のため、スプレッドシートへの書き込みは行いません。")
            for row in data_rows[:5]:
                log(f"  例: {row}")
        elif data_rows:
            window_start = min(row[0] for row in data_rows if row[0])
            merge_and_write(
                cfg,
                sheet_name,
                HEADER,
                data_rows,
                date_col=0,
                window_start=window_start,
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
