#!/usr/bin/env python3
"""
集計用のデータ読み込みと計算（表示は含まない）

rawシートを一度だけ読み込み、任意の期間で切り出せる形に整えます。
月別シートを3枚作るときにシートを3回読み直さずに済みます。
"""

from collections import defaultdict

from common import log, read_sheet

# 広告費はレポート上は税抜きで表示されるため、実際に請求される金額（税込）に
# 揃えるためにこの倍率を掛ける。広告費が関わるすべての集計・比率にかかる。
AD_COST_TAX_MULTIPLIER = 1.1

# 商品原価（1個あたり、税抜き）。商品名に含まれるキーワードで判定する。
# 判定順に意味があるため、より具体的なキーワードを先に置くこと
# （例:「ナイトクリーム」は「クリーム」を含むが、一般的な「クリーム」ではなく
# 専用のキーワードとして先に判定する）。
PRODUCT_COST_BY_KEYWORDS = [
    (1500, ("ナイトクリーム",)),
    (1400, ("エッセンス", "美容液")),
    (1300, ("クリアローション", "化粧水", "ローション")),
]


def unit_cost_for(product_name):
    """商品名からキーワードで原価（1個あたり）を判定する。一致しなければ0。"""
    name = product_name or ""
    for cost, keywords in PRODUCT_COST_BY_KEYWORDS:
        if any(kw in name for kw in keywords):
            return cost
    return 0


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
    for name in names:
        if name in header:
            return header.index(name)
    return default


def pct(numerator, denominator, digits=2):
    if not denominator:
        return 0.0
    return round(numerator / denominator * 100, digits)


def delta_pct(current, previous, digits=1):
    """増減率。前月が0なら空文字（率が意味を持たないため）。"""
    if not previous:
        return ""
    return round((current - previous) / abs(previous) * 100, digits)


