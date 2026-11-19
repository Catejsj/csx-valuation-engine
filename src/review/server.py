"""Local review screen: the PDF page beside each extracted value; confirm, correct or reject.

Standard library HTTP server, bound to 127.0.0.1 only. Requests whose Host header is not
local are refused (protects against DNS-rebinding pages in the browser).

    python -m src.main review            # opens http://127.0.0.1:8770
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import threading
from datetime import date
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from src.core import checks, paths, store
from src.core.schema import Fact, Method, Statement

STATIC = Path(__file__).with_name("static")
_lock = threading.Lock()          # one writer at a time
_pdf_index: dict[str, Path] = {}
_analysis_lock = threading.Lock()
_pool = None


def _lower_priority() -> None:
    try:
        import os
        os.nice(10)
    except (AttributeError, OSError):   # Windows has no nice(); the work still runs, at normal priority
        pass


def _analyse_job(pdf: str, sha256: str, pages: list[int]) -> list[dict]:
    from src.report.disclosure import analyse_pdf
    return analyse_pdf(Path(pdf), sha256, set(pages))


_in_flight: dict = {}   # sha256 -> future of a queued PDF analysis


def _worker_pool():
    global _pool
    if _pool is None:
        import multiprocessing
        from concurrent.futures import ProcessPoolExecutor
        _pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"),
                                    initializer=_lower_priority)
    return _pool


def _queue_analysis(pdf: Path, sha256: str, pages: set[int]) -> None:
    """Start a PDF analysis in the worker without waiting for it (a request never blocks on it)."""
    fut = _in_flight.get(sha256)
    if fut is None or fut.done():
        _in_flight[sha256] = _worker_pool().submit(_analyse_job, str(pdf), sha256, sorted(pages))


def _analyse_elsewhere(pdf: Path, sha256: str, pages: set[int]) -> list[dict]:
    """Read a PDF in a separate low-priority process.

    PDF analysis is heavy Python work; in the server's own process it would hold the
    interpreter and every click would wait behind it. The worker saves the result to the
    disk cache too, so each file is analysed once.
    """
    return _worker_pool().submit(_analyse_job, str(pdf), sha256, sorted(pages)).result()
_problem_cache: dict[str, list[dict]] = {}


def _conn():
    conn = store.connect(paths.db_path())
    store.init_db(conn)
    return conn


_scanned = False
_lookup = threading.local()


def _lookup_conn():
    """One reusable connection per thread for file lookups (opening one per call is slow)."""
    conn = getattr(_lookup, "conn", None)
    if conn is None:
        conn = store.connect(paths.db_path())
        _lookup.conn = conn
    return conn


def find_pdf(sha256: str) -> Path | None:
    """Locate the local copy of a filing by its hash, without reading every PDF.

    1. uploads are stored as data/uploads/<sha256>.pdf;
    2. downloaded reports are listed in the documents table with their path and hash;
    3. only for a file in neither (e.g. hand-added filings), the PDF folders are hashed
       once, skipping every file the documents table already accounts for.
    """
    hit = _pdf_index.get(sha256)
    if hit is not None and hit.exists():
        return hit
    up = paths.data_dir() / "uploads" / f"{sha256}.pdf"
    if up.is_file():
        _pdf_index[sha256] = up
        return up
    try:
        row = _lookup_conn().execute("SELECT local_path FROM documents WHERE sha256=? AND local_path<>'' LIMIT 1",
                                     (sha256,)).fetchone()
    except Exception:   # noqa: BLE001 - fall back to the scan
        row = None
    if row is not None and Path(row[0]).is_file():
        _pdf_index[sha256] = Path(row[0])
        return _pdf_index[sha256]
    global _scanned
    if not _scanned:
        _scanned = True
        try:
            known = {r[0] for r in _lookup_conn().execute("SELECT local_path FROM documents WHERE local_path<>''")}
        except Exception:   # noqa: BLE001
            known = set()
        indexed = set(_pdf_index.values())
        for folder in ("pdfs", "uploads"):
            for pdf in (paths.data_dir() / folder).rglob("*.pdf"):
                if pdf not in indexed and str(pdf) not in known:
                    _pdf_index[hashlib.sha256(pdf.read_bytes()).hexdigest()] = pdf
    return _pdf_index.get(sha256)


def _fact_json(fid: int, f: Fact) -> dict:
    return {
        "id": fid, "statement": f.statement.value, "period_end": f.period_end.isoformat(),
        "line_item": f.line_item, "label": f.label_as_printed, "value": str(f.value_as_reported),
        "currency": f.currency.value, "scale": int(f.scale), "page": f.page, "method": f.method.value,
        "status": f.check_status.value, "confirmed_by": f.confirmed_by,
    }


def _with_original(conn, fid: int, f: Fact) -> dict:
    out = _fact_json(fid, f)
    out["original"] = store.original_reading(conn, fid)
    return out


def filing_checks(facts: list[tuple[int, Fact]]) -> list[dict]:
    """Re-run the accounting identities on the stored values (so corrections are re-checked)."""
    groups: dict[tuple[Statement, date], dict[str, Decimal]] = {}
    pages: dict[tuple[Statement, date], set[int]] = {}
    for _, f in facts:
        if f.check_status.value == "failed":
            continue
        key = (f.statement, f.period_end)
        groups.setdefault(key, {})[f.line_item] = f.value_as_reported
        pages.setdefault(key, set()).add(f.page)
    # Printed order (row ids follow the page) lets the "rows add up to their total" checks run
    # on stored values too, so a wrong correction to a single row turns a check red.
    ordered: dict[tuple[Statement, date], list[tuple[str, str, str, Decimal, int]]] = {}
    for fid, f in sorted(facts, key=lambda x: x[0]):
        if f.check_status.value == "failed":
            continue
        rows = ordered.setdefault((f.statement, f.period_end), [])
        section = ""
        if f.statement is Statement.CASH_FLOW:
            seen = {r[0] for r in rows}
            section = "financing" if "cfi" in seen else "investing" if "cfo" in seen else "operating"
        rows.append((f.line_item, f.label_as_printed, section, f.value_as_reported, f.page))
    out = []
    for (stmt, period), values in sorted(groups.items(), key=lambda kv: (kv[0][0].value, kv[0][1])):
        key_pages = tuple(sorted(pages[(stmt, period)]))
        for c in checks.identity_checks(stmt, period.isoformat(), values, key_pages):
            out.append(_check_json(c))
        for c in checks.component_checks(stmt, period.isoformat(), ordered.get((stmt, period), [])):
            out.append(_check_json(c))
    for period in {p for (_, p) in groups}:
        bs = groups.get((Statement.BALANCE_SHEET, period))
        cf = groups.get((Statement.CASH_FLOW, period))
        if bs and cf:
            for c in checks.cross_statement_checks(period.isoformat(), bs, cf, ()):
                out.append(_check_json(c))
    return out


def _check_json(c: checks.CheckResult) -> dict:
    return {"name": c.name, "statement": c.statement.value, "column": c.column, "status": c.status,
            "left": None if c.left is None else str(c.left), "right": None if c.right is None else str(c.right),
            "diff": None if c.diff is None else str(c.diff), "pages": list(c.pages)}


def filing_problems(filing_id: str, sha: str, stored: set[tuple[str, int]]) -> list[dict]:
    """Rows the extractor could not read (re-extracted once, then cached)."""
    if sha in _problem_cache:
        return _problem_cache[sha]
    pdf = find_pdf(sha)
    result: list[dict] = []
    if pdf is not None:
        from src.extract.statements import extract_statements
        for table in extract_statements(pdf):
            for row in table.rows:
                if row.problem:
                    result.append({"statement": table.statement.value, "page": row.page,
                                   "label": row.label, "problem": row.problem,
                                   "period_end": table.period_end.isoformat(),
                                   "currency": table.columns[0].currency.value,
                                   "scale": int(table.columns[0].scale)})
    _problem_cache[sha] = result
    return result


def _latest_annual(conn, issuer: str):
    """The newest annual filing of a company that has its PDF here (the AI reads that one)."""
    for row in conn.execute("SELECT * FROM filings WHERE issuer_id=? AND included=1 "
                            "ORDER BY kind != 'annual', published_on DESC", (issuer,)):
        if find_pdf(row["pdf_sha256"]) is not None:
            return row
    return None


_page_cache: "dict[str, tuple[tuple, dict]]" = {}
_PAGE_CACHE_MAX = 64
CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def _fingerprint(conn, issuer: str) -> tuple:
    """Everything a company's page depends on, in a few indexed lookups (about a millisecond).
    Any change (a new value, a review action, a file switched on or off, a sector,
    new prices, an edited settings file) changes it, so a cached page is never stale."""
    row = conn.execute("""SELECT
        (SELECT COUNT(*) || ':' || IFNULL(MAX(id), 0) FROM facts WHERE issuer_id = :i),
        (SELECT IFNULL(MAX(e.id), 0) FROM fact_events e JOIN facts f ON f.id = e.fact_id WHERE f.issuer_id = :i),
        (SELECT group_concat(id || ':' || included || ':' || kind || ':' || IFNULL(title, ''), '|')
           FROM filings WHERE issuer_id = :i),
        (SELECT name || '|' || IFNULL(sector, '') FROM issuers WHERE id = :i),
        (SELECT IFNULL(MAX(on_date), '') FROM prices WHERE issuer_id = :i),
        (SELECT IFNULL(MAX(on_date), '') FROM fx_rates)""", {"i": issuer}).fetchone()
    settings = tuple(sorted((f.name, f.stat().st_mtime_ns) for f in CONFIG_DIR.rglob("*.toml")))
    return tuple(row) + settings


def report_payload(conn, issuer: str) -> dict:
    """A company's page, built once per change and then served from memory."""
    key = _fingerprint(conn, issuer)
    hit = _page_cache.get(issuer)
    if hit is not None and hit[0] == key:
        return hit[1]
    payload = _build_payload(conn, issuer)
    if not payload.get("disclosure_pending"):        # pages still waiting for a PDF check are not kept
        _page_cache[issuer] = (key, payload)
        while len(_page_cache) > _PAGE_CACHE_MAX:
            _page_cache.pop(next(iter(_page_cache)))
    return payload


