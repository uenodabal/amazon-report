#!/usr/bin/env python3
"""
モックによる動作検証。実際のAPIには接続しません。
    python3 test_mock.py
"""
import gzip
import json
import os
import sys
import types
from datetime import timedelta

os.environ.update({
    "LWA_CLIENT_ID": "test-id",
    "LWA_CLIENT_SECRET": "test-secret",
    "LWA_REFRESH_TOKEN": "test-refresh",
})

import common  # noqa: E402
import main  # noqa: E402
import sales_traffic as st  # noqa: E402

main.POLL_INTERVAL_SEC = 0
st.POLL_INTERVAL_SEC = 0

failures = []


def check(label, condition, detail=""):
    if condition:
        print(f"  ✅ {label}")
    else:
        print(f"  ❌ {label} {detail}")
        failures.append(label)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b""):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.text = json.dumps(payload, ensure_ascii=False) if payload is not None else ""

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._payload


def install(handler):
    """common.requests を差し replace する（main/st は common 経由で呼ぶ）。"""
    common.requests = types.SimpleNamespace(request=handler, RequestException=Exception)


# ===========================================================================
print("\n=== モック検証 ===\n")

cfg = common.Config()
cfg.validate(need_sheets=False)
print("① 設定")
check("必須環境変数チェックが通る", True)

# ---------------------------------------------------------------------------
print("\n② 注文レポート（main.py）")

ORDERS_TSV = (
    "amazon-order-id\tpurchase-date\tsku\tproduct-name\tquantity\titem-price\n"
    "249-1234567-1234567\t2026-08-01T10:00:00+09:00\tEXP-001\tテスト商品Ａ\t2\t3980\n"
    "249-7654321-7654321\t2026-08-02T14:30:00+09:00\tEXP-002\t商品Ｂ（特殊文字：①②）\t1\t12800\n"
    "\n"
)
ORDERS_GZ = gzip.compress(ORDERS_TSV.encode("cp932"))
orders_state = {"poll": 0, "throttled": False}


def orders_handler(method, url, **kwargs):
    if url == common.TOKEN_URL:
        return FakeResponse(200, {"access_token": "Atza|dummy"})
    if url.endswith("/reports/2021-06-30/reports") and method == "POST":
        if not orders_state["throttled"]:
            orders_state["throttled"] = True
            return FakeResponse(429, {"errors": [{"message": "throttled"}]})
        return FakeResponse(200, {"reportId": "R-1"})
    if "/reports/2021-06-30/reports/R-1" in url:
        orders_state["poll"] += 1
        if orders_state["poll"] < 3:
            return FakeResponse(200, {"processingStatus": "IN_QUEUE"})
        return FakeResponse(200, {"processingStatus": "DONE", "reportDocumentId": "D-1"})
    if "/reports/2021-06-30/documents/D-1" in url:
        return FakeResponse(200, {"url": "https://x.invalid/r.gz", "compressionAlgorithm": "GZIP"})
    if url == "https://x.invalid/r.gz":
        return FakeResponse(200, content=ORDERS_GZ)
    raise AssertionError(f"想定外: {method} {url}")


install(orders_handler)
token = common.get_access_token(cfg)
check("アクセストークンを取得できる", token == "Atza|dummy")

end = common.datetime.now(common.JST)
rid = main.create_report(token, end - timedelta(days=7), end)
check("429でリトライして成功する", orders_state["throttled"] and rid == "R-1")

did = main.wait_for_report(token, rid)
check("DONEまでポーリングする", did == "D-1" and orders_state["poll"] == 3)

rows = main.download_report(token, did)
check("gzipを解凍できる", len(rows) == 3, f"→ {len(rows)}行")
check("cp932の日本語を読める", rows[1][3] == "テスト商品Ａ")
check("特殊文字①②を保持する", "①②" in rows[2][3])
check("空行を除去する", len(rows) == 3)

# ---------------------------------------------------------------------------
print("\n③ ASIN別売上（sales_traffic.py）")

DK_RECORD = {
    "startDate": "2026-08-30",
    "endDate": "2026-08-30",
    "parentAsin": "B0PARENT01",
    "childAsin": "B0CHILD001",
    "sku": None,
    "sales": {
        "orderedProductSales": {"amount": 39800.0, "currencyCode": "JPY"},
        "unitsOrdered": 10,
        "totalOrderItems": 8,
        "unitsShipped": 9,
        "unitsRefunded": 1,
        "refundRate": 10.0,
    },
    "traffic": {
        "sessions": 250,
        "pageViews": 400,
        "browserSessions": 100,
        "mobileAppSessions": 150,
        "buyBoxPercentage": 98.5,
        "unitSessionPercentage": 4.0,
    },
}
DK_JSONL = json.dumps(DK_RECORD, ensure_ascii=False) + "\n"
dk_state = {"poll": 0}


def dk_handler(method, url, **kwargs):
    if url == common.TOKEN_URL:
        return FakeResponse(200, {"access_token": "Atza|dummy"})
    if url.endswith("/dataKiosk/2023-11-15/queries") and method == "POST":
        body = kwargs.get("json", {}).get("query", "")
        assert st.DATASET in body, "データセット名がクエリに含まれていない"
        assert "salesAndTrafficByAsin" in body
        assert common.MARKETPLACE_JP in body, "marketplaceIds が必須"
        return FakeResponse(200, {"queryId": "Q-1"})
    if "/dataKiosk/2023-11-15/queries/Q-1" in url:
        dk_state["poll"] += 1
        if dk_state["poll"] < 2:
            return FakeResponse(200, {"processingStatus": "IN_PROGRESS"})
        return FakeResponse(200, {"processingStatus": "DONE", "dataDocumentId": "DD-1"})
    if "/dataKiosk/2023-11-15/documents/DD-1" in url:
        return FakeResponse(200, {"documentUrl": "https://x.invalid/d.jsonl"})
    if url == "https://x.invalid/d.jsonl":
        return FakeResponse(200, content=DK_JSONL.encode("utf-8"))
    raise AssertionError(f"想定外: {method} {url}")


install(dk_handler)

q = st.build_query("2026-08-30", "CHILD")
check("クエリにmarketplaceIdsが入る", common.MARKETPLACE_JP in q)
check("クエリが2024_04_24データセットを指す", "2024_04_24" in q)

st_rows = st.fetch_one_day("Atza|dummy", "2026-08-30", "CHILD")
check("1日分を取得できる", len(st_rows) == 1, f"→ {len(st_rows)}件")

if st_rows:
    row = dict(zip(st.HEADER, st_rows[0]))
    check("日付が入る", row["date"] == "2026-08-30", f"→ {row['date']}")
    check("子ASINが入る", row["childAsin"] == "B0CHILD001")
    check("売上金額を展開できる", row["orderedProductSales"] == 39800.0)
    check("通貨コードを展開できる", row["currency"] == "JPY")
    check("セッション数が入る", row["sessions"] == 250)
    check("転換率が入る", row["unitSessionPercentage"] == 4.0)
    check("skuがNoneでも空文字になる", row["sku"] == "")
    check("列数がヘッダーと一致する", len(st_rows[0]) == len(st.HEADER))

