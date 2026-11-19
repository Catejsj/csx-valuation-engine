"""Daily share prices and the KHR/USD rate published by the exchange.

Same public requests as the exchange's own price and exchange-rate pages, one company
at a time. Prices are in riel as published. Market capitalisation is stored exactly as
printed; the exchange shows it in millions of riel (checked against price x shares for
one company, see tests), so the unit is recorded with it.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

from . import csx

PRICES = csx.API + "/market-data/stock/prices"
FX = csx.API + "/market-data/stock/exchange-rate"
SOURCE = csx.SITE
MARKET_CAP_UNIT = "million KHR"


def _num(text: str | None) -> str | None:
    if text is None:
        return None
    # Some closing prices carry a marker such as "2930(SQ)" (a quoted rather than traded price).
    t = re.sub(r"\(.*?\)", "", str(text)).replace(",", "").strip()
    return t if re.fullmatch(r"-?\d+(\.\d+)?", t) else None


def _ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def sync_prices(conn: sqlite3.Connection, *, since: date = date(2012, 1, 1), full: bool = False,
                log=print) -> int:
    added = 0
    today = date.today()
    for c in csx.companies():
        sym, isin = (c.get("symbolEn") or "").strip(), (c.get("icode") or "").strip()
        if not sym or not isin:
            continue
        csx.ensure_issuers(conn, "stock", [sym])
        start = since
        if c.get("date"):
            start = max(since, datetime.strptime(c["date"], "%d-%b-%Y").date())
        last = conn.execute("SELECT MAX(on_date) FROM prices WHERE issuer_id=?", (sym,)).fetchone()[0]
        if last and not full:   # only what is new (re-read the last week in case of late corrections)
            start = max(start, date.fromisoformat(last) - timedelta(days=7))
        rows = []
        for board in ("main", "growth"):
            body = {"board": board, "symbol": isin, "fromDate": _ymd(start), "toDate": _ymd(today),
                    "tradingMethod": "all"}
            try:
                res = csx._json(PRICES + "?lang=en", body)
            except csx.urllib.error.HTTPError as e:
                if e.code == 400:
                    continue
                raise
            rows = (res.get("data") or {}).get("historicalPrice") or []
            if rows:
                break
        n = 0
        for r in rows:
            close, vol = _num(r.get("closingPrice")), _num(r.get("tradingVolume"))
            if close is None or float(close) <= 0:
                continue
            on = datetime.strptime(r["date"], "%d/%m/%Y").date().isoformat()
            cur = conn.execute(
                """INSERT INTO prices (issuer_id, on_date, close, volume, currency, source_url, market_cap, market_cap_unit)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT (issuer_id, on_date) DO UPDATE SET close=excluded.close, volume=excluded.volume,
                       market_cap=excluded.market_cap, market_cap_unit=excluded.market_cap_unit""",
                (sym, on, close, int(vol or 0), "KHR", SOURCE, _num(r.get("marketCap")), MARKET_CAP_UNIT))
            n += cur.rowcount
        conn.execute("UPDATE issuers SET name=? WHERE id=? AND name=id", ((c.get("nameEn") or sym).strip(), sym))
        conn.commit()
        added += n
        log(f"prices {sym}: {len(rows)} days")
    return added


def sync_fx(conn: sqlite3.Connection, *, since: date = date(2012, 1, 1), full: bool = False,
            log=print) -> int:
    last = conn.execute("SELECT MAX(on_date) FROM fx_rates WHERE base='USD' AND quote='KHR'").fetchone()[0]
    if last and not full:
        since = max(since, date.fromisoformat(last) - timedelta(days=7))
    res = csx._json(FX, {"fromDate": _ymd(since), "toDate": _ymd(date.today())})
    rows = (res.get("data") or {}).get("historicalExchangeRate") or []
    for r in rows:
        rate = _num(r.get("exchangeRate"))
        if rate is None:
            continue
        conn.execute("""INSERT INTO fx_rates (on_date, base, quote, rate, source_url) VALUES (?,?,?,?,?)
                        ON CONFLICT (on_date, base, quote) DO UPDATE SET rate=excluded.rate""",
                     (r["date"], "USD", "KHR", rate, SOURCE))
    conn.commit()
    log(f"exchange rate: {len(rows)} days")
    return len(rows)


REFRESH_HOURS = 12


def refresh_if_stale(conn: sqlite3.Connection, log=lambda *_: None) -> str:
    """Bring prices and the exchange rate up to date, at most once every REFRESH_HOURS.

    Called when the app opens. Offline or blocked networks are fine: the app keeps the
    prices it has and says how old they are.
    """
    row = conn.execute("SELECT value FROM meta WHERE key='market_checked_at'").fetchone()
    now = datetime.now(timezone.utc)
    if row is not None and (now - datetime.fromisoformat(row[0])).total_seconds() < REFRESH_HOURS * 3600:
        return "fresh"
    try:
        sync_fx(conn, log=log)
        sync_prices(conn, log=log)
    except (csx.SourceUnavailable, OSError) as e:
        log(f"price refresh skipped: {e}")
        return "offline"
    conn.execute("INSERT INTO meta (key, value) VALUES ('market_checked_at', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now.isoformat(timespec="seconds"),))
    conn.commit()
    return "updated"


def last_price(conn: sqlite3.Connection, issuer_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM prices WHERE issuer_id=? ORDER BY on_date DESC LIMIT 1", (issuer_id,)).fetchone()
    return dict(row) if row else None