_xlsx_cache: "dict[str, tuple[tuple, bytes]]" = {}


def workbook_bytes(conn, issuer: str) -> bytes:
    """The Excel workbook, built once per change of the company's data (same fingerprint as the page)."""
    from src.export.workbook import build_workbook
    key = _fingerprint(conn, issuer) + (date.today().isoformat(),)   # the workbook prints its date
    hit = _xlsx_cache.get(issuer)
    if hit is not None and hit[0] == key:
        return hit[1]
    body = build_workbook(conn, issuer).to_bytes()
    _xlsx_cache[issuer] = (key, body)
    while len(_xlsx_cache) > 16:
        _xlsx_cache.pop(next(iter(_xlsx_cache)))
    return body


def _build_payload(conn, issuer: str) -> dict:
    from src.report.build import build_report
    from src.report.overview import overview
    from src.value.engine import value
    report = build_report(conn, issuer)
    source = _latest_annual(conn, issuer)
    report["disclosure"] = disclosure_payload(conn, issuer, source, compute=False)
    report["disclosure_pending"] = source is not None and report["disclosure"] is None
    report["value"] = value(conn, issuer, official_only=False)
    report["overview"] = overview(report, report["value"])
    return report


def disclosure_status(conn, issuer: str) -> dict:
    """For the page: the checklist if it is ready, else {"pending": true} after queueing the work."""
    source = _latest_annual(conn, issuer)
    if source is None:
        return {"disclosure": None, "pending": False}
    ready = disclosure_payload(conn, issuer, source, compute=False)
    if ready is not None:
        return {"disclosure": ready, "pending": False}
    pdf = find_pdf(source["pdf_sha256"])
    if pdf is None:
        return {"disclosure": None, "pending": False}
    pages = {r[0] for r in conn.execute("SELECT DISTINCT page FROM facts WHERE filing_id=?", (source["id"],))}
    _queue_analysis(pdf, source["pdf_sha256"], pages)
    return {"disclosure": None, "pending": True}


