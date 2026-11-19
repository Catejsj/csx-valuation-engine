"""The overview: what the statements say, in plain sentences, computed by code.

Every sentence is built from the report's own numbers (src.report.build) and the valuation
(src.value.engine). Nothing is guessed: a topic without the numbers it needs says what is missing.
Words describe direction ("grew", "fell") and name common rules of thumb as such; they never
recommend buying or selling.
"""
from __future__ import annotations

from decimal import Decimal

from src.report.build import fmt_money


MODEL_NAME = {"bank": "bank model (residual income)", "fcff": "cash flow model (DCF)",
              "dividend": "dividend model", "fcfe": "cash flow to equity model",
              "regulated return": "regulated return model", "property": "book value model"}


def _series(report: dict, key: str) -> dict[int, Decimal]:
    for m in report["lines"] + report["ratios"]:
        if m["key"] == key:
            return {int(y): Decimal(v) for y, v in m["values"].items() if v is not None}
    return {}


def _pct(x) -> str:
    return f"{Decimal(x) * 100:.1f}%"


def _x(x) -> str:
    return f"{Decimal(x):.1f}x"


def _span(s: dict[int, Decimal]) -> tuple[int, int]:
    return min(s), max(s)


def _growth(report: dict, money) -> dict | None:
    rev, ni = _series(report, "revenue"), _series(report, "net_income")
    oi = _series(report, "operating_income")
    if len(oi) >= 2:
        rev = oi
    s, what = ((rev, "Operating income" if rev is oi else "Revenue") if len(rev) >= 2 else (ni, "Profit"))
    if len(s) < 2:
        return None
    first, last = _span(s)
    a, b = s[first], s[last]
    points = []
    if a > 0 and b > 0 and last > first:
        rate = (b / a) ** (Decimal(1) / (last - first)) - 1
        head = f"{what} {'grew' if b > a else 'fell'} from {money(a)} in FY{first} to {money(b)} in FY{last}" + (
            f", {_pct(abs(rate))} a year on average." if last - first > 1 else f" ({'+' if b > a else '−'}{_pct(abs(rate))}).")
        tone = "up" if b > a else "down"
    else:
        head = f"{what} went from {money(a)} in FY{first} to {money(b)} in FY{last}."
        tone = "flat"
    prev = s.get(last - 1)
    if prev is not None and prev != 0 and last - first > 1:
        ch = b / prev - 1
        points.append(f"Last year: {'+' if ch >= 0 else '−'}{_pct(abs(ch))} against FY{last - 1}.")
    ys = sorted(s)
    ups = sum(1 for p, q in zip(ys, ys[1:]) if q == p + 1 and s[q] > s[p])
    pairs = sum(1 for p, q in zip(ys, ys[1:]) if q == p + 1)
    if pairs >= 3:
        points.append(f"It rose in {ups} of {pairs} years.")
    if what == "Profit":
        points.append("Revenue is not read as one line from these reports, so profit is used.")
    if what == "Operating income":
        points.append("A bank's revenue: net interest income plus fees and other income.")
    return {"key": "growth", "title": "Growth", "headline": head, "points": points, "tone": tone,
            "why": "Growth is what the valuation's forecast starts from: a long, steady record makes the forecast "
                   "less of a guess than one or two good years."}


def _profit(report: dict) -> dict | None:
    om, nm, roe = _series(report, "operating_margin"), _series(report, "net_margin"), _series(report, "roe")
    if not (om or nm or roe or _series(report, "cost_to_income")):
        return None
    points, head, tone = [], None, "flat"
    for s, name in ((om, "Operating margin"), (nm, "Net margin")):
        if len(s) >= 2:
            first, last = _span(s)
            d = (s[last] - s[first]) * 100
            line = (f"{name} {_pct(s[last])} in FY{last}, "
                    f"{'up' if d > 0 else 'down'} {abs(d):.1f} points from {_pct(s[first])} in FY{first}.")
            if head is None:
                head, tone = line, ("up" if d > 0 else "down" if d < 0 else "flat")
            else:
                points.append(line)
        elif s:
            y = max(s)
            line = f"{name} {_pct(s[y])} in FY{y}."
            if head is None:
                head = line
            else:
                points.append(line)
    ci, cor = _series(report, "cost_to_income"), _series(report, "cost_of_risk")
    if len(ci) >= 2:
        first, last = _span(ci)
        d = (ci[last] - ci[first]) * 100
        line = (f"Cost to income {_pct(ci[last])} in FY{last} ({_pct(ci[first])} in FY{first}): the share of "
                "income spent on running the bank.")
        if head is None:
            head, tone = line, ("up" if d < 0 else "down" if d > 0 else "flat")
        else:
            points.append(line)
    if cor:
        y = max(cor)
        points.append(f"Loan losses took {_pct(cor[y])} of loans in FY{y} (cost of risk)"
                      + (f"; {_pct(sum(cor.values()) / len(cor))} on average." if len(cor) > 1 else "."))
    if roe:
        y = max(roe)
        avg = sum(roe.values()) / len(roe)
        line = (f"Return on equity {_pct(roe[y])} in FY{y}"
                + (f" ({_pct(avg)} on average over {len(roe)} years)." if len(roe) > 1 else "."))
        if head is None:
            head = line
        else:
            points.append(line)
    return {"key": "profit", "title": "Profitability", "headline": head, "points": points, "tone": tone,
            "why": "Margins show how much of each sale is kept; return on equity shows what the business earns on "
                   "the shareholders' money, which is compared with the cost of equity below."}


