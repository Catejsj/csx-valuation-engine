"""Standardised financial statements, for programs rather than people.

One row per value, in a fixed long ("tidy") shape that is the same for every company:

    company_id, statement, period_start, period_end, period_type, months, line_item,
    is_standard, label_as_printed, value, currency, value_as_printed, scale,
    check_status, filing_id, published_on, page, pdf_sha256, source_link, source_where

- `value` is the full amount (printed value x scale) as an exact decimal string. Never a float.
- `line_item` is a standard code from `dictionary()` when the printed line matches one,
  otherwise the company's own line, kept with `is_standard = false`. Nothing is merged or
  estimated; every row is a number printed in a report.
- When a later report reprints a figure (a restatement), the newest published value is used.
- Only the three primary statements are exported (no notes, no text).

Formats: JSON (with a header describing units, licence and sources), CSV (same columns,
UTF-8, comma, header row), and a JSON Schema for the JSON file.
"""
from __future__ import annotations

import csv
import io
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from src.core.store import NEWEST_JOIN, NEWEST_ORDER

FORMAT = "csx.statements/1"

# code: (statement, description, other stored names that mean the same line)
STANDARD_ITEMS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    # balance sheet
    "ppe": ("BS", "Property, plant and equipment", ("property_and_equipment", "property_plant_and_equipment")),
    "right_of_use_assets": ("BS", "Right-of-use assets", ("right_of_use_asset",)),
    "intangible_assets": ("BS", "Intangible assets", ()),
    "investment_properties": ("BS", "Investment properties", ("investment_property",)),
    "inventories": ("BS", "Inventories", ()),
    "trade_receivables": ("BS", "Trade and other receivables", ()),
    "cash_and_equivalents": ("BS", "Cash and cash equivalents", ("cash_and_bank_balances",)),
    "total_non_current_assets": ("BS", "Total non-current assets", ()),
    "total_current_assets": ("BS", "Total current assets", ()),
    "total_assets": ("BS", "Total assets", ()),
    "deferred_tax_assets": ("BS", "Deferred tax assets", ()),
    "contract_assets": ("BS", "Contract assets", ()),
    "assets_held_for_sale": ("BS", "Assets held for sale", ()),
    "loans_and_advances": ("BS", "Loans and advances to customers (banks)", ("loans_and_advances_to_customers",)),
    "placements_with_banks": ("BS", "Deposits and placements with other banks (banks)",
                              ("deposits_and_placements_with_other_banks", "balances_with_other_banks")),
    "statutory_deposits": ("BS", "Statutory deposits with the central bank (banks)",
                           ("statutory_deposits_with_the_national_bank_of_cambodia",)),
    "financial_investments": ("BS", "Financial investments", ()),
    "deposits_from_customers": ("BS", "Deposits from customers (banks)", ()),
    "deposits_from_banks": ("BS", "Deposits and placements of other banks and financial institutions (banks)",
                            ("deposits_and_placements_of_other_banks_and_financial_institutions",)),
    "subordinated_debts": ("BS", "Subordinated debts", ("subordinated_debt",)),
    "reserves": ("BS", "Reserves", ()),
    "non_controlling_interests": ("BS", "Non-controlling interests", ()),
    "share_capital": ("BS", "Share capital", ()),
    "share_premium": ("BS", "Share premium", ("share_premiums",)),
    "retained_earnings": ("BS", "Retained earnings (accumulated losses)", ()),
    "total_equity": ("BS", "Total equity", ()),
    "borrowings": ("BS", "Borrowings", ()),
    "lease_liabilities": ("BS", "Lease liabilities", ()),
    "debt_securities": ("BS", "Debt securities issued", ()),
    "trade_payables": ("BS", "Trade and other payables", ()),
    "contract_liabilities": ("BS", "Contract liabilities", ()),
    "current_tax_liabilities": ("BS", "Current income tax liabilities", ("income_tax_payables", "income_tax_payable",
                                                                         "current_income_tax_liabilities")),
    "deferred_tax_liabilities": ("BS", "Deferred tax liabilities", ()),
    "total_non_current_liabilities": ("BS", "Total non-current liabilities", ()),
    "total_current_liabilities": ("BS", "Total current liabilities", ()),
    "total_liabilities": ("BS", "Total liabilities", ()),
    "total_equity_and_liabilities": ("BS", "Total equity and liabilities", ()),
    # income statement
    "revenue": ("IS", "Revenue", ("revenues",)),
    "cost_of_sales": ("IS", "Cost of sales", ("cost_of_goods_sold", "cost_of_services")),
    "gross_profit": ("IS", "Gross profit (loss)", ("gross_loss",)),
    "other_income": ("IS", "Other income", ()),
    "administrative_expenses": ("IS", "Administrative expenses", ("general_and_administrative_expenses",)),
    "operating_profit": ("IS", "Operating profit (loss)", ()),
    "finance_income": ("IS CF", "Finance income (in CF: the adjustment line)", ("interest_income",)),
    "finance_costs": ("IS CF", "Finance costs (in CF: the adjustment line)", ("interest_expense",)),
    "profit_before_tax": ("IS CF", "Profit (loss) before income tax (in CF: the starting line)",
                          ("profit_loss_before_tax", "profit_before_income_tax", "loss_before_income_tax",
                           "loss_profit_before_tax")),
    "income_tax": ("IS", "Income tax expense (credit)", ("tax_expense",)),
    "net_income": ("IS", "Profit (loss) for the period", ("profit_for_the_financial_year",)),
    "total_comprehensive_income": ("IS", "Total comprehensive income",
                                   ("total_comprehensive_income_for_the_financial_year",
                                    "total_comprehensive_income_for_the_year", "total_comprehensive_income_for_the_period")),
    "depreciation_amortisation": ("IS CF", "Depreciation and amortisation", ("depreciation_and_amortisation",)),
    # cash flow
    "operating_profit_before_working_capital": ("CF", "Operating profit before changes in working capital",
                                                ("operating_profit_before_changes_in_working_capital",
                                                 "operating_profit_before_working_capital_changes")),
    "change_in_receivables": ("CF", "Change in trade and other receivables", ("trade_and_other_receivables",)),
    "change_in_inventories": ("CF", "Change in inventories", ("inventories",)),
    "change_in_payables": ("CF", "Change in trade and other payables", ("trade_and_other_payables",)),
    "cash_generated_from_operations": ("CF", "Cash generated from operations",
                                       ("cash_generated_from_operating_activities",)),
    "cfo": ("CF", "Net cash from operating activities", ()),
    "capex": ("CF", "Purchase of property, plant and equipment",
              ("purchase_of_property_and_equipment", "purchases_of_property_and_equipment",
               "purchase_of_property_plant_and_equipment")),
    "purchase_of_intangibles": ("CF", "Purchase of intangible assets", ("purchase_of_intangible_assets",
                                                                        "purchases_of_intangible_assets")),
    "interest_received": ("CF", "Interest received", ()),
    "cfi": ("CF", "Net cash from investing activities", ()),
    "dividends_paid": ("CF", "Dividends paid", ("dividend_paid", "payments_of_dividends", "payment_of_dividends")),
    "interest_paid": ("CF", "Interest paid", ()),
    "proceeds_from_borrowings": ("CF", "Proceeds from borrowings", ("drawdown_of_borrowings", "drawdowns_of_borrowings",
                                                                     "drawdown_of_bank_borrowings")),
    "repayments_of_borrowings": ("CF", "Repayments of borrowings", ("repayment_of_borrowings",
                                                                     "repayments_of_bank_borrowings")),
    "lease_payments": ("CF", "Payments of lease liabilities", ("payment_of_lease_liabilities",
                                                               "payments_of_lease_liabilities",
                                                               "payments_of_lease_liabilities_principal")),
    "income_tax_paid": ("CF", "Income tax paid", ()),
    "cff": ("CF", "Net cash from financing activities", ()),
    "net_change_in_cash": ("CF", "Net increase (decrease) in cash and cash equivalents",
                           ("net_decrease_increase_in_cash_and_cash_equivalents",
                            "net_change_in_cash_and_cash_equivalents",
                            "net_increase_decrease_in_cash_and_cash_equivalents")),
    "cash_begin": ("CF", "Cash and cash equivalents at the beginning of the period",
                   ("cash_and_cash_equivalents_at_beginning_of_the_year", "cash_and_cash_equivalents_at_beginning_of_year",
                    "cash_and_cash_equivalents_at_beginning_of_financial_year",
                    "cash_and_cash_equivalents_at_the_beginning_of_the_period",
                    "cash_and_cash_equivalents_at_beginning_of_the_period")),
    "fx_effect_on_cash": ("CF", "Effect of exchange rate changes on cash", ()),
    "cash_end": ("CF", "Cash and cash equivalents at the end of the period",
                 ("cash_and_cash_equivalents_at_end_of_the_year", "cash_and_cash_equivalents_at_end_of_year",
                  "cash_and_cash_equivalents_at_end_of_financial_year",
                  "cash_and_cash_equivalents_at_the_end_of_the_period",
                  "cash_and_cash_equivalents_at_end_of_the_period")),
}

