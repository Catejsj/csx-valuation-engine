"""Arithmetic checks that catch misread numbers before anyone uses them.

All checks compare values within one printed column (same year, currency and scale), so
no conversion is involved. Tolerance: one printed unit per term, for rounding.
A check whose inputs are missing is reported as "cannot check", never as a pass.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.core.schema import Statement


@dataclass(frozen=True)
class CheckResult:
    name: str
    statement: Statement
    column: str            # e.g. "2024 USD"
    status: str            # "pass" | "fail" | "cannot check"
    left: Decimal | None = None
    right: Decimal | None = None
    pages: tuple[int, ...] = ()

    @property
    def diff(self) -> Decimal | None:
        if self.left is None or self.right is None:
            return None
        return self.left - self.right


def _compare(name, stmt, column, left, right, n_terms, pages) -> CheckResult:
    if left is None or right is None:
        return CheckResult(name, stmt, column, "cannot check", left, right, pages)
    ok = abs(left - right) <= Decimal(max(1, n_terms))
    return CheckResult(name, stmt, column, "pass" if ok else "fail", left, right, pages)


def _sum(values: dict[str, Decimal], keys: list[str]) -> Decimal | None:
    if any(k not in values for k in keys):
        return None
    return sum((values[k] for k in keys), Decimal(0))


def identity_checks(stmt: Statement, column: str, v: dict[str, Decimal],
                    pages: tuple[int, ...]) -> list[CheckResult]:
    """Accounting identities between named totals."""
    def eq(name, left_keys, right_keys):
        return _compare(name, stmt, column, _sum(v, left_keys), _sum(v, right_keys),
                        len(left_keys) + len(right_keys), pages)

    if stmt is Statement.BALANCE_SHEET:
        return [
            eq("assets = equity + liabilities", ["total_assets"], ["total_equity_and_liabilities"]),
            eq("current + non-current assets = total assets",
               ["total_current_assets", "total_non_current_assets"], ["total_assets"]),
            eq("current + non-current liabilities = total liabilities",
               ["total_current_liabilities", "total_non_current_liabilities"], ["total_liabilities"]),
            eq("equity + liabilities = total", ["total_equity", "total_liabilities"],
               ["total_equity_and_liabilities"]),
        ]
    if stmt is Statement.INCOME:
        return [
            eq("operating profit + finance income + finance costs = profit before tax",
               ["operating_profit", "finance_income", "finance_costs"], ["profit_before_tax"]),
            eq("profit before tax + income tax = profit for the year",
               ["profit_before_tax", "income_tax"], ["net_income"]),
        ]
    if stmt is Statement.CASH_FLOW:
        return [
            eq("operating + investing + financing = net change in cash", ["cfo", "cfi", "cff"],
               ["net_change_in_cash"]),
            eq("opening cash + change + FX = closing cash",
               ["cash_begin", "net_change_in_cash", "fx_effect_on_cash"], ["cash_end"]),
        ]
    return []


def component_checks(stmt: Statement, column: str, rows: list[tuple[str, str, str, Decimal, int]]
                     ) -> list[CheckResult]:
    """Rows add up to their subtotal.

    rows: (line_item, label, section, value, page) in printed order.
    Balance sheet: each "Total ..." row = sum of the rows in its section since the last total.
    Income statement: operating profit = every row above it.
    Cash flow: investing / financing totals = rows in their section.
    """
    results: list[CheckResult] = []
    if stmt is Statement.BALANCE_SHEET:
        # Each total = the rows since the previous total, optionally plus the most recent
        # totals not yet used by a higher total (total assets = non-current + current;
        # total equity = owners' total + non-controlling interests).
        bucket: list[tuple[Decimal, int]] = []
        since_total: list[tuple[Decimal, int]] = []   # every row since the last total, across sections
        pending: list[tuple[Decimal, int]] = []
        current_section = None
        for item, label, section, value, page in rows:
            if section != current_section:
                bucket, current_section = [], section
            if not (label.lower().startswith(("total", "subtotal", "attributable to owners",
                                              "equity attributable to")) or item.startswith("total_")):
                bucket.append((value, page))
                since_total.append((value, page))
                continue
            loose = sum((x for x, _ in bucket), Decimal(0))
            result = None
            if bucket:
                pages = tuple(sorted({p for _, p in bucket} | {page}))
                result = _compare(f"rows add up to '{label}'", stmt, column, loose, value, len(bucket) + 1, pages)
            if result is None or result.status == "fail":
                for k in range(1, len(pending) + 1):
                    used = pending[-k:]
                    pages = tuple(sorted({p for _, p in bucket + used} | {page}))
                    chained = _compare(f"previous totals + rows add up to '{label}'", stmt, column,
                                       loose + sum((x for x, _ in used), Decimal(0)), value,
                                       len(bucket) + k + 1, pages)
                    if chained.status == "pass":
                        result = chained
                        del pending[-k:]
                        break
            if (result is None or result.status == "fail") and len(since_total) > len(bucket) and not pending:
                # a wrapped label read as a section heading splits the rows of one total
                pages = tuple(sorted({p for _, p in since_total} | {page}))
                across = _compare(f"rows add up to '{label}' (across headings)", stmt, column,
                                  sum((x for x, _ in since_total), Decimal(0)), value, len(since_total) + 1, pages)
                if across.status == "pass":
                    result = across
            if result is not None:
                results.append(result)
            bucket, since_total = [], []
            pending.append((value, page))
    elif stmt is Statement.INCOME:
        above: list[tuple[Decimal, int]] = []
        for item, label, section, value, page in rows:
            if item != "operating_profit" and (label.lower().startswith(("total", "subtotal", "gross profit"))
                                               or label.lower().strip() in ("revenue", "revenues")):
                # a printed subtotal replaces the rows it adds up (so they are not counted twice)
                for k in range(len(above), 0, -1):   # the rows just above that it adds up
                    if abs(sum((x for x, _ in above[-k:]), Decimal(0)) - value) <= k + 1:
                        above = above[:len(above) - k]
                        break
                above.append((value, page))
                continue
            if item == "operating_profit":
                if above:
                    pages = tuple(sorted({p for _, p in above} | {page}))
                    results.append(_compare("rows add up to operating profit", stmt, column,
                                            sum((x for x, _ in above), Decimal(0)), value,
                                            len(above) + 1, pages))
                break
            above.append((value, page))
    elif stmt is Statement.CASH_FLOW:
        for total_item, word in (("cfi", "investing"), ("cff", "financing")):
            parts: list[tuple[Decimal, int]] = []
            total_row: tuple[Decimal, int] | None = None
            for it, lab, sec, v, p in rows:          # only rows printed above the total count
                if it == total_item:
                    total_row = (v, p)
                    break
                if word in sec and it not in ("cfo", "cfi", "cff", "net_change_in_cash"):
                    parts.append((v, p))
            if parts and total_row:
                value, page = total_row
                pages = tuple(sorted({p for _, p in parts} | {page}))
                results.append(_compare(f"rows add up to {word} cash flow", stmt, column,
                                        sum((x for x, _ in parts), Decimal(0)), value,
                                        len(parts) + 1, pages))
    return results


def cross_statement_checks(column: str, bs: dict[str, Decimal], cf: dict[str, Decimal],
                           pages: tuple[int, ...]) -> list[CheckResult]:
    return [_compare("closing cash (cash flow) = cash on balance sheet", Statement.CASH_FLOW, column,
                     cf.get("cash_end"), bs.get("cash_and_equivalents"), 2, pages)]
