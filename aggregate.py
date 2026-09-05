#!/usr/bin/env python3
"""
集計用のデータ読み込みと計算（表示は含まない）

rawシートを一度だけ読み込み、任意の期間で切り出せる形に整えます。
月別シートを3枚作るときにシートを3回読み直さずに済みます。
"""

from collections import defaultdict

from common import log, read_sheet


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
        self.traffic = []      # {date, asin, sales, units, sessions, refunded}
        self.finances = []     # {date, sku, principal, tax_shipping, promo, ...}
        self.order_days = set()
        self._load(cfg)

    # -- 読み込み ----------------------------------------------------------
    def _load(self, cfg):
        self._load_orders(cfg)
        self._load_traffic(cfg)
        self._load_finances(cfg)

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
        }
        total["fees"] = total["referral"] + total["fba"] + total["other_fees"]
        total["traffic_days"] = len({r["date"] for r in traffic})
        total["finance_days"] = len({r["date"] for r in finances})
        total["asins"] = len({r["asin"] for r in traffic})

        # 日別
        by_day = defaultdict(lambda: defaultdict(float))
        for r in traffic:
            for k in ("sales", "units", "sessions", "refunded"):
                by_day[r["date"]][k] += r[k]
        for r in finances:
            by_day[r["date"]]["net"] += r["net"]
            by_day[r["date"]]["fees"] += r["referral"] + r["fba"] + r["other"]

        # 商品（ASIN）別
        by_asin = defaultdict(lambda: defaultdict(float))
        for r in traffic:
            for k in ("sales", "units", "sessions", "refunded"):
                by_asin[r["asin"]][k] += r[k]

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