PERIOD_MONTHS = {"FY": 12, "12M": 12, "9M": 9, "H1": 6, "Q1": 3, "Q2": 3, "Q3": 3, "Q4": 3}
SUFFIXES = ("_current", "_non_current")

COLUMNS = ["company_id", "statement", "period_start", "period_end", "period_type", "months", "line_item",
           "is_standard", "label_as_printed", "value", "currency", "value_as_printed", "scale",
           "check_status", "filing_id", "published_on", "page", "pdf_sha256", "source_link", "source_where"]

# more printed spellings seen in the exchange's reports (2026-11-19)
EXTRA_ALIASES: dict[str, tuple[str, ...]] = {
    "capex": ('purchases_of_property_plant_and_equipment', 'acquisitions_of_property_and_equipment', 'acquisitions_of_property_plant_and_equipment'),
    "purchase_of_intangibles": ('acquisitions_of_intangible_assets',),
    "revenue": ('total_revenue', 'total_revenues'),
    "cash_and_equivalents": ('cash_and_cash_equivalent',),
    "total_equity_and_liabilities": ('total_equity_and_liability', 'total_liabilities_and_equity'),
    "profit_before_tax": ('profit_loss_before_income_tax', 'loss_profit_before_income_tax'),
    "total_comprehensive_income": ('total_comprehensive_income_loss_for_the_year', 'total_comprehensive_loss_for_the_year'),
    "cff": ('cash_used_in_generated_from_financing_activities',),
    "cost_of_sales": ('cost_of_sales_and_services',),
    "net_income": ('loss_for_the_financial_year', 'loss_for_the_year', 'loss_for_the_period', 'loss_profit_for_the_period',
                   'loss_profit_for_the_year', 'profit_loss_for_the_period', 'profit_loss_for_the_year',
                   'profit_for_the_period', 'net_loss_for_the_year'),
    "operating_profit": ('operating_loss', 'operating_loss_profit', 'operating_profit_loss'),
}
for _code, _more in EXTRA_ALIASES.items():
    _st, _desc, _aliases = STANDARD_ITEMS[_code]
    STANDARD_ITEMS[_code] = (_st, _desc, _aliases + _more)

