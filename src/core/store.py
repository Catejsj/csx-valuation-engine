"""SQLite store for issuers, filings, facts, FX rates and prices.

Amounts are stored as exact decimal text. Converted values are never stored.
Restatements: the same period can appear in several filings; latest_fact() picks the
most recently published filing, and older values stay queryable.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .schema import Fact, Filing, FxRate, Issuer, Price

SCHEMA_VERSION = 7

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS issuers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    exchange TEXT NOT NULL,
    instrument_type TEXT NOT NULL CHECK (instrument_type IN ('equity','bond')),
    sector TEXT NOT NULL DEFAULT '',
    reporting_currency TEXT CHECK (reporting_currency IN ('USD','KHR'))
);

CREATE TABLE IF NOT EXISTS filings (
    id TEXT PRIMARY KEY,
    issuer_id TEXT NOT NULL REFERENCES issuers(id),
    kind TEXT NOT NULL CHECK (kind IN ('annual','semiannual','quarterly','other')),
    source_url TEXT NOT NULL,
    pdf_sha256 TEXT NOT NULL,
    published_on TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    retrieved_at TEXT,
    origin TEXT NOT NULL DEFAULT 'official' CHECK (origin IN ('official','upload')),
    included INTEGER NOT NULL DEFAULT 1 CHECK (included IN (0, 1))
);

CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    issuer_id TEXT NOT NULL REFERENCES issuers(id),
    filing_id TEXT NOT NULL REFERENCES filings(id),
    statement TEXT NOT NULL CHECK (statement IN ('BS','IS','CF','EQ','NOTES')),
    period_end TEXT NOT NULL,
    period_type TEXT NOT NULL CHECK (period_type IN ('FY','H1','Q1','Q2','Q3','Q4','9M','12M')),
    line_item TEXT NOT NULL,
    label_as_printed TEXT NOT NULL,
    label_km TEXT NOT NULL DEFAULT '',
    value_as_reported TEXT NOT NULL,
    currency TEXT NOT NULL CHECK (currency IN ('USD','KHR')),
    scale INTEGER NOT NULL CHECK (scale IN (1, 1000, 1000000)),
    page INTEGER NOT NULL CHECK (page >= 1),
    method TEXT NOT NULL CHECK (method IN ('table','text','manual')),
    check_status TEXT NOT NULL CHECK (check_status IN ('proposed','confirmed','failed')),
    confirmed_by TEXT NOT NULL DEFAULT '',
    confirmed_at TEXT,
    UNIQUE (filing_id, statement, period_end, period_type, line_item)
);
CREATE INDEX IF NOT EXISTS facts_lookup ON facts (issuer_id, line_item, period_end);

-- Every review action, so a corrected value never loses what the code first read.
CREATE TABLE IF NOT EXISTS fact_events (
    id INTEGER PRIMARY KEY,
    fact_id INTEGER NOT NULL REFERENCES facts(id),
    at TEXT NOT NULL,
    by TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('confirm','correct','fail','add')),
    old_value TEXT,
    new_value TEXT,
    note TEXT NOT NULL DEFAULT ''
);

-- Retired (2026-11-19): AI-written report text. Kept so older databases open unchanged; nothing reads it.
CREATE TABLE IF NOT EXISTS narratives (
    filing_id TEXT NOT NULL REFERENCES filings(id),
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (filing_id, model, prompt_version)
);

-- Reports found on a public source: link, date, hash. One row per attached file.
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    board TEXT NOT NULL,
    symbol TEXT NOT NULL,
    remote_id INTEGER NOT NULL,
    file_name TEXT NOT NULL DEFAULT '',
    original_name TEXT NOT NULL DEFAULT '',
    lang TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    published_on TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('annual','semiannual','quarterly','other')),
    fiscal_year INTEGER,
    period TEXT NOT NULL DEFAULT '',
    page_url TEXT NOT NULL,
    file_url TEXT NOT NULL DEFAULT '',
    local_path TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL DEFAULT '',
    bytes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    filing_id TEXT REFERENCES filings(id),
    read_status TEXT NOT NULL DEFAULT '',
    UNIQUE (source, board, symbol, remote_id, file_name)
);

CREATE TABLE IF NOT EXISTS fx_rates (
    on_date TEXT NOT NULL,
    base TEXT NOT NULL,
    quote TEXT NOT NULL,
    rate TEXT NOT NULL,
    source_url TEXT NOT NULL,
    PRIMARY KEY (on_date, base, quote)
);

CREATE TABLE IF NOT EXISTS prices (
    issuer_id TEXT NOT NULL REFERENCES issuers(id),
    on_date TEXT NOT NULL,
    close TEXT NOT NULL,
    volume INTEGER NOT NULL CHECK (volume >= 0),
    currency TEXT NOT NULL,
    source_url TEXT NOT NULL,
    market_cap TEXT,
    market_cap_unit TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (issuer_id, on_date)
);
"""


