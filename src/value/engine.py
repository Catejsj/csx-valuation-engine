"""Value lens: route a company to the model that fits its sector and return a value range.

Output (a plain dict, JSON-ready):
- status: "ok", "not enough data",
  "not handled" (no clean model fits), or "not meaningful" (e.g. negative free cash flow).
- value: bear / base / bull equity value in USD (from the sensitivity settings), per share
  when a share count is known, and the same range after the liquidity discount band.
- market: the latest market capitalisation and where it sits against the range (words like
  "below the range", never buy or sell).
- inputs: every assumption used with its source, date and verified flag.
- steps: how the number was built, so it can be rebuilt by hand.
- missing / notes: what the code did not find or assumed.

The price never feeds the value; it is only compared with it (and used for the capital
structure weights of a listed company).
"""
from __future__ import annotations

import sqlite3

from . import inputs as inp
from . import models
from .assumptions import Assumptions, load

DISCLAIMER = ("Educational estimate from public reports and editable assumptions. A range, not a target "
              "price, and not investment advice.")


def _fmt(x: float) -> str:
    return f"{x:,.0f}"


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def _need_years(model: str) -> int:
    from src.core.coverage import load_rules
    key = {"fcff": "value.dcf", "bank": "value.bank_excess_return", "dividend": "value.dividend_discount"}.get(model)
    return int(load_rules().get(key, {}).get("min_years", 1)) if key else 1


LOW_PAYOUT = 0.30   # below this, a dividend model shows only a small part of the value
THIN_FCFE = 0.25    # FCFE below this share of profit: heavy reinvestment, use the regulated-return view


def value(conn: sqlite3.Connection, issuer_id: str, *, official_only: bool = True,
          assumptions: Assumptions | None = None) -> dict:
    a: Assumptions = assumptions if assumptions is not None else load()
    row = conn.execute("SELECT id, name, sector FROM issuers WHERE id=?", (issuer_id,)).fetchone()
    out: dict = {"company": issuer_id, "name": row["name"] if row else issuer_id, "disclaimer": DISCLAIMER,
                 "currency": "USD", "status": "", "model": "", "sector": "", "inputs": [], "steps": [],
                 "missing": [], "notes": [], "value": None, "market": None, "liquidity": None,
                 "cost_of_capital": None, "sensitivity": None}
    sector = a.sectors.get(issuer_id)
    a.used.add("sector_map")
    chosen = (row["sector"] if row is not None else "") or ""
    out["sector_source"] = "config" if sector else ""
    if not sector and chosen in a.sector_models:      # picked in the app (uploads, new listings)
        sector = {"sector": chosen, **a.sector_models[chosen]}
        out["sector_source"] = "app"
    out["sector_choices"] = sorted(a.sector_models)
    if not sector or sector.get("model") in (None, "none"):
        out["status"] = "not handled"
        out["missing"].append("no sector chosen for this company yet, so no model is picked")
        return _finish(out, a)
    model, out["model"], out["sector"] = sector["model"], sector["model"], sector.get("sector", "")
    hist = inp.years(conn, issuer_id, official_only=official_only)
    full = [y for y in hist if y.get("total_assets") is not None and y.get("net_income") is not None
            and y.get("cfo") is not None]
    need = _need_years(model)
    if len(full) < need:
        out["status"] = "not enough data"
        out["missing"].append(f"{need} full years needed for this model, {len(full)} available")
        return _finish(out, a)
    mkt = inp.market(conn, issuer_id)
    tax, rf = a.get("corporate_tax_rate"), a.get("risk_free_rate")
    erp, crp, g = a.get("mature_equity_risk_premium"), a.get("country_risk_premium"), a.get("terminal_growth")
    a.used.update({"industry_betas", "forecast", "sensitivity"})
    ind = a.betas.get(sector.get("industry", ""))
    if ind is None:
        out["status"] = "not enough data"
        out["missing"].append(f"industry '{sector.get('industry')}' has no beta in config/market.toml")
        return _finish(out, a)
    last = full[-1]
    out["base_year"] = last.end
    try:
        _r, _how = _avg_roe(full)
        out["roe_avg_usd"] = _r                     # in dollars, like the cost of equity
        out["roe_avg_years"] = [h[:4] for h in _how]
    except ValueError:
        pass
    if any(y.fx for y in full):
        out["notes"].append("KHR figures converted to USD: " + full[-1].fx)
    try:
        if model == "bank":
            _bank(out, full, a, mkt, ind, rf, erp, crp, g, tax)
        elif model == "dividend":
            _dividend(out, full, a, mkt, ind, rf, erp, crp, g, tax)
        elif model == "property":
            _property(out, full, mkt)
        else:
            _fcff(out, full, a, mkt, ind, rf, erp, crp, g, tax)
    except ValueError as e:
        out["status"] = "not meaningful"
        out["notes"].append(str(e))
    if out["value"] and not out["status"] and out["value"]["base"] <= 0:
        out["status"] = "not meaningful"
        out["notes"].append("Debt is larger than the value of the cash flows, so the equity value comes out "
                            "zero or negative. The range is kept in the steps for study but not shown as a value.")
        out["value"] = None
    if out["value"] and not out["status"]:
        out["status"] = "ok"
        checks = [v for k, v in out["steps"] if k.startswith("cross-check") and v > 0]
        base = out["value"]["base"]
        if any(c > 2 * base or c < base / 2 for c in checks):
            out["notes"].append("The methods disagree by more than 2x (see the cross-checks in the steps): the value "
                                "depends heavily on the method chosen. Treat the range as uncertain.")
        _market_and_liquidity(out, a, mkt)
    return _finish(out, a)


