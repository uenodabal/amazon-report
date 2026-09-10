#!/usr/bin/env python3
"""
共通処理（認証・HTTP・スプレッドシート書き込み）

main.py と sales_traffic.py の両方から読み込まれます。
"""

import gzip
import os
import sys
import time
from datetime import datetime, timedelta, timezone

# --- Pythonバージョンの検査 -------------------------------------------------
# gspread / google-auth は typing.Literal（Python 3.8以降）を使うため、
# 3.7以下では書き込み直前まで進んでから import エラーで落ちる。
# 原因が分かりにくいので、起動直後に明示的に知らせる。
if sys.version_info < (3, 8):
    _v = ".".join(map(str, sys.version_info[:3]))
    raise SystemExit(
        f"""
このスクリプトには Python 3.8 以上が必要です（現在: {_v}）。
使用中のPython: {sys.executable}

対処方法（いずれか）:

  A. 仮想環境を作る（推奨）
       /usr/bin/python3 -m venv venv
       source venv/bin/activate
       pip install -r requirements.txt

  B. pyenv で新しいバージョンをこのフォルダだけに設定する
       pyenv install 3.11.9
       pyenv local 3.11.9
       python3 -m pip install -r requirements.txt

いずれの場合も、実行前に `source .env` を忘れずに。
"""
    )

import requests

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------
JST = timezone(timedelta(hours=9))

TOKEN_URL = "https://api.amazon.com/auth/o2/token"
SPAPI_ENDPOINT = "https://sellingpartnerapi-fe.amazon.com"  # 極東リージョン
MARKETPLACE_JP = "A1VC38T7YXB528"

# 1回の書き込みで送る行数。大きすぎると通信が切れやすくなる。
SHEET_CHUNK_ROWS = 2000

# スプレッドシート操作のリトライ回数
SHEET_MAX_RETRIES = 5


def log(message):
    """時刻付きで進捗を表示する。"""
    print(f"[{datetime.now(JST):%Y-%m-%d %H:%M:%S}] {message}", flush=True)


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
class Config:
    def __init__(self):
        self.client_id = os.environ.get("LWA_CLIENT_ID", "")
        self.client_secret = os.environ.get("LWA_CLIENT_SECRET", "")
        self.refresh_token = os.environ.get("LWA_REFRESH_TOKEN", "")
        self.sa_json_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        self.spreadsheet_id = os.environ.get("SPREADSHEET_ID", "")
        # Slackへの毎朝の実績報告用（Incoming Webhook）。任意（未設定なら投稿しない）。
        self.slack_webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "")

    def worksheet(self, env_name, default):
        """シート名を環境変数から読む。未設定なら既定値。"""
        return os.environ.get(env_name) or default

    def validate(self, need_sheets=True):
        missing = [
            name
            for name, value in [
                ("LWA_CLIENT_ID", self.client_id),
                ("LWA_CLIENT_SECRET", self.client_secret),
                ("LWA_REFRESH_TOKEN", self.refresh_token),
            ]
            if not value
        ]
        if need_sheets:
            missing += [
                name
                for name, value in [
                    ("GOOGLE_SERVICE_ACCOUNT_JSON", self.sa_json_path),
                    ("SPREADSHEET_ID", self.spreadsheet_id),
                ]
                if not value
            ]
        if missing:
            raise SystemExit(
                "環境変数が設定されていません: " + ", ".join(missing) + "\n"
                "`source .env` を実行してから再度お試しください。"
            )
        if need_sheets and not os.path.exists(self.sa_json_path):
            raise SystemExit(
                f"サービスアカウントのJSONファイルが見つかりません: {self.sa_json_path}"
            )