# 入れ子形式のJSONLでも読めるか
nested = json.dumps({st.DATASET: {"salesAndTrafficByAsin": [DK_RECORD]}}, ensure_ascii=False)
nested_rows = st.parse_jsonl(nested, "2026-08-30")
check("入れ子形式のJSONLも解釈できる", len(nested_rows) == 1, f"→ {len(nested_rows)}件")

# ---------------------------------------------------------------------------
print("\n④ 異常系")


def fatal_handler(method, url, **kwargs):
    if "/dataKiosk/2023-11-15/queries/Q-1" in url:
        return FakeResponse(200, {"processingStatus": "FATAL"})
    return dk_handler(method, url, **kwargs)


install(fatal_handler)
try:
    st.wait_for_query("t", "Q-1")
    check("FATAL時に例外を投げる", False, "→ 例外なし")
except RuntimeError as exc:
    check("FATAL時にBrand Analyticsロールを案内する", "Brand Analytics" in str(exc))


def cancelled_handler(method, url, **kwargs):
    if "/dataKiosk/2023-11-15/queries/Q-1" in url:
        return FakeResponse(200, {"processingStatus": "CANCELLED"})
    return dk_handler(method, url, **kwargs)


install(cancelled_handler)
data_id, _ = st.wait_for_query("t", "Q-1")
check("CANCELLED（データ0件）を例外にしない", data_id is None)


def auth_error_handler(method, url, **kwargs):
    if url == common.TOKEN_URL:
        return FakeResponse(400, {"error": "invalid_grant"})
    return orders_handler(method, url, **kwargs)


install(auth_error_handler)
try:
    common.get_access_token(cfg)
    check("認証エラーを握りつぶさない", False, "→ 例外なし")
except RuntimeError as exc:
    check("認証エラーの内容を例外に含める", "invalid_grant" in str(exc))

# ---------------------------------------------------------------------------
print("\n⑤ 手数料・入金内訳（finances.py）")

import finances as fin  # noqa: E402

SHIPMENT_EVENT = {
    "AmazonOrderId": "249-1111111-1111111",
    "PostedDate": "2026-08-30T12:00:00Z",
    "ShipmentItemList": [
        {
            "SellerSKU": "EXP-001",
            "QuantityShipped": 2,
            "ItemChargeList": [
                {"ChargeType": "Principal", "ChargeAmount": {"CurrencyAmount": 7960, "CurrencyCode": "JPY"}},
                {"ChargeType": "Tax", "ChargeAmount": {"CurrencyAmount": 796, "CurrencyCode": "JPY"}},
                {"ChargeType": "ShippingCharge", "ChargeAmount": {"CurrencyAmount": 500, "CurrencyCode": "JPY"}},
            ],
            "ItemFeeList": [
                {"FeeType": "Commission", "FeeAmount": {"CurrencyAmount": -796, "CurrencyCode": "JPY"}},
                {"FeeType": "FBAPerUnitFulfillmentFee", "FeeAmount": {"CurrencyAmount": -580, "CurrencyCode": "JPY"}},
            ],
            "PromotionList": [
                {"PromotionAmount": {"CurrencyAmount": -300, "CurrencyCode": "JPY"}}
            ],
        }
    ],
}

REFUND_EVENT = {
    "AmazonOrderId": "249-2222222-2222222",
    "PostedDate": "2026-08-31T09:00:00Z",
    "ShipmentItemAdjustmentList": [
        {
            "SellerSKU": "EXP-002",
            "QuantityShipped": 1,
            "ItemChargeAdjustmentList": [
                {"ChargeType": "Principal", "ChargeAmount": {"CurrencyAmount": -3980, "CurrencyCode": "JPY"}}
            ],
            "ItemFeeAdjustmentList": [
                {"FeeType": "Commission", "FeeAmount": {"CurrencyAmount": 398, "CurrencyCode": "JPY"}}
            ],
        }
    ],
}

SERVICE_FEE_EVENT = {
    "FeeReason": "FBAInventoryStorageFee",
    "SellerSKU": "EXP-001",
    "PostedDate": "2026-08-31T00:00:00Z",
    "FeeList": [
        {"FeeType": "FBAInventoryStorageFee", "FeeAmount": {"CurrencyAmount": -1200, "CurrencyCode": "JPY"}}
    ],
}

check("金額を取り出せる", fin.amount_of({"CurrencyAmount": 123.5}) == 123.5)
check("金額が無ければ0になる", fin.amount_of(None) == 0.0 and fin.amount_of({}) == 0.0)

ch = fin.classify_charges(SHIPMENT_EVENT["ShipmentItemList"][0]["ItemChargeList"])
check("商品代金を分離できる", ch[0] == 7960, f"→ {ch}")
check("税を分離できる", ch[1] == 796)
check("配送料を分離できる", ch[2] == 500)

fe = fin.classify_fees(SHIPMENT_EVENT["ShipmentItemList"][0]["ItemFeeList"])
check("販売手数料を分離できる（Commission）", fe[0] == -796, f"→ {fe}")
check("FBA手数料を分離できる", fe[1] == -580)
check("販売手数料はReferralFeeでも認識する",
      fin.classify_fees([{"FeeType": "ReferralFee", "FeeAmount": {"CurrencyAmount": -100}}])[0] == -100)

ship_rows = fin.flatten_shipment_events([SHIPMENT_EVENT], "出荷", "2026-08-30")
check("出荷イベントを展開できる", len(ship_rows) == 1)
if ship_rows:
    r = dict(zip(fin.HEADER, ship_rows[0]))
    check("日付を日付部分だけにする", r["postedDate"] == "2026-08-30", f"→ {r['postedDate']}")
    check("SKUが入る", r["sku"] == "EXP-001")
    check("プロモーション値引が入る", r["プロモーション値引"] == -300)
    # 7960 + 796 + 500 + 0 - 300 - 796 - 580 = 7580
    check("純額を正しく計算する", r["純額"] == 7580, f"→ {r['純額']}")

refund_rows = fin.flatten_shipment_events([REFUND_EVENT], "返金", "2026-08-31")
check("返金イベント（Adjustment形式）も展開できる", len(refund_rows) == 1)
if refund_rows:
    r = dict(zip(fin.HEADER, refund_rows[0]))
    check("返金は純額がマイナスになる", r["純額"] == -3582, f"→ {r['純額']}")

svc_rows = fin.flatten_service_fee_events([SERVICE_FEE_EVENT], "2026-08-31")
check("保管手数料を展開できる", len(svc_rows) == 1)
if svc_rows:
    r = dict(zip(fin.HEADER, svc_rows[0]))
    check("保管手数料がFBA手数料に入る", r["FBA手数料"] == -1200, f"→ {r['FBA手数料']}")
    check("理由がイベント種別に入る", "FBAInventoryStorageFee" in r["eventType"])

# ページング（payloadラップあり／なし両方）
fin_state = {"page": 0}