def _finish(out: dict, a: Assumptions) -> dict:
    out["inputs"] = [a.inputs[n].public() for n in sorted(a.used)]
    unchecked = [i.name for i in a.unverified_used()]
    out["unverified_inputs"] = unchecked
    if unchecked and out["status"] == "ok":
        out["notes"].insert(0, "Inputs not checked by a person yet (each lists its source and date): "
                            + ", ".join(unchecked) + ".")
    return out


def _capital(out, last: inp.Year, mkt, ind, rf, erp, crp, tax, a: Assumptions, *, bank: bool = False):
    """Cost of equity (and WACC for non-banks) with each step recorded."""
    debt = 0.0 if bank else (last.sum_of(*inp.DEBT_CODES) or 0.0)
    if bank:
        beta = float(ind.get("levered") or models.relever(ind["unlevered"], tax, ind["de"]))
        how = "industry levered beta (banks: debt is raw material, not financing)"
        de = None
    elif mkt:
        de = debt / mkt.market_cap_usd
        beta = models.relever(ind["unlevered"], tax, de)
        how = f"industry unlevered beta {ind['unlevered']} relevered at this company's D/E {de:.2f} (market value)"
    else:
        de = float(ind["de"])
        beta = models.relever(ind["unlevered"], tax, de)
        how = f"industry unlevered beta {ind['unlevered']} relevered at the industry D/E {de:.2f} (no share price)"
    ke = models.cost_of_equity(rf, beta, erp, crp)
    cc = {"risk_free_rate": rf, "beta": beta, "beta_how": how, "mature_erp": erp,   # exact: rounded only for display
          "country_risk_premium": crp, "cost_of_equity": ke}
    out["steps"].append((f"cost of equity = {_pct(rf)} + {beta:.3f} x {_pct(erp)} + {_pct(crp)}", round(ke, 6)))
    if not bank:
        fin = last.get("finance_costs")
        kd = None
        if fin and debt > 0:
            implied = abs(fin) / debt
            if rf <= implied <= rf + 0.15:
                kd, kd_how = implied, f"finance costs / debt = {_pct(implied)}"
        if kd is None:
            kd = rf + a.get("default_spread")
            kd_how = f"risk-free + country default spread = {_pct(kd)}"
        equity_w = mkt.market_cap_usd if mkt else 1.0
        debt_w = debt if mkt else float(ind["de"])
        w = models.wacc(ke, kd, tax, debt_w, equity_w) if (debt_w + equity_w) > 0 else ke
        cc.update({"debt_usd": debt, "cost_of_debt": kd, "cost_of_debt_how": kd_how,
                   "tax_rate": tax, "wacc": w,
                   "weights": "market value of equity and book debt" if mkt else "industry D/E (no share price)"})
        out["steps"].append((f"WACC with cost of debt {_pct(kd)} ({kd_how}), tax {_pct(tax)}", round(w, 6)))
    out["cost_of_capital"] = cc
    return cc