# Which report is "newer" when two print the same figure differently: the one covering the
# later period, then the later publication date. (An upload's publication date is the day
# it was uploaded, so upload order alone must not decide.) Use with the filings alias `g`.
REPORT_PERIOD = "(SELECT MAX(x.period_end) FROM facts x WHERE x.filing_id = g.id)"
NEWEST_LAST = f"{REPORT_PERIOD}, g.published_on"
# The same rule as a one-pass join for queries over a whole company (about 5x faster than the
# per-row subquery on large companies). Its one parameter, the company id, comes before the
# WHERE parameters; order with NEWEST_ORDER.
NEWEST_JOIN = ("JOIN (SELECT filing_id AS rp_id, MAX(period_end) AS rp FROM facts WHERE issuer_id = ? "
               "GROUP BY filing_id) rp ON rp.rp_id = g.id")
NEWEST_ORDER = "rp.rp, g.published_on"


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)   # wait for another process (reader + downloader) instead of failing
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        # WAL: readers and the writer never block each other (the app stays usable while
        # reports are downloaded or read). Kept in the file; back up with sqlite3 ".backup", not a plain file copy.
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            pass   # another connection holds the file; the next start switches it
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring databases made by older versions up to date (add-only changes)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(filings)")}
    if cols and "origin" not in cols:
        conn.execute("ALTER TABLE filings ADD COLUMN origin TEXT NOT NULL DEFAULT 'official'")
    if cols and "included" not in cols:
        conn.execute("ALTER TABLE filings ADD COLUMN included INTEGER NOT NULL DEFAULT 1")
    pcols = {r[1] for r in conn.execute("PRAGMA table_info(prices)")}
    if pcols and "market_cap" not in pcols:
        conn.execute("ALTER TABLE prices ADD COLUMN market_cap TEXT")
        conn.execute("ALTER TABLE prices ADD COLUMN market_cap_unit TEXT NOT NULL DEFAULT ''")
    facts_sql = (conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='facts'").fetchone()
                 or [""])[0]
    if facts_sql and "'9M'" not in facts_sql:
        # v7: year-to-date period types. SQLite cannot change a CHECK in place, so the
        # table is rebuilt with every row and id kept (fact_events points at fact ids).
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN")
            conn.execute(facts_sql.replace("CREATE TABLE facts", "CREATE TABLE facts_v7", 1).replace(
                "('FY','H1','Q1','Q2','Q3','Q4')", "('FY','H1','Q1','Q2','Q3','Q4','9M','12M')"))
            conn.execute("INSERT INTO facts_v7 SELECT * FROM facts")
            conn.execute("DROP TABLE facts")
            conn.execute("ALTER TABLE facts_v7 RENAME TO facts")
            conn.execute("CREATE INDEX IF NOT EXISTS facts_lookup ON facts (issuer_id, line_item, period_end)")
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise sqlite3.IntegrityError("facts rebuild broke a reference")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    dcols = {r[1] for r in conn.execute("PRAGMA table_info(documents)")}
    if dcols and "read_status" not in dcols:
        conn.execute("ALTER TABLE documents ADD COLUMN read_status TEXT NOT NULL DEFAULT ''")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    _migrate(conn)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None or row[0] != str(SCHEMA_VERSION):    # write only when it changes
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
    conn.commit()


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def add_issuer(conn: sqlite3.Connection, issuer: Issuer) -> None:
    conn.execute(
        "INSERT INTO issuers VALUES (?,?,?,?,?,?)",
        (
            issuer.id, issuer.name, issuer.exchange, issuer.instrument_type.value, issuer.sector,
            issuer.reporting_currency.value if issuer.reporting_currency else None,
        ),
    )
    conn.commit()