# ---------------------------------------------------------------------------
# HTTP（リトライ付き）
# ---------------------------------------------------------------------------
def request_with_retry(method, url, *, max_retries=5, **kwargs):
    """429（レート制限）と5xxに対して指数バックオフでリトライする。"""
    delay = 1
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.request(method, url, timeout=120, **kwargs)
        except requests.RequestException as exc:
            last_error = exc
            if attempt == max_retries:
                break
            log(f"  通信エラー（{attempt}/{max_retries}）: {exc} — {delay}秒後に再試行")
            time.sleep(delay)
            delay *= 2
            continue

        if response.status_code == 429 or response.status_code >= 500:
            last_error = f"HTTP {response.status_code}: {response.text[:300]}"
            if attempt == max_retries:
                break
            label = "レート制限" if response.status_code == 429 else "サーバーエラー"
            log(f"  {label}（{attempt}/{max_retries}）— {delay}秒後に再試行")
            time.sleep(delay)
            delay *= 2
            continue

        if not response.ok:
            raise RuntimeError(
                f"APIエラー HTTP {response.status_code}\n"
                f"URL: {url}\n"
                f"応答: {response.text[:1000]}"
            )
        return response

    raise RuntimeError(f"リトライ上限に達しました。最後のエラー: {last_error}")


def get_access_token(cfg):
    """リフレッシュトークンからアクセストークン（有効1時間）を取得する。"""
    log("アクセストークンを取得しています…")
    response = request_with_retry(
        "POST",
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": cfg.refresh_token,
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
        },
    )
    token = response.json().get("access_token")
    if not token:
        raise RuntimeError(f"アクセストークンを取得できませんでした: {response.text[:500]}")
    log("  取得しました。")
    return token


def decompress_if_needed(raw):
    """gzipであれば解凍する。requestsが自動解凍済みの場合はそのまま返す。"""
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


def decode_text(raw):
    """日本のレポートは cp932 で返ることがあるため、順に試す。"""
    for encoding in ("utf-8", "cp932", "shift_jis"):
        try:
            text = raw.decode(encoding)
            log(f"  文字コード: {encoding}")
            return text
        except UnicodeDecodeError:
            continue
    log("  文字コードを判定できなかったため、置換文字を使って読み込みました。")
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Googleスプレッドシート
# ---------------------------------------------------------------------------
def _is_transient(exc):
    """一時的な通信不良か（再試行する価値があるか）を判定する。"""
    text = str(exc)
    if any(key in text for key in (
        "RemoteDisconnected", "Connection aborted", "Connection reset",
        "timed out", "Read timed out", "Temporary failure",
        "Max retries exceeded", "BadStatusLine",
    )):
        return True

    try:
        import requests.exceptions as rex
        if isinstance(exc, (rex.ConnectionError, rex.ChunkedEncodingError,
                            rex.Timeout, rex.ProxyError)):
            return True
    except Exception:
        pass

    # gspread の APIError はレスポンスを持つ。5xx と 429 は再試行する。
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) in (429, 500, 502, 503, 504):
        return True
    return False


def sheet_retry(label, func):
    """
    スプレッドシート操作を、一時的な通信不良に対して再試行する。

    Amazon側には最初からリトライを入れていたが、Google側は素通しだった。
    そのため一度の切断で、取得済みのデータごと処理が失われていた。
    """
    delay = 2
    for attempt in range(1, SHEET_MAX_RETRIES + 1):
        try:
            return func()
        except Exception as exc:
            if attempt == SHEET_MAX_RETRIES or not _is_transient(exc):
                raise
            log(f"  {label}に失敗（{attempt}/{SHEET_MAX_RETRIES}）: "
                f"{str(exc)[:120]} — {delay}秒後に再試行")
            time.sleep(delay)
            delay *= 2


