"""What the public API returns. Read-only, official filings only.

Every function here returns plain JSON-ready data and is used both by the live API
server and by the static export (a folder of JSON files that any free static host
can serve). Nothing here ever returns uploads, reviewer names, review times or
local file paths. Amounts are exact decimal strings, never floats.
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal

API_VERSION = "v1"
LICENCE = {
    "code": "PolyForm Noncommercial 1.0.0 (see NOTICE for student, teaching and research use)",
    "data": "Numbers read from public company reports. Free for non-commercial use; commercial use "
            "needs permission. Always cite the original report (each value carries its source link and page).",
}
SOURCES = [
    {"name": "Cambodia Securities Exchange (CSX)", "link_text": "The Cambodia Securities Exchange Home Page",
     "url": "https://csx.com.kh",
     "note": "Report listings and documents are published by CSX and the listed companies. "
             "This project stores links, file hashes and numbers read from the reports, not the reports themselves. "
             "Corrections or removal on request."},
]

STATEMENT_NAMES = {"BS": "balance sheet", "IS": "income statement", "CF": "cash flow statement",
                   "EQ": "changes in equity", "NOTES": "notes"}


class NotFound(KeyError):
    pass


CSX_HOME = "https://csx.com.kh"


def public_link(url: str, symbol: str = "", title: str = "", published: str = "") -> dict:
    """How a source is cited publicly.

    The exchange's terms allow links only as text pointing at its home page, so reports
    found there are cited as the home page plus where to find the report on it. Other
    sources keep their direct link.
    """
    if url.startswith(CSX_HOME):
        where = "Company Information > Annual / Financial Reports"
        if symbol:
            where += f" > {symbol}"
        if title:
            where += f" > {title}" + (f" ({published})" if published else "")
        return {"link_text": "The Cambodia Securities Exchange Home Page", "url": CSX_HOME, "where": where}
    return {"link_text": title or url, "url": url, "where": ""}


def index() -> dict:
    return {
        "api": "Cambodian listed-company data", "version": API_VERSION, "licence": LICENCE,
        "sources": SOURCES,
        "endpoints": {
            "companies": "/v1/companies",
            "company": "/v1/companies/{id}",
            "facts": "/v1/companies/{id}/facts?statement=BS&period_type=FY&line_item=total_assets&all_versions=0",
            "series": "/v1/companies/{id}/series/{line_item}",
            "report": "/v1/companies/{id}/report",
            "documents": "/v1/documents?symbol=CGSM&kind=annual&year=2025",
            "line_items": "/v1/line-items",
            "workbook": "/v1/companies/{id}/workbook.xlsx",
            "statements": "/v1/companies/{id}/statements.json  (standardised, one row per value; also statements.csv)",
            "standard_items": "/v1/standard-items",
            "valuation": "/v1/companies/{id}/valuation  (a range, with every input and its source)",
            "statements_schema": "/v1/schema/statements.json",
            "prices": "/v1/companies/{id}/prices?from=2025-01-01",
            "fx": "/v1/fx?from=2025-01-01",
        },
        "notes": [
            "Amounts are strings with exact decimals; multiply value by scale for the full number.",
            "check_status 'confirmed' = a person compared it with the page; 'proposed' = read by code, sums checked, not yet compared by a person.",
            "When a later report reprints a figure differently (a restatement), the newest published value is returned; all_versions=1 returns every version.",
        ],
    }


def _issuer(conn: sqlite3.Connection, issuer_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM issuers WHERE id = ?", (issuer_id,)).fetchone()
    if row is None:
        raise NotFound(issuer_id)
    return row


def _official_filings(conn: sqlite3.Connection, issuer_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, kind, title, published_on, source_url, pdf_sha256 FROM filings "
        "WHERE issuer_id = ? AND origin = 'official' AND included = 1 ORDER BY published_on DESC",
        (issuer_id,))]


def _documents(conn: sqlite3.Connection, where: str = "", args: tuple = ()) -> list[dict]:
    rows = conn.execute(
        "SELECT symbol, board, title, published_on, kind, fiscal_year, period, page_url, "
        "sha256, bytes, filing_id FROM documents WHERE sha256 <> '' " + where +
        " ORDER BY symbol, published_on DESC, remote_id DESC", args)
    out = []
    for r in rows:
        d = dict(r)
        d["read_into_data"] = d.pop("filing_id") is not None
        d["source"] = public_link(d.pop("page_url"), d["symbol"], d["title"], d["published_on"])
        out.append(d)
    return out


def companies(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM issuers ORDER BY instrument_type, id"):
        years = conn.execute(
            """SELECT DISTINCT substr(f.period_end,1,4) y FROM facts f JOIN filings g ON g.id=f.filing_id
               WHERE f.issuer_id=? AND g.origin='official' AND g.included=1 AND f.check_status!='failed'
                 AND f.period_type='FY' ORDER BY y""", (r["id"],)).fetchall()
        docs = conn.execute("SELECT COUNT(*) FROM documents WHERE symbol=? AND sha256<>''", (r["id"],)).fetchone()[0]
        if not years and not docs and not _official_filings(conn, r["id"]):
            continue   # upload-only companies never appear
        out.append({"id": r["id"], "name": r["name"], "exchange": r["exchange"],
                    "instrument": r["instrument_type"], "sector": r["sector"],
                    "currency": r["reporting_currency"], "years_with_data": [int(y[0]) for y in years],
                    "documents": docs})
    return out


def company(conn: sqlite3.Connection, issuer_id: str) -> dict:
    r = _issuer(conn, issuer_id)
    match = [c for c in companies(conn) if c["id"] == issuer_id]
    if not match:
        raise NotFound(issuer_id)
    out = match[0]
    out["filings_read"] = [{"id": f["id"], "kind": f["kind"], "title": f["title"],
                            "published_on": f["published_on"], "pdf_sha256": f["pdf_sha256"],
                            "source": public_link(f["source_url"], issuer_id, f["title"], f["published_on"])}
                           for f in _official_filings(conn, issuer_id)]
    out["documents"] = _documents(conn, "AND symbol = ?", (r["id"],))
    return out


def facts(conn: sqlite3.Connection, issuer_id: str, *, statement: str | None = None,
          period_type: str | None = None, line_item: str | None = None,
          all_versions: bool = False) -> list[dict]:
    _issuer(conn, issuer_id)
    from src.core.store import NEWEST_JOIN, NEWEST_ORDER
    sql = [f"""SELECT f.statement, f.period_end, f.period_type, f.line_item, f.label_as_printed,
                     f.value_as_reported, f.currency, f.scale, f.page, f.check_status,
                     g.id AS filing_id, g.title AS filing_title, g.published_on, g.source_url, g.pdf_sha256
              FROM facts f JOIN filings g ON g.id = f.filing_id {NEWEST_JOIN}
              WHERE f.issuer_id = ? AND g.origin = 'official' AND g.included = 1
                AND f.check_status != 'failed'"""]
    args: list[object] = [issuer_id, issuer_id]
    for col, val in (("f.statement", statement), ("f.period_type", period_type), ("f.line_item", line_item)):
        if val:
            sql.append(f"AND {col} = ?")
            args.append(val)
    sql.append(f"ORDER BY f.period_end, f.statement, {NEWEST_ORDER}, f.id")
    rows = [dict(r) for r in conn.execute(" ".join(sql), args)]
    for r in rows:
        link = public_link(r.pop("source_url"), issuer_id, r.pop("filing_title"), r["published_on"])
        r["source"] = {"filing_id": r.pop("filing_id"), "published_on": r.pop("published_on"),
                       "url": link["url"], "where": link["where"],
                       "pdf_sha256": r.pop("pdf_sha256"), "page": r.pop("page")}
    if all_versions:
        return rows
    latest: dict[tuple, dict] = {}
    for r in rows:    # ordered by publication date, so newer filings overwrite older ones
        latest[(r["statement"], r["period_end"], r["period_type"], r["line_item"])] = r
    return list(latest.values())


def series(conn: sqlite3.Connection, issuer_id: str, line_item: str) -> dict:
    points = [f for f in facts(conn, issuer_id, line_item=line_item, period_type="FY")]
    if not points:
        raise NotFound(line_item)
    return {"company": issuer_id, "line_item": line_item,
            "points": [{"period_end": p["period_end"], "statement": p["statement"],
                        "value": str(Decimal(p["value_as_reported"]) * int(p["scale"])),
                        "currency": p["currency"], "check_status": p["check_status"],
                        "source": p["source"]} for p in points]}


def report(conn: sqlite3.Connection, issuer_id: str) -> dict:
    from src.report.build import build_report
    company(conn, issuer_id)   # 404 for unknown / upload-only companies
    rep = build_report(conn, issuer_id, official_only=True)
    rep.pop("uses_uploads", None)
    rep["filings"] = [{"id": f["id"], "kind": f["kind"], "title": f["title"], "published_on": f["published_on"],
                       "source": public_link(f["source_url"], issuer_id, f["title"], f["published_on"])}
                      for f in rep["filings"]]
    return rep


def documents(conn: sqlite3.Connection, *, symbol: str | None = None, kind: str | None = None,
              year: int | None = None) -> list[dict]:
    where, args = [], []
    if symbol:
        where.append("AND symbol = ?"); args.append(symbol)
    if kind:
        where.append("AND kind = ?"); args.append(kind)
    if year:
        where.append("AND fiscal_year = ?"); args.append(year)
    return _documents(conn, " ".join(where), tuple(args))


def line_items(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT f.statement, f.line_item, COUNT(DISTINCT f.issuer_id) companies, MIN(f.label_as_printed) example
           FROM facts f JOIN filings g ON g.id = f.filing_id
           WHERE g.origin = 'official' AND f.check_status != 'failed'
           GROUP BY f.statement, f.line_item ORDER BY f.statement, f.line_item""")
    return [{"statement": r["statement"], "statement_name": STATEMENT_NAMES.get(r["statement"], ""),
             "line_item": r["line_item"], "companies": r["companies"], "example_label": r["example"]}
            for r in rows]


def prices(conn: sqlite3.Connection, issuer_id: str, since: str | None = None) -> dict:
    company(conn, issuer_id)
    rows = conn.execute("SELECT on_date, close, volume, currency, market_cap, market_cap_unit FROM prices "
                        "WHERE issuer_id=? AND on_date>=? ORDER BY on_date", (issuer_id, since or "0000")).fetchall()
    return {"company": issuer_id, "source": public_link(CSX_HOME), "note": "daily closing prices as published",
            "prices": [dict(r) for r in rows]}


def fx(conn: sqlite3.Connection, since: str | None = None) -> dict:
    rows = conn.execute("SELECT on_date, rate FROM fx_rates WHERE base='USD' AND quote='KHR' AND on_date>=? "
                        "ORDER BY on_date", (since or "0000",)).fetchall()
    return {"pair": "KHR per USD", "source": public_link(CSX_HOME), "rates": [dict(r) for r in rows]}
