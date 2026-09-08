#!/usr/bin/env python3
"""
シートの見た目（表示形式と色）

書き込んだ内容を走査して、Google Sheets APIの書式リクエストを組み立てます。
行の位置を決め打ちせず「■で始まる行は見出し」「既知のヘッダー行の下は表」と
判定するため、ブロックの長さが変わっても崩れません。
"""

# --- 色 ---------------------------------------------------------------------
NAVY = {"red": 0.12, "green": 0.22, "blue": 0.39}       # 見出しの地色
WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
SECTION_BG = {"red": 0.91, "green": 0.93, "blue": 0.96}  # ■セクション
BAND_BG = {"red": 0.97, "green": 0.98, "blue": 0.99}     # 交互の行
TOTAL_BG = {"red": 1.0, "green": 0.95, "blue": 0.80}     # 合計行
WARN_BG = {"red": 1.0, "green": 0.93, "blue": 0.93}      # 警告行
NOTE_FG = {"red": 0.45, "green": 0.48, "blue": 0.53}     # 注記の文字色
BORDER = {"style": "SOLID", "width": 1,
          "color": {"red": 0.80, "green": 0.84, "blue": 0.89}}

# --- 表示形式 ---------------------------------------------------------------
# 金額はマイナスを赤で出す（手数料・返金が一目で分かる）
MONEY = {"type": "NUMBER", "pattern": '¥#,##0;[Red]-¥#,##0'}
NUMBER = {"type": "NUMBER", "pattern": "#,##0"}
PERCENT = {"type": "NUMBER", "pattern": '0.00"%";[Red]-0.00"%"'}
DECIMAL = {"type": "NUMBER", "pattern": "#,##0.0"}  # 指数・1日あたり平均など

# 列ごとの型（表のヘッダー行の内容から判定する）
TEXT = "text"
COLUMN_TYPES = {
    ("日付", "売上", "広告費", "販売個数", "広告での販売個数",
     "セッション", "広告セッション", "転換率%", "広告転換率%",
     "手数料", "実入金額", "原価金額", "利益額"):
        [TEXT, "money", "money", "num", "num", "num", "num",
         "pct", "pct", "money", "money", "money", "money"],
    ("順位", "ASIN", "商品名", "売上", "前月比%", "販売個数", "セッション",
     "転換率%", "手数料", "実入金額", "原価合計",
     "実入金率%", "原価率%", "Amazon粗利率%", "返品数"):
        [TEXT, TEXT, TEXT, "money", "pct", "num", "num",
         "pct", "money", "money", "money", "pct", "pct", "pct", "num"],
    ("月", "売上", "前月比%", "販売個数", "セッション", "転換率%", "手数料",
     "実入金額", "実入金率%", "返品数", "取扱ASIN数", "データ充足"):
        [TEXT, "money", "pct", "num", "num", "pct", "money",
         "money", "pct", "num", "num", TEXT],
    ("日付", "キャンペーン名", "インプレッション", "費用", "CPM", "クリック数", "CPC",
     "CTR%", "CV（購入数）", "CVR%", "CPA", "ROAS", "ACOS%"):
        [TEXT, TEXT, "num", "money", "money", "num", "money",
         "pct", "num", "pct", "money", "dec", "pct"],
}

# キャンペーン別の内訳表（月別シートの広告サマリーの右に添える）。
# build_requests の通常走査ではなく write_range 経由（build_side_table_requests）
# で使うため、COLUMN_TYPESとは別に持つ。
CAMPAIGN_TABLE_HEADER = [
    "キャンペーン名", "インプレッション", "費用", "CPM", "クリック数", "CPC",
    "CTR%", "CV（購入数）", "CVR%", "CPA", "ROAS", "ACOS%",
]
CAMPAIGN_TABLE_TYPES = [
    TEXT, "num", "money", "money", "num", "money",
    "pct", "num", "pct", "money", "dec", "pct",
]

