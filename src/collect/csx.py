"""Collect company reports from the exchange's public website.

Uses the same public, unauthenticated requests the website's own report pages make,
slowly (one request every few seconds) and with retries. Each document keeps its
source link, its published date and the SHA-256 of the file, so every number read
from it can be traced back. PDFs stay on this computer; the public dataset only ever
carries links, hashes and numbers.

A timeout is "try again later", never "no new reports": the site is reachable from
some networks and not others.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

SITE = "https://csx.com.kh"
API = SITE + "/api/v1/website"
BOARDS = {
    "stock": API + "/company/stock/annual-reports",
    "bond": API + "/company/bond/corp/annual-reports",
}
FILE_URL = API + "/file/view-attach"
USER_AGENT = "Mozilla/5.0 (research data collector; polite, low rate)"
DELAY_S = 2.0
TIMEOUT_S = 60
MAX_BYTES = 200 * 1024 * 1024


class SourceUnavailable(RuntimeError):
    """The site did not answer. Try again later (possibly from another network)."""


@dataclass
class Document:
    board: str            # stock / bond
    symbol: str
    remote_id: int
    title: str
    published: date
    kind: str             # annual / semiannual / quarterly / other
    fiscal_year: int | None
    period: str           # FY, H1, Q1..Q4, or ''


_last_call = 0.0


def _wait() -> None:
    global _last_call
    gap = time.monotonic() - _last_call
    if gap < DELAY_S:
        time.sleep(DELAY_S - gap)
    _last_call = time.monotonic()


def _request(url: str, data: bytes | None = None, *, tries: int = 3) -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, */*"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    last: Exception | None = None
    for attempt in range(tries):
        _wait()
        try:
            req = urllib.request.Request(url, data=data, headers=headers,
                                         method="POST" if data is not None else "GET")
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                body = resp.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise ValueError(f"response over {MAX_BYTES} bytes: {url}")
                return body
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                raise
            last = e
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = e
        time.sleep(3 * (attempt + 1))
    raise SourceUnavailable(f"{url}: {last}")


def _json(url: str, body: dict | None = None) -> dict:
    raw = _request(url, None if body is None else json.dumps(body).encode())
    out = json.loads(raw)
    if out.get("status") not in (200, None):
        raise SourceUnavailable(f"{url}: status {out.get('status')} {out.get('error', '')}")
    return out


# --- what a title says ------------------------------------------------------

_QUARTER_WORDS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
                  "fourth": 4, "4th": 4}


def classify(title: str) -> tuple[str, int | None, str]:
    """Return (kind, fiscal_year, period) from a report title. Unknown -> ('other', year, '')."""
    t = re.sub(r"\s+", " ", title.replace("​", " ")).strip().lower()
    years = [int(y) for y in re.findall(r"\b(20\d\d)\b", t)]
    year = years[-1] if years else None
    q = re.search(r"\b(first|second|third|fourth|1st|2nd|3rd|4th)\b[^.]*?quarter", t) \
        or re.search(r"quarter(?:ly)?[^.]*?\b(q[1-4])\b", t)
    if "quarter" in t:
        n = None
        if q:
            g = q.group(1)
            n = int(g[1]) if g.startswith("q") else _QUARTER_WORDS[g]
        else:
            m = re.search(r"\bq([1-4])\b", t)
            n = int(m.group(1)) if m else None
        return ("quarterly", year, f"Q{n}" if n else "")
    if re.search(r"semi.?annual|half.?year|six.?month|\bh1\b|first half", t):
        return ("semiannual", year, "H1")
    if "annual" in t:
        return ("annual", year, "FY")
    return ("other", year, "")


def _date(text: str) -> date:
    return datetime.strptime(text.strip(), "%d/%m/%Y").date()


# --- the site's lists ---------------------------------------------------------

def companies() -> list[dict]:
    """Listed companies with their listing date, ISIN and names."""
    return _json(API + "/company/stock/list-companies").get("data") or []


def symbols(board: str) -> list[str]:
    data = _json(BOARDS[board]).get("data") or []
    if board == "stock":
        return [d["symbolEn"].strip() for d in data if d.get("symbolEn")]
    return [d["value"].strip() for d in data if d.get("value")]


def report_index(board: str, symbol: str) -> list[Document]:
    out: list[Document] = []
    page = 1
    while True:
        q = urllib.parse.urlencode({"page": page, "size": 50, "lang": "en"})
        res = _json(f"{BOARDS[board]}/{urllib.parse.quote(symbol)}?{q}", {})
        for d in res.get("data") or []:
            title = (d.get("title") or d.get("titleKh") or "").strip()
            kind, year, period = classify(title)
            out.append(Document(board, symbol, int(d["id"]), title, _date(d["date"]),
                                kind, year, period))
        if page >= int(res.get("totalPages") or 1):
            return out
        page += 1


def attachments(doc: Document) -> list[dict]:
    q = urllib.parse.urlencode({"lang": "en"})
    res = _json(f"{BOARDS[doc.board]}/{urllib.parse.quote(doc.symbol)}/{doc.remote_id}?{q}")
    return (res.get("data") or {}).get("attachFiles") or []


def file_url(att: dict) -> str:
    params = {
        "postId": str(att["postId"]), "fileName": att["fileName"],
        "boardLang": att.get("boardLang") or "", "originalFileName": att.get("originalFileName") or "",
        "boardId": att.get("boardId") or "",
    }
    if att.get("fileOrder") is not None:
        params["fileOrder"] = str(att["fileOrder"])
    return FILE_URL + "?" + urllib.parse.urlencode(params)


def page_url(doc: Document) -> str:
    """The human-readable page for a report on the exchange's website (for credits)."""
    section = "stock" if doc.board == "stock" else "corporate-bond"
    return f"{SITE}/en/company-information/{section}/annual-financial-reports/{doc.symbol}/{doc.remote_id}"