def _cash(report: dict, money, bank: bool) -> dict | None:
    if bank:
        return {"key": "cash", "title": "Cash", "tone": "flat",
                "headline": "For a bank, cash from operations moves with deposits and loans, so it is not compared "
                            "with profit here.",
                "points": ["The bank model values it from book equity and return on equity instead."],
                "why": "A bank's deposits are its raw material, not its cash earnings."}
    cfo, ni, fcf = _series(report, "cfo"), _series(report, "net_income"), _series(report, "fcf")
    both = sorted(set(cfo) & set(ni))
    if not both and not fcf:
        return None
    points, head, tone = [], None, "flat"
    if both:
        c, p = sum(cfo[y] for y in both), sum(ni[y] for y in both)
        if p > 0:
            r = c / p
            head = (f"Over {len(both)} year{'s' if len(both) > 1 else ''}, cash from operations was {_x(r)} profit "
                    f"({money(c)} against {money(p)}).")
            tone = "up" if r >= 1 else "watch"
            points.append("Above 1x means the reported profit came in as cash; well below 1x means it is tied up "
                          "in unpaid bills, stock or other items.")
        else:
            head = f"Profit over {len(both)} years adds up to {money(p)}; cash from operations to {money(c)}."
    if fcf:
        pos = sum(1 for v in fcf.values() if v > 0)
        total = sum(fcf.values())
        line = (f"Free cash flow (after capital spending) was positive in {pos} of {len(fcf)} years, "
                f"{money(total)} in total.")
        if head is None:
            head, tone = line, ("up" if pos == len(fcf) else "watch" if pos * 2 < len(fcf) else "flat")
        else:
            points.insert(0, line)
            if pos * 2 < len(fcf):
                tone = "watch"
    return {"key": "cash", "title": "Cash", "headline": head, "points": points, "tone": tone,
            "why": "Valuation models discount cash, not profit: free cash flow is what could be paid to the "
                   "owners and lenders."}


def _debt(report: dict, money, bank: bool) -> dict | None:
    nd, de = _series(report, "net_debt"), _series(report, "debt_to_equity")
    nde, ic, cr = _series(report, "net_debt_to_ebitda"), _series(report, "interest_cover"), _series(report, "current_ratio")
    if not (nd or de or nde or ic or cr or _series(report, "equity_to_assets")):
        return None
    points, head, tone = [], None, "flat"
    if nd:
        y = max(nd)
        head = (f"Net debt {money(nd[y])} in FY{y}" if nd[y] > 0
                else f"More cash than borrowings in FY{y} (net cash {money(-nd[y])})") + "."
    if de:
        y = max(de)
        line = f"Borrowings and bonds are {_x(de[y])} equity (FY{y})."
        if head is None:
            head = line
        else:
            points.append(line)
    if nde and not bank:
        y = max(nde)
        points.append(f"Net debt is {_x(nde[y])} EBITDA (FY{y}). A common rule of thumb reads above 3x as heavily borrowed.")
        if nde[y] > 3:
            tone = "watch"
    if ic and not bank:
        y = max(ic)
        points.append(f"Operating profit covers finance costs {_x(ic[y])} (FY{y}). A common rule of thumb reads "
                      "below 2x as little room if profit dips.")
        if ic[y] < 2:
            tone = "watch"
    if cr and not bank:
        y = max(cr)
        points.append(f"Current assets are {_x(cr[y])} the bills due within a year (FY{y}).")
    if bank:
        ea, ld = _series(report, "equity_to_assets"), _series(report, "loans_to_deposits")
        if ea:
            y = max(ea)
            points.append(f"Equity is {_pct(ea[y])} of assets (FY{y}): the owners' cushion against losses.")
        if ld:
            y = max(ld)
            points.append(f"Loans are {_x(ld[y])} customer deposits (FY{y}); above 1x means part of the lending is "
                          "funded by borrowing rather than deposits.")
        points.append("For a bank, deposits are not counted as debt here.")
    if head is None:
        head = points.pop(0)
    return {"key": "debt", "title": "Debt and safety", "headline": head, "points": points, "tone": tone,
            "why": "Debt raises the return to owners in good years and the risk in bad ones; lenders are paid "
                   "first."}


