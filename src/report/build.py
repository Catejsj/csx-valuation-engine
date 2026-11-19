"""The company report: every number computed here, from stored facts, with its pages.

For each fiscal year the value comes from the most recently published filing that reports
it (so restated figures win). Nothing missing is filled in: a metric whose inputs are absent
is listed under "not found" with the line items it needed.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal

from src.core import store
from src.core.coverage import full_years, readiness

ONE = Decimal(1)


@dataclass
class Value:
    amount: Decimal                  # whole currency units (scale applied)
    pages: set[tuple[str, int]]      # (filing_id, page)
    upload: bool = False


@dataclass
class Metric:
    key: str
    label: str
    kind: str                        # "money" | "pct" | "x" (multiple)
    values: dict[int, Decimal | None] = field(default_factory=dict)
    formula: str = ""
    missing: list[str] = field(default_factory=list)


def _facts_by_year(conn: sqlite3.Connection, issuer: str,
                   official_only: bool = False) -> dict[tuple[str, str, int], Value]:
    only = " AND g.origin = 'official'" if official_only else ""
    rows = conn.execute(f"""
        SELECT f.statement, f.line_item, CAST(substr(f.period_end,1,4) AS INTEGER) AS y,
               f.value_as_reported, f.scale, f.page, f.filing_id, g.published_on, g.origin
        FROM facts f JOIN filings g ON g.id = f.filing_id {store.NEWEST_JOIN}
        WHERE f.issuer_id = ? AND f.period_type = 'FY' AND f.check_status != 'failed' AND g.included = 1{only}
        ORDER BY {store.NEWEST_ORDER}, f.id""", (issuer, issuer)).fetchall()
    out: dict[tuple[str, str, int], Value] = _Facts()
    for r in rows:   # later (newer) filings overwrite earlier ones
        out[(r["statement"], r["line_item"], r["y"])] = Value(
            Decimal(r["value_as_reported"]) * int(r["scale"]), {(r["filing_id"], r["page"])}, r["origin"] == "upload")
    return out


class _Facts(dict):
    """Facts keyed (statement, line_item, year), with a lazy index by (statement, year) so sums
    over a statement-year look at that group only, not at every value of the company."""
    _groups = None

    def group(self, stmt: str, year: int) -> list:
        if self._groups is None:
            groups: dict = {}
            for (s, item, y), v in self.items():
                groups.setdefault((s, y), []).append((item, v))
            self._groups = groups
        return self._groups.get((stmt, year), [])


def _sum_prefix(facts, stmt: str, prefixes: tuple[str, ...], year: int) -> Value | None:
    pool = facts.group(stmt, year) if isinstance(facts, _Facts) else \
        [(item, v) for (s, item, y), v in facts.items() if s == stmt and y == year]
    parts = [v for item, v in pool if item.startswith(prefixes) and not item.startswith("total")]
    if not parts:
        return None
    return Value(sum((p.amount for p in parts), Decimal(0)), set().union(*(p.pages for p in parts)),
                 any(p.upload for p in parts))


def is_bank(conn: sqlite3.Connection, issuer: str, facts=None) -> bool:
    """A bank by its sector (settings file, or chosen in the app), or, failing that, by its statements:
    net interest income printed and no revenue line."""
    try:
        from src.value.assumptions import load
        a = load()
        sec = a.sectors.get(issuer)
        if sec is None:
            row = conn.execute("SELECT sector FROM issuers WHERE id=?", (issuer,)).fetchone()
            sec = a.sector_models.get((row["sector"] if row else "") or "")
        if sec is not None:
            return sec.get("model") == "bank"
    except Exception:   # settings unreadable: fall back to the statements
        pass
    if facts is None:
        return False
    items = {k[1] for k in facts}
    return "net_interest_income" in items and "revenue" not in items


def build_report(conn: sqlite3.Connection, issuer: str, official_only: bool = False) -> dict:
    """The company report. official_only=True is the public version: uploads are never used."""
    row = conn.execute("SELECT * FROM issuers WHERE id = ?", (issuer,)).fetchone()
    if row is None:
        raise KeyError(issuer)
    facts = _facts_by_year(conn, issuer, official_only)
    years = full_years(conn, issuer, official_only)
    only = " AND g.origin = 'official'" if official_only else ""
    currency = row["reporting_currency"] or "USD"
    pages_used: dict[str, set[tuple[str, int]]] = {}

    def get(stmt, item, y):
        v = facts.get((stmt, item, y))
        if v is not None:
            pages_used.setdefault(item, set()).update(v.pages)
        return v.amount if v is not None else None

    def debt(y):
        v = _sum_prefix(facts, "BS", ("borrowings", "debt_securities"), y)
        if v is not None:
            pages_used.setdefault("debt", set()).update(v.pages)
        return v.amount if v is not None else None

    def leases(y):
        v = _sum_prefix(facts, "BS", ("lease_liabilities",), y)
        return v.amount if v is not None else None

    def da(y):
        cf = get("CF", "depreciation_amortisation", y)
        if cf is not None:
            return abs(cf)
        is_ = get("IS", "depreciation_amortisation", y)
        return abs(is_) if is_ is not None else None

    def div(a, b):
        if a is None or b is None or b == 0:
            return None
        return a / b

    lines: list[Metric] = []
    ratios: list[Metric] = []

    def money(key, label, fn, formula, needs):
        m = Metric(key, label, "money", {y: fn(y) for y in years}, formula)
        m.missing = needs if all(v is None for v in m.values.values()) else []
        lines.append(m)
        return m

    def ratio(key, label, kind, fn, formula, needs):
        m = Metric(key, label, kind, {y: fn(y) for y in years}, formula)
        m.missing = needs if all(v is None for v in m.values.values()) else []
        ratios.append(m)
        return m

    rev = money("revenue", "Revenue", lambda y: get("IS", "revenue", y), "as printed", ["revenue"])
    op = money("operating_profit", "Operating profit", lambda y: get("IS", "operating_profit", y), "as printed",
               ["operating_profit"])
    ebitda = money("ebitda", "EBITDA", lambda y: None if get("IS", "operating_profit", y) is None or da(y) is None
                   else get("IS", "operating_profit", y) + da(y), "operating profit + depreciation and amortisation",
                   ["operating_profit", "depreciation_amortisation"])
    ni = money("net_income", "Profit for the year", lambda y: get("IS", "net_income", y), "as printed", ["net_income"])
    cfo = money("cfo", "Cash from operations", lambda y: get("CF", "cfo", y), "as printed", ["cfo"])
    capex = money("capex", "Capital spending", lambda y: get("CF", "capex", y), "payments for property, plant and equipment",
                  ["capex"])
    fcf = money("fcf", "Free cash flow", lambda y: None if cfo.values[y] is None or capex.values[y] is None
                else cfo.values[y] + capex.values[y], "cash from operations + capital spending (spending is negative)",
                ["cfo", "capex"])
    assets = money("total_assets", "Total assets", lambda y: get("BS", "total_assets", y), "as printed", ["total_assets"])
    equity = money("total_equity", "Total equity", lambda y: get("BS", "total_equity", y), "as printed", ["total_equity"])
    cash = money("cash", "Cash and equivalents", lambda y: get("BS", "cash_and_equivalents", y), "as printed",
                 ["cash_and_equivalents"])
    dbt = money("debt", "Borrowings and bonds", debt, "all borrowings + debt securities (leases shown separately)",
                ["borrowings", "debt_securities"])
    money("leases", "Lease liabilities", leases, "all lease liabilities", ["lease_liabilities"])
    nd = money("net_debt", "Net debt", lambda y: None if dbt.values[y] is None or cash.values[y] is None
               else dbt.values[y] - cash.values[y], "borrowings and bonds − cash", ["debt", "cash"])

    def growth(m):
        def fn(y):
            prev = m.values.get(y - 1)
            return None if prev in (None, 0) or m.values[y] is None else m.values[y] / prev - ONE
        return fn

    ratio("revenue_growth", "Revenue growth", "pct", growth(rev), "revenue ÷ last year's revenue − 1", ["revenue (2 years)"])
    ratio("operating_margin", "Operating margin", "pct", lambda y: div(op.values[y], rev.values[y]),
          "operating profit ÷ revenue", ["operating_profit", "revenue"])
    ratio("ebitda_margin", "EBITDA margin", "pct", lambda y: div(ebitda.values[y], rev.values[y]), "EBITDA ÷ revenue",
          ["EBITDA", "revenue"])
    ratio("net_margin", "Net margin", "pct", lambda y: div(ni.values[y], rev.values[y]), "profit ÷ revenue",
          ["net_income", "revenue"])

    def roe(y):
        e0, e1 = equity.values.get(y - 1), equity.values[y]
        base = (e0 + e1) / 2 if e0 is not None and e1 is not None else e1
        return div(ni.values[y], base)
    ratio("roe", "Return on equity", "pct", roe, "profit ÷ average equity (this and last year)", ["net_income", "total_equity"])
    ratio("roa", "Return on assets", "pct", lambda y: div(ni.values[y], assets.values[y]), "profit ÷ total assets",
          ["net_income", "total_assets"])
    ratio("debt_to_equity", "Debt to equity", "x", lambda y: div(dbt.values[y], equity.values[y]),
          "borrowings and bonds ÷ equity", ["debt", "total_equity"])
    ratio("net_debt_to_ebitda", "Net debt to EBITDA", "x", lambda y: div(nd.values[y], ebitda.values[y]),
          "net debt ÷ EBITDA", ["net debt", "EBITDA"])
    ratio("interest_cover", "Interest cover", "x",
          lambda y: div(op.values[y], abs(get("IS", "finance_costs", y)) if get("IS", "finance_costs", y) else None),
          "operating profit ÷ finance costs", ["operating_profit", "finance_costs"])
    ratio("current_ratio", "Current ratio", "x",
          lambda y: div(get("BS", "total_current_assets", y), get("BS", "total_current_liabilities", y)),
          "current assets ÷ current liabilities", ["total_current_assets", "total_current_liabilities"])
    ratio("cash_conversion", "Cash conversion", "x", lambda y: div(cfo.values[y], ni.values[y]),
          "cash from operations ÷ profit", ["cfo", "net_income"])

    bank = is_bank(conn, issuer, facts)
    if bank:
        # Banks print no revenue, EBITDA or current assets: their income is interest and fees, and
        # deposits are their raw material, not debt. They get the measures analysts use for banks.
        lines.clear()
        ratios.clear()

        def other_income(y):
            for item in ("other_income_net", "other_income", "other_incomes", "other_operating_income"):
                v = get("IS", item, y)
                if v is not None:
                    return v
            return None

        nii = money("net_interest_income", "Net interest income", lambda y: get("IS", "net_interest_income", y),
                    "as printed", ["net_interest_income"])
        fees = money("net_fees", "Net fee and commission income",
                     lambda y: get("IS", "net_fee_and_commission_income", y), "as printed",
                     ["net_fee_and_commission_income"])
        oi = money("operating_income", "Operating income",
                   lambda y: None if nii.values[y] is None or fees.values[y] is None
                   else nii.values[y] + fees.values[y] + (other_income(y) or 0),
                   "net interest income + net fees + other income (a bank's revenue)",
                   ["net_interest_income", "net_fee_and_commission_income"])
        imp = money("impairment", "Loan loss charges", lambda y: get("IS", "net_impairment_losses", y),
                    "net impairment losses, as printed (negative = a charge)", ["net_impairment_losses"])
        pbt = money("profit_before_tax", "Profit before tax", lambda y: get("IS", "profit_before_tax", y),
                    "as printed", ["profit_before_tax"])
        costs = money("operating_costs", "Operating costs",
                      lambda y: None if None in (oi.values[y], imp.values[y], pbt.values[y])
                      else -(oi.values[y] + imp.values[y] - pbt.values[y]),
                      "−(operating income + loan loss charges − profit before tax)",
                      ["operating income", "net_impairment_losses", "profit_before_tax"])
        ni = money("net_income", "Profit for the year", lambda y: get("IS", "net_income", y), "as printed",
                   ["net_income"])
        loans = money("loans", "Loans to customers (net)", lambda y: get("BS", "loans_and_advances_net", y)
                      or get("BS", "loans_and_advances", y), "as printed", ["loans_and_advances"])
        dep = money("deposits", "Customer deposits", lambda y: get("BS", "deposits_from_customers", y),
                    "as printed", ["deposits_from_customers"])
        assets = money("total_assets", "Total assets", lambda y: get("BS", "total_assets", y), "as printed",
                       ["total_assets"])
        equity = money("total_equity", "Total equity", lambda y: get("BS", "total_equity", y), "as printed",
                       ["total_equity"])
        dbt = money("debt", "Borrowings and bonds", debt, "borrowings + debt securities (deposits not included)",
                    ["borrowings", "debt_securities"])

        def avg(m, y):
            a0, a1 = m.values.get(y - 1), m.values[y]
            return (a0 + a1) / 2 if a0 is not None and a1 is not None else a1

        ratio("operating_income_growth", "Operating income growth", "pct", growth(oi),
              "operating income ÷ last year's − 1", ["operating income (2 years)"])
        ratio("profit_growth", "Profit growth", "pct", growth(ni), "profit ÷ last year's − 1", ["net_income (2 years)"])
        ratio("nii_to_assets", "Net interest income to assets", "pct",
              lambda y: div(nii.values[y], avg(assets, y)), "net interest income ÷ average total assets",
              ["net_interest_income", "total_assets"])
        ratio("cost_to_income", "Cost to income", "pct",
              lambda y: None if costs.values[y] is None else div(-costs.values[y], oi.values[y]),
              "operating costs ÷ operating income",
              ["operating costs", "operating income"])
        ratio("cost_of_risk", "Cost of risk", "pct",
              lambda y: None if imp.values[y] is None else div(-imp.values[y], avg(loans, y)),
              "loan loss charges ÷ average loans", ["net_impairment_losses", "loans"])
        ratio("loans_to_deposits", "Loans to deposits", "x", lambda y: div(loans.values[y], dep.values[y]),
              "loans ÷ customer deposits", ["loans", "deposits"])
        ratio("equity_to_assets", "Equity to assets", "pct", lambda y: div(equity.values[y], assets.values[y]),
              "equity ÷ total assets", ["total_equity", "total_assets"])
        ratio("roe", "Return on equity", "pct", lambda y: div(ni.values[y], avg(equity, y)),
              "profit ÷ average equity (this and last year)", ["net_income", "total_equity"])
        ratio("roa", "Return on assets", "pct", lambda y: div(ni.values[y], avg(assets, y)),
              "profit ÷ average total assets", ["net_income", "total_assets"])

    filings = [dict(r) for r in conn.execute(
        "SELECT id, kind, title, published_on, origin, source_url, included FROM filings g WHERE issuer_id = ? "
        + only + " ORDER BY published_on DESC",
        (issuer,))]
    status = conn.execute("""SELECT SUM(f.check_status='confirmed') c, SUM(f.check_status='proposed') p, COUNT(*) n
                             FROM facts f JOIN filings g ON g.id = f.filing_id
                             WHERE f.issuer_id = ? AND g.included = 1""" + only, (issuer,)).fetchone()
    uploads_used = any(v.upload for v in facts.values())

    def js(m: Metric) -> dict:
        return {"key": m.key, "label": m.label, "kind": m.kind, "formula": m.formula, "missing": m.missing,
                "values": {str(y): (None if v is None else str(v)) for y, v in m.values.items()}}

    return {
        "issuer": issuer, "name": row["name"], "exchange": row["exchange"], "currency": currency,
        "years": years, "lines": [js(m) for m in lines], "ratios": [js(m) for m in ratios],
        "filings": filings, "uses_uploads": uploads_used, "kind": "bank" if bank else "company",
        "review": {"confirmed": status["c"] or 0, "proposed": status["p"] or 0, "total": status["n"] or 0},
        "readiness": [r.__dict__ for r in readiness(conn, issuer, official_only=official_only)],
        "not_found": sorted({n for m in lines + ratios for n in m.missing}),
    }


def fmt_money(amount: Decimal | None, currency: str) -> str:
    if amount is None:
        return "n/a"
    sign = "-" if amount < 0 else ""
    a = abs(amount)
    unit = "$" if currency == "USD" else "KHR "
    for div, suffix in ((Decimal(10) ** 9, "bn"), (Decimal(10) ** 6, "M"), (Decimal(10) ** 3, "K")):
        if a >= div:
            return f"{sign}{unit}{a / div:,.1f}{suffix}"
    return f"{sign}{unit}{a:,.0f}"


def fmt_metric(value: Decimal | None, kind: str, currency: str) -> str:
    if value is None:
        return "n/a"
    if kind == "pct":
        return f"{value * 100:.1f}%"
    if kind == "x":
        return f"{value:.2f}x"
    return fmt_money(value, currency)


def placeholders(report: dict) -> dict[str, str]:
    """Code-formatted figures the AI may refer to by name, for the latest year (and growth)."""
    if not report["years"]:
        return {}
    y = str(report["years"][-1])
    out = {"latest_year": f"FY{y}"}
    for m in report["lines"] + report["ratios"]:
        raw = m["values"].get(y)
        if raw is not None:
            out[m["key"]] = fmt_metric(Decimal(raw), m["kind"], report["currency"])
    return out