# 月間サマリーは行ごとに型が変わるため、行ラベルで判定する
ROW_TYPES = {
    # 金額
    "売上": "money", "実入金額": "money", "平均単価": "money",
    "着地予想 売上": "money", "着地予想 実入金額": "money",
    "販売手数料（紹介料）": "money", "FBA手数料（配送・保管）": "money",
    "その他手数料": "money", "プロモーション値引": "money", "手数料合計": "money",
    "原価合計": "money",
    "広告費合計": "money", "着地予想 広告費": "money", "広告経由売上": "money",
    "平均単価（広告経由）": "money", "CPA（広告費÷販売個数）": "money",
    # 件数
    "販売個数": "num", "セッション": "num", "返品数": "num",
    "着地予想 販売個数": "num",
    "販売個数（広告経由）": "num", "着地予想 販売個数（広告経由）": "num",
    "セッション（広告）": "num",
    # 割合
    "転換率%": "pct", "実入金率%": "pct", "対売上比%": "pct",
    "原価比率%": "pct", "広告費比率%": "pct", "ACOS%": "pct",
    "転換率%（広告）": "pct", "広告費の対全体売上比%": "pct",
    # 小数（指数・平均・ROAS）
    "1日あたり販売個数": "dec",
    "売上指数": "dec", "　うち セッション指数": "dec",
    "　うち 転換率指数": "dec", "　うち 平均単価指数": "dec",
    "検算（3指数の積）": "dec", "ROAS": "dec",
}

FORMAT_OF = {"money": MONEY, "num": NUMBER, "pct": PERCENT, "dec": DECIMAL}

# 列ごとの型を持たないが、見出しとして扱いたい行（月間サマリーの1行目など）
HEADER_ROWS = {
    ("指標", "今月", "前月", "増減", "増減率%"),
}


def _cell(sheet_id, row, col_start, col_end, fmt, fields):
    return {"repeatCell": {
        "range": {"sheetId": sheet_id, "startRowIndex": row, "endRowIndex": row + 1,
                  "startColumnIndex": col_start, "endColumnIndex": col_end},
        "cell": {"userEnteredFormat": fmt},
        "fields": fields,
    }}


def _range(sheet_id, r1, r2, c1, c2, fmt, fields):
    return {"repeatCell": {
        "range": {"sheetId": sheet_id, "startRowIndex": r1, "endRowIndex": r2,
                  "startColumnIndex": c1, "endColumnIndex": c2},
        "cell": {"userEnteredFormat": fmt},
        "fields": fields,
    }}


def build_requests(sheet_id, rows):
    """
    書き込んだ内容から書式リクエストを組み立てる。

    走査の考え方:
      ・1行目            → タイトル
      ・■で始まる行      → セクション見出し
      ・既知のヘッダー行  → 表（列ごとに表示形式を当てる）
      ・それ以外の連続行  → ラベル＋値のブロック（サマリーなど）
    どのブロックにも枠線を引き、シート全体のグリッド線は消します。
    """
    requests = []
    width = max((len(r) for r in rows), default=1)

    # ---- 既存の書式をいったん全消去する ------------------------------------
    # worksheet.clear() は値しか消さないため、前回の色・罫線・白文字が
    # 古い行位置に残る。行構成が変わると、無関係な場所に帯が出たり
    # 文字が白いまま見えなくなったりするので、必ず先にリセットする。
    requests.append({"repeatCell": {
        "range": {"sheetId": sheet_id},   # 範囲指定なし＝シート全体
        "cell": {"userEnteredFormat": {}},
        "fields": "userEnteredFormat",
    }})

    # ---- シート全体のグリッド線を消す ------------------------------------
    # 表の枠線だけを見せたいので、既定の薄い格子は非表示にする。
    requests.append({"updateSheetProperties": {
        "properties": {"sheetId": sheet_id, "gridProperties": {"hideGridlines": True}},
        "fields": "gridProperties.hideGridlines",
    }})

    # ---- タイトル行（1行目）------------------------------------------------
    requests.append(_cell(
        sheet_id, 0, 0, width,
        {"backgroundColor": NAVY,
         "textFormat": {"bold": True, "fontSize": 13, "foregroundColor": WHITE}},
        "userEnteredFormat(backgroundColor,textFormat)",
    ))

    index = 1
    while index < len(rows):
        row = rows[index]

        # 空行は区切りとして読み飛ばす
        if not row or not any(str(c).strip() for c in row):
            index += 1
            continue

        first = str(row[0])

        # ---- 注記（※で始まる行）-------------------------------------------
        # 表の外に置きたいので、枠線もラベル書式も付けない。
        if first.startswith("※"):
            requests.append(_cell(
                sheet_id, index, 0, width,
                {"textFormat": {"italic": True, "fontSize": 9,
                                "foregroundColor": NOTE_FG}},
                "userEnteredFormat.textFormat",
            ))
            index += 1
            continue

        # ---- セクション見出し（■で始まる行）-------------------------------
        if first.startswith("■"):
            requests.append(_cell(
                sheet_id, index, 0, width,
                {"backgroundColor": SECTION_BG,
                 "textFormat": {"bold": True, "fontSize": 11}},
                "userEnteredFormat(backgroundColor,textFormat)",
            ))
            index += 1
            continue

        # ---- 表（既知のヘッダー行）-----------------------------------------
        key = tuple(str(c) for c in row)
        if key in COLUMN_TYPES:
            types = COLUMN_TYPES[key]
            ncols = len(types)
            requests.append(_cell(
                sheet_id, index, 0, ncols,
                {"backgroundColor": NAVY,
                 "textFormat": {"bold": True, "foregroundColor": WHITE},
                 "horizontalAlignment": "CENTER"},
                "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
            ))
            start, end = index + 1, index + 1
            while end < len(rows) and _is_data_row(rows[end]):
                end += 1
            if end > start:
                requests += _table_body(sheet_id, rows, start, end, types, ncols)
            index = end
            continue

        # ---- ラベル＋値のブロック（月間サマリーなど）------------------------
        start, end = index, index
        while end < len(rows) and _is_data_row(rows[end]) \
                and tuple(str(c) for c in rows[end]) not in COLUMN_TYPES:
            end += 1
        requests += _label_block(sheet_id, rows, start, end)
        index = end

    # ---- 列幅 --------------------------------------------------------------
    requests.append({"updateDimensionProperties": {
        "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                  "startIndex": 0, "endIndex": 1},
        "properties": {"pixelSize": 210},
        "fields": "pixelSize",
    }})
    requests.append({"updateDimensionProperties": {
        "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                  "startIndex": 1, "endIndex": max(width, 2)},
        "properties": {"pixelSize": 115},
        "fields": "pixelSize",
    }})

    return requests