def _growth(full: list[inp.Year], a: Assumptions) -> tuple[float, str]:
    revs = [(y.end, y.get("revenue")) for y in full if y.get("revenue")]
    lo, hi = float(a.forecast["growth_min"]), float(a.forecast["growth_max"])
    if len(revs) >= 2:
        c = models.cagr(revs[0][1], revs[-1][1], int(revs[-1][0][:4]) - int(revs[0][0][:4]))   # years between, not points
        if c is not None:
            return min(max(c, lo), hi), f"revenue growth {revs[0][0][:4]}-{revs[-1][0][:4]} {_pct(c)}, kept within {_pct(lo)} to {_pct(hi)}"
    return 0.0, "no revenue history; first-stage growth set to 0%"


def _range(fn, base_rate: float, g: float, a: Assumptions) -> dict:
    step, gs = float(a.sensitivity["cost_of_capital_step"]), float(a.sensitivity["growth_step"])
    rates = [base_rate + step, base_rate, base_rate - step]
    growths = [g - gs, g, g + gs]
    grid = [[round(fn(r, gg)) for gg in growths] for r in rates]
    return {"bear": grid[0][0], "base": grid[1][1], "bull": grid[2][2],
            "table": {"rows": [round(r, 4) for r in rates], "cols": [round(x, 4) for x in growths], "values": grid}}


def _fcff(out, full, a, mkt, ind, rf, erp, crp, g, tax):
    last = full[-1]
    cc = _capital(out, last, mkt, ind, rf, erp, crp, tax, a)
    flows = []
    for y in full[-3:]:
        cfo, capex = y.get("cfo"), y.get("capex")
        if capex is None:
            out["missing"].append(f"{y.end[:4]}: no capital expenditure line found; that year is left out")
            continue
        add_back = abs(y.get("interest_paid") or 0.0) * (1 - tax) if y.values.get("_interest_in_cfo") else 0.0
        intang = abs(y.get("purchase_of_intangibles") or 0.0) + abs(y.get("purchase_of_investment_property") or 0.0)
        # licences, spectrum, software and investment property are investment too
        f = cfo - abs(capex) - intang + add_back
        flows.append(f)
        out["steps"].append((f"FCFF {y.end[:4]} = CFO {_fmt(cfo)} - capex {_fmt(abs(capex))}"
                             + (f" - intangibles and investment property bought {_fmt(intang)}" if intang else "")
                             + (f" + after-tax interest paid {_fmt(add_back)}" if add_back else ""), round(f)))
    if not flows:
        raise ValueError("no year with both operating cash flow and capital expenditure")
    base = sum(flows) / len(flows)
    out["steps"].append((f"base FCFF = average of {len(flows)} year(s)", round(base)))
    _swing_note(out, flows, base, "free cash flow")
    if base <= 0:
        raise ValueError(f"average free cash flow is negative ({_fmt(base)} USD); a cash-flow model would give "
                         "no meaningful value. Look at book value or an asset-based view instead.")
    g1, g1_how = _growth(full, a)
    years = int(a.forecast["years"])
    out["steps"].append((f"first-stage growth: {g1_how}", round(g1, 6)))
    debt, cash = cc["debt_usd"], last.sum_of("cash_and_equivalents") or 0.0   # also when printed as a "current" line
    if not last.sum_of("cash_and_equivalents"):
        out["missing"].append("no cash line found; equity value counts no cash")

    def equity(rate, gg):
        return models.fcff_value(base, g1, years, gg, rate).value - debt + cash
    ev = models.fcff_value(base, g1, years, g, cc["wacc"])
    out["steps"] += [(k, round(v)) for k, v in ev.steps]
    out["steps"] += [("enterprise value", round(ev.value)), ("less debt", -round(debt)), ("plus cash", round(cash))]
    if len(flows) >= 2 and flows[-1] > 0:
        latest = models.fcff_value(flows[-1], g1, years, g, cc["wacc"]).value - debt + cash
        out["steps"].append((f"cross-check: same model on the latest year's free cash flow only ({_fmt(flows[-1])})", round(latest)))
    _trend_note(out, flows, "free cash flow")
    out["value"] = _range(equity, cc["wacc"], g, a)


def _trend_note(out, flows, what):
    """An average hides a steady fall: say so when each year is lower than the one before."""
    if len(flows) >= 3 and all(b < a for a, b in zip(flows, flows[1:])):
        out["notes"].append(f"The {what} fell every year ({', '.join(_fmt(f) for f in flows)} USD); the 3-year average "
                            "used as the base is above the latest year, so the value may be too high.")


