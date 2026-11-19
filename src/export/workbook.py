"""The company workbook: everything a student needs to start their own model.

Sheets:
  Read me          what is in the file, units, sources, licence, how values were checked
  Summary          key figures by year (computed by the engine) and ratios as live formulas
  Balance sheet    every line as printed, by year, with the page it came from
  Income statement
  Cash flow
  Market           latest price, market cap, KHR/USD; daily prices with a live USD column
  Sources          every report used: date, where to find it, file fingerprint (SHA-256)

official_only=True is the public version (no uploads); the app's own download includes
the person's uploads, because it is their workbook.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

from src.core import store

from .xlsx import Formula, Workbook, ref

STATEMENTS = [("BS", "Balance sheet"), ("IS", "Income statement"), ("CF", "Cash flow")]

# Extra figures the ratio formulas need, read straight from the statements.
HELPERS = [("finance_costs", "IS", "finance_costs", "Finance costs"),
           ("current_assets", "BS", "total_current_assets", "Total current assets"),
           ("current_liabilities", "BS", "total_current_liabilities", "Total current liabilities")]

LICENCE = ("Free for study, teaching, research and student competitions; commercial use needs permission. "
           "Numbers are read from the companies' published reports: always cite the original report.")


def _statement_rows(conn: sqlite3.Connection, issuer: str, stmt: str, official_only: bool):
    only = " AND g.origin='official'" if official_only else ""
    rows = conn.execute(f"""
        SELECT f.id, f.line_item, f.label_as_printed, CAST(substr(f.period_end,1,4) AS INTEGER) y,
               f.value_as_reported, f.scale, f.page, f.check_status, f.filing_id, g.published_on
        FROM facts f JOIN filings g ON g.id=f.filing_id {store.NEWEST_JOIN}
        WHERE f.issuer_id=? AND f.statement=? AND f.period_type='FY' AND f.check_status!='failed'
          AND g.included=1{only}
        ORDER BY {store.NEWEST_ORDER}, f.id""", (issuer, issuer, stmt)).fetchall()
    values: dict[tuple[str, int], dict] = {}
    order: dict[str, tuple[str, int, int]] = {}   # line_item -> (label, published rank, id)
    for r in rows:   # newer reports overwrite older ones (restatements)
        values[(r["line_item"], r["y"])] = dict(r)
        order[r["line_item"]] = (r["label_as_printed"], r["published_on"], r["id"])
    newest = max((v[1] for v in order.values()), default="")
    items = sorted(order, key=lambda k: (order[k][1] != newest, order[k][2]))
    years = sorted({y for (_, y) in values})
    return items, years, values, {k: order[k][0] for k in items}


def _fx_by_date(conn: sqlite3.Connection) -> list[tuple[str, Decimal]]:
    return [(r[0], Decimal(r[1])) for r in conn.execute(
        "SELECT on_date, rate FROM fx_rates WHERE base='USD' AND quote='KHR' ORDER BY on_date")]


def build_workbook(conn: sqlite3.Connection, issuer: str, *, official_only: bool = False) -> Workbook:
    from src.api.data import public_link
    from src.report.build import build_report

    rep = build_report(conn, issuer, official_only=official_only)
    cur = rep["currency"]
    wb = Workbook()

    # ---- Read me ---------------------------------------------------------------------------
    me = wb.sheet("Read me")
    me.width(1, 26)
    me.width(2, 100)
    me.row([f"{rep['name']} ({issuer})"], style="title")
    me.row([f"Workbook generated {date.today().isoformat()}"], style="note")
    me.blank()
    rv = rep["review"]
    for k, v in [
        ("Currency", f"{cur}. Amounts are full numbers (the printed value × the report's scale)."),
        ("Years with all three statements", ", ".join(map(str, rep["years"])) or "none yet"),
        ("How values were checked", f"{rv['confirmed']} compared with the page by a person; {rv['proposed']} read by "
                                    "code with sums checked but not yet compared by a person."),
        ("Restatements", "When a later report reprints a year differently, the newest report's figure is used."),
        ("Summary sheet", "Key figures are computed by the engine. Ratios are live formulas: change a number and "
                          "they update. Blank means a figure is missing, never a guess."),
        ("Pages", "Each statement sheet shows the page of the report every value came from."),
        ("Uploads", "Not included (public data only)." if official_only else
                    ("Includes values from your own uploaded files." if rep.get("uses_uploads") else "None used.")),
        ("Source", "Reports published on the Cambodia Securities Exchange website (The Cambodia Securities "
                   "Exchange Home Page: https://csx.com.kh) and by the companies. See the Sources sheet."),
        ("Licence", LICENCE),
    ]:
        me.row([k, v], styles=["head", "text"])

    # ---- statements --------------------------------------------------------------------------
    for stmt, title in STATEMENTS:
        items, years, values, labels = _statement_rows(conn, issuer, stmt, official_only)
        ws = wb.sheet(title)
        ws.width(1, 46)
        ws.freeze_rows, ws.freeze_cols = 1, 1
        head = ["Line (as printed)"] + [f"FY{y}" for y in years] + [f"Page FY{y}" for y in years] + ["Key"]
        ws.row(head, style="head")
        for i in range(len(years)):
            ws.width(2 + i, 16)
        for item in items:
            vals, pages = [], []
            for y in years:
                v = values.get((item, y))
                vals.append(Decimal(v["value_as_reported"]) * int(v["scale"]) if v else None)
                pages.append(v["page"] if v else None)
            bold = item.startswith("total") or item in ("net_income", "cfo", "cfi", "cff", "operating_profit")
            ws.row([labels[item]] + vals + pages + [item],
                   styles=["text"] + ["bold_money" if bold else "money"] * len(years) + ["text"] * len(years) + ["note"])
        if not items:
            ws.row(["No values read for this statement yet."], style="note")

    # ---- Summary ------------------------------------------------------------------------------
    sm = wb.sheet("Summary")
    years = rep["years"] or sorted({int(y) for m in rep["lines"] for y in m["values"]})
    sm.width(1, 30)
    sm.width(len(years) + 2, 60)
    sm.freeze_rows, sm.freeze_cols = 1, 1
    sm.row(["Figure"] + [f"FY{y}" for y in years] + ["How it is worked out"], style="head")
    at: dict[str, int] = {}
    for m in rep["lines"]:
        vals = [Decimal(m["values"][str(y)]) if m["values"].get(str(y)) not in (None, "") else None for y in years]
        at[m["key"]] = sm.row([m["label"]] + vals + [m["formula"]],
                              styles=["text"] + ["money"] * len(years) + ["note"])
    helpers = {}
    for key, stmt, item, label in HELPERS:
        _, _, values, _ = _statement_rows(conn, issuer, stmt, official_only)
        vals = []
        for y in years:
            v = values.get((item, y))
            vals.append(Decimal(v["value_as_reported"]) * int(v["scale"]) if v else None)
        helpers[key] = sm.row([label] + vals + ["as printed"], styles=["text"] + ["money"] * len(years) + ["note"])
    at.update(helpers)
    sm.blank()
    sm.row(["Ratio (live formula)"] + [f"FY{y}" for y in years] + ["Formula"], style="head")

    def c(key: str, i: int) -> str | None:
        return ref(2 + i, at[key]) if key in at else None

    def ratio(label: str, fmt: str, make, text: str) -> None:
        cells = []
        for i in range(len(years)):
            f = make(i)
            cells.append(Formula(f'IFERROR({f},"")') if f else None)
        sm.row([label] + cells + [text], styles=["text"] + [fmt] * len(years) + ["note"])

    def simple(a: str, b: str):
        return lambda i: f"{c(a, i)}/{c(b, i)}" if c(a, i) and c(b, i) else None

    def growth(key: str):
        return lambda i: f"{c(key, i)}/{c(key, i - 1)}-1" if i and key in at else None

    def on_avg(a: str, b: str):
        return lambda i: (f"{c(a, i)}/AVERAGE({c(b, i - 1)},{c(b, i)})" if i else f"{c(a, i)}/{c(b, i)}") \
            if a in at and b in at else None

    if rep.get("kind") == "bank":
        ratio("Operating income growth", "pct", growth("operating_income"), "operating income ÷ last year's − 1")
        ratio("Profit growth", "pct", growth("net_income"), "profit ÷ last year's − 1")
        ratio("Net interest income to assets", "pct", on_avg("net_interest_income", "total_assets"),
              "net interest income ÷ average total assets")
        ratio("Cost to income", "pct", lambda i: f"-{c('operating_costs', i)}/{c('operating_income', i)}"
              if "operating_costs" in at and "operating_income" in at else None, "operating costs ÷ operating income")
        ratio("Cost of risk", "pct", lambda i: (f"-{c('impairment', i)}/AVERAGE({c('loans', i - 1)},{c('loans', i)})"
              if i else f"-{c('impairment', i)}/{c('loans', i)}") if "impairment" in at and "loans" in at else None,
              "loan loss charges ÷ average loans")
        ratio("Loans to deposits", "x", simple("loans", "deposits"), "loans ÷ customer deposits")
        ratio("Equity to assets", "pct", simple("total_equity", "total_assets"), "equity ÷ total assets")
        ratio("Return on equity", "pct", on_avg("net_income", "total_equity"), "profit ÷ average equity")
        ratio("Return on assets", "pct", on_avg("net_income", "total_assets"), "profit ÷ average total assets")
    else:
        ratio("Revenue growth", "pct", lambda i: f"{c('revenue', i)}/{c('revenue', i - 1)}-1" if i and 'revenue' in at else None,
              "revenue ÷ last year's revenue − 1")
        ratio("Operating margin", "pct", simple("operating_profit", "revenue"), "operating profit ÷ revenue")
        ratio("EBITDA margin", "pct", simple("ebitda", "revenue"), "EBITDA ÷ revenue")
        ratio("Net margin", "pct", simple("net_income", "revenue"), "profit ÷ revenue")
        ratio("Return on equity", "pct",
              lambda i: f"{c('net_income', i)}/AVERAGE({c('total_equity', i - 1)},{c('total_equity', i)})"
              if i and "net_income" in at and "total_equity" in at else None, "profit ÷ average equity (this and last year)")
        ratio("Return on assets", "pct", simple("net_income", "total_assets"), "profit ÷ total assets")
        ratio("Debt to equity", "x", simple("debt", "total_equity"), "borrowings and bonds ÷ equity")
        ratio("Net debt to EBITDA", "x", simple("net_debt", "ebitda"), "net debt ÷ EBITDA")
        ratio("Interest cover", "x",
              lambda i: f"{c('operating_profit', i)}/ABS({c('finance_costs', i)})" if c("operating_profit", i) else None,
              "operating profit ÷ finance costs")
        ratio("Current ratio", "x", simple("current_assets", "current_liabilities"), "current assets ÷ current liabilities")
        ratio("Cash conversion", "x", simple("cfo", "net_income"), "cash from operations ÷ profit")

    # ---- Market --------------------------------------------------------------------------------
    prices = conn.execute("SELECT * FROM prices WHERE issuer_id=? ORDER BY on_date DESC", (issuer,)).fetchall()
    mk = wb.sheet("Market")
    for col, w in {1: 14, 2: 14, 3: 14, 4: 20, 5: 12, 6: 14, 7: 18}.items():
        mk.width(col, w)
    if not prices:
        mk.row(["No share price: this company is not listed, or prices have not been downloaded. "
                "Valuation still works; there is just no market price to compare with."], style="note")
    else:
        fx = _fx_by_date(conn)
        fx_i = len(fx) - 1

        def rate_on(day: str):
            nonlocal fx_i
            while fx_i >= 0 and fx[fx_i][0] > day:
                fx_i -= 1
            return fx[fx_i][1] if fx_i >= 0 else None

        mk.row(["Latest", "", "", "", "", "", ""], style="head")
        r_close = mk.row(["Close (KHR)", Decimal(prices[0]["close"]), prices[0]["on_date"]], styles=["text", "money", "note"])
        cap = prices[0]["market_cap"]
        r_cap = mk.row(["Market cap (million KHR)", Decimal(cap) if cap else None, "as published by the exchange"],
                       styles=["text", "money", "note"])
        r_fx = mk.row(["KHR per USD", rate_on(prices[0]["on_date"]), "exchange's daily rate"], styles=["text", "money", "note"])
        mk.row(["Market cap (USD)", Formula(f"IFERROR(B{r_cap}*1000000/B{r_fx},\"\")"), "market cap ÷ rate"],
               styles=["text", "money", "note"])
        mk.row(["Shares implied", Formula(f"IFERROR(B{r_cap}*1000000/B{r_close},\"\")"),
                "market cap ÷ price; check against the share count in the report"], styles=["text", "money", "note"])
        mk.blank()
        top = mk.row(["Date", "Close (KHR)", "Volume", "Market cap (m KHR)", "KHR/USD", "Close (USD)", ""], style="head")
        mk.freeze_rows = top
        fx_i = len(fx) - 1
        for p in prices:
            r = mk.next_row
            mk.row([date.fromisoformat(p["on_date"]), Decimal(p["close"]), p["volume"],
                    Decimal(p["market_cap"]) if p["market_cap"] else None, rate_on(p["on_date"]),
                    Formula(f'IFERROR(B{r}/E{r},"")')],
                   styles=["date", "money", "money", "money", "money", "num2"])

    # ---- Sources --------------------------------------------------------------------------------
    so = wb.sheet("Sources")
    for col, w in {1: 18, 2: 12, 3: 52, 4: 12, 5: 70, 6: 66}.items():
        so.width(col, w)
    so.row(["Report", "Type", "Title", "Published", "Where to find it", "File fingerprint (SHA-256)"], style="head")
    only = " AND origin='official'" if official_only else ""
    for f in conn.execute(f"SELECT * FROM filings WHERE issuer_id=? AND included=1{only} ORDER BY published_on DESC",
                          (issuer,)):
        if f["origin"] == "upload":
            where = "your upload (stays on this computer)"
        else:
            link = public_link(f["source_url"], issuer, f["title"], f["published_on"])
            where = f"{link['url']}" + (f" > {link['where']}" if link["where"] else "")
        so.row([f["id"], f["kind"], f["title"], f["published_on"], where, f["pdf_sha256"]])
    so.blank()
    so.row(["Credit: The Cambodia Securities Exchange Home Page (https://csx.com.kh) and each company's "
            "own report. Corrections or removal on request."], style="note")
    return wb