def write_rows(cfg, worksheet_name, rows, formatter=None):
    """
    ワークシートを洗い替えして書き込む。
    何度実行しても結果が同じになる（冪等）。
    rows[0] はヘッダー行。

    formatter を渡すと、書き込み後に書式（表示形式・色）を適用する。
    formatter(worksheet_id, rows) -> Sheets APIのリクエスト配列
    """
    import gspread

    log(f"スプレッドシート「{worksheet_name}」へ書き込んでいます…")
    spreadsheet = _open_spreadsheet(cfg)

    needed_rows = max(len(rows) + 10, 100)
    needed_cols = max((len(rows[0]) if rows else 1) + 2, 26)

    def prepare():
        try:
            sheet = spreadsheet.worksheet(worksheet_name)
            sheet.clear()
            sheet.resize(rows=needed_rows, cols=needed_cols)
            return sheet
        except gspread.exceptions.WorksheetNotFound:
            log(f"  ワークシート「{worksheet_name}」が無いため作成します。")
            return spreadsheet.add_worksheet(
                title=worksheet_name, rows=needed_rows, cols=needed_cols
            )

    worksheet = sheet_retry("シートの準備", prepare)

    if not rows:
        worksheet.update(values=[["データがありません"]], range_name="A1")
        log("  対象期間のデータが0件でした。")
        return

    for start in range(0, len(rows), SHEET_CHUNK_ROWS):
        chunk = rows[start : start + SHEET_CHUNK_ROWS]
        sheet_retry(
            f"{start + len(chunk)}行目までの書き込み",
            lambda c=chunk, s=start: worksheet.update(values=c, range_name=f"A{s + 1}"),
        )
        log(f"  {start + len(chunk)} / {len(rows)} 行")

    sheet_retry("見出し行の固定", lambda: worksheet.freeze(rows=1))

    if formatter is not None:
        try:
            requests_list = formatter(worksheet.id, rows)
            if requests_list:
                # 書式は一括で送る（1件ずつだと遅く、APIの呼び出し回数も増える）
                sheet_retry(
                    "書式の適用",
                    lambda: spreadsheet.batch_update({"requests": requests_list}),
                )
                log(f"  書式を適用しました（{len(requests_list)} 件）。")
        except Exception as exc:
            # 見た目の失敗でデータを失わないよう、ここでは処理を止めない
            log(f"  （書式の適用に失敗しました: {exc}）")

    log(f"  完了しました。{len(rows) - 1} 件を書き込みました。")


def write_range(cfg, worksheet_name, top_left, rows, formatter=None):
    """
    既存シートの指定セルを起点に、追加の表を書き込む（シート自体は洗い替えしない）。

    月別シートの余白（広告サマリーの右など）にキャンペーン別内訳のような
    付帯的な表を添える用途。write_rows でそのシートの本体を書き込んだ「あと」に
    呼び出すこと（write_rows はシートを clear するため、順番を逆にすると消える）。

    formatter を渡す場合は formatter(worksheet_id, top_row, top_col, rows) の形で
    呼び出す（row/col は0始まりのシート全体での絶対位置）。
    """
    import gspread

    if not rows:
        return

    spreadsheet = _open_spreadsheet(cfg)
    try:
        worksheet = sheet_retry(
            f"シート「{worksheet_name}」を開く（付帯表）",
            lambda: spreadsheet.worksheet(worksheet_name),
        )
    except gspread.exceptions.WorksheetNotFound:
        log(f"  ワークシート「{worksheet_name}」が見つからないため、付帯表の書き込みをスキップします。")
        return

    row0, col0 = gspread.utils.a1_to_rowcol(top_left)
    needed_rows = row0 - 1 + len(rows) + 5
    needed_cols = col0 - 1 + max(len(r) for r in rows) + 2
    if worksheet.row_count < needed_rows or worksheet.col_count < needed_cols:
        sheet_retry(
            "シートの拡張（付帯表用）",
            lambda: worksheet.resize(
                rows=max(worksheet.row_count, needed_rows),
                cols=max(worksheet.col_count, needed_cols),
            ),
        )

    sheet_retry(
        f"付帯表の書き込み（{top_left}）",
        lambda: worksheet.update(values=rows, range_name=top_left),
    )

    if formatter is not None:
        try:
            requests_list = formatter(worksheet.id, row0 - 1, col0 - 1, rows)
            if requests_list:
                sheet_retry(
                    "付帯表の書式適用",
                    lambda: spreadsheet.batch_update({"requests": requests_list}),
                )
        except Exception as exc:
            log(f"  （付帯表の書式適用に失敗しました: {exc}）")


