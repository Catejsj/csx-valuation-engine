"""Map printed row labels to our standard line-item names.

Mapped labels get a standard name (revenue, total_assets, cfo, ...). Unmapped labels keep
a snake_case version of the printed label, so no row is lost. Balance-sheet labels that
appear in both the current and non-current sections get a _current / _non_current suffix.
"""
from __future__ import annotations

import re
from collections import Counter

from src.core.schema import Statement

from .statements import Row

STANDARD: dict[Statement, dict[str, str]] = {
    Statement.BALANCE_SHEET: {
        "total assets": "total_assets",
        "total current assets": "total_current_assets",
        "total non current assets": "total_non_current_assets",
        "current assets": "total_current_assets",          # section totals printed without "Total"
        "non current assets": "total_non_current_assets",
        "current liabilities": "total_current_liabilities",
        "non current liabilities": "total_non_current_liabilities",
        "cash and cash equivalents": "cash_and_equivalents",
        "inventories": "inventories",
        "trade and other receivables": "trade_receivables",
        "property plant and equipment": "ppe",
        "right of use assets": "right_of_use_assets",
        "intangible assets": "intangible_assets",
        "total equity": "total_equity",
        "share capital": "share_capital",
        "share premium": "share_premium",
        "retained earnings": "retained_earnings",
        "total liabilities": "total_liabilities",
        "total current liabilities": "total_current_liabilities",
        "total non current liabilities": "total_non_current_liabilities",
        "total equity and liabilities": "total_equity_and_liabilities",
        "total liabilities and equity": "total_equity_and_liabilities",
        "borrowings": "borrowings",
        "lease liabilities": "lease_liabilities",
        "debt securities": "debt_securities",
        "trade and other payables": "trade_payables",
        "current tax liabilities": "current_tax_liabilities",
        "deferred tax liabilities net": "deferred_tax_liabilities",
    },
    Statement.INCOME: {
        "revenue": "revenue",
        "revenues": "revenue",
        "operating profit": "operating_profit",
        "finance income": "finance_income",
        "finance costs": "finance_costs",
        "profit before income tax": "profit_before_tax",
        "profit before tax": "profit_before_tax",
        "income tax credit": "income_tax",
        "income tax expense": "income_tax",
        "income tax": "income_tax",
        "profit for the year": "net_income",
        "depreciation and amortisation": "depreciation_amortisation",
        "total comprehensive income for the year": "total_comprehensive_income",
    },
    Statement.CASH_FLOW: {
        "net increase in cash and cash equivalents": "net_change_in_cash",
        "net decrease in cash and cash equivalents": "net_change_in_cash",
        "net increase decrease in cash and cash equivalents": "net_change_in_cash",
        "cash and cash equivalents at the beginning of the year": "cash_begin",
        "cash and cash equivalents at the end of the year": "cash_end",
        "payments for property plant and equipment": "capex",
        "purchase of property plant and equipment": "capex",
        "interest paid": "interest_paid",
        "income tax paid": "income_tax_paid",
        "exchange differences": "fx_effect_on_cash",
        "depreciation and amortisation charge": "depreciation_amortisation",
        "depreciation and amortisation": "depreciation_amortisation",
    },
}


def normalize(label: str) -> str:
    text = label.lower().replace("–", " ").replace("—", " ").replace("-", " ")
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def slug(label: str) -> str:
    out = normalize(label).replace(" ", "_")
    if not out or not out[0].isalpha():
        out = "row_" + out
    return out[:80]


def _cash_flow_total(norm: str) -> str | None:
    if norm.startswith("net cash"):
        for kind, name in (("operating", "cfo"), ("investing", "cfi"), ("financing", "cff")):
            if f"{kind} activities" in norm:
                return name
    return None


def _section_suffix(section: str) -> str:
    if "non current" in normalize(section):
        return "_non_current"
    if "current" in normalize(section):
        return "_current"
    return ""


def line_items(statement: Statement, rows: list[Row]) -> list[str]:
    """One line-item name per row, unique within the statement."""
    norms = [normalize(r.label) for r in rows]
    counts = Counter(norms)
    names: list[str] = []
    for row, norm in zip(rows, norms):
        name = STANDARD.get(statement, {}).get(norm)
        if name == "fx_effect_on_cash" and not {"net_change_in_cash", "cash_begin"} & set(names):
            name = None   # an FX adjustment inside operating activities, not the effect on cash
        if name is None and statement is Statement.CASH_FLOW:
            name = _cash_flow_total(norm)
            if name is None and ("translation differences" in norm or norm == "exchange differences") \
                    and {"net_change_in_cash", "cash_begin"} & set(names):
                name = "fx_effect_on_cash"
        if name is None and statement is Statement.INCOME and norm.startswith("income tax") \
                and "paid" not in norm:
            name = "income_tax"
        if name is None and statement is Statement.INCOME and re.match(
                r"(net )?(profit|loss|profit loss|loss profit|profit \(loss\)) for the (financial )?(year|period)\b", norm) \
                and "attributable" not in norm:
            # "Profit for the year, representing total comprehensive income", "... carried forward to next page"
            name = "net_income"
        if name is None:
            name = slug(row.label)
        if statement is Statement.BALANCE_SHEET and counts[norm] > 1 and not norm.startswith("total"):
            name += _section_suffix(row.section)
        names.append(name)
    seen: Counter[str] = Counter()
    unique = []
    for name in names:
        seen[name] += 1
        unique.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    return unique