def fin_handler(method, url, **kwargs):
    if url == common.TOKEN_URL:
        return FakeResponse(200, {"access_token": "Atza|dummy"})
    if "finances/v0/financialEvents" in url:
        fin_state["page"] += 1
        if fin_state["page"] == 1:
            return FakeResponse(200, {"payload": {
                "FinancialEvents": {"ShipmentEventList": [SHIPMENT_EVENT]},
                "NextToken": "T2",
            }})
        # 2ページ目は payload ラップなしの形で返す
        return FakeResponse(200, {
            "FinancialEvents": {"ServiceFeeEventList": [SERVICE_FEE_EVENT]},
        })
    raise AssertionError(f"想定外: {method} {url}")


install(fin_handler)
fin.PAGE_SLEEP_SEC = 0
from datetime import datetime as _dt  # noqa: E402
paged = fin.fetch_financial_events(
    "t", _dt(2026, 8, 1, tzinfo=common.JST), _dt(2026, 8, 31, tzinfo=common.JST)
)
check("NextTokenで2ページ目まで取得する", len(paged) == 2, f"→ {len(paged)}件")
check("payloadラップの有無どちらでも読める", fin_state["page"] == 2)

# ---------------------------------------------------------------------------
print("\n⑥ 精算レポート（settlement.py）")

import settlement as st2  # noqa: E402
from run_all import JOBS as run_all_jobs  # noqa: E402

# 精算レポートは createReport でリクエストできない仕様。
# コード中にPOSTリクエストが存在しないこと（＝GETのみで取得していること）を確認する。
_settlement_src = open("settlement.py").read()
check("レポート生成をリクエストしていない（GETのみ）",
      '"POST"' not in _settlement_src,
      "→ POSTリクエストが含まれている")
check("V2形式を優先している", st2.REPORT_TYPE.endswith("_V2"))
check("旧形式のフォールバックがある", st2.LEGACY_REPORT_TYPE != st2.REPORT_TYPE)

# getReports の createdSince は90日より前を受け付けない（HTTP 400 になる）
check("createdSinceの上限が90日以内に収まっている",
      st2.MAX_SINCE_DAYS <= 90, f"→ {st2.MAX_SINCE_DAYS}日")
check("--full の精算件数が90日で取得可能な範囲に収まっている",
      next(j[3] for j in run_all_jobs if j[1] == "settlement.py") <= 6,
      "→ 90日では6サイクル程度しか存在しない")

SETTLE_TSV = (
    "settlement-id\tsettlement-start-date\tposted-date\ttransaction-type\tamount\n"
    "12345678901\t2026-08-01\t2026-08-15\tOrder\t7580\n"
)


def settle_handler(method, url, **kwargs):
    if url == common.TOKEN_URL:
        return FakeResponse(200, {"access_token": "Atza|dummy"})
    if url.endswith("/reports/2021-06-30/reports"):
        return FakeResponse(200, {"reports": [
            {"reportId": "S1", "reportDocumentId": "SD1", "processingStatus": "DONE",
             "dataStartTime": "2026-08-01T00:00:00Z", "dataEndTime": "2026-08-15T00:00:00Z"},
            {"reportId": "S0", "reportDocumentId": "SD0", "processingStatus": "IN_PROGRESS",
             "dataEndTime": "2026-08-20T00:00:00Z"},
        ]})
    if "/documents/SD1" in url:
        return FakeResponse(200, {"url": "https://x.invalid/s.tsv"})
    if url == "https://x.invalid/s.tsv":
        return FakeResponse(200, content=SETTLE_TSV.encode("cp932"))
    raise AssertionError(f"想定外: {method} {url}")


install(settle_handler)
found = st2.list_settlement_reports("t", st2.REPORT_TYPE, _dt(2026, 3, 1, tzinfo=common.JST))
check("DONE以外を除外する", len(found) == 1, f"→ {len(found)}件")
srows = st2.download_settlement("t", found[0])
check("精算レポートを読める", len(srows) == 2 and srows[1][0] == "12345678901")

# ---------------------------------------------------------------------------
print("\n⑥-2 日時のタイムゾーン（400エラーの再発防止）")

from datetime import datetime as _DT  # noqa: E402

tz_state = {"params": None}


def tz_handler(method, url, **kwargs):
    if url == common.TOKEN_URL:
        return FakeResponse(200, {"access_token": "Atza|dummy"})
    if "finances/v0/financialEvents" in url:
        tz_state["params"] = kwargs.get("params")
        return FakeResponse(200, {"payload": {"FinancialEvents": {}}})
    raise AssertionError(f"想定外: {method} {url}")


install(tz_handler)
_jst_now = _DT(2026, 8, 31, 22, 53, tzinfo=common.JST)
fin.fetch_financial_events("t", _jst_now - timedelta(days=30), _jst_now)
_sent = tz_state["params"]["PostedBefore"]
check("JSTをUTCへ変換してから送る（9時間ずれない）",
      _sent == "2026-08-31T13:53:00Z", f"→ {_sent}")
check("PostedAfterもUTCになる",
      tz_state["params"]["PostedAfter"] == "2026-08-01T13:53:00Z",
      f"→ {tz_state['params']['PostedAfter']}")

_sent_utc = st2.list_settlement_reports.__doc__ is not None  # 実呼び出しは下で
settle_state = {"params": None}


def settle_tz_handler(method, url, **kwargs):
    if url.endswith("/reports/2021-06-30/reports"):
        settle_state["params"] = kwargs.get("params")
        return FakeResponse(200, {"reports": []})
    raise AssertionError(f"想定外: {method} {url}")


install(settle_tz_handler)
st2.list_settlement_reports("t", st2.REPORT_TYPE, _jst_now)
check("精算レポートのcreatedSinceもUTCになる",
      settle_state["params"]["createdSince"] == "2026-08-31T13:53:00Z",
      f"→ {settle_state['params']['createdSince']}")

# ---------------------------------------------------------------------------
print("\n⑥-3 スプレッドシート通信のリトライ（今回の欠損の再発防止）")

class FakeAPIError(Exception):
    def __init__(self, status):
        super().__init__(f"APIError {status}")
        self.response = types.SimpleNamespace(status_code=status)

# 一時的な不調と判定すべきもの
for label, exc in [
    ("RemoteDisconnected", Exception("('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))")),
    ("Connection reset", Exception("Connection reset by peer")),
    ("読み込みタイムアウト", Exception("Read timed out")),
    ("HTTP 503", FakeAPIError(503)),
    ("HTTP 429", FakeAPIError(429)),
]:
    check(f"{label} を再試行対象と判定する", common._is_transient(exc))

# 再試行してはいけないもの（権限エラーなどは即座に知らせるべき）
for label, exc in [
    ("HTTP 403（権限不足）", FakeAPIError(403)),
    ("HTTP 404", FakeAPIError(404)),
    ("プログラムの誤り", ValueError("列が見つかりません")),
]:
    check(f"{label} は再試行しない", not common._is_transient(exc))

# 実際に再試行して成功すること
attempts = {"n": 0}

def flaky():
    attempts["n"] += 1
    if attempts["n"] < 3:
        raise Exception("('Connection aborted.', RemoteDisconnected('...'))")
    return "成功"