def disclosure_payload(conn, issuer: str, source=None, compute: bool = True) -> dict | None:
    """The disclosure checklist for a company's newest annual report.

    compute=False never opens the PDF: it returns the saved result or None, so the report
    page can open instantly and fetch this part afterwards (GET /api/disclosure/<id>).
    """
    from src.report.disclosure import cached, restated_figures
    source = source if source is not None else _latest_annual(conn, issuer)
    if source is None:
        return None
    stmt_pages = {r[0] for r in conn.execute("SELECT DISTINCT page FROM facts WHERE filing_id=?", (source["id"],))}
    checks = cached(source["pdf_sha256"], stmt_pages)
    if checks is None:
        if not compute:
            return None
        pdf = find_pdf(source["pdf_sha256"])
        if pdf is None:
            return None
        with _analysis_lock:     # one PDF at a time (the warm-up may be on the same file)
            checks = cached(source["pdf_sha256"], stmt_pages) or _analyse_elsewhere(pdf, source["pdf_sha256"], stmt_pages)
    return {"filing_id": source["id"], "title": source["title"], "checks": checks,
            "restated": restated_figures(conn, issuer)}


def relabel(conn, filing_id: str, pages: dict, exact: bool = True) -> dict:
    """Re-read an upload with the statement pages a person chose: {"7": "BS", "10": "CF", "11": "CF"}.

    exact=True: exactly the listed pages are read (a statement left out stays out); the
    person's checked and fixed values are kept wherever the same row is read again."""
    from src.core.schema import Statement
    from src.extract.load import build
    from src.extract.statements import extract_statements
    row = conn.execute("SELECT * FROM filings WHERE id=?", (filing_id,)).fetchone()
    if row is None:
        raise KeyError(filing_id)
    if row["origin"] != "upload":
        raise store.NotEditable("official filings cannot be relabelled; only your own uploads can")
    pdf = find_pdf(row["pdf_sha256"])
    if pdf is None:
        raise KeyError("local PDF for " + filing_id)
    force: dict[int, Statement | None] = {}
    for page, label in pages.items():
        page_no = int(page)
        if page_no < 1:
            raise ValueError("page numbers start at 1")
        force[page_no] = None if label in ("none", "", None) else Statement(label)
    tables = extract_statements(pdf, force, only_listed=exact)
    report = build(tables, row["issuer_id"], filing_id, row["kind"])
    n, kept, dropped = store.replace_upload_facts(conn, filing_id, report.facts)
    return {"values": n, "kept_reviews": kept, "dropped_reviews": dropped,
            "statements": store.statements_summary(conn, filing_id),
            "checks_passed": sum(c.status == "pass" for c in report.checks), "checks_failed": len(report.failed),
            "problems": report.problems}