def _swing_note(out, flows, base, what):
    if len(flows) >= 2 and base and (max(flows) - min(flows)) > 2 * abs(base):
        out["notes"].append(f"The yearly {what} swings a lot ({_fmt(min(flows))} to {_fmt(max(flows))} USD), "
                            "often from working capital; the average used as the base is uncertain.")


def _avg_roe(full) -> tuple[float, list[str]]:
    roes, how = [], []
    for prev, cur in zip(full[-4:-1], full[-3:]):
        if int(cur.end[:4]) - int(prev.end[:4]) != 1:
            continue    # a missing year in between: no average equity for this year
        e0, e1, ni = prev.get("total_equity"), cur.get("total_equity"), cur.get("net_income")
        if e0 and e1 and ni is not None:
            r = ni / ((e0 + e1) / 2)
            roes.append(r)
            how.append(f"{cur.end[:4]} {_pct(r)}")
    if not roes:
        raise ValueError("return on equity needs two years of equity and a year of profit")
    return sum(roes) / len(roes), how


def _payout(full) -> float:
    pays = [abs(y.get("dividends_paid") or 0.0) / y.get("net_income") for y in full[-3:]
            if y.get("net_income") and y.get("net_income") > 0]
    return min(max(sum(pays) / len(pays), 0.0), 1.0) if pays else 0.0


def _bank(out, full, a, mkt, ind, rf, erp, crp, g, tax):
    last = full[-1]
    cc = _capital(out, last, mkt, ind, rf, erp, crp, tax, a, bank=True)
    book = (last.get("total_equity") or 0.0) - (last.get("non_controlling_interests") or 0.0)
    roe, how = _avg_roe(full)
    payout = _payout(full)
    g_book = roe * (1 - payout)
    years = int(a.forecast["years"])
    out["steps"] += [(f"book equity (owners) {last.end[:4]}", round(book)),
                     ("average return on equity: " + ", ".join(how), round(roe, 6)),
                     (f"payout ratio (dividends / profit, average) {_pct(payout)}; book growth = ROE x retention", round(g_book, 6))]
    ke = cc["cost_of_equity"]
    w = float(a.forecast.get("excess_return_persistence", 0.62))
    r = models.residual_income(book, roe, ke, g_book, years, g, persistence=w)
    out["steps"] += [(k if k != "terminal value" else f"terminal value (the excess return fades by {w:.2f} a year)", round(v))
                     for k, v in r.steps]
    forever = models.residual_income(book, roe, ke, g_book, years, g).value
    out["steps"].append((f"cross-check: if the excess return lasted forever, growing at {_pct(g)}", round(forever)))
    pb = models.justified_pb(roe, ke, g)
    out["steps"].append((f"cross-check: justified P/B = (ROE - g) / (ke - g) = {pb:.2f}, x book", round(pb * book)))
    out["value"] = _range(lambda k, gg: models.residual_income(book, roe, k, g_book, years, gg, persistence=w).value, ke, g, a)


def _dividend(out, full, a, mkt, ind, rf, erp, crp, g, tax):
    last = full[-1]
    cc = _capital(out, last, mkt, ind, rf, erp, crp, tax, a)
    d0 = abs(last.get("dividends_paid") or 0.0)
    roe, how = _avg_roe(full)
    payout = _payout(full)
    if d0 <= 0 or payout < LOW_PAYOUT:
        out["steps"].append((f"payout {_pct(payout)} is below {_pct(LOW_PAYOUT)}: free cash flow to equity is used, "
                             "because a dividend model would show only the small part paid out", round(payout, 6)))
        return _fcfe(out, full, a, cc, roe, payout, g, tax, d0)
    sustainable = roe * (1 - payout)
    lo, hi = float(a.forecast["growth_min"]), float(a.forecast["growth_max"])
    g1 = min(max(sustainable, lo), hi)
    years = int(a.forecast["years"])
    out["steps"] += [(f"dividends paid {last.end[:4]}", round(d0)),
                     ("average return on equity: " + ", ".join(how), round(roe, 6)),
                     (f"first-stage growth = ROE x (1 - payout {_pct(payout)}), kept within {_pct(lo)}-{_pct(hi)}", round(g1, 6))]
    ke = cc["cost_of_equity"]
    r = models.dividend_value(d0, g1, years, g, ke)
    out["steps"] += [(k, round(v)) for k, v in r.steps]
    out["notes"].append("A dividend model values only what is paid out; cash kept in the company is not counted.")
    out["value"] = _range(lambda k, gg: models.dividend_value(d0, g1, years, gg, k).value, ke, g, a)
    _regulated_check(out, full, a, cc, roe, payout, g)