def add_filing(conn: sqlite3.Connection, filing: Filing) -> None:
    conn.execute(
        "INSERT INTO filings (id, issuer_id, kind, source_url, pdf_sha256, published_on, title, "
        "retrieved_at, origin) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            filing.id, filing.issuer_id, filing.kind.value, filing.source_url, filing.pdf_sha256,
            filing.published_on.isoformat(), filing.title, _iso(filing.retrieved_at), filing.origin.value,
        ),
    )
    conn.commit()


def add_fact(conn: sqlite3.Connection, fact: Fact) -> int:
    """Insert one fact; returns its row id. Raises sqlite3.IntegrityError on a duplicate."""
    cur = conn.execute(
        """INSERT INTO facts (issuer_id, filing_id, statement, period_end, period_type, line_item,
               label_as_printed, label_km, value_as_reported, currency, scale, page, method,
               check_status, confirmed_by, confirmed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            fact.issuer_id, fact.filing_id, fact.statement.value, fact.period_end.isoformat(),
            fact.period_type.value, fact.line_item, fact.label_as_printed, fact.label_km,
            str(fact.value_as_reported), fact.currency.value, int(fact.scale), fact.page,
            fact.method.value, fact.check_status.value, fact.confirmed_by, _iso(fact.confirmed_at),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def add_fx_rate(conn: sqlite3.Connection, fx: FxRate) -> None:
    conn.execute(
        "INSERT INTO fx_rates VALUES (?,?,?,?,?)",
        (fx.on.isoformat(), fx.base.value, fx.quote.value, str(fx.rate), fx.source_url),
    )
    conn.commit()


def add_price(conn: sqlite3.Connection, price: Price) -> None:
    conn.execute(
        "INSERT INTO prices (issuer_id, on_date, close, volume, currency, source_url) VALUES (?,?,?,?,?,?)",
        (price.issuer_id, price.on.isoformat(), str(price.close), price.volume,
         price.currency.value, price.source_url),
    )
    conn.commit()


def _row_to_fact(row: sqlite3.Row) -> Fact:
    return Fact(
        issuer_id=row["issuer_id"], filing_id=row["filing_id"], statement=row["statement"],
        period_end=date.fromisoformat(row["period_end"]), period_type=row["period_type"],
        line_item=row["line_item"], label_as_printed=row["label_as_printed"],
        value_as_reported=Decimal(row["value_as_reported"]), currency=row["currency"],
        scale=row["scale"], page=row["page"], method=row["method"],
        check_status=row["check_status"], label_km=row["label_km"],
        confirmed_by=row["confirmed_by"],
        confirmed_at=datetime.fromisoformat(row["confirmed_at"]) if row["confirmed_at"] else None,
    )


def facts_for(conn: sqlite3.Connection, issuer_id: str, line_item: str | None = None) -> list[Fact]:
    """All facts for an issuer (every filing, including restated ones), oldest period first."""
    sql = "SELECT * FROM facts WHERE issuer_id = ?"
    args: list[object] = [issuer_id]
    if line_item is not None:
        sql += " AND line_item = ?"
        args.append(line_item)
    sql += " ORDER BY period_end, line_item, id"
    return [_row_to_fact(r) for r in conn.execute(sql, args)]


def latest_fact(
    conn: sqlite3.Connection, issuer_id: str, line_item: str, period_end: date,
    period_type: str = "FY", statement: str | None = None,
) -> Fact | None:
    """The value from the most recently published filing for that period. None if not found.

    Failed facts are skipped. Ties on published_on are broken by the later-inserted row.
    """
    sql = """SELECT f.* FROM facts f JOIN filings g ON g.id = f.filing_id
             WHERE f.issuer_id = ? AND f.line_item = ? AND f.period_end = ?
               AND f.period_type = ? AND f.check_status != 'failed'"""
    args: list[object] = [issuer_id, line_item, period_end.isoformat(), period_type]
    if statement is not None:
        sql += " AND f.statement = ?"
        args.append(statement)
    sql += f" ORDER BY {REPORT_PERIOD} DESC, g.published_on DESC, f.id DESC LIMIT 1"
    row = conn.execute(sql, args).fetchone()
    return _row_to_fact(row) if row else None


def facts_with_ids(conn: sqlite3.Connection, filing_id: str) -> list[tuple[int, Fact]]:
    """All facts of one filing with their row ids, in statement and printed order."""
    rows = conn.execute(
        "SELECT * FROM facts WHERE filing_id = ? ORDER BY statement, period_end DESC, id", (filing_id,))
    return [(int(r["id"]), _row_to_fact(r)) for r in rows]


def fact_by_id(conn: sqlite3.Connection, fact_id: int) -> Fact | None:
    row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
    return _row_to_fact(row) if row else None


def _event(conn, fact_id, by, action, old=None, new=None, note="") -> None:
    conn.execute(
        "INSERT INTO fact_events (fact_id, at, by, action, old_value, new_value, note) VALUES (?,?,?,?,?,?,?)",
        (fact_id, datetime.now().replace(microsecond=0).isoformat(), by, action, old, new, note))


def _need_reviewer(by: str) -> str:
    by = (by or "").strip()
    if not by:
        raise ValueError("reviewer name required")
    return by


class Unchanged(ValueError):
    """A correction that equals the current value: nothing is written."""


def original_reading(conn: sqlite3.Connection, fact_id: int) -> str | None:
    """The value as the reader first took it from the page, if it was later corrected."""
    row = conn.execute("SELECT old_value FROM fact_events WHERE fact_id=? AND action='correct' "
                       "AND old_value IS NOT NULL ORDER BY id LIMIT 1", (fact_id,)).fetchone()
    return row["old_value"] if row else None


def restore_original(conn: sqlite3.Connection, fact_id: int, by: str) -> None:
    """Undo every fix: put back the value read from the page, marked 'not checked yet'."""
    by = _need_reviewer(by)
    fact = fact_by_id(conn, fact_id)
    if fact is None:
        raise KeyError(fact_id)
    original = original_reading(conn, fact_id)
    if original is None:
        raise Unchanged("this value was never changed")
    if str(fact.value_as_reported) == original:
        raise Unchanged("already the original reading")
    conn.execute("UPDATE facts SET value_as_reported=?, method='text', check_status='proposed', confirmed_by='', "
                 "confirmed_at=NULL WHERE id=?", (original, fact_id))
    _event(conn, fact_id, by, "correct", str(fact.value_as_reported), original, "restored the original reading")
    conn.commit()


def confirm_fact(conn: sqlite3.Connection, fact_id: int, by: str) -> None:
    by = _need_reviewer(by)
    if fact_by_id(conn, fact_id) is None:
        raise KeyError(fact_id)
    now = datetime.now().replace(microsecond=0).isoformat()
    conn.execute("UPDATE facts SET check_status='confirmed', confirmed_by=?, confirmed_at=? WHERE id=?",
                 (by, now, fact_id))
    _event(conn, fact_id, by, "confirm")
    conn.commit()


def fail_fact(conn: sqlite3.Connection, fact_id: int, by: str, note: str = "") -> None:
    by = _need_reviewer(by)
    if fact_by_id(conn, fact_id) is None:
        raise KeyError(fact_id)
    conn.execute("UPDATE facts SET check_status='failed', confirmed_by='', confirmed_at=NULL WHERE id=?",
                 (fact_id,))
    _event(conn, fact_id, by, "fail", note=note)
    conn.commit()


def correct_fact(conn: sqlite3.Connection, fact_id: int, new_value: object, by: str, note: str = "") -> None:
    """Replace a misread value with what the page shows. The old value stays in fact_events."""
    from .schema import to_decimal
    by = _need_reviewer(by)
    old = fact_by_id(conn, fact_id)
    if old is None:
        raise KeyError(fact_id)
    value = to_decimal(new_value, "correction")
    if value == old.value_as_reported:
        raise Unchanged("same as the stored value; nothing changed")
    now = datetime.now().replace(microsecond=0).isoformat()
    conn.execute("UPDATE facts SET value_as_reported=?, method='manual', check_status='confirmed', "
                 "confirmed_by=?, confirmed_at=? WHERE id=?", (str(value), by, now, fact_id))
    _event(conn, fact_id, by, "correct", str(old.value_as_reported), str(value), note)
    conn.commit()


def add_manual_fact(conn: sqlite3.Connection, fact: Fact, by: str) -> int:
    """A value typed in by a reviewer from the page (for rows the extractor could not read)."""
    by = _need_reviewer(by)
    fact_id = add_fact(conn, fact)
    _event(conn, fact_id, by, "add", None, str(fact.value_as_reported))
    conn.commit()
    return fact_id


def events_for(conn: sqlite3.Connection, fact_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT at, by, action, old_value, new_value, note FROM fact_events WHERE fact_id=? ORDER BY id",
        (fact_id,))]


def filing_by_sha(conn: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM filings WHERE pdf_sha256 = ? ORDER BY origin = 'upload'",
                        (sha256,)).fetchone()


PUBLIC_FIELDS = ("issuer_id", "filing_id", "statement", "period_end", "period_type", "line_item",
                 "label_as_printed", "value_as_reported", "currency", "scale", "page", "check_status")


def public_facts(conn: sqlite3.Connection, issuer_id: str | None = None) -> list[dict]:
    """Facts that may leave this computer (snapshot, API).

    Only official filings, only confirmed or proposed values (never failed ones), and never
    the reviewer's name or review times: those stay local.
    """
    sql = (f"SELECT {', '.join('f.' + c for c in PUBLIC_FIELDS)}, g.source_url FROM facts f "
           "JOIN filings g ON g.id = f.filing_id "
           "WHERE g.origin = 'official' AND f.check_status != 'failed'")
    args: list[object] = []
    if issuer_id is not None:
        sql += " AND f.issuer_id = ?"
        args.append(issuer_id)
    return [dict(r) for r in conn.execute(sql + " ORDER BY f.issuer_id, f.period_end, f.id", args)]


# ---- fixing mistakes (uploads only; official filings are not editable by users) ----------------

class NotEditable(ValueError):
    pass


def _upload_filing(conn: sqlite3.Connection, filing_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM filings WHERE id = ?", (filing_id,)).fetchone()
    if row is None:
        raise KeyError(filing_id)
    if row["origin"] != "upload":
        raise NotEditable("official filings cannot be changed; only your own uploads can")
    return row


def update_filing(conn: sqlite3.Connection, filing_id: str, *, kind: str | None = None,
                  title: str | None = None) -> None:
    """Change the report type or title of an upload. Period types of its facts follow the kind."""
    from .schema import FilingKind
    row = _upload_filing(conn, filing_id)
    new_kind = FilingKind(kind).value if kind else row["kind"]
    new_title = (title.strip()[:120] if title is not None else row["title"]) or row["title"]
    conn.execute("UPDATE filings SET kind = ?, title = ? WHERE id = ?", (new_kind, new_title, filing_id))
    for fid, period_end in conn.execute("SELECT id, period_end FROM facts WHERE filing_id = ?", (filing_id,)).fetchall():
        conn.execute("UPDATE facts SET period_type = ? WHERE id = ?",
                     (period_type_for(new_kind, date.fromisoformat(period_end)), fid))
    conn.commit()


def period_type_for(kind: str, period_end: date) -> str:
    """FY for annual reports, H1 for half-year, Q1..Q4 from the period-end month for quarterly."""
    if kind == "semiannual":
        return "H1"
    if kind == "quarterly":
        return f"Q{(period_end.month - 1) // 3 + 1}"
    return "FY"


def move_filing(conn: sqlite3.Connection, filing_id: str, to_issuer: str, to_name: str = "") -> str:
    """Put an upload under the right company (fixes a wrong company code or mixed-up files).

    Creates the target company if it does not exist. Returns the filing's new id.
    Companies left with no filings are removed.
    """
    import re as _re
    row = _upload_filing(conn, filing_id)
    to_issuer = to_issuer.strip().upper()
    if not _re.fullmatch(r"[A-Z0-9][A-Z0-9_.-]{0,19}", to_issuer):
        raise ValueError("company code: letters and digits only, up to 20 characters")
    old_issuer = row["issuer_id"]
    exists = conn.execute("SELECT name FROM issuers WHERE id = ?", (to_issuer,)).fetchone()
    if exists is None:
        if not to_name.strip():
            raise ValueError("a new company needs a name")
        old = conn.execute("SELECT * FROM issuers WHERE id = ?", (old_issuer,)).fetchone()
        conn.execute("INSERT INTO issuers VALUES (?,?,?,?,?,?)",
                     (to_issuer, to_name.strip()[:120], old["exchange"], old["instrument_type"], old["sector"],
                      old["reporting_currency"]))
    elif to_name.strip() and to_issuer != old_issuer:
        pass  # an existing company keeps its name; rename it separately
    new_id = f"{to_issuer}-UP-{row['pdf_sha256'][:8]}"
    if conn.execute("SELECT 1 FROM filings WHERE id = ? AND id != ?", (new_id, filing_id)).fetchone():
        raise ValueError("that company already has this exact file")
    conn.execute("PRAGMA defer_foreign_keys = ON")
    conn.execute("UPDATE filings SET id = ?, issuer_id = ? WHERE id = ?", (new_id, to_issuer, filing_id))
    conn.execute("UPDATE facts SET filing_id = ?, issuer_id = ? WHERE filing_id = ?", (new_id, to_issuer, filing_id))
    conn.execute("UPDATE narratives SET filing_id = ? WHERE filing_id = ?", (new_id, filing_id))
    _drop_empty_issuer(conn, old_issuer)
    conn.commit()
    return new_id


def rename_company(conn: sqlite3.Connection, issuer_id: str, name: str) -> None:
    """Fix a company name. Only for companies that have no official filings."""
    if conn.execute("SELECT 1 FROM filings WHERE issuer_id = ? AND origin = 'official'", (issuer_id,)).fetchone():
        raise NotEditable("this company comes from the official dataset; its name cannot be changed here")
    if not name.strip():
        raise ValueError("name cannot be empty")
    cur = conn.execute("UPDATE issuers SET name = ? WHERE id = ?", (name.strip()[:120], issuer_id))
    if cur.rowcount == 0:
        raise KeyError(issuer_id)
    conn.commit()


def delete_upload(conn: sqlite3.Connection, filing_id: str) -> str:
    """Remove one of your uploads and everything read from it. Returns the stored PDF's hash."""
    row = _upload_filing(conn, filing_id)
    conn.execute("DELETE FROM fact_events WHERE fact_id IN (SELECT id FROM facts WHERE filing_id = ?)", (filing_id,))
    conn.execute("DELETE FROM facts WHERE filing_id = ?", (filing_id,))
    conn.execute("DELETE FROM narratives WHERE filing_id = ?", (filing_id,))
    conn.execute("DELETE FROM filings WHERE id = ?", (filing_id,))
    _drop_empty_issuer(conn, row["issuer_id"])
    conn.commit()
    return row["pdf_sha256"]


def set_included(conn: sqlite3.Connection, filing_id: str, included: bool) -> None:
    """Switch a filing on or off for the report, ratios, coverage and valuation.

    Nothing is deleted: a switched-off filing keeps its file and values and can be switched
    back on. Works for official filings too, because it only changes this computer's view.
    """
    cur = conn.execute("UPDATE filings SET included = ? WHERE id = ?", (1 if included else 0, filing_id))
    if cur.rowcount == 0:
        raise KeyError(filing_id)
    conn.commit()


STATEMENT_NAMES = {"BS": "Balance sheet", "IS": "Income statement", "CF": "Cash flow", "EQ": "Changes in equity",
                   "NOTES": "Notes"}


def statements_summary(conn: sqlite3.Connection, filing_id: str) -> list[dict]:
    """Which statements were read from a filing, from which pages, for which periods."""
    rows = conn.execute("""SELECT statement, GROUP_CONCAT(DISTINCT page) pages, COUNT(*) n,
                                  GROUP_CONCAT(DISTINCT substr(period_end,1,4)) years,
                                  SUM(check_status='confirmed') confirmed
                           FROM facts WHERE filing_id = ? GROUP BY statement""", (filing_id,)).fetchall()
    order = {"BS": 0, "IS": 1, "CF": 2}
    out = [{"statement": r["statement"], "name": STATEMENT_NAMES.get(r["statement"], r["statement"]),
            "pages": sorted(int(p) for p in r["pages"].split(",")), "values": r["n"],
            "years": sorted(r["years"].split(",")), "confirmed": r["confirmed"] or 0} for r in rows]
    return sorted(out, key=lambda x: order.get(x["statement"], 9))


def remove_statement(conn: sqlite3.Connection, filing_id: str, statement: str) -> int:
    """Remove one statement read from an upload (e.g. a page read as the wrong statement)."""
    _upload_filing(conn, filing_id)
    ids = [r[0] for r in conn.execute("SELECT id FROM facts WHERE filing_id=? AND statement=?", (filing_id, statement))]
    if not ids:
        raise KeyError(f"{filing_id} has no {statement}")
    conn.executemany("DELETE FROM fact_events WHERE fact_id=?", [(i,) for i in ids])
    conn.executemany("DELETE FROM facts WHERE id=?", [(i,) for i in ids])
    conn.commit()
    return len(ids)


def replace_upload_facts(conn: sqlite3.Connection, filing_id: str, facts: list[Fact]) -> tuple[int, int, int]:
    """Re-read an upload after its statement pages were changed, keeping the person's work.

    A checked, fixed or rejected value is carried over to the new reading when the same row
    (statement, period, line) is read again with the same original number; its review history
    moves with it. Rows a person added by hand are kept as they are. Reviews whose row is no
    longer read, or now reads a different number, are dropped (and counted).
    Returns (values stored, reviews kept, reviews dropped).
    """
    _upload_filing(conn, filing_id)
    old = conn.execute("SELECT * FROM facts WHERE filing_id=?", (filing_id,)).fetchall()
    events: dict[int, list] = {}
    for e in conn.execute("SELECT e.* FROM fact_events e JOIN facts f ON f.id = e.fact_id "
                          "WHERE f.filing_id=? ORDER BY e.id", (filing_id,)):
        events.setdefault(e["fact_id"], []).append(e)
    reviewed = {}
    for r in old:
        touched = r["check_status"] != "proposed" or r["method"] == "manual" or events.get(r["id"])
        if touched:
            original = original_reading(conn, r["id"]) or r["value_as_reported"]
            reviewed[(r["statement"], r["period_end"], r["period_type"], r["line_item"])] = (r, original)
    conn.execute("DELETE FROM fact_events WHERE fact_id IN (SELECT id FROM facts WHERE filing_id=?)", (filing_id,))
    conn.execute("DELETE FROM facts WHERE filing_id=?", (filing_id,))
    conn.execute("DELETE FROM narratives WHERE filing_id=?", (filing_id,))
    new_ids = {}
    for fact in facts:
        fid = add_fact(conn, fact)
        new_ids[(fact.statement.value if hasattr(fact.statement, "value") else fact.statement,
                 fact.period_end.isoformat(), getattr(fact.period_type, "value", fact.period_type), fact.line_item)] = \
            (fid, str(fact.value_as_reported))
    kept = dropped = 0
    cols = [c for c in old[0].keys() if c != "id"] if old else []
    for key, (r, original) in reviewed.items():
        target = new_ids.get(key)
        if target is None and r["method"] == "manual":       # a row added by hand: keep it
            cur = conn.execute(f"INSERT INTO facts ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                               [r[c] for c in cols])
            target_id = cur.lastrowid
        elif target is not None and target[1] == str(original):
            target_id = target[0]
            conn.execute("UPDATE facts SET value_as_reported=?, method=?, check_status=?, confirmed_by=?, "
                         "confirmed_at=? WHERE id=?", (r["value_as_reported"], r["method"], r["check_status"],
                                                       r["confirmed_by"], r["confirmed_at"], target_id))
        else:
            dropped += 1
            continue
        for e in events.get(r["id"], []):
            conn.execute("INSERT INTO fact_events (fact_id, at, by, action, old_value, new_value, note) "
                         "VALUES (?,?,?,?,?,?,?)", (target_id, e["at"], e["by"], e["action"], e["old_value"],
                                                    e["new_value"], e["note"]))
        kept += 1
    conn.commit()
    return len(facts), kept, dropped


def _drop_empty_issuer(conn: sqlite3.Connection, issuer_id: str) -> None:
    if not conn.execute("SELECT 1 FROM filings WHERE issuer_id = ?", (issuer_id,)).fetchone():
        conn.execute("DELETE FROM prices WHERE issuer_id = ?", (issuer_id,))
        conn.execute("DELETE FROM issuers WHERE id = ?", (issuer_id,))


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = ("issuers", "filings", "facts", "fact_events", "fx_rates", "prices")
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