_sleep = common.time.sleep
common.time.sleep = lambda s: None       # 待たずに試す
try:
    result = common.sheet_retry("テスト", flaky)
    check("切断されても再試行して成功する", result == "成功" and attempts["n"] == 3,
          f"→ {attempts['n']}回")

    # 再試行しても駄目なら、握りつぶさず例外を上げる
    try:
        common.sheet_retry("テスト", lambda: (_ for _ in ()).throw(
            Exception("Connection aborted. RemoteDisconnected")))
        check("上限まで試したら例外を上げる", False, "→ 例外なし")
    except Exception:
        check("上限まで試したら例外を上げる", True)

    # 恒久的なエラーは即座に上げる（無駄に待たない）
    perm = {"n": 0}
    def forbidden():
        perm["n"] += 1
        raise FakeAPIError(403)
    try:
        common.sheet_retry("テスト", forbidden)
    except Exception:
        pass
    check("権限エラーは1回で諦める", perm["n"] == 1, f"→ {perm['n']}回")
finally:
    common.time.sleep = _sleep

_common_src = open("common.py").read()
for label, target in [
    ("読み込み", "worksheet.get_all_values"),
    ("書き込み", "行目までの書き込み"),
    ("シートを開く", "スプレッドシートを開く"),
    ("実行ログの追記", "実行ログの追記"),
    ("書式の適用", "書式の適用"),
]:
    check(f"{label}がリトライで包まれている", target in _common_src)

# ---------------------------------------------------------------------------
print("\n⑦ 蓄積方式（merge_and_write）")

# read_sheet / write_rows を差し替えて、マージ結果だけを検証する
_written = {}


def fake_write_rows(cfg, name, rows):
    _written[name] = rows


def make_fake_read(existing):
    def _read(cfg, name):
        return existing
    return _read


common.write_rows = fake_write_rows
_dummy_cfg = common.Config()

# --- 期間差し替え（date_col + window_start）---
HDR = ["date", "asin", "sales"]
OLD = [HDR,
       ["2026-08-01", "A1", "100"],   # 期間外 → 残る
       ["2026-08-10", "A1", "200"],   # 期間内 → 消える
       ["2026-08-11", "A2", "300"]]   # 期間内 → 消える
NEW = [["2026-08-10", "A1", "999"], ["2026-08-12", "A3", "50"]]

common.read_sheet = make_fake_read(OLD)
common.merge_and_write(_dummy_cfg, "s", HDR, NEW,
                       date_col=0, window_start="2026-08-10", sort_cols=[0])
res = _written["s"]
check("期間外の古い行を保持する", ["2026-08-01", "A1", "100"] in res)
check("期間内の古い行を置き換える", ["2026-08-10", "A1", "200"] not in res)
check("新しい行が入る", ["2026-08-10", "A1", "999"] in res)
check("行数が正しい（ヘッダー+1+2）", len(res) == 4, f"→ {len(res)}行")
check("日付順に並ぶ", [r[0] for r in res[1:]] == sorted(r[0] for r in res[1:]))

# --- キー差し替え（key_cols）---
OLD2 = [HDR,
        ["2026-07-01", "A1", "10"],
        ["2026-08-05", "A2", "20"]]
NEW2 = [["2026-08-05", "A2", "77"], ["2026-09-01", "A9", "88"]]
common.read_sheet = make_fake_read(OLD2)
common.merge_and_write(_dummy_cfg, "s2", HDR, NEW2, key_cols=[0], sort_cols=[0])
res2 = _written["s2"]
check("同じキーの行だけ置き換える", ["2026-08-05", "A2", "77"] in res2 and ["2026-08-05", "A2", "20"] not in res2)
check("別キーの行は残る", ["2026-07-01", "A1", "10"] in res2)

# --- 列構成が変わったら全置換 ---
common.read_sheet = make_fake_read([["date", "asin"], ["2026-08-01", "A1"]])
common.merge_and_write(_dummy_cfg, "s3", HDR, NEW, date_col=0, window_start="2026-08-10")
check("列構成が変わったら全置換する", len(_written["s3"]) == len(NEW) + 1)

# --- --replace は既存を捨てる ---
common.read_sheet = make_fake_read(OLD)
common.merge_and_write(_dummy_cfg, "s4", HDR, NEW,
                       date_col=0, window_start="2026-08-10", replace=True)
check("--replaceで全置換する", len(_written["s4"]) == len(NEW) + 1)

# --- 既存が空でも動く ---
common.read_sheet = make_fake_read([])
common.merge_and_write(_dummy_cfg, "s5", HDR, NEW, date_col=0, window_start="2026-08-10")
check("既存シートが無くても動く", len(_written["s5"]) == len(NEW) + 1)

# --- 列数が足りない既存行を壊さない ---
common.read_sheet = make_fake_read([HDR, ["2026-08-01", "A1"]])
common.merge_and_write(_dummy_cfg, "s6", HDR, NEW, date_col=0, window_start="2026-08-10")
check("列数が足りない既存行を補完する",
      all(len(r) == 3 for r in _written["s6"]))

# ---------------------------------------------------------------------------
print("\n⑧ 集計ダッシュボード（dashboard.py）")

import dashboard as dash  # noqa: E402

check("数値変換ができる", dash.to_float("1,234") == 1234.0)
check("通貨記号・%を除去する", dash.to_float("¥1,234") == 1234.0 and dash.to_float("12.5%") == 12.5)
check("空文字は0になる", dash.to_float("") == 0.0 and dash.to_float(None) == 0.0)
check("数値でない文字列は0になる", dash.to_float("該当なし") == 0.0)
check("割合を計算できる", dash.pct(5, 100) == 5.0)
check("分母0でも落ちない", dash.pct(5, 0) == "")

SHEETS = {
    "raw_orders": [
        ["amazon-order-id", "purchase-date", "sku", "product-name", "asin", "quantity"],
        ["249-1", "2026-08-20", "EXP-001", "テスト商品Ａ", "B0AAA", "1"],
        ["249-2", "2026-08-21", "EXP-002", "テスト商品Ｂ", "B0BBB", "2"],
    ],
    "raw_sales_traffic": [
        ["date", "parentAsin", "childAsin", "sku", "unitsOrdered", "orderedProductSales",
         "currency", "totalOrderItems", "unitsShipped", "unitsRefunded", "refundRate",
         "sessions", "pageViews", "browserSessions", "mobileAppSessions",
         "buyBoxPercentage", "unitSessionPercentage"],
        ["2026-08-20", "B0AAA", "B0AAA", "", "10", "39800", "JPY", "8", "10", "1", "10",
         "200", "300", "80", "120", "98", "5"],
        ["2026-08-21", "B0BBB", "B0BBB", "", "5", "12800", "JPY", "5", "5", "0", "0",
         "100", "150", "40", "60", "95", "5"],
        ["2026-01-01", "B0AAA", "B0AAA", "", "999", "999999", "JPY", "0", "0", "0", "0",
         "0", "0", "0", "0", "0", "0"],   # 期間外 → 集計対象外
    ],
    "raw_finances": [
        ["postedDate", "eventType", "amazonOrderId", "sku", "quantity", "商品代金", "税",
         "配送料", "その他売上", "プロモーション値引", "販売手数料", "FBA手数料",
         "その他手数料", "純額"],
        ["2026-08-20", "出荷", "249-1", "EXP-001", "1", "39800", "0", "0", "0", "0",
         "-3980", "-2900", "0", "32920"],
        ["2026-08-21", "出荷", "249-2", "EXP-002", "2", "12800", "0", "0", "0", "0",
         "-1280", "-1160", "0", "10360"],
        ["2026-08-21", "手数料（保管料）", "", "", "", "0", "0", "0", "0", "0",
         "0", "-500", "0", "-500"],   # SKU無し → 紐づかない費用
    ],
}
common.read_sheet = lambda cfg, name: SHEETS.get(name, [])
dash.read_sheet = common.read_sheet

