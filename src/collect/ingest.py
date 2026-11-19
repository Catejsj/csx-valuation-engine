"""Read the statements in downloaded reports into the fact table.

Each downloaded document is read with the same rules-only reader as a hand-added
filing. The result is written back on the document row (read_status), so it is clear
what was read, what failed and why.

A filing whose arithmetic checks fail is still stored, but switched off: its numbers
stay out of reports and the API until a person has looked at it in "Check numbers"
and switched it on.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

from src.core import store
from src.core.schema import Filing, Issuer

KIND_CODE = {"annual": "AR", "semiannual": "H1-", "quarterly": "", "other": "DOC-"}


def filing_id_for(doc: sqlite3.Row) -> str:
    year = doc["fiscal_year"] or doc["published_on"][:4]
    if doc["kind"] == "annual":
        base = f"{doc['symbol']}-AR{year}"
    elif doc["kind"] == "quarterly" and doc["period"]:
        base = f"{doc['symbol']}-{doc['period']}-{year}"
    elif doc["kind"] == "semiannual":
        base = f"{doc['symbol']}-H1-{year}"
    else:
        base = f"{doc['symbol']}-DOC{doc['remote_id']}"
    if doc["lang"] and doc["lang"] != "en":
        base += f"-{doc['lang']}"
    return base


def _free_id(conn: sqlite3.Connection, wanted: str) -> str:
    if conn.execute("SELECT 1 FROM filings WHERE id=?", (wanted,)).fetchone() is None:
        return wanted
    n = 2
    while conn.execute("SELECT 1 FROM filings WHERE id=?", (f"{wanted}-v{n}",)).fetchone():
        n += 1
    return f"{wanted}-v{n}"


def _set(conn: sqlite3.Connection, doc_id: int, status: str, filing_id: str | None = None) -> None:
    conn.execute("UPDATE documents SET read_status=?, filing_id=COALESCE(?, filing_id) WHERE id=?",
                 (status[:300], filing_id, doc_id))
    conn.commit()


def read_document(conn: sqlite3.Connection, doc: sqlite3.Row) -> str:
    from src.extract.load import build, save
    from src.extract.statements import extract_statements

    same = conn.execute("SELECT id FROM filings WHERE pdf_sha256=?", (doc["sha256"],)).fetchone()
    if same is not None:
        _set(conn, doc["id"], f"same file as {same['id']} (already read)", same["id"])
        return "same"
    pdf = Path(doc["local_path"])
    if not pdf.is_file():
        _set(conn, doc["id"], "file missing on this computer")
        return "missing"
    try:
        tables = extract_statements(pdf)
    except Exception as e:  # noqa: BLE001 - a broken PDF must not stop the batch
        _set(conn, doc["id"], f"could not open: {e}")
        return "error"
    if not tables:
        _set(conn, doc["id"], "no statements found (scanned pages or an unfamiliar layout)")
        return "none"
    fid = _free_id(conn, filing_id_for(doc))
    report = build(tables, doc["symbol"], fid, doc["kind"])
    if not report.facts:
        _set(conn, doc["id"], "statement pages found but no values read")
        return "none"
    row = conn.execute("SELECT name FROM issuers WHERE id=?", (doc["symbol"],)).fetchone()
    issuer = Issuer(id=doc["symbol"], name=row["name"] if row else doc["symbol"],
                    reporting_currency=tables[0].columns[0].currency)
    filing = Filing(id=fid, issuer_id=doc["symbol"], kind=doc["kind"], source_url=doc["page_url"],
                    pdf_sha256=doc["sha256"], published_on=date.fromisoformat(doc["published_on"]),
                    title=doc["title"], retrieved_at=datetime.now().replace(microsecond=0))
    try:
        n = save(conn, issuer, filing, report)
    except sqlite3.Error as e:   # one bad report must not stop the batch
        conn.rollback()
        conn.execute("DELETE FROM facts WHERE filing_id=?", (fid,))
        conn.execute("DELETE FROM filings WHERE id=?", (fid,))
        _set(conn, doc["id"], f"could not store: {e}")
        return "error"
    if conn.execute("SELECT reporting_currency FROM issuers WHERE id=?", (doc["symbol"],)).fetchone()[0] is None:
        conn.execute("UPDATE issuers SET reporting_currency=? WHERE id=?", (tables[0].columns[0].currency.value, doc["symbol"]))
    failed = len(report.failed)
    passed = sum(c.status == "pass" for c in report.checks)
    stmts = "/".join(sorted({t.statement.value for t in tables}))
    if failed:
        store.set_included(conn, fid, False)
        _set(conn, doc["id"], f"read {n} values ({stmts}); {failed} check(s) failed, switched off until reviewed", fid)
        return "failed-checks"
    if stmts != "BS/CF/IS":
        store.set_included(conn, fid, False)
        _set(conn, doc["id"], f"read {n} values ({stmts} only); partial, switched off until reviewed", fid)
        return "partial"
    twin = conn.execute(
        "SELECT g.id FROM filings g JOIN documents d ON d.filing_id = g.id WHERE g.issuer_id=? AND g.kind=? "
        "AND d.fiscal_year IS ? AND d.period=? AND g.included=1 AND g.id<>?",
        (doc["symbol"], doc["kind"], doc["fiscal_year"], doc["period"], fid)).fetchone()
    if twin is not None:
        store.set_included(conn, fid, False)
        _set(conn, doc["id"], f"read {n} values; second copy of {twin['id']}, switched off", fid)
        return "second-copy"
    _set(conn, doc["id"], f"read {n} values ({stmts}); {passed} checks pass", fid)
    conn.commit()
    return "ok"


def read_all(conn: sqlite3.Connection, *, kinds=("annual",), only: set[str] | None = None,
             log=print) -> dict[str, int]:
    marks = ",".join("?" * len(kinds))
    rows = conn.execute(
        f"""SELECT * FROM documents WHERE sha256<>'' AND filing_id IS NULL AND kind IN ({marks})
            AND (read_status = '' OR read_status IS NULL) ORDER BY symbol, published_on""", tuple(kinds)).fetchall()
    stats: dict[str, int] = {}
    for doc in rows:
        if only and doc["symbol"] not in only:
            continue
        result = read_document(conn, doc)
        stats[result] = stats.get(result, 0) + 1
        status = conn.execute("SELECT read_status FROM documents WHERE id=?", (doc["id"],)).fetchone()[0]
        log(f"{doc['symbol']:<6} {doc['published_on']} {doc['title'][:50]:<50} {status}")
    return stats
