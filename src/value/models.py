"""The valuation formulas, as plain functions with no database or files.

Textbook versions, written so each step can be rebuilt by hand in a spreadsheet:

- cost_of_equity:   rf + beta x mature ERP + country risk premium
- relever:          beta_L = beta_U x (1 + (1 - t) x D/E)
- wacc:             E/(D+E) x ke + D/(D+E) x kd x (1 - t)
- fcff_value:       two-stage: years 1..n grow at g1, then a terminal value at g
- residual_income:  book equity + PV of (ROE - ke) x opening book equity, then a terminal value
                    (lasting forever, or fading by a persistence factor each year)
- dividend_value:   two-stage dividend discount
All rates are decimals (0.05 = 5%). Cash flows are discounted at the end of each year.
"""
from __future__ import annotations

from dataclasses import dataclass, field


def cost_of_equity(rf: float, beta: float, erp: float, crp: float) -> float:
    return rf + beta * erp + crp


def relever(beta_u: float, tax: float, de: float) -> float:
    return beta_u * (1 + (1 - tax) * de)


def wacc(ke: float, kd: float, tax: float, debt: float, equity: float) -> float:
    total = debt + equity
    return equity / total * ke + debt / total * kd * (1 - tax)


@dataclass
class Result:
    value: float                      # equity value (or enterprise value for fcff before the bridge)
    steps: list[tuple[str, float]] = field(default_factory=list)


def fcff_value(base_fcff: float, g1: float, years: int, g: float, rate: float) -> Result:
    """Enterprise value from free cash flow to the firm (two-stage)."""
    if rate <= g:
        raise ValueError("discount rate must be above terminal growth")
    steps, pv, cf = [], 0.0, base_fcff
    for t in range(1, years + 1):
        cf *= 1 + g1
        pv += cf / (1 + rate) ** t
        steps.append((f"FCFF year {t}", cf))
    tv = cf * (1 + g) / (rate - g)
    pv_tv = tv / (1 + rate) ** years
    steps += [("PV of years 1..n", pv), ("terminal value", tv), ("PV of terminal value", pv_tv)]
    return Result(pv + pv_tv, steps)


def residual_income(book: float, roe: float, ke: float, g_book: float, years: int, g: float,
                    persistence: float | None = None) -> Result:
    """Equity value = book + PV of excess returns (ROE - ke) on opening book, then terminal.
    persistence=None: the last excess return lasts forever, growing at g. A number between 0 and 1:
    it fades by that factor each year (the next year's is persistence x this year's), so the terminal
    value is RI x w / (1 + ke - w)."""
    if ke <= g:
        raise ValueError("cost of equity must be above terminal growth")
    steps, pv, b = [], 0.0, book
    ri = 0.0
    for t in range(1, years + 1):
        ri = (roe - ke) * b
        pv += ri / (1 + ke) ** t
        steps.append((f"excess return year {t}", ri))
        b *= 1 + g_book
    tv = ri * (1 + g) / (ke - g) if persistence is None else ri * persistence / (1 + ke - persistence)
    pv_tv = tv / (1 + ke) ** years
    steps += [("PV of excess returns", pv), ("terminal value", tv), ("PV of terminal value", pv_tv)]
    return Result(book + pv + pv_tv, steps)


def justified_pb(roe: float, ke: float, g: float) -> float:
    if ke <= g:
        raise ValueError("cost of equity must be above terminal growth")
    return (roe - g) / (ke - g)


def dividend_value(d0: float, g1: float, years: int, g: float, ke: float) -> Result:
    if ke <= g:
        raise ValueError("cost of equity must be above terminal growth")
    steps, pv, d = [], 0.0, d0
    for t in range(1, years + 1):
        d *= 1 + g1
        pv += d / (1 + ke) ** t
        steps.append((f"dividends year {t}", d))
    tv = d * (1 + g) / (ke - g)
    pv_tv = tv / (1 + ke) ** years
    steps += [("PV of dividends years 1..n", pv), ("terminal value", tv), ("PV of terminal value", pv_tv)]
    return Result(pv + pv_tv, steps)


def cagr(first: float, last: float, years: int) -> float | None:
    if years <= 0 or first <= 0 or last <= 0:
        return None
    return (last / first) ** (1 / years) - 1