sku_map = dash.load_sku_map(_dummy_cfg)
check("SKU→ASIN対応を作れる", sku_map.get("EXP-001", ("",))[0] == "B0AAA", f"→ {sku_map}")
check("商品名も拾える", sku_map.get("EXP-001", ("", ""))[1] == "テスト商品Ａ")

st_asin, st_day = dash.load_sales_traffic(_dummy_cfg, "2026-08-01")
check("期間外の行を除外する", "999999" not in str(st_asin), f"→ {dict(st_asin)}")
check("ASIN別に集計できる", st_asin["B0AAA"]["sales"] == 39800.0)
check("セッションを集計できる", st_asin["B0AAA"]["sessions"] == 200.0)

fin_asin, fin_tot, fin_days = dash.load_finances(_dummy_cfg, "2026-08-01", sku_map)
check("SKUからASINへ手数料を紐付ける", fin_asin["B0AAA"]["net"] == 32920.0, f"→ {dict(fin_asin)}")
check("手数料合計を集計できる", fin_asin["B0AAA"]["fees"] == -6880.0)
check("SKU不明の費用を別枠に集計する", fin_tot["unmapped"] == -500.0, f"→ {fin_tot['unmapped']}")

table = dash.build_product_table(st_asin, fin_asin, sku_map)
check("商品別テーブルを作れる", len(table) == 2, f"→ {len(table)}件")
check("売上の大きい順に並ぶ", table[0][0] == "B0AAA")
if table:
    r = dict(zip(dash.PRODUCT_HEADER, table[0]))
    check("転換率を計算できる（10/200=5%）", r["転換率%"] == 5.0, f"→ {r['転換率%']}")
    check("実入金率を計算できる（32920/39800≒82.7%）",
          abs(r["実入金率%"] - 82.71) < 0.1, f"→ {r['実入金率%']}")
    check("商品名が入る", r["商品名"] == "テスト商品Ａ")

orders_sales, orders_days = dash.load_orders_sales(_dummy_cfg, "2026-08-01", "2026-08-31")
summary, warns = dash.build_summary(
    st_day, fin_tot, "2026-08-01", "2026-08-31", 30, orders_sales, fin_days
)
flat = {row[0]: row[1] for row in summary if len(row) == 2}
check("サマリーに売上が入る", flat["売上"] == 52600)
check("サマリーに転換率が入る", flat["転換率%"] == 5.0, f"→ {flat['転換率%']}")

# --- データ充足状況の警告（今回の不具合の再発防止）---
check("データ不足を検知して警告する", len(warns) >= 1, f"→ {warns}")
check("不足日数を明示する", "2/30日" in str(flat.get("ASIN別売上のデータ", "")),
      f"→ {flat.get('ASIN別売上のデータ')}")
check("埋め方のコマンドを案内する", any("sales_traffic.py --days 30" in w for w in warns))

# データが揃っていれば警告を出さない
full_days = {f"2026-08-{d:02d}": {"sales": 1.0} for d in range(1, 31)}
_s, _w = dash.build_summary(full_days, fin_tot, "2026-08-01", "2026-08-31", 30, 30.0, set(full_days))
check("揃っていれば警告しない", _w == [], f"→ {_w}")

# 売上の食い違いを検知する
_s2, _ = dash.build_summary(st_day, fin_tot, "2026-08-01", "2026-08-31", 30, 1000000.0, fin_days)
_flat2 = [str(r[0]) for r in _s2 if r]
check("2つの売上が乖離したら警告する",
      any("10%以上ずれ" in t for t in _flat2), "→ 乖離警告なし")

# ---------------------------------------------------------------------------
print("\n⑧-2 売上→実入金の橋渡し")

BRIDGE_TOTALS = {
    "net": 43280.0, "fees": -8160.0, "unmapped": -500.0,
    "principal": 52600.0, "tax_shipping": 0.0, "promo": -1160.0,
    "referral": -5260.0, "fba": -4060.0, "other_fees": 1160.0,
}
bridge = dash.build_bridge(50000.0, BRIDGE_TOTALS)
bflat = {str(r[0]): (r[1] if len(r) > 1 else None) for r in bridge if r}
check("売上から始まる", "売上（Amazon公式・注文ベース）" in bflat)
check("出荷タイミングのズレを出す（52600-50000=2600）",
      bflat["  出荷タイミングのズレ"] == 2600, f"→ {bflat.get('  出荷タイミングのズレ')}")
check("各手数料を分けて表示する",
      "− 販売手数料" in bflat and "− FBA手数料" in bflat)
check("プロモーション値引を独立表示する", "− プロモーション値引" in bflat)
check("実入金額で終わる", bflat["＝ 実入金額（純額）"] == 43280)
check("内訳の合計が純額と一致すれば警告しない",
      not any("一致しません" in str(r[0]) for r in bridge if r))

# 合計が合わないケースは警告する
BAD = dict(BRIDGE_TOTALS)
BAD["net"] = 99999.0
bad_bridge = dash.build_bridge(50000.0, BAD)
check("内訳と純額がずれたら警告する",
      any("一致しません" in str(r[0]) for r in bad_bridge if r))

# 手数料データが無いときは案内を出す
empty_bridge = dash.build_bridge(50000.0, {
    "net": 0.0, "fees": 0.0, "unmapped": 0.0, "principal": 0.0,
    "tax_shipping": 0.0, "promo": 0.0, "referral": 0.0, "fba": 0.0, "other_fees": 0.0,
})
check("手数料データが無ければ案内を出す",
      any("finances.py" in str(r[0]) for r in empty_bridge if r))

# ---------------------------------------------------------------------------
print("\n⑧-3 差異の診断（diagnose.py）")

import diagnose as diag  # noqa: E402

DIAG_SHEETS = dict(SHEETS)
DIAG_SHEETS["raw_orders"] = [
    ["amazon-order-id", "purchase-date", "sku", "product-name", "asin", "quantity",
     "item-price", "shipping-price", "item-tax", "order-status", "sales-channel",
     "fulfillment-channel", "is-business-order"],
    ["249-1", "2026-08-20", "EXP-001", "商品Ａ", "B0AAA", "1",
     "39800", "500", "3980", "Shipped", "Amazon.co.jp", "AFN", "false"],
    ["249-2", "2026-08-21", "EXP-002", "商品Ｂ", "B0BBB", "2",
     "12800", "0", "1280", "Shipped", "Amazon.co.jp", "AFN", "false"],
    ["249-3", "2026-08-25", "EXP-001", "商品Ａ", "B0AAA", "1",
     "", "0", "", "Pending", "Amazon.co.jp", "AFN", "false"],       # 価格が空欄
    ["249-4", "2026-08-26", "EXP-002", "商品Ｂ", "B0BBB", "1",
     "5000", "0", "500", "Cancelled", "Amazon.co.jp", "AFN", "false"],
]
diag.read_sheet = lambda cfg, name: DIAG_SHEETS.get(name, [])