_ALIAS = {alias: code for code, (_, _, aliases) in STANDARD_ITEMS.items() for alias in aliases}


def standard_code(statement: str, stored: str) -> tuple[str, bool]:
    """Map a stored line-item name to (code, is_standard). Balance sheet items printed in both
    the current and non-current sections keep their suffix (borrowings_current)."""
    base, suffix = stored, ""
    if statement == "BS" and base.endswith("_net"):
        base = base[:-4]                    # "Property and equipment - net"
    if statement == "BS":
        for s in SUFFIXES[::-1]:            # check "_non_current" before "_current"
            if base.endswith(s) and not base.startswith("total_"):
                base, suffix = base[: -len(s)], s
                break
    code = base if base in STANDARD_ITEMS else _ALIAS.get(base)
    if code and statement in STANDARD_ITEMS[code][0].split():
        return code + suffix, True
    return stored, False


def _period_start(end: date, months: int) -> date:
    first = end + timedelta(days=1)
    y, m = divmod(first.month - 1 - months, 12)
    return date(first.year + y, m + 1, first.day if first.day <= 28 else 28)


def dictionary() -> list[dict]:
    return [{"code": code, "statement": st, "description": desc, "also_matches": list(aliases),
             "note": "balance sheet codes may carry _current / _non_current when printed in both sections"
             if st == "BS" else ""}
            for code, (st, desc, aliases) in STANDARD_ITEMS.items()]