def _meaning(report: dict, val: dict | None) -> dict | None:
    if not val:
        return None
    cc = val.get("cost_of_capital") or {}
    coe = cc.get("cost_of_equity")
    roe = _series(report, "roe")
    eq = _series(report, "total_equity")
    ni = _series(report, "net_income")
    mk = val.get("market") or {}
    points, head, tone = [], None, "flat"
    if coe and roe:
        ys = sorted(roe)[-3:]
        avg = sum(roe[y] for y in ys) / len(ys)
        gap = (avg - Decimal(str(coe))) * 100
        head = (f"It earned {_pct(avg)} on equity (average of FY{ys[0]}–FY{ys[-1]}) against a "
                f"{_pct(coe)} cost of equity, "
                + ("about the same: the owners' money earns roughly what they require, so it is worth about "
                   "its book value." if abs(gap) < Decimal("0.5") else
                   f"{abs(gap):.1f} points above: each dollar the owners leave in the business earns more than "
                   "they require, so it is worth more than its book value."
                   if gap > 0 else
                   f"{abs(gap):.1f} points below: each dollar kept in the business earns less than the owners "
                   "require, so it is worth less than its book value unless returns rise."))
        tone = "flat" if abs(gap) < Decimal("0.5") else "up" if gap > 0 else "watch"
        if report.get("currency") == "KHR":
            points.append("Return on equity is in riel and the cost of equity in dollars; the riel has moved "
                          "little against the dollar, so the comparison holds roughly.")
    rate = mk.get("khr_per_usd")
    to_usd = (lambda v: v / Decimal(str(rate))) if report.get("currency") == "KHR" and rate else (lambda v: v)
    cap = mk.get("market_cap_usd")
    if cap and eq:
        y = max(eq)
        book = to_usd(eq[y])
        if book > 0:
            points.append(f"The market values it at {_x(Decimal(cap) / book)} its book equity "
                          f"({fmt_money(Decimal(cap), 'USD')} against {fmt_money(book, 'USD')}, FY{y}).")
    if cap and ni:
        y = max(ni)
        p = to_usd(ni[y])
        if p > 0:
            points.append(f"That is {Decimal(cap) / p:.1f} times last year's profit (price to earnings).")
    v = val.get("value") or {}
    if val.get("status") == "ok" and v.get("base") is not None:
        rng = f"{fmt_money(Decimal(v['bear']), 'USD')}–{fmt_money(Decimal(v['bull']), 'USD')}"
        where = mk.get("market_cap_vs_range")
        line = f"The {MODEL_NAME.get(val.get('model'), val.get('model'))} puts the equity at {rng}" + (
            f"; the market cap is {where}." if where else " (it has no share price to compare).")
        if where == "above the range":
            line += " The price assumes more growth, higher returns or less risk than the model does."
        elif where == "below the range":
            line += " The price assumes less growth, lower returns or more risk than the model does."
        points.append(line)
    elif val.get("status") and val.get("status") != "ok":
        points.append({"not handled": "No model is chosen yet: pick a sector in the Value section.",
                       "not enough data": "The model needs more years of statements than are loaded.",
                       "not meaningful": "The model gives no meaningful value; the Value section says why."}
                      .get(val["status"], "The Value section explains the result."))
    if head is None:
        if not points:
            return None
        head = points.pop(0)
    return {"key": "meaning", "title": "What it means for the value", "headline": head, "points": points,
            "tone": tone,
            "why": "This is the point of the numbers above: a company is worth more than the money put into it "
                   "only if it earns more on that money than investors could expect elsewhere for the same risk."}


def overview(report: dict, val: dict | None = None) -> dict:
    """Topics in reading order. Each has a headline, points, a tone (up / down / flat / watch) and why it
    matters. `missing` names the topics left out for lack of numbers."""
    money = lambda v: fmt_money(v, report["currency"])  # noqa: E731
    bank = report.get("kind") == "bank" or (bool(val) and val.get("model") == "bank")
    topics, missing = [], []
    for name, t in (("growth", _growth(report, money)), ("profitability", _profit(report)),
                    ("cash", _cash(report, money, bank)), ("debt", _debt(report, money, bank)),
                    ("value", _meaning(report, val))):
        (topics.append(t) if t else missing.append(name))
    return {"topics": topics, "missing": missing}