o = diag.analyze_orders(_dummy_cfg, "2026-08-01", "2026-08-31")
check("注文を状態別に分けられる", set(o["by_status"]) == {"Shipped", "Pending", "Cancelled"},
      f"→ {set(o['by_status'])}")
check("価格が空欄の行を数える", o["by_status"]["Pending"]["blank_price"] == 1)
check("キャンセルを別枠にする", o["by_status"]["Cancelled"]["amount"] == 5000.0)
check("配送料を集計する", o["shipping"] == 500.0)
check("消費税を集計する", o["tax"] == 5760.0, f"→ {o['tax']}")
check("販路別に分けられる", "Amazon.co.jp" in o["by_channel"])

d = diag.analyze_sales_traffic(_dummy_cfg, "2026-08-01", "2026-08-31")
check("ASIN別売上を集計できる", d["total"] == 52600.0, f"→ {d['total']}")
check("欠落日を検出できる（注文4日 vs 売上2日）",
      len(o["dates"] - d["dates"]) == 2, f"→ {o['dates'] - d['dates']}")

# ---------------------------------------------------------------------------
print("\n⑧-4 月別集計（aggregate.py / monthly.py）")

import aggregate as agg  # noqa: E402
import monthly as mon  # noqa: E402

# 8月と7月の2ヶ月分。8月は売上増・転換率低下、7月は基準。
MONTH_SHEETS = {
    "raw_orders": [
        ["amazon-order-id", "purchase-date", "sku", "product-name", "asin", "item-price", "order-status"],
        ["1", "2026-07-10", "EXP-001", "商品Ａ", "B0AAA", "10000", "Shipped"],
        ["2", "2026-08-10", "EXP-002", "商品Ｂ", "B0BBB", "20000", "Shipped"],
    ],
    "raw_sales_traffic": [
        ["date", "parentAsin", "childAsin", "sku", "unitsOrdered", "orderedProductSales",
         "currency", "totalOrderItems", "unitsShipped", "unitsRefunded", "refundRate",
         "sessions", "pageViews", "browserSessions", "mobileAppSessions",
         "buyBoxPercentage", "unitSessionPercentage"],
        # 7月: 売上100,000 / 個数50 / セッション1000 → 転換率5%
        ["2026-07-15", "B0AAA", "B0AAA", "", "50", "100000", "JPY", "", "", "2", "",
         "1000", "", "", "", "", ""],
        # 8月: 売上150,000 / 個数60 / セッション2000 → 転換率3%（セッション増・転換率低下）
        ["2026-08-10", "B0AAA", "B0AAA", "", "40", "100000", "JPY", "", "", "1", "",
         "1400", "", "", "", "", ""],
        ["2026-08-11", "B0BBB", "B0BBB", "", "20", "50000", "JPY", "", "", "0", "",
         "600", "", "", "", "", ""],
    ],
    "raw_finances": [
        ["postedDate", "eventType", "amazonOrderId", "sku", "quantity", "商品代金", "税",
         "配送料", "その他売上", "プロモーション値引", "販売手数料", "FBA手数料",
         "その他手数料", "純額"],
        ["2026-07-15", "出荷", "1", "EXP-001", "50", "100000", "0", "0", "0", "0",
         "-10000", "-5000", "0", "85000"],
        ["2026-08-10", "出荷", "2", "EXP-001", "40", "100000", "0", "0", "0", "0",
         "-10000", "-5000", "0", "85000"],
        ["2026-08-11", "出荷", "3", "EXP-002", "20", "50000", "0", "0", "0", "0",
         "-5000", "-2500", "0", "42500"],
    ],
}
common.read_sheet = lambda cfg, name: MONTH_SHEETS.get(name, [])
agg.read_sheet = common.read_sheet

data = agg.Data(_dummy_cfg)
check("SKU→ASIN対応を読める", data.sku_map.get("EXP-001", ("",))[0] == "B0AAA")
check("トラフィック行を読める", len(data.traffic) == 3, f"→ {len(data.traffic)}")
check("財務行を読める", len(data.finances) == 3)

months = data.months_available()
check("存在する月を新しい順に返す", months[:2] == ["2026-08", "2026-07"], f"→ {months}")

aug = data.summarize("2026-08")
jul = data.summarize("2026-07")
check("月の売上を集計できる", aug["total"]["sales"] == 150000.0, f"→ {aug['total']['sales']}")
check("月の実入金を集計できる", aug["total"]["net"] == 127500.0, f"→ {aug['total']['net']}")
check("月の手数料を集計できる", aug["total"]["fees"] == -22500.0, f"→ {aug['total']['fees']}")
check("日別に分解できる", len(aug["by_day"]) == 2, f"→ {len(aug['by_day'])}")
check("ASIN別に分解できる", len(aug["by_asin"]) == 2)
check("存在しない月は0で返す", data.summarize("2020-01")["total"]["sales"] == 0.0)

check("前月の計算が正しい", mon.prev_month("2026-08") == "2026-07")
check("年をまたぐ前月も正しい", mon.prev_month("2026-01") == "2025-12")
check("月の日数を返す", mon.days_in_month("2026-02") == 28 and mon.days_in_month("2026-08") == 31)

# 着地予想は「進行中の月」だけに出るため、8月を当月とみなして検証する
mon.current_month = lambda: "2026-08"

summary = mon.build_summary("2026-08", aug, jul)
sflat = {str(r[0]): r for r in summary if r}

# --- 手数料の内訳 ---
check("手数料を内訳で表示する",
      all(k in sflat for k in ("販売手数料（紹介料）", "FBA手数料（配送・保管）",
                               "その他手数料", "プロモーション値引", "手数料合計")))
check("販売手数料の金額が正しい", sflat["販売手数料（紹介料）"][1] == -15000,
      f"→ {sflat['販売手数料（紹介料）'][1]}")
check("FBA手数料の金額が正しい", sflat["FBA手数料（配送・保管）"][1] == -7500)
check("内訳の合計が手数料合計と一致する",
      sflat["販売手数料（紹介料）"][1] + sflat["FBA手数料（配送・保管）"][1]
      + sflat["その他手数料"][1] == sflat["手数料合計"][1])
check("対売上比%を出す", "対売上比%" in sflat)

# --- 1日あたり販売個数 ---
check("1日あたり販売個数を表示する", "1日あたり販売個数" in sflat)
check("1日あたりはデータのある日数で割る（60個÷2日=30）",
      sflat["1日あたり販売個数"][1] == 30.0, f"→ {sflat['1日あたり販売個数'][1]}")
check("平均単価を表示する（150000÷60=2500）",
      sflat["平均単価"][1] == 2500, f"→ {sflat['平均単価'][1]}")