def _open_spreadsheet(cfg):
    import gspread
    from google.oauth2.service_account import Credentials

    credentials = Credentials.from_service_account_file(
        cfg.sa_json_path,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    client = gspread.authorize(credentials)
    try:
        return sheet_retry(
            "スプレッドシートを開く",
            lambda: client.open_by_key(cfg.spreadsheet_id),
        )
    except gspread.exceptions.APIError as exc:
        raise RuntimeError(
            "スプレッドシートを開けませんでした。\n"
            "サービスアカウントのメールアドレスに、対象スプレッドシートの"
            "編集権限を付与しているか確認してください。\n"
            f"詳細: {exc}"
        ) from exc


def read_sheet(cfg, worksheet_name):
    """
    既存シートの内容を読む。シートが無ければ空リストを返す。
    戻り値は文字列の二次元リスト（1行目はヘッダー）。
    """
    import gspread

    spreadsheet = _open_spreadsheet(cfg)
    try:
        worksheet = spreadsheet.worksheet(worksheet_name)
    except gspread.exceptions.WorksheetNotFound:
        return []
    values = sheet_retry(
        f"シート「{worksheet_name}」の読み込み",
        worksheet.get_all_values,
    )
    return [r for r in values if any(str(c).strip() for c in r)]


def _normalize(row, width):
    """行の長さをヘッダーに合わせる。"""
    row = list(row)
    if len(row) < width:
        row += [""] * (width - len(row))
    return row[:width]


def merge_and_write(
    cfg,
    worksheet_name,
    header,
    new_rows,
    *,
    date_col=None,
    window_start=None,
    key_cols=None,
    replace=False,
    sort_cols=None,
):
    """
    既存データを保持したまま、新しく取得した分だけを差し替えて書き込む（蓄積方式）。

    差し替えの判定は2通り:
      * date_col + window_start … 指定日以降の行を新データで置き換える（期間の洗い替え）
      * key_cols               … 同じキーを持つ行を新データで置き換える

    replace=True で従来どおりの全置換に戻せる。
    """
    header = list(header)
    width = len(header)
    new_rows = [_normalize(r, width) for r in new_rows]

    if replace:
        log("  （--replace 指定のため全置換します）")
        merged = new_rows
    else:
        existing = read_sheet(cfg, worksheet_name)

        if not existing:
            merged = new_rows
        elif [str(c) for c in existing[0]] != [str(c) for c in header]:
            log("  ※ 既存シートの列構成が変わっているため、全置換します。")
            merged = new_rows
        else:
            old_rows = [_normalize(r, width) for r in existing[1:]]
            before = len(old_rows)

            if key_cols is not None:
                new_keys = {tuple(str(r[i]) for i in key_cols) for r in new_rows}
                kept = [r for r in old_rows if tuple(str(r[i]) for i in key_cols) not in new_keys]
            elif date_col is not None and window_start is not None:
                # 取得対象期間より前の行だけ残す（期間内は新データで置き換える）
                kept = [r for r in old_rows if str(r[date_col])[:10] < window_start]
            else:
                raise ValueError("date_col+window_start か key_cols のどちらかが必要です")

            merged = kept + new_rows
            log(f"  既存 {before} 行のうち {len(kept)} 行を保持し、{len(new_rows)} 行を追加・更新します。")

    if sort_cols:
        def sort_key(row):
            return tuple(str(row[i]) for i in sort_cols)

        merged.sort(key=sort_key)

    if len(merged) > 100000:
        log(f"  ⚠ 行数が {len(merged):,} 行に達しています。")
        log("     スプレッドシートは1ファイル1,000万セルが上限です。")
        log("     動作が重くなってきたら、年ごとにファイルを分けることを検討してください。")

    write_rows(cfg, worksheet_name, [header] + merged)


def append_row(cfg, worksheet_name, row, header=None):
    """
    ワークシートの末尾に1行だけ追記する（実行ログ用）。
    シートが無ければ header 付きで作成する。
    書き込みに失敗しても例外を投げない（ログ記録が本処理を妨げないため）。
    """
    try:
        import gspread

        spreadsheet = _open_spreadsheet(cfg)

        try:
            worksheet = spreadsheet.worksheet(worksheet_name)
        except gspread.exceptions.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=worksheet_name, rows=1000, cols=10)
            if header:
                worksheet.update(values=[header], range_name="A1")
                worksheet.freeze(rows=1)

        sheet_retry(
            "実行ログの追記",
            lambda: worksheet.append_row(
                [str(v) for v in row], value_input_option="USER_ENTERED"
            ),
        )
        return True
    except Exception as exc:  # ログ記録の失敗で本処理を止めない
        log(f"  （実行ログのシート記録に失敗しました: {exc}）")
        return False