class Handler(BaseHTTPRequestHandler):
    server_version = "csx-review"

    def log_message(self, fmt, *args):   # keep the terminal quiet
        pass

    # --- helpers -------------------------------------------------------------------------
    def _local_only(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            self._send(HTTPStatus.FORBIDDEN, {"error": "local access only"})
            return False
        return True

    def _send(self, status, payload, content_type="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # page images never change for a given report and page; everything else is live data
        self.send_header("Cache-Control", "private, max-age=86400" if content_type == "image/png" else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise ValueError("JSON body required")
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    # --- GET -----------------------------------------------------------------------------
    def do_GET(self):
        if not self._local_only():
            return
        try:
            self._route_get()
        except KeyError as exc:
            self._send(HTTPStatus.NOT_FOUND, {"error": f"not found: {exc}"})
        except Exception as exc:  # report, don't crash the server
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _route_get(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._send(HTTPStatus.OK, (STATIC / "review.html").read_bytes(), "text/html; charset=utf-8")
        m = re.fullmatch(r"/fonts/([a-z0-9-]+\.woff2)", path)
        if m:
            f = STATIC / "fonts" / m.group(1)
            if not f.is_file():
                raise KeyError(path)
            return self._send(HTTPStatus.OK, f.read_bytes(), "font/woff2")
        conn = _conn()
        m = re.fullmatch(r"/api/export/([A-Za-z0-9_.-]+)\.(xlsx|csv|json)", path)
        if m:
            issuer, fmt = m.group(1), m.group(2)
            if conn.execute("SELECT 1 FROM issuers WHERE id=?", (issuer,)).fetchone() is None:
                raise KeyError(issuer)
            if fmt == "xlsx":
                body = workbook_bytes(conn, issuer)
                ctype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            else:   # the standardised statements; on this computer they include your own uploads
                from src.export import standard
                if fmt == "csv":
                    body = standard.to_csv(conn, issuer, official_only=False).encode("utf-8-sig")  # BOM: Excel reads UTF-8
                    ctype = "text/csv; charset=utf-8"
                else:
                    body = json.dumps(standard.to_json(conn, issuer, official_only=False), ensure_ascii=False,
                                      indent=1).encode("utf-8")
                    ctype = "application/json; charset=utf-8"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Disposition", f'attachment; filename="{issuer}-{date.today().isoformat()}.{fmt}"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return None
        m = re.fullmatch(r"/api/report/([^/]+)", path)
        if m:
            return self._send(HTTPStatus.OK, report_payload(conn, m.group(1)))
        m = re.fullmatch(r"/api/disclosure/([^/]+)", path)
        if m:
            return self._send(HTTPStatus.OK, disclosure_status(conn, m.group(1)))
        if path == "/api/update":
            from src import update
            st = update.read_status()
            st["running"] = update.running(st)
            return self._send(HTTPStatus.OK, st)
        if path == "/api/filings":
            rows = conn.execute("""
                SELECT g.id, g.issuer_id, i.name, g.title, g.published_on, g.origin, g.kind, g.included,
                       COUNT(f.id) AS n,
                       SUM(f.check_status='confirmed') AS confirmed,
                       SUM(f.check_status='failed') AS failed
                FROM filings g JOIN issuers i ON i.id = g.issuer_id
                LEFT JOIN facts f ON f.filing_id = g.id
                GROUP BY g.id ORDER BY g.issuer_id, g.published_on DESC""").fetchall()
            out = []
            for r in rows:
                item = dict(r)
                item["statements"] = store.statements_summary(conn, r["id"])
                out.append(item)
            return self._send(HTTPStatus.OK, out)
        m = re.fullmatch(r"/api/filing/([^/]+)", path)
        if m:
            filing = conn.execute("SELECT * FROM filings WHERE id=?", (m.group(1),)).fetchone()
            if filing is None:
                raise KeyError(m.group(1))
            facts = store.facts_with_ids(conn, filing["id"])
            return self._send(HTTPStatus.OK, {
                "filing": dict(filing), "facts": [_with_original(conn, i, f) for i, f in facts],
                "checks": filing_checks(facts), "has_pdf": find_pdf(filing["pdf_sha256"]) is not None})
        m = re.fullmatch(r"/api/filing/([^/]+)/problems", path)
        if m:
            filing = conn.execute("SELECT * FROM filings WHERE id=?", (m.group(1),)).fetchone()
            if filing is None:
                raise KeyError(m.group(1))
            return self._send(HTTPStatus.OK, filing_problems(filing["id"], filing["pdf_sha256"], set()))
        m = re.fullmatch(r"/api/page/([^/]+)/(\d+)\.png", path)
        if m:
            return self._send(HTTPStatus.OK, self._render(conn, m.group(1), int(m.group(2))), "image/png")
        m = re.fullmatch(r"/api/locate/(\d+)", path)
        if m:
            return self._send(HTTPStatus.OK, self._locate(conn, int(m.group(1))))
        m = re.fullmatch(r"/api/coverage/([^/]+)", path)
        if m:
            from src.core.coverage import full_years, readiness
            issuer = m.group(1)
            return self._send(HTTPStatus.OK, {
                "issuer": issuer, "years": full_years(conn, issuer),
                "official_years": full_years(conn, issuer, official_only=True),
                "calculations": [r.__dict__ for r in readiness(conn, issuer)]})
        m = re.fullmatch(r"/api/filing/([^/]+)/statements", path)
        if m:
            if conn.execute("SELECT 1 FROM filings WHERE id=?", (m.group(1),)).fetchone() is None:
                raise KeyError(m.group(1))
            return self._send(HTTPStatus.OK, store.statements_summary(conn, m.group(1)))
        m = re.fullmatch(r"/api/fact/(\d+)/history", path)
        if m:
            return self._send(HTTPStatus.OK, store.events_for(conn, int(m.group(1))))
        raise KeyError(path)

    def _pdf_for_filing(self, conn, filing_id: str) -> Path:
        filing = conn.execute("SELECT pdf_sha256 FROM filings WHERE id=?", (filing_id,)).fetchone()
        if filing is None:
            raise KeyError(filing_id)
        pdf = find_pdf(filing["pdf_sha256"])
        if pdf is None:
            raise KeyError("local PDF for " + filing_id)
        return pdf

    def _render(self, conn, filing_id: str, page_no: int) -> bytes:
        """A page image, drawn once per file and page and then read from data/cache/pages."""
        row = conn.execute("SELECT pdf_sha256 FROM filings WHERE id=?", (filing_id,)).fetchone()
        cached_png = paths.data_dir() / "cache" / "pages" / f"{row[0]}-{page_no}.png" if row else None
        if cached_png is not None and cached_png.is_file():
            return cached_png.read_bytes()
        import pypdfium2 as pdfium
        pdf_path = self._pdf_for_filing(conn, filing_id)
        doc = pdfium.PdfDocument(str(pdf_path))
        try:
            if not 1 <= page_no <= len(doc):
                raise KeyError(f"page {page_no}")
            image = doc[page_no - 1].render(scale=2).to_pil()
            buf = io.BytesIO()
            image.save(buf, format="PNG", optimize=True)
            data = buf.getvalue()
        finally:
            doc.close()
        if cached_png is not None:
            try:
                cached_png.parent.mkdir(parents=True, exist_ok=True)
                cached_png.write_bytes(data)
            except OSError:
                pass
        return data

    def _locate(self, conn, fact_id: int) -> dict:
        from .locate import locate
        row = conn.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
        if row is None:
            raise KeyError(fact_id)
        periods = [r[0] for r in conn.execute(
            "SELECT DISTINCT period_end FROM facts WHERE filing_id=? AND statement=? ORDER BY period_end DESC",
            (row["filing_id"], row["statement"]))]
        pdf = self._pdf_for_filing(conn, row["filing_id"])
        rank = periods.index(row["period_end"])
        return locate(pdf, row["page"], row["label_as_printed"], Decimal(row["value_as_reported"]),
                      prefer_left=rank == 0, period_rank=rank, n_periods=len(periods))

    # --- POST ----------------------------------------------------------------------------
    def _upload(self) -> None:
        """POST /api/upload?issuer=ID&name=NAME&kind=annual with the raw PDF as the body."""
        import tempfile
        from urllib.parse import parse_qs, urlparse
        from src.intake.upload import MAX_BYTES, ingest
        if self.headers.get("Content-Type", "").split(";")[0] != "application/pdf":
            return self._send(HTTPStatus.BAD_REQUEST, {"error": "send the file as application/pdf"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BYTES:
            return self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE if length else HTTPStatus.BAD_REQUEST,
                              {"error": f"file must be between 1 byte and {MAX_BYTES // (1024 * 1024)} MB"})
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        issuer, name = q.get("issuer", "").strip().upper(), q.get("name", "").strip()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9_.-]{0,19}", issuer) or not name:
            return self._send(HTTPStatus.BAD_REQUEST, {"error": "company code (letters/digits) and name are required"})
        data = self.rfile.read(length)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "upload.pdf"
            path.write_bytes(data)
            with _lock:
                r = ingest(path, issuer, name, q.get("kind", "annual"), conn=_conn(),
                           display_name=q.get("filename", ""))
        self._send(HTTPStatus.OK if r.accepted else HTTPStatus.UNPROCESSABLE_ENTITY, r.__dict__)

    def do_POST(self):
        if not self._local_only():
            return
        if self.path.split("?")[0] == "/api/upload":
            try:
                return self._upload()
            except Exception as exc:
                return self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
        try:
            body = self._body()
            with _lock:
                result = self._route_post(body)
            self._send(HTTPStatus.OK, result)
        except KeyError as exc:
            self._send(HTTPStatus.NOT_FOUND, {"error": f"not found: {exc}"})
        except (ValueError, TypeError) as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _route_post(self, body: dict) -> dict:
        conn = _conn()
        path = self.path.split("?")[0]
        by = body.get("by", "")
        m = re.fullmatch(r"/api/fact/(\d+)/(confirm|correct|fail|restore)", path)
        if m:
            fid, action = int(m.group(1)), m.group(2)
            if action == "restore":
                store.restore_original(conn, fid, by)
            elif action == "confirm":
                store.confirm_fact(conn, fid, by)
            elif action == "fail":
                store.fail_fact(conn, fid, by, body.get("note", ""))
            else:
                store.correct_fact(conn, fid, body.get("value"), by, body.get("note", ""))
            fact = store.fact_by_id(conn, fid)
            return {"fact": _with_original(conn, fid, fact)}
        m = re.fullmatch(r"/api/company/([^/]+)/sector", path)
        if m:   # the sector picks the valuation model for companies not in config/sectors.toml
            from src.value.assumptions import load
            sector = str(body.get("sector", ""))
            if sector and sector not in load().sector_models:
                raise ValueError(f"unknown sector: {sector}")
            if conn.execute("UPDATE issuers SET sector=? WHERE id=?", (sector, m.group(1))).rowcount == 0:
                raise KeyError(m.group(1))
            conn.commit()
            from src.value.engine import value
            return value(conn, m.group(1), official_only=False)
        m = re.fullmatch(r"/api/filing/([^/]+)/edit", path)
        if m:
            filing_id = m.group(1)
            row = conn.execute("SELECT issuer_id FROM filings WHERE id=?", (filing_id,)).fetchone()
            if row is None:
                raise KeyError(filing_id)
            if body.get("kind") or body.get("title") is not None:
                store.update_filing(conn, filing_id, kind=body.get("kind"), title=body.get("title"))
            code = str(body.get("company_code", "")).strip().upper()
            name = str(body.get("company_name", "")).strip()
            if code and code != row["issuer_id"]:
                filing_id = store.move_filing(conn, filing_id, code, name)
            elif name:
                current = conn.execute("SELECT name FROM issuers WHERE id=?", (row["issuer_id"],)).fetchone()
                if current and current["name"] != name:
                    store.rename_company(conn, row["issuer_id"], name)
            return {"filing_id": filing_id}
        m = re.fullmatch(r"/api/filing/([^/]+)/relabel", path)
        if m:
            return relabel(conn, m.group(1), body.get("pages", {}), exact=body.get("exact", True) is not False)
        m = re.fullmatch(r"/api/filing/([^/]+)/remove-statement", path)
        if m:
            n = store.remove_statement(conn, m.group(1), str(body.get("statement", "")))
            return {"removed": n, "statements": store.statements_summary(conn, m.group(1))}
        m = re.fullmatch(r"/api/filing/([^/]+)/include", path)
        if m:
            store.set_included(conn, m.group(1), bool(body.get("included")))
            return {"filing_id": m.group(1), "included": bool(body.get("included"))}
        m = re.fullmatch(r"/api/filing/([^/]+)/delete", path)
        if m:
            sha = store.delete_upload(conn, m.group(1))
            if not conn.execute("SELECT 1 FROM filings WHERE pdf_sha256=?", (sha,)).fetchone():
                stored = paths.data_dir() / "uploads" / f"{sha}.pdf"
                if stored.is_file():
                    stored.unlink()
                _pdf_index.pop(sha, None)
            return {"deleted": m.group(1)}
        m = re.fullmatch(r"/api/filing/([^/]+)/confirm-page", path)
        if m:
            filing_id, page = m.group(1), int(body.get("page", 0))
            facts = store.facts_with_ids(conn, filing_id)
            failing = [c for c in filing_checks(facts) if c["status"] == "fail" and page in c["pages"]]
            if failing:
                raise ValueError("a check fails on this page; fix or reject values one by one first")
            ids = [i for i, f in facts if f.page == page and f.check_status.value == "proposed"]
            for fid in ids:
                store.confirm_fact(conn, fid, by)
            return {"confirmed": len(ids)}
        m = re.fullmatch(r"/api/filing/([^/]+)/add", path)
        if m:
            filing = conn.execute("SELECT * FROM filings WHERE id=?", (m.group(1),)).fetchone()
            if filing is None:
                raise KeyError(m.group(1))
            from src.extract.labels import slug
            label = str(body.get("label", "")).strip()
            fact = Fact(
                issuer_id=filing["issuer_id"], filing_id=filing["id"], statement=body.get("statement"),
                period_end=date.fromisoformat(body.get("period_end", "")), period_type="FY",
                line_item=body.get("line_item") or slug(label), label_as_printed=label,
                value_as_reported=str(body.get("value", "")), currency=body.get("currency"),
                scale=int(body.get("scale", 1)), page=int(body.get("page", 0)), method=Method.MANUAL)
            fid = store.add_manual_fact(conn, fact, by)
            store.confirm_fact(conn, fid, by)
            return {"fact": _fact_json(fid, store.fact_by_id(conn, fid))}
        raise KeyError(path)


def _warm_up() -> None:
    """Prepare each company's disclosure checklist in the background (only the report the page
    shows: the newest annual), saved to disk so it is done once per file, not on every start."""
    try:
        conn = _conn()
        issuers = [r[0] for r in conn.execute("SELECT DISTINCT issuer_id FROM filings WHERE included = 1").fetchall()]
        for issuer in issuers:
            disclosure_payload(conn, issuer)
    except Exception:   # warm-up is only an optimisation; a failure shows up on the real request
        pass
    try:   # daily prices and the exchange rate, at most every 12 hours; offline is fine
        from src.collect.market import refresh_if_stale
        refresh_if_stale(store.connect(paths.db_path()))
    except Exception:
        pass


def serve(port: int = 8770, open_browser: bool = True) -> None:
    url = f"http://127.0.0.1:{port}/"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:   # most often: the app is already open in another window
        print(f"the app is already running (or another program uses port {port}); opening {url}")
        if open_browser:
            import webbrowser
            webbrowser.open(url)
        return
    threading.Thread(target=_warm_up, daemon=True).start()
    print(f"the app is open at {url}\nkeep this window open while you use it; close it (or press Ctrl+C) to stop", flush=True)
    if open_browser:
        import webbrowser
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