# --- 着地予想 ---
labels = [str(r[0]) for r in summary if r]
check("着地予想の3行を出す",
      all(k in sflat for k in ("着地予想 売上", "着地予想 販売個数", "着地予想 実入金額")))
check("着地予想 売上は売上の1行下",
      labels[labels.index("売上") + 1] == "着地予想 売上", f"→ {labels[:6]}")
check("着地予想 販売個数は1日あたり販売個数の下",
      labels[labels.index("1日あたり販売個数") + 1] == "着地予想 販売個数")
check("着地予想 実入金額は実入金率%の下",
      labels[labels.index("実入金率%") + 1] == "着地予想 実入金額")
check("着地予想 売上＝平均×日数（150000÷2×31）",
      sflat["着地予想 売上"][1] == 2325000, f"→ {sflat['着地予想 売上'][1]}")
check("着地予想 販売個数＝平均×日数（60÷2×31）",
      sflat["着地予想 販売個数"][1] == 930, f"→ {sflat['着地予想 販売個数'][1]}")
check("着地予想 実入金額は入金データのある日数で割る（127500÷2×31）",
      sflat["着地予想 実入金額"][1] == 1976250, f"→ {sflat['着地予想 実入金額'][1]}")
check("着地予想の前月列は前月の確定実績",
      sflat["着地予想 売上"][2] == 100000, f"→ {sflat['着地予想 売上'][2]}")
check("着地予想にも増減率が入る",
      sflat["着地予想 売上"][4] == 2225.0, f"→ {sflat['着地予想 売上'][4]}")
check("経過日数を見出しの右に置く",
      summary[0][:2] == ["■ 月間サマリー", "経過日数 2/31日"], f"→ {summary[0]}")

# 終わった月には出さない（確定値なので予想は誤解のもと）
past = mon.build_summary("2026-07", jul, data.summarize("2026-06"))
check("過去の月には着地予想を出さない",
      not any(str(r[0]).startswith("着地予想") for r in past if r))
check("過去の月の見出しに経過日数を付けない",
      past[0] == ["■ 月間サマリー"], f"→ {past[0]}")

# データが1日も無ければ0除算せず、行そのものを出さない
empty = mon.build_summary("2026-08", data.summarize("2020-01"), jul)
check("データが無い月は着地予想を省く",
      not any(str(r[0]).startswith("着地予想") for r in empty if r))

check("書式に着地予想の行を登録している",
      __import__("sheet_format").ROW_TYPES.get("着地予想 売上") == "money"
      and __import__("sheet_format").ROW_TYPES.get("着地予想 販売個数") == "num"
      and __import__("sheet_format").ROW_TYPES.get("着地予想 実入金額") == "money")

# --- 指数による要因分析 ---
check("売上指数を出す（150000÷100000=150）", sflat["売上指数"][1] == 150.0,
      f"→ {sflat['売上指数'][1]}")
check("セッション指数を出す（2000÷1000=200）",
      sflat["　うち セッション指数"][1] == 200.0)
check("転換率指数を出す（3%÷5%=60）", sflat["　うち 転換率指数"][1] == 60.0)
check("平均単価指数を出す（2500÷2000=125）",
      sflat["　うち 平均単価指数"][1] == 125.0)
check("3指数の積が売上指数と一致する（200×60×125÷10000=150）",
      sflat["検算（3指数の積）"][1] == 150.0, f"→ {sflat['検算（3指数の積）'][1]}")
check("検算が合えば警告を出さない",
      not any("検算が売上指数と一致しません" in str(r[0]) for r in summary if r))
check("サマリーに前月比が入る", sflat["売上"][2] == 100000, f"→ {sflat['売上']}")
check("増減を計算する", "+50,000" in str(sflat["売上"][3]), f"→ {sflat['売上'][3]}")
check("増減率を計算する", sflat["売上"][4] == 50.0, f"→ {sflat['売上'][4]}")
check("増加に▲を付ける", "▲" in str(sflat["売上"][3]))
check("転換率の低下を検出する", sflat["転換率%"][1] < sflat["転換率%"][2],
      f"→ 今月{sflat['転換率%'][1]} 前月{sflat['転換率%'][2]}")

# 売上変動の要因分析：セッション+100%、転換率-40% → セッションが主因
cause = [r for r in summary if r and str(r[0]) == "主因"]
check("売上変動の主因を判定する", len(cause) == 1, f"→ {cause}")
if cause:
    check("セッション変動を主因と判定する", "セッション" in str(cause[0][1]), f"→ {cause[0][1]}")

check("データ不足を月別シートでも警告する",
      any("⚠ 不足" in str(c) for r in summary for c in r))

daily = mon.build_daily(aug)
check("日別が日付順に並ぶ", daily[2][0] == "2026-08-10" and daily[3][0] == "2026-08-11")
check("日別に合計行が付く", daily[-2][0] == "合計", f"→ {daily[-2][0]}")
check("合計行が月間売上と一致する", daily[-2][1] == 150000)

products = mon.build_products(aug, jul, data.asin_names())
check("商品別が売上順に並ぶ", products[2][1] == "B0AAA" and products[3][1] == "B0BBB")
check("商品別に前月比が入る（100000→100000で0%）", products[2][4] == 0.0, f"→ {products[2][4]}")
check("前月に無い商品は前月比が空", products[3][4] == "", f"→ {products[3][4]}")

sheet = mon.build_month_sheet("2026-08", aug, jul, data.asin_names())
check("月別シートが3ブロックを含む",
      all(any(str(r[0]).startswith(b) for r in sheet if r)
          for b in ("■ 月間サマリー", "■ 日別の推移", "■ 商品別ランキング")))
check("シート冒頭に年月が入る", "2026年8月" in str(sheet[0][0]))

trend = mon.build_trend(data, ["2026-07", "2026-08"])
trows = [r for r in trend if r and str(r[0]).startswith("2026-")]
check("月次推移が古い順に並ぶ", trows[0][0] == "2026-07" and trows[1][0] == "2026-08")
check("推移に前月比が入る", trows[1][2] == 50.0, f"→ {trows[1][2]}")
check("最初の月は前月比が空", trows[0][2] == "")
check("推移にデータ充足が入る", "31日" in str(trows[1][-1]), f"→ {trows[1][-1]}")

# ---------------------------------------------------------------------------
print("\n⑧-5 シートの書式（sheet_format.py）")

import sheet_format as sf  # noqa: E402

fmt_rows = mon.build_month_sheet("2026-08", aug, jul, data.asin_names())
reqs = sf.build_requests(123, fmt_rows)

def has_pattern(pattern):
    return any(pattern in str(r) for r in reqs)

check("書式リクエストを生成する", len(reqs) > 0, f"→ {len(reqs)}件")
check("すべて対象シートIDを指す",
      all(str(r).count("'sheetId': 123") >= 1 for r in reqs))