def build_side_table_requests(sheet_id, top_row, top_col, rows, types):
    """
    シート内の任意の位置（月別シートの広告サマリーの右など）に独立して置く
    小さな表の書式を組み立てる。build_requests のメイン走査（行全体を見出しと
    突き合わせる方式）とは無関係に動くので、既存の表を壊さずに追加できる。

    top_row / top_col はシート全体での絶対位置（0始まり）。rows[0] が見出し行。
    """
    if not rows:
        return []
    ncols = len(types)
    header_row = top_row
    data_start, data_end = top_row + 1, top_row + len(rows)

    requests = [
        # この範囲だけ既存の書式をリセットする（再実行してもズレを残さないため）
        {"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": header_row, "endRowIndex": data_end,
                      "startColumnIndex": top_col, "endColumnIndex": top_col + ncols},
            "cell": {"userEnteredFormat": {}},
            "fields": "userEnteredFormat",
        }},
        _cell(sheet_id, header_row, top_col, top_col + ncols,
              {"backgroundColor": NAVY,
               "textFormat": {"bold": True, "foregroundColor": WHITE},
               "horizontalAlignment": "CENTER"},
              "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"),
    ]

    if data_end > data_start:
        for col, kind in enumerate(types):
            if kind == TEXT:
                continue
            requests.append(_range(
                sheet_id, data_start, data_end, top_col + col, top_col + col + 1,
                {"numberFormat": FORMAT_OF[kind], "horizontalAlignment": "RIGHT"},
                "userEnteredFormat(numberFormat,horizontalAlignment)",
            ))

        for row_index in range(data_start, data_end):
            label = str(rows[row_index - top_row][0]) if rows[row_index - top_row] else ""
            if label == "合計":
                requests.append(_cell(
                    sheet_id, row_index, top_col, top_col + ncols,
                    {"backgroundColor": TOTAL_BG, "textFormat": {"bold": True}},
                    "userEnteredFormat(backgroundColor,textFormat)",
                ))
            elif (row_index - data_start) % 2 == 1:
                requests.append(_cell(
                    sheet_id, row_index, top_col, top_col + ncols,
                    {"backgroundColor": BAND_BG}, "userEnteredFormat.backgroundColor",
                ))

    requests.append({"updateBorders": {
        "range": {"sheetId": sheet_id, "startRowIndex": header_row, "endRowIndex": data_end,
                  "startColumnIndex": top_col, "endColumnIndex": top_col + ncols},
        "innerHorizontal": BORDER, "innerVertical": BORDER,
        "top": BORDER, "bottom": BORDER, "left": BORDER, "right": BORDER,
    }})
    requests.append({"updateDimensionProperties": {
        "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                  "startIndex": top_col, "endIndex": top_col + ncols},
        "properties": {"pixelSize": 130},
        "fields": "pixelSize",
    }})

    return requests