# --- local record --------------------------------------------------------------

def safe_name(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name)
    stem = re.sub(r"-+", "-", re.sub(r"\.{2,}", "-", stem)).strip("-.")
    return stem[:120] or "file"


def record(conn: sqlite3.Connection, doc: Document, att: dict | None, *, local_path: str = "",
           sha256: str = "", size: int = 0, status: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO documents (source, board, symbol, remote_id, file_name, original_name, lang,
               title, published_on, kind, fiscal_year, period, page_url, file_url, local_path,
               sha256, bytes, status, checked_at)
           VALUES ('csx',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT (source, board, symbol, remote_id, file_name) DO UPDATE SET
               title=excluded.title, published_on=excluded.published_on, kind=excluded.kind,
               fiscal_year=excluded.fiscal_year, period=excluded.period,
               local_path=CASE WHEN excluded.local_path<>'' THEN excluded.local_path ELSE local_path END,
               sha256=CASE WHEN excluded.sha256<>'' THEN excluded.sha256 ELSE sha256 END,
               bytes=CASE WHEN excluded.bytes>0 THEN excluded.bytes ELSE bytes END,
               status=excluded.status, checked_at=excluded.checked_at""",
        (doc.board, doc.symbol, doc.remote_id, (att or {}).get("fileName", ""),
         (att or {}).get("originalFileName", ""), (att or {}).get("boardLang", ""),
         doc.title, doc.published.isoformat(), doc.kind, doc.fiscal_year, doc.period,
         page_url(doc), file_url(att) if att else "", local_path, sha256, size, status, now),
    )
    conn.commit()


def known(conn: sqlite3.Connection, doc: Document, att: dict) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM documents WHERE source='csx' AND board=? AND symbol=? AND remote_id=? "
        "AND file_name=? AND sha256<>''",
        (doc.board, doc.symbol, doc.remote_id, att["fileName"]),
    ).fetchone()


def fetch_file(att: dict, dest: Path) -> tuple[str, int]:
    body = _request(file_url(att))
    if not body.startswith(b"%PDF"):
        raise ValueError(f"not a PDF: {att.get('originalFileName')}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(body)
    tmp.replace(dest)
    return hashlib.sha256(body).hexdigest(), len(body)


def ensure_issuers(conn: sqlite3.Connection, board: str, syms: list[str]) -> None:
    """Add companies the database does not know yet (never renames existing ones)."""
    names = {}
    if board == "stock":
        names = {c["symbolEn"].strip(): (c.get("nameEn") or "").strip() for c in companies() if c.get("symbolEn")}
    for sym in syms:
        conn.execute("INSERT OR IGNORE INTO issuers (id, name, exchange, instrument_type) VALUES (?,?,?,?)",
                     (sym, names.get(sym) or sym, "CSX", "equity" if board == "stock" else "bond"))
    conn.commit()


def sync(conn: sqlite3.Connection, pdf_root: Path, *, boards=("stock", "bond"),
         only: set[str] | None = None, kinds: set[str] | None = None,
         download: bool = True, log=print) -> dict[str, int]:
    """Index every report and download the ones not yet on disk. Safe to re-run."""
    stats = {"indexed": 0, "downloaded": 0, "already": 0, "failed": 0, "skipped": 0}
    for board in boards:
        syms = symbols(board)
        ensure_issuers(conn, board, syms)
        for sym in syms:
            if only and sym not in only:
                continue
            docs = report_index(board, sym)
            log(f"{board} {sym}: {len(docs)} reports")
            for doc in docs:
                stats["indexed"] += 1
                if kinds and doc.kind not in kinds:
                    if not conn.execute("SELECT 1 FROM documents WHERE source='csx' AND board=? AND symbol=? "
                                        "AND remote_id=?", (doc.board, doc.symbol, doc.remote_id)).fetchone():
                        record(conn, doc, None, status="indexed")   # placeholder only for reports never seen
                    stats["skipped"] += 1
                    continue
                atts = attachments(doc)
                if not atts:
                    record(conn, doc, None, status="no-file")
                    continue
                for att in atts:
                    if not str(att.get("fileName", "")).lower().endswith(".pdf"):
                        record(conn, doc, att, status="not-pdf")
                        continue
                    row = known(conn, doc, att)
                    if row and Path(row["local_path"]).is_file():
                        stats["already"] += 1
                        continue
                    if not download:
                        record(conn, doc, att, status="indexed")
                        continue
                    name = safe_name(f"csx-{doc.remote_id}-{att.get('originalFileName') or att['fileName']}")
                    dest = pdf_root / sym / name
                    try:
                        sha, size = fetch_file(att, dest)
                    except SourceUnavailable:
                        raise
                    except Exception as e:  # noqa: BLE001 - one bad file must not stop the run
                        record(conn, doc, att, status=f"failed: {e}"[:200])
                        stats["failed"] += 1
                        log(f"  ! {doc.title}: {e}")
                        continue
                    record(conn, doc, att, local_path=str(dest), sha256=sha, size=size, status="downloaded")
                    stats["downloaded"] += 1
                    log(f"  + {doc.published} {doc.kind:<10} {doc.title[:60]} ({size // 1024} KB)")
    return stats