def _fcfe(out, full, a, cc, roe, payout, g, tax, d0):
    """Free cash flow to equity: operating cash flow - capex + net borrowing (after interest)."""
    out["model"] = "fcfe"
    flows = []
    for y in full[-3:]:
        cfo, capex = y.get("cfo"), y.get("capex")
        if capex is None:
            out["missing"].append(f"{y.end[:4]}: no capital expenditure line found; that year is left out")
            continue
        # all debt: loans, bonds, and the principal of leases (counted as debt, as in the cash flow model)
        net_borrowing = ((y.get("proceeds_from_borrowings") or 0.0) + (y.get("proceeds_from_debt_securities") or 0.0)
                         - abs(y.get("repayments_of_borrowings") or 0.0) - abs(y.get("repayments_of_debt_securities") or 0.0)
                         - abs(y.get("lease_payments") or 0.0))
        interest = 0.0 if y.values.get("_interest_in_cfo") else abs(y.get("interest_paid") or 0.0)
        intang = abs(y.get("purchase_of_intangibles") or 0.0) + abs(y.get("purchase_of_investment_property") or 0.0)
        f = cfo - abs(capex) - intang + net_borrowing - interest
        flows.append(f)
        out["steps"].append((f"FCFE {y.end[:4]} = CFO {_fmt(cfo)} - capex {_fmt(abs(capex))}"
                             + (f" - intangibles and investment property bought {_fmt(intang)}" if intang else "")
                             + f" + net borrowing {_fmt(net_borrowing)}" + (f" - interest paid {_fmt(interest)}" if interest else ""), round(f)))
    if not flows:
        raise ValueError("no year with both operating cash flow and capital expenditure")
    base = sum(flows) / len(flows)
    out["steps"].append((f"base FCFE = average of {len(flows)} year(s)", round(base)))
    _swing_note(out, flows, base, "free cash flow to equity")
    profits = [y.get("net_income") for y in full[-3:] if (y.get("net_income") or 0) > 0]
    avg_profit = sum(profits) / len(profits) if profits else 0.0
    if base <= 0:
        out["steps"].append(("free cash flow to equity is negative on average (investment is larger than "
                             "operating cash): the regulated-return view is used instead", round(base)))
        return _regulated(out, full, a, cc, roe, payout, g)
    if avg_profit > 0 and base < THIN_FCFE * avg_profit:
        out["steps"].append((f"free cash flow to equity ({_fmt(base)}) is under {THIN_FCFE:.0%} of average profit "
                             f"({_fmt(avg_profit)}): the company is reinvesting almost everything, so valuing only "
                             "that thin cash would understate it; the regulated-return view is used instead", round(base)))
        return _regulated(out, full, a, cc, roe, payout, g)
    lo, hi = float(a.forecast["growth_min"]), float(a.forecast["growth_max"])
    g1 = min(max(roe * (1 - payout), lo), hi)
    years = int(a.forecast["years"])
    ke = cc["cost_of_equity"]
    r = models.dividend_value(base, g1, years, g, ke)       # same two-stage discounting, on FCFE
    out["steps"].append((f"first-stage growth = ROE x retention, kept within {_pct(lo)}-{_pct(hi)}", round(g1, 6)))
    out["steps"] += [(k.replace("dividends", "FCFE"), round(v)) for k, v in r.steps]
    if d0 > 0:
        dv = models.dividend_value(d0, g1, years, g, ke).value
        out["steps"].append((f"cross-check: dividend model on {_fmt(d0)} paid", round(dv)))
    out["value"] = _range(lambda k, gg: models.dividend_value(base, g1, years, gg, k).value, ke, g, a)
    _regulated_check(out, full, a, cc, roe, payout, g)


def _regulated_inputs(full, a, roe, payout):
    last = full[-1]
    book = (last.get("total_equity") or 0.0) - (last.get("non_controlling_interests") or 0.0)
    return book, roe * (1 - payout), int(a.forecast["years"])