def _is_data_row(row):
    """ブロックの続きとみなせる行か（空行・見出し・注記で区切る）。"""
    if not row or not any(str(c).strip() for c in row):
        return False
    first = str(row[0])
    return not first.startswith("■") and not first.startswith("※")


def _label_block(sheet_id, rows, start, end):
    """ラベル＋値の並び（月間サマリーなど）に枠線と表示形式を当てる。"""
    requests = []
    block = rows[start:end]
    ncols = max((len(r) for r in block), default=1)

    for offset, row in enumerate(block):
        row_index = start + offset
        label = str(row[0]) if row else ""

        # 表の見出しとして扱う行（列の型は持たないが体裁は揃える）
        if tuple(str(c) for c in row) in HEADER_ROWS:
            requests.append(_cell(
                sheet_id, row_index, 0, ncols,
                {"backgroundColor": NAVY,
                 "textFormat": {"bold": True, "foregroundColor": WHITE},
                 "horizontalAlignment": "CENTER"},
                "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
            ))
            continue

        # 警告行は行全体を淡い赤にする
        if label.startswith("⚠"):
            requests.append(_cell(
                sheet_id, row_index, 0, ncols,
                {"backgroundColor": WARN_BG, "textFormat": {"bold": True}},
                "userEnteredFormat(backgroundColor,textFormat)",
            ))
            continue

        # ラベル列は太字＋薄い地色
        requests.append(_cell(
            sheet_id, row_index, 0, 1,
            {"backgroundColor": BAND_BG, "textFormat": {"bold": True}},
            "userEnteredFormat(backgroundColor,textFormat)",
        ))

        # 値の表示形式（行ラベルで判定）
        if label in ROW_TYPES and len(row) >= 2:
            fmt = FORMAT_OF[ROW_TYPES[label]]
            requests.append(_cell(
                sheet_id, row_index, 1, min(len(row), 3),
                {"numberFormat": fmt, "horizontalAlignment": "RIGHT"},
                "userEnteredFormat(numberFormat,horizontalAlignment)",
            ))
            if len(row) >= 5:
                requests.append(_cell(
                    sheet_id, row_index, 4, 5,
                    {"numberFormat": PERCENT, "horizontalAlignment": "RIGHT"},
                    "userEnteredFormat(numberFormat,horizontalAlignment)",
                ))

    # ブロック全体に枠線
    if ncols >= 1 and end > start:
        requests.append({"updateBorders": {
            "range": {"sheetId": sheet_id, "startRowIndex": start, "endRowIndex": end,
                      "startColumnIndex": 0, "endColumnIndex": ncols},
            "innerHorizontal": BORDER, "innerVertical": BORDER,
            "top": BORDER, "bottom": BORDER, "left": BORDER, "right": BORDER,
        }})

    return requests


def _table_body(sheet_id, rows, start, end, types, ncols):
    """表の中身に、列ごとの表示形式・交互の背景色・罫線を適用する。"""
    requests = []

    # 列ごとの表示形式
    for col, kind in enumerate(types):
        if kind == TEXT:
            continue
        requests.append(_range(
            sheet_id, start, end, col, col + 1,
            {"numberFormat": FORMAT_OF[kind], "horizontalAlignment": "RIGHT"},
            "userEnteredFormat(numberFormat,horizontalAlignment)",
        ))

    # 1行おきに薄い背景色（読み違いを防ぐ）
    for row_index in range(start, end):
        label = str(rows[row_index][0]) if rows[row_index] else ""
        if label == "合計":
            requests.append(_cell(
                sheet_id, row_index, 0, ncols,
                {"backgroundColor": TOTAL_BG, "textFormat": {"bold": True}},
                "userEnteredFormat(backgroundColor,textFormat)",
            ))
        elif (row_index - start) % 2 == 1:
            requests.append(_cell(
                sheet_id, row_index, 0, ncols,
                {"backgroundColor": BAND_BG},
                "userEnteredFormat.backgroundColor",
            ))

    # 表全体に罫線
    requests.append({"updateBorders": {
        "range": {"sheetId": sheet_id, "startRowIndex": start - 1, "endRowIndex": end,
                  "startColumnIndex": 0, "endColumnIndex": ncols},
        "innerHorizontal": BORDER, "innerVertical": BORDER,
        "top": BORDER, "bottom": BORDER, "left": BORDER, "right": BORDER,
    }})

    return requests
