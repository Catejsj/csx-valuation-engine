"""Turn extracted statement tables into stored facts, and run every check on every column.

Stored: the primary-currency columns (the first currency printed, i.e. the reporting
currency), for every year shown. Other currency columns (e.g. a KHR translation) are not
stored, but they are still checked, and they give an implied FX rate as a cross-check.
Rows with a parsing problem are never stored; they are listed for review.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from src.core import checks, store
from src.core.schema import Fact, Filing, Issuer, Method, Statement

from .labels import line_items
from .statements import Column, StatementTable


@dataclass
class LoadReport:
    facts: list[Fact] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    checks: list[checks.CheckResult] = field(default_factory=list)
    implied_fx: list[str] = field(default_factory=list)

    @property
    def failed(self) -> list[checks.CheckResult]:
        return [c for c in self.checks if c.status == "fail"]


def _period_for(table: StatementTable, col: Column, kind: str = "annual") -> date:
    if kind != "annual" and col.end_month and col.end_day and (col.end_month, col.end_day) != (1, 1):
        return date(col.year, col.end_month, col.end_day)   # "30 September 2026  31 December 2025"
    end = table.period_end
    if col.year == end.year:
        return end
    day = min(end.day, 28) if end.month == 2 else end.day
    return date(col.year, end.month, day)


def _period_type(kind: str, table: StatementTable, col: Column, end: date) -> str | None:
    """Period type to store this column under, or None when it is not stored.

    Quarterly and half-year reports: only the current period is stored (comparatives come
    from their own reports). Balance sheet: Q1..Q4 / H1 by date. Income and cash flow:
    3-month columns are Q1..Q4, 6-month H1, 9-month 9M, 12-month 12M (a full year printed
    in a quarterly report, unaudited; FY is kept for the audited annual report).
    """
    if kind == "annual":
        return store.period_type_for(kind, end)
    if end != table.period_end:
        return None
    if table.statement is Statement.BALANCE_SHEET:
        return store.period_type_for(kind, end)
    months = col.months or (6 if kind == "semiannual" else None)
    if months == 3:
        return f"Q{(end.month - 1) // 3 + 1}"
    return {6: "H1", 9: "9M", 12: "12M"}.get(months)


def _col_name(col: Column) -> str:
    scale = {1: "", 1_000: " thousand", 1_000_000: " million"}[int(col.scale)]
    entity = f" {col.entity}" if col.entity else ""
    span = f" {col.months}m" if col.months else ""
    return f"{col.year}{span} {col.currency.value}{scale}{entity}"


def build(tables: list[StatementTable], issuer_id: str, filing_id: str, kind: str = "annual") -> LoadReport:
    report = LoadReport()
    by_stmt = {t.statement: t for t in tables}
    values_by: dict[tuple[Statement, int], dict[str, Decimal]] = {}
    stored: set[tuple] = set()     # (statement, period end, period type) already taken by an earlier column

    for table in tables:
        names = line_items(table.statement, table.rows)
        primary = table.columns[0].currency
        for row, name in zip(table.rows, names):
            if row.problem:
                report.problems.append(f"p{row.page} {table.statement.value} '{row.label}': {row.problem}")
        for ci, col in enumerate(table.columns):
            good = [(n, r) for n, r in zip(names, table.rows) if not r.problem]
            values = {n: r.values[ci] for n, r in good}
            values_by[(table.statement, ci)] = values
            pages = tuple(table.pages)
            report.checks += checks.identity_checks(table.statement, _col_name(col), values, pages)
            report.checks += checks.component_checks(
                table.statement, _col_name(col),
                [(n, r.label, r.section, r.values[ci], r.page) for n, r in good])
            if col.currency is not primary or col.entity == "company":
                continue   # facts: the primary currency, and the group when group + company are printed
            end = _period_for(table, col, kind)
            ptype = _period_type(kind, table, col, end)
            if ptype is None:
                continue
            key = (table.statement, end, str(ptype))
            if key in stored:
                report.problems.append(f"{table.statement.value} column '{_col_name(col)}' repeats a period "
                                       "already stored from this report (header misread?); not stored")
                continue
            stored.add(key)
            for n, r in good:
                report.facts.append(Fact(
                    issuer_id=issuer_id, filing_id=filing_id, statement=table.statement,
                    period_end=end, period_type=ptype, line_item=n,
                    label_as_printed=r.label, value_as_reported=r.values[ci],
                    currency=col.currency, scale=col.scale, page=r.page, method=Method.TEXT))

    bs, cf = by_stmt.get(Statement.BALANCE_SHEET), by_stmt.get(Statement.CASH_FLOW)
    if bs and cf and bs.columns == cf.columns:
        for ci, col in enumerate(bs.columns):
            report.checks += checks.cross_statement_checks(
                _col_name(col), values_by[(Statement.BALANCE_SHEET, ci)],
                values_by[(Statement.CASH_FLOW, ci)], tuple(bs.pages + cf.pages))

    # Implied FX from a translated column (info only; compared with NBC rates in Phase 3).
    if bs:
        cols = bs.columns
        for i, a in enumerate(cols):
            for j, b in enumerate(cols):
                if a.year == b.year and a.currency.value == "USD" and b.currency.value == "KHR":
                    va = values_by[(Statement.BALANCE_SHEET, i)].get("total_assets")
                    vb = values_by[(Statement.BALANCE_SHEET, j)].get("total_assets")
                    if va and vb:
                        rate = (vb * int(b.scale)) / (va * int(a.scale))
                        report.implied_fx.append(f"{a.year}: total assets imply {rate:.2f} KHR per USD (period end)")
    return report


def save(conn: sqlite3.Connection, issuer: Issuer, filing: Filing, report: LoadReport) -> int:
    """Store issuer (if new), filing and facts. Returns the number of facts stored."""
    if conn.execute("SELECT 1 FROM issuers WHERE id = ?", (issuer.id,)).fetchone() is None:
        store.add_issuer(conn, issuer)
    store.add_filing(conn, filing)
    for fact in report.facts:
        store.add_fact(conn, fact)
    return len(report.facts)