def _regulated(out, full, a, cc, roe, payout, g):
    """Utilities: value = book equity + PV of the return earned above (or below) the cost of equity."""
    out["model"] = "regulated return"
    book, g_book, years = _regulated_inputs(full, a, roe, payout)
    if book <= 0:
        raise ValueError("book equity is zero or negative; the regulated-return view is not meaningful")
    ke = cc["cost_of_equity"]
    out["steps"] += [(f"book equity (owners) {full[-1].end[:4]}", round(book)),
                     (f"return on equity {_pct(roe)} vs cost of equity {_pct(ke)}; book growth = ROE x retention", round(g_book, 6))]
    w = float(a.forecast.get("excess_return_persistence", 0.62))
    r = models.residual_income(book, roe, ke, g_book, years, g, persistence=w)
    out["steps"] += [(k if k != "terminal value" else f"terminal value (the excess return fades by {w:.2f} a year)", round(v))
                     for k, v in r.steps]
    out["value"] = _range(lambda k, gg: models.residual_income(book, roe, k, g_book, years, gg, persistence=w).value, ke, g, a)


def _regulated_check(out, full, a, cc, roe, payout, g):
    book, g_book, years = _regulated_inputs(full, a, roe, payout)
    if book > 0:
        v = models.residual_income(book, roe, cc["cost_of_equity"], g_book, years, g,
                                   persistence=float(a.forecast.get("excess_return_persistence", 0.62))).value
        out["steps"].append((f"cross-check (regulated return): book {_fmt(book)} + PV of (ROE - ke) x book", round(v)))


def _property(out, full, mkt):
    last = full[-1]
    book = (last.get("total_equity") or 0.0) - (last.get("non_controlling_interests") or 0.0)
    out["steps"].append((f"book equity (owners) {last.end[:4]}", round(book)))
    out["notes"].append("Property: net asset value from book equity. Fair values of land and projects are in the "
                        "notes, which are not read yet, so this is a book NAV, not a market NAV; no range is invented.")
    if book <= 0:
        raise ValueError("book equity is zero or negative; a net asset value is not meaningful")
    out["value"] = {"bear": round(book), "base": round(book), "bull": round(book), "table": None}


def _market_and_liquidity(out, a: Assumptions, mkt) -> None:
    v = out["value"]
    liq = a.liquidity
    if mkt and out.get("base_year"):
        from datetime import date as _d
        age = (_d.fromisoformat(mkt.as_of) - _d.fromisoformat(out["base_year"])).days / 365.25
        if age > 1.5:
            out["notes"].append(f"The newest full year read is {out['base_year'][:4]}, about {age:.0f} years before the share "
                                f"price date ({mkt.as_of}): the value uses old figures, the market cap today's price.")
    a.used.add("liquidity_discount")
    if mkt:
        if mkt.zero_volume_share >= liq["zero_volume_share_high"]:
            band = "high"
        elif mkt.zero_volume_share >= liq["zero_volume_share_medium"]:
            band = "medium"
        else:
            band = "low"
        position = ("below the range" if mkt.market_cap_usd < min(v["bear"], v["bull"]) else
                    "above the range" if mkt.market_cap_usd > max(v["bear"], v["bull"]) else "within the range")
        out["market"] = {"as_of": mkt.as_of, "market_cap_usd": round(mkt.market_cap_usd), "price_khr": mkt.price_khr,
                         "khr_per_usd": mkt.fx, "shares": round(mkt.shares), "market_cap_vs_range": position}
        out["value"]["per_share_khr"] = {k: round(v[k] / mkt.shares * mkt.fx) for k in ("bear", "base", "bull")}
        measures = {"days": mkt.days, "zero_volume_share": round(mkt.zero_volume_share, 4),
                    "turnover_per_year": round(mkt.turnover, 4),
                    "amihud_per_usd_million": round(mkt.amihud, 6) if mkt.amihud is not None else None}
    else:
        band, measures = "unlisted", {}
        out["notes"].append("No share price: value only, nothing to compare with.")
    lo, hi = liq[band]
    out["liquidity"] = {"band": band, "discount": [lo, hi], "measures": measures,
                        "value_after_discount": {"low": round(v["bear"] * (1 - hi)), "high": round(v["bull"] * (1 - lo))}}
