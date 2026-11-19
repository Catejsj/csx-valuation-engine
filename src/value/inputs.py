"""Company inputs for valuation: full-year figures in USD, market data and liquidity.

Figures come from the standardised statements (the same rows the export publishes), so
every number used can be traced to a page. KHR figures are converted at the exchange's
published KHR/USD rate: balance sheet at the period-end rate, income statement and cash
flow at the average rate over the period.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

DEBT_CODES = ("borrowings", "lease_liabilities", "debt_securities", "subordinated_debts")


@dataclass
class Year:
    end: str
    values: dict[str, float] = field(default_factory=dict)     # standard code -> USD
    sources: dict[str, str] = field(default_factory=dict)      # standard code -> "filing p.N"
    fx: str = ""                                               # how KHR was converted, if it was

    def get(self, code: str) -> float | None:
        return self.values.get(code)

    def sum_of(self, *codes: str) -> float | None:
        """Sum a code and its _current / _non_current parts, whichever were printed."""
        total, found = 0.0, False
        for code in codes:
            for key in (code, f"{code}_current", f"{code}_non_current"):
                if key in self.values:
                    total += self.values[key]
                    found = True
        return total if found else None


def _rate_on(conn: sqlite3.Connection, day: str) -> float | None:
    r = conn.execute("SELECT rate FROM fx_rates WHERE base='USD' AND quote='KHR' AND on_date<=? "
                     "ORDER BY on_date DESC LIMIT 1", (day,)).fetchone()
    return float(r[0]) if r else None


def _rate_avg(conn: sqlite3.Connection, start: str, end: str) -> float | None:
    r = conn.execute("SELECT AVG(CAST(rate AS REAL)), COUNT(*) FROM fx_rates WHERE base='USD' AND quote='KHR' "
                     "AND on_date BETWEEN ? AND ?", (start, end)).fetchone()
    return float(r[0]) if r and r[1] else None


def _interest_in_operating(conn: sqlite3.Connection, filing_id: str, period_end: str) -> bool | None:
    """Is 'interest paid' printed inside operating activities? (IFRS allows either place.)
    Facts are stored in printed order, so it is above 'net cash from operating' when its id is lower."""
    ids = dict(conn.execute(
        "SELECT line_item, MIN(id) FROM facts WHERE filing_id=? AND statement='CF' AND period_end=? "
        "AND line_item IN ('interest_paid','cfo') GROUP BY line_item", (filing_id, period_end)).fetchall())
    if "interest_paid" not in ids or "cfo" not in ids:
        return None
    return ids["interest_paid"] < ids["cfo"]


def years(conn: sqlite3.Connection, issuer_id: str, *, official_only: bool = True) -> list[Year]:
    """Full years, oldest first, with every standard code in USD."""
    from src.export.standard import rows
    out: dict[str, Year] = {}
    for r in rows(conn, issuer_id, official_only=official_only, period_types={"FY"}):
        if not r["is_standard"]:
            continue
        y = out.setdefault(r["period_end"], Year(r["period_end"]))
        value = float(r["value"])
        if r["currency"] == "KHR":
            end = r["period_end"]
            if r["statement"] == "BS":
                rate = _rate_on(conn, end)
                how = f"balance sheet at {rate:,.0f} KHR/USD ({end})" if rate else ""
            else:
                start = (date.fromisoformat(end) - timedelta(days=364)).isoformat()
                rate = _rate_avg(conn, start, end)
                how = f"income and cash flow at the {start}..{end} average, {rate:,.0f} KHR/USD" if rate else ""
            if not rate:
                continue      # no rate for that date: leave the value out rather than guess
            value /= rate
            if how and how not in y.fx:
                y.fx = (y.fx + "; " if y.fx else "") + how
        y.values[r["line_item"]] = value
        y.sources[r["line_item"]] = f"{r['filing_id']} p.{r['page']}"
        if r["line_item"] == "cfo":
            y.values["_interest_in_cfo"] = 1.0 if _interest_in_operating(conn, r["filing_id"], r["period_end"]) else 0.0
    return [out[k] for k in sorted(out)]


@dataclass
class Market:
    as_of: str
    price_khr: float
    market_cap_usd: float
    shares: float
    fx: float
    zero_volume_share: float
    turnover: float          # value traded over the last year / market cap
    amihud: float | None     # mean |daily return| per USD 1 million traded
    days: int


def market(conn: sqlite3.Connection, issuer_id: str, lookback: int = 250) -> Market | None:
    rows = conn.execute("SELECT on_date, close, volume, market_cap FROM prices WHERE issuer_id=? "
                        "ORDER BY on_date DESC LIMIT ?", (issuer_id, lookback + 1)).fetchall()
    rows = [r for r in rows if r["market_cap"]]
    if len(rows) < 20:
        return None
    last = rows[0]
    fx = _rate_on(conn, last["on_date"])
    if not fx:
        return None
    price = float(last["close"])
    cap_usd = float(last["market_cap"]) * 1_000_000 / fx      # market cap is published in million KHR
    window = rows[:lookback]
    zero = sum(1 for r in window if int(r["volume"]) == 0) / len(window)
    traded = sum(int(r["volume"]) * float(r["close"]) for r in window) / fx
    ratios = []
    for a, b in zip(window, rows[1:lookback + 1]):
        vol_usd = int(a["volume"]) * float(a["close"]) / fx
        if vol_usd > 0 and float(b["close"]) > 0:
            ratios.append(abs(float(a["close"]) / float(b["close"]) - 1) / (vol_usd / 1_000_000))
    return Market(as_of=last["on_date"], price_khr=price, market_cap_usd=cap_usd,
                  shares=float(last["market_cap"]) * 1_000_000 / price, fx=fx, zero_volume_share=zero,
                  turnover=traded / cap_usd * (250 / len(window)), amihud=sum(ratios) / len(ratios) if ratios else None,
                  days=len(window))