class Data:
    """rawシートを読み込んで保持する。期間での絞り込みは後から行う。"""

    def __init__(self, cfg):
        self.sku_map = {}      # sku -> (asin, 商品名)
        self.asin_cost = {}    # asin -> 原価（1個あたり）
        self.traffic = []      # {date, asin, sales, units, sessions, refunded}
        self.finances = []     # {date, sku, principal, tax_shipping, promo, ...}
        self.ads = []          # {date, cost(税込), sales, units, clicks}
        self.order_days = set()
        self._load(cfg)

    # -- 読み込み ----------------------------------------------------------
    def _load(self, cfg):
        self._load_orders(cfg)
        self._build_asin_cost()
        self._load_traffic(cfg)
        self._load_finances(cfg)
        self._load_ads(cfg)

    def _load_orders(self, cfg):
        rows = read_sheet(cfg, cfg.worksheet("WORKSHEET_ORDERS", "raw_orders"))
        if len(rows) < 2:
            log("  raw_orders が空です。")
            return
        h = [str(c) for c in rows[0]]
        i_sku, i_asin = index_of(h, "sku"), index_of(h, "asin")
        i_name, i_date = index_of(h, "product-name"), index_of(h, "purchase-date")
        if i_sku is None or i_asin is None:
            log("  raw_orders に sku / asin 列がありません。")
            return

        for row in rows[1:]:
            if len(row) <= max(i_sku, i_asin):
                continue
            sku, asin = str(row[i_sku]).strip(), str(row[i_asin]).strip()
            if i_date is not None and len(row) > i_date:
                date = str(row[i_date])[:10]
                if date:
                    self.order_days.add(date)
            if not sku or not asin:
                continue
            name = str(row[i_name]).strip() if i_name is not None and len(row) > i_name else ""
            self.sku_map[sku] = (asin, name or self.sku_map.get(sku, ("", ""))[1])
        log(f"  raw_orders: SKU→ASIN {len(self.sku_map)} 件 / {len(self.order_days)} 日")

    def _build_asin_cost(self):
        """商品名から原価を判定し、ASINごとに保持する（原価計算用）。"""
        unmatched = []
        for asin, name in self.sku_map.values():
            if not asin or asin in self.asin_cost:
                continue
            cost = unit_cost_for(name)
            self.asin_cost[asin] = cost
            if cost == 0:
                unmatched.append(f"{asin}（{name}）")
        if unmatched:
            log(f"  ⚠ 原価が判定できなかった商品が{len(unmatched)}件あります"
                "（0円として計算します）: " + ", ".join(unmatched[:5])
                + ("…" if len(unmatched) > 5 else ""))

    def _load_traffic(self, cfg):
        rows = read_sheet(cfg, cfg.worksheet("WORKSHEET_SALES_TRAFFIC", "raw_sales_traffic"))
        if len(rows) < 2:
            log("  raw_sales_traffic が空です。")
            return
        h = [str(c) for c in rows[0]]
        idx = {
            "date": index_of(h, "date", default=0),
            "asin": index_of(h, "childAsin", "parentAsin", default=2),
            "sales": index_of(h, "orderedProductSales"),
            "units": index_of(h, "unitsOrdered"),
            "sessions": index_of(h, "sessions"),
            "refunded": index_of(h, "unitsRefunded"),
        }
        for row in rows[1:]:
            date = str(row[idx["date"]])[:10] if len(row) > idx["date"] else ""
            asin = str(row[idx["asin"]]).strip() if len(row) > idx["asin"] else ""
            if not date or not asin:
                continue

            def val(key):
                col = idx[key]
                return to_float(row[col]) if col is not None and len(row) > col else 0.0

            self.traffic.append({
                "date": date, "asin": asin,
                "sales": val("sales"), "units": val("units"),
                "sessions": val("sessions"), "refunded": val("refunded"),
            })
        log(f"  raw_sales_traffic: {len(self.traffic)} 行")

    def _load_finances(self, cfg):
        rows = read_sheet(cfg, cfg.worksheet("WORKSHEET_FINANCES", "raw_finances"))
        if len(rows) < 2:
            log("  raw_finances が空です。")
            return
        h = [str(c) for c in rows[0]]
        idx = {
            "date": index_of(h, "postedDate", default=0),
            "sku": index_of(h, "sku", default=3),
            "principal": index_of(h, "商品代金"),
            "tax": index_of(h, "税"),
            "shipping": index_of(h, "配送料"),
            "other_sales": index_of(h, "その他売上"),
            "promo": index_of(h, "プロモーション値引"),
            "referral": index_of(h, "販売手数料"),
            "fba": index_of(h, "FBA手数料"),
            "other": index_of(h, "その他手数料"),
            "net": index_of(h, "純額"),
        }
        for row in rows[1:]:
            date = str(row[idx["date"]])[:10] if len(row) > idx["date"] else ""
            if not date:
                continue

            def val(key):
                col = idx[key]
                return to_float(row[col]) if col is not None and len(row) > col else 0.0

            self.finances.append({
                "date": date,
                "sku": str(row[idx["sku"]]).strip() if len(row) > idx["sku"] else "",
                "principal": val("principal"),
                "tax_shipping": val("tax") + val("shipping") + val("other_sales"),
                "promo": val("promo"),
                "referral": val("referral"),
                "fba": val("fba"),
                "other": val("other"),
                "net": val("net"),
            })
        log(f"  raw_finances: {len(self.finances)} 行")

    def _load_ads(self, cfg):
        """
        Amazon Adsの実績（メール取り込み版 raw_ads_email）を読み込む。

        キャンペーン単位ではなく日別の合計だけを使う（商品別の内訳は今のところ不要）。
        広告費は税抜きで記録されているため、ここで税込（×1.1）に換算しておく。
        こうしておけば、以降このデータを使うところ全てが自動的に税込になる。
        """
        sheet_name = cfg.worksheet("WORKSHEET_ADS_EMAIL", "raw_ads_email")
        rows = read_sheet(cfg, sheet_name)
        if len(rows) < 2:
            log(f"  {sheet_name} が空です（広告データはまだ0件として扱います）。")
            return
        h = [str(c) for c in rows[0]]
        idx = {
            "date": index_of(h, "日付", default=0),
            "clicks": index_of(h, "クリック数"),
            # 「合計費用（調整済み）」は空欄になっていることがあるため、
            # 「合計費用」を正として使い、そちらが空の行だけ調整済みの値で補う。
            "cost": index_of(h, "合計費用"),
            "cost_adjusted": index_of(h, "合計費用（調整済み）"),
            "sales": index_of(h, "売上"),
            "units": index_of(h, "注文された商品点数"),
        }
        for row in rows[1:]:
            date = str(row[idx["date"]])[:10] if len(row) > idx["date"] else ""
            if not date:
                continue

            def val(key):
                col = idx[key]
                return to_float(row[col]) if col is not None and len(row) > col else 0.0

            cost = val("cost") or val("cost_adjusted")

            self.ads.append({
                "date": date,
                "cost": cost * AD_COST_TAX_MULTIPLIER,
                "sales": val("sales"),
                "units": val("units"),
                "clicks": val("clicks"),
            })
        log(f"  {sheet_name}: {len(self.ads)} 行（広告費は税込×{AD_COST_TAX_MULTIPLIER}換算）")

    # -- 期間で切り出す ----------------------------------------------------
    def months_available(self):
        """データが存在する月（新しい順）。"""
        months = {r["date"][:7] for r in self.traffic}
        months |= {r["date"][:7] for r in self.finances}
        months |= {d[:7] for d in self.order_days}
        return sorted(months, reverse=True)

    def summarize(self, month):
        """
        指定した月（"2026-08"）の集計を返す。
        月が存在しなくても、すべて0の結果を返す（前月比の計算で使うため）。
        """
        traffic = [r for r in self.traffic if r["date"].startswith(month)]
        finances = [r for r in self.finances if r["date"].startswith(month)]
        ads = [r for r in self.ads if r["date"].startswith(month)]

        total = {
            "sales": sum(r["sales"] for r in traffic),
            "units": sum(r["units"] for r in traffic),
            "sessions": sum(r["sessions"] for r in traffic),
            "refunded": sum(r["refunded"] for r in traffic),
            "net": sum(r["net"] for r in finances),
            "principal": sum(r["principal"] for r in finances),
            "tax_shipping": sum(r["tax_shipping"] for r in finances),
            "promo": sum(r["promo"] for r in finances),
            "referral": sum(r["referral"] for r in finances),
            "fba": sum(r["fba"] for r in finances),
            "other_fees": sum(r["other"] for r in finances),
            "cogs": sum(r["units"] * self.asin_cost.get(r["asin"], 0) for r in traffic),
            "ads_cost": sum(r["cost"] for r in ads),
            "ads_sales": sum(r["sales"] for r in ads),
            "ads_units": sum(r["units"] for r in ads),
            "ads_clicks": sum(r["clicks"] for r in ads),
        }
        total["fees"] = total["referral"] + total["fba"] + total["other_fees"]
        total["traffic_days"] = len({r["date"] for r in traffic})
        total["finance_days"] = len({r["date"] for r in finances})
        total["ads_days"] = len({r["date"] for r in ads})
        total["asins"] = len({r["asin"] for r in traffic})

        # 日別
        by_day = defaultdict(lambda: defaultdict(float))
        for r in traffic:
            unit_cost = self.asin_cost.get(r["asin"], 0)
            for k in ("sales", "units", "sessions", "refunded"):
                by_day[r["date"]][k] += r[k]
            by_day[r["date"]]["cogs"] += r["units"] * unit_cost
        for r in finances:
            by_day[r["date"]]["net"] += r["net"]
            by_day[r["date"]]["fees"] += r["referral"] + r["fba"] + r["other"]
        for r in ads:
            by_day[r["date"]]["ads_cost"] += r["cost"]
            by_day[r["date"]]["ads_sales"] += r["sales"]
            by_day[r["date"]]["ads_units"] += r["units"]
            by_day[r["date"]]["ads_clicks"] += r["clicks"]

        # 商品（ASIN）別
        by_asin = defaultdict(lambda: defaultdict(float))
        for r in traffic:
            unit_cost = self.asin_cost.get(r["asin"], 0)
            for k in ("sales", "units", "sessions", "refunded"):
                by_asin[r["asin"]][k] += r[k]
            by_asin[r["asin"]]["cogs"] += r["units"] * unit_cost

        unmapped_net = 0.0
        for r in finances:
            asin = self.sku_map.get(r["sku"], ("", ""))[0]
            if not asin:
                unmapped_net += r["net"]
                continue
            by_asin[asin]["net"] += r["net"]
            by_asin[asin]["fees"] += r["referral"] + r["fba"] + r["other"]
        total["unmapped"] = unmapped_net

        return {"total": total, "by_day": by_day, "by_asin": by_asin}

    def asin_names(self):
        names = {}
        for asin, name in self.sku_map.values():
            if asin and name and asin not in names:
                names[asin] = name
        return names