check("金額を通貨・小数なしにする", has_pattern("¥#,##0"))
check("マイナス金額を赤にする", has_pattern("[Red]-¥#,##0"))
check("パーセントに%を付ける", has_pattern('0.00"%"'))
check("件数を桁区切りにする", any(r == "#,##0" for r in
      [d.get("pattern") for req in reqs
       for d in [req.get("repeatCell", {}).get("cell", {})
                 .get("userEnteredFormat", {}).get("numberFormat", {})]]))
check("タイトル行に色を付ける",
      any(r.get("repeatCell", {}).get("range", {}).get("startRowIndex") == 0
          and "backgroundColor" in str(r) for r in reqs))
check("表ヘッダーを白文字にする",
      any("'foregroundColor': {'red': 1.0" in str(r) for r in reqs))
check("合計行を強調する",
      any(str(sf.TOTAL_BG) in str(r) for r in reqs))
check("表に罫線を引く", has_pattern("updateBorders"))
check("列幅を設定する", has_pattern("updateDimensionProperties"))
check("グリッド線を消す", has_pattern("hideGridlines"))

# 前回の書式が残る不具合の再発防止
reset = reqs[0].get("repeatCell", {})
check("最初に既存の書式を全消去する",
      reset.get("fields") == "userEnteredFormat"
      and reset.get("cell", {}).get("userEnteredFormat") == {},
      f"→ {reset.get('fields')}")
check("消去の対象がシート全体である（行列の指定なし）",
      set(reset.get("range", {}).keys()) == {"sheetId"},
      f"→ {reset.get('range')}")
check("消去が他のどの書式よりも先に実行される",
      all(i == 0 or r.get("repeatCell", {}).get("fields") != "userEnteredFormat"
          for i, r in enumerate(reqs)))

# サマリー系のブロックにも枠線が引かれること（今回の指摘の再発防止）
border_reqs = [r for r in reqs if "updateBorders" in r]
check("枠線を複数ブロックに引く", len(border_reqs) >= 4, f"→ {len(border_reqs)}ブロック")

def block_at(label):
    """指定ラベルの行が、いずれかの枠線範囲に含まれるか"""
    idx = next((i for i, r in enumerate(fmt_rows) if r and str(r[0]) == label), None)
    if idx is None:
        return False
    return any(b["updateBorders"]["range"]["startRowIndex"] <= idx
               < b["updateBorders"]["range"]["endRowIndex"] for b in border_reqs)

check("月間サマリーに枠線が付く", block_at("売上"))
check("売上変動の要因に枠線が付く", block_at("主因"))
check("データの充足状況に枠線が付く", block_at("ASIN別売上"))
check("日別の推移に枠線が付く", block_at("合計"))
check("サマリーの見出し行も表ヘッダーと同じ体裁にする",
      any(r.get("repeatCell", {}).get("range", {}).get("startRowIndex")
          == next(i for i, x in enumerate(fmt_rows) if x and str(x[0]) == "指標")
          and "0.39" in str(r) for r in reqs))
# 注記行（※）は表の外に置く
note_idx = next(i for i, r in enumerate(fmt_rows) if r and str(r[0]).startswith("※"))
check("注記行を枠線の外に出す",
      not any(b["updateBorders"]["range"]["startRowIndex"] <= note_idx
              < b["updateBorders"]["range"]["endRowIndex"] for b in border_reqs))
check("注記行を控えめな文字色にする",
      any(r.get("repeatCell", {}).get("range", {}).get("startRowIndex") == note_idx
          and "italic" in str(r) for r in reqs))
check("ラベル列を強調する",
      any(r.get("repeatCell", {}).get("range", {}).get("endColumnIndex") == 1
          for r in reqs))

# 数値でない行に数値書式を当てていないこと
title_reqs = [r for r in reqs
              if r.get("repeatCell", {}).get("range", {}).get("startRowIndex") == 0]
check("タイトル行に数値書式を当てない",
      all("numberFormat" not in str(r) for r in title_reqs))

# 月次推移シートも書式化できる
trend_reqs = sf.build_requests(456, mon.build_trend(data, ["2026-07", "2026-08"]))
check("月次推移シートも書式化できる", len(trend_reqs) > 0)
check("月次推移でも通貨書式が付く",
      any("¥#,##0" in str(r) for r in trend_reqs))

# 空や崩れた入力で落ちないこと
check("空の行があっても落ちない", len(sf.build_requests(1, [["題"], [], ["■ 見出し"]])) > 0)
check("1行だけでも落ちない", len(sf.build_requests(1, [["題"]])) > 0)

# ---------------------------------------------------------------------------
print("\n⑨ 自動実行（run_all.py）")

import run_all  # noqa: E402

ok_output = """
[2026-08-31 06:00:10] レポートをダウンロードしています…
[2026-08-31 06:00:12]   32 件の注文明細を取得しました。
[2026-08-31 06:00:15] スプレッドシート「raw_orders」へ書き込んでいます…
[2026-08-31 06:00:18]   完了しました。32 件を書き込みました。
[2026-08-31 06:00:18] 正常に終了しました。
"""
check("成功時に件数を要約できる", "32 件を書き込みました" in run_all.extract_summary(ok_output),
      f"→ {run_all.extract_summary(ok_output)}")

err_output = """
[2026-08-31 06:00:10] アクセストークンを取得しています…
[2026-08-31 06:00:11] エラーで終了しました: スプレッドシートを開けませんでした。
"""
check("失敗時にエラー内容を抽出できる",
      "スプレッドシート" in run_all.extract_error(err_output),
      f"→ {run_all.extract_error(err_output)}")

check("エラー行が無くても何か返す", run_all.extract_error("なにかの出力\n") != "")
check("空出力でも落ちない", run_all.extract_error("") == "不明なエラー")

check("ジョブ定義に注文レポートが含まれる",
      any(j[1] == "main.py" for j in run_all.JOBS))
check("ジョブ定義にASIN別売上が含まれる",
      any(j[1] == "sales_traffic.py" for j in run_all.JOBS))
check("--full で日数が増える",
      all(j[3] >= j[2] for j in run_all.JOBS))

# 実行中のログが見えなくなる不具合の再発防止
_src = open("run_all.py").read()
check("子プロセスの出力を溜め込まない（capture_output を使わない）",
      "capture_output=True" not in _src)
check("出力を1行ずつ流す", "for line in process.stdout" in _src)
check("子プロセスのバッファリングを止める", 'PYTHONUNBUFFERED="1"' in _src)
check("固まっても抜けられるようウォッチドッグを持つ",
      "threading.Timer" in _src and "process.kill" in _src)

_sh = open("run_daily.sh").read()
check("実行中のスリープを抑止する（caffeinate）", "caffeinate -i" in _sh)

# append_row は失敗しても例外を投げない（本処理を止めない）ことの確認
broken_cfg = common.Config()
broken_cfg.sa_json_path = "/存在しないパス.json"
broken_cfg.spreadsheet_id = "dummy"
result = common.append_row(broken_cfg, "_log", ["a", "b"], header=["x", "y"])
check("ログ記録が失敗しても例外を投げない", result is False)

# ---------------------------------------------------------------------------
print("\n" + "=" * 40)
if failures:
    print(f"❌ {len(failures)} 件の失敗: {failures}")
    sys.exit(1)
print("✅ すべての検証に合格しました")