def rows(conn: sqlite3.Connection, issuer_id: str, *, official_only: bool = True,
         period_types: set[str] | None = None) -> list[dict]:
    """Every value for one company, newest version of each figure, in the fixed column order."""
    from src.api.data import public_link
    only = "AND g.origin = 'official'" if official_only else ""
    found = conn.execute(
        f"""SELECT f.statement, f.period_end, f.period_type, f.line_item, f.label_as_printed,
                   f.value_as_reported, f.currency, f.scale, f.page, f.check_status,
                   g.id AS filing_id, g.title, g.published_on, g.source_url, g.pdf_sha256
            FROM facts f JOIN filings g ON g.id = f.filing_id {NEWEST_JOIN}
            WHERE f.issuer_id = ? AND g.included = 1 AND f.check_status != 'failed'
              AND f.statement IN ('BS','IS','CF') {only}
            ORDER BY {NEWEST_ORDER}, f.id""", (issuer_id, issuer_id)).fetchall()
    latest: dict[tuple, dict] = {}
    taken: dict[tuple, str] = {}    # one standard code per printed line within a report
    for r in found:
        if period_types and r["period_type"] not in period_types:
            continue
        code, standard = standard_code(r["statement"], r["line_item"])
        slot = (r["filing_id"], r["statement"], r["period_end"], r["period_type"], code)
        if standard and taken.setdefault(slot, r["line_item"]) != r["line_item"]:
            code, standard = r["line_item"], False   # two printed lines would share a code: keep both apart
        end = date.fromisoformat(r["period_end"])
        months = None if r["statement"] == "BS" else PERIOD_MONTHS.get(r["period_type"])
        link = public_link(r["source_url"], issuer_id, r["title"] or "", r["published_on"]) if official_only \
            else {"url": r["source_url"], "where": ""}
        row = {
            "company_id": issuer_id, "statement": r["statement"],
            "period_start": _period_start(end, months).isoformat() if months else "",
            "period_end": r["period_end"], "period_type": r["period_type"], "months": months or "",
            "line_item": code, "is_standard": standard, "label_as_printed": r["label_as_printed"],
            "value": str(Decimal(r["value_as_reported"]) * int(r["scale"])), "currency": r["currency"],
            "value_as_printed": r["value_as_reported"], "scale": int(r["scale"]),
            "check_status": r["check_status"], "filing_id": r["filing_id"], "published_on": r["published_on"],
            "page": r["page"], "pdf_sha256": r["pdf_sha256"], "source_link": link["url"], "source_where": link["where"],
        }
        latest[(r["statement"], r["period_end"], r["period_type"], code)] = row   # newer filings overwrite
    order = {"BS": 0, "IS": 1, "CF": 2}
    return sorted(latest.values(), key=lambda x: (x["period_end"], x["period_type"], order[x["statement"]]))


def to_json(conn: sqlite3.Connection, issuer_id: str, *, official_only: bool = True) -> dict:
    from src.api.data import LICENCE, SOURCES
    issuer = conn.execute("SELECT id, name, reporting_currency FROM issuers WHERE id = ?", (issuer_id,)).fetchone()
    return {
        "format": FORMAT,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "company": {"id": issuer["id"], "name": issuer["name"], "reporting_currency": issuer["reporting_currency"]},
        "units": "value = full amount in `currency` as an exact decimal string; value_as_printed x scale = value",
        "periods": {"FY": "audited annual", "12M": "twelve months printed in a quarterly report (unaudited)",
                    "9M": "nine months year to date", "H1": "six months", "Q1-Q4": "three months, named by the "
                    "calendar month of period_end", "BS": "balance sheet values are at period_end (months empty)"},
        "columns": COLUMNS,
        "licence": LICENCE, "sources": SOURCES if official_only else [],
        "facts": rows(conn, issuer_id, official_only=official_only),
    }


def to_csv(conn: sqlite3.Connection, issuer_id: str, *, official_only: bool = True) -> str:
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator="\n")
    w.writeheader()
    for r in rows(conn, issuer_id, official_only=official_only):
        w.writerow({**r, "is_standard": "true" if r["is_standard"] else "false",
                    "label_as_printed": _safe_cell(r["label_as_printed"])})
    return out.getvalue()


def _safe_cell(text: str) -> str:
    """A label must never run as a formula when the CSV is opened in a spreadsheet."""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def json_schema() -> dict:
    s, n = {"type": "string"}, {"type": ["integer", "string"]}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "csx.statements/1",
        "title": "Standardised financial statements (one company)",
        "type": "object",
        "required": ["format", "company", "columns", "facts"],
        "properties": {
            "format": {"const": FORMAT},
            "generated_at": s, "units": s, "periods": {"type": "object"}, "licence": {"type": "object"},
            "sources": {"type": "array"},
            "company": {"type": "object", "required": ["id", "name"],
                        "properties": {"id": s, "name": s, "reporting_currency": {"type": ["string", "null"]}}},
            "columns": {"type": "array", "items": s},
            "facts": {"type": "array", "items": {
                "type": "object", "required": COLUMNS, "additionalProperties": False,
                "properties": {
                    "company_id": s, "statement": {"enum": ["BS", "IS", "CF"]},
                    "period_start": {"type": "string", "pattern": r"^(\d{4}-\d{2}-\d{2})?$"},
                    "period_end": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
                    "period_type": {"enum": list(PERIOD_MONTHS)}, "months": {"enum": [3, 6, 9, 12, ""]},
                    "line_item": s, "is_standard": {"type": "boolean"}, "label_as_printed": s,
                    "value": {"type": "string", "pattern": r"^-?\d+(\.\d+)?$"},
                    "currency": {"enum": ["USD", "KHR"]}, "value_as_printed": s,
                    "scale": {"enum": [1, 1000, 1000000]},
                    "check_status": {"enum": ["proposed", "confirmed"]}, "filing_id": s, "published_on": s,
                    "page": n, "pdf_sha256": s, "source_link": s, "source_where": s}}},
        },
    }
