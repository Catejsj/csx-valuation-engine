"""Accept a user's own filing PDF, but only after it passes three gates.

Gate 1 — is it a safe, ordinary PDF?
    PDF signature, size and page limits, not encrypted, and no active content markers
    (JavaScript, launch actions, embedded files, XFA forms, rich media). The engine never runs
    anything inside a PDF; it only reads text. These markers are refused anyway, because a
    normal financial report has no reason to contain them. Limits: markers inside compressed
    object streams can escape a byte scan, which is why gate 1 is a filter, not a guarantee.
Gate 2 — is it a financial report we can read?
    At least one primary statement found; a text layer (scanned PDFs go to manual entry).
Gate 3 — do the numbers hold together?
    All arithmetic checks are run and reported; the auditor's report is looked for.

An accepted upload is stored as data/uploads/<sha256>.pdf (the user's file name is never used
as a path), marked origin=upload, and its values stay "proposed" in the user's own workspace.
Uploads never enter the public dataset or the API. If the same file (same SHA-256) already
exists as an official filing, the official one is used instead.
"""
from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from src.core import paths, store
from src.core.schema import Filing, Issuer, Origin

MAX_BYTES = 50 * 1024 * 1024
MAX_PAGES = 400
ACTIVE_MARKERS = {
    rb"/JavaScript": "JavaScript",
    rb"/JS\b": "JavaScript",
    rb"/Launch\b": "launch action (opens another program)",
    rb"/EmbeddedFile": "embedded file",
    rb"/XFA\b": "XFA form",
    rb"/RichMedia": "rich media",
    rb"/SubmitForm": "form submission",
}


@dataclass
class IntakeResult:
    accepted: bool
    sha256: str = ""
    refused: list[str] = field(default_factory=list)    # why it was not accepted
    warnings: list[str] = field(default_factory=list)   # accepted, but the student should know
    pages: int = 0
    text_pages: int = 0
    filing_id: str = ""
    facts_stored: int = 0
    checks_passed: int = 0
    checks_failed: int = 0
    same_as_official: str = ""
    statements: list = field(default_factory=list)    # [{"statement", "name", "pages"}] what was found


def _structure(data: bytes) -> bytes:
    """The PDF's object structure, without page and image data.

    Searching the raw file for "/JS" gives false alarms: those bytes turn up by chance inside
    compressed images. Real scripts and launch actions live in object dictionaries, either
    in plain text or inside compressed object streams (/ObjStm), so those are kept (object
    streams are decompressed) and every other stream body is skipped. A stream that cannot
    be decompressed is kept raw, so nothing is hidden by a broken stream.
    """
    import zlib
    out, pos = [], 0
    for m in re.finditer(rb"stream\r?\n", data):
        if m.start() < pos:
            continue                      # inside a stream already skipped (or "endstream")
        head = data[pos:m.start()]
        out.append(head)
        end = data.find(b"endstream", m.end())
        if end < 0:
            out.append(data[m.end():])
            pos = len(data)
            break
        if b"/ObjStm" in head[-4096:]:
            try:
                out.append(zlib.decompress(data[m.end():end]))
            except zlib.error:
                out.append(data[m.end():end])
        pos = end + len(b"endstream")
    out.append(data[pos:])
    return b"\n".join(out)


def check_file(data: bytes) -> IntakeResult:
    """Gate 1 and gate 2 on raw bytes. Nothing is stored."""
    result = IntakeResult(accepted=False, sha256=hashlib.sha256(data).hexdigest())
    if len(data) == 0:
        result.refused.append("the file is empty")
        return result
    if len(data) > MAX_BYTES:
        result.refused.append(f"the file is larger than {MAX_BYTES // (1024 * 1024)} MB")
        return result
    if b"%PDF-" not in data[:1024]:
        result.refused.append("not a PDF (the PDF signature is missing), whatever its file name says")
        return result
    structure = _structure(data)
    if re.search(rb"/Encrypt\b", structure):
        result.refused.append("the PDF is encrypted or password-protected")
        return result
    found = sorted({label for pattern, label in ACTIVE_MARKERS.items() if re.search(pattern, structure)})
    if found:
        result.refused.append("the PDF contains active content: " + ", ".join(found))
        return result

    import pypdfium2 as pdfium
    try:
        doc = pdfium.PdfDocument(data)
    except Exception as exc:  # damaged or not really a PDF
        result.refused.append(f"the PDF cannot be opened ({type(exc).__name__})")
        return result
    try:
        result.pages = len(doc)
        if result.pages == 0:
            result.refused.append("the PDF has no pages")
            return result
        if result.pages > MAX_PAGES:
            result.refused.append(f"the PDF has {result.pages} pages; the limit is {MAX_PAGES}")
            return result
        for i in range(result.pages):
            textpage = doc[i].get_textpage()
            if textpage.get_text_range().strip():
                result.text_pages += 1
            textpage.close()
    finally:
        doc.close()
    if result.text_pages == 0:
        result.refused.append("no text layer (a scanned document); enter the numbers by hand instead")
        return result
    if result.text_pages < result.pages * 0.5:
        result.warnings.append(f"only {result.text_pages} of {result.pages} pages have text; some pages are images")
    result.accepted = True
    return result


def ingest(pdf_path: Path, issuer_id: str, issuer_name: str, kind: str = "annual",
           conn=None, display_name: str = "") -> IntakeResult:
    """Run all three gates on a file and store it as the user's own filing if it passes."""
    from src.extract.load import build, save
    from src.extract.statements import extract_statements

    data = Path(pdf_path).read_bytes()
    result = check_file(data)
    if not result.accepted:
        return result
    conn = conn or store.connect(paths.db_path())
    store.init_db(conn)

    existing = store.filing_by_sha(conn, result.sha256)
    if existing is not None:
        result.same_as_official = existing["id"] if existing["origin"] == Origin.OFFICIAL.value else ""
        result.filing_id = existing["id"]
        result.warnings.append(
            f"this exact file is already loaded as {existing['id']}"
            + (" (official source); the official values are used" if result.same_as_official else ""))
        return result

    uploads = paths.data_dir() / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    stored_pdf = uploads / f"{result.sha256}.pdf"
    tables = extract_statements(pdf_path)
    if not tables:
        result.accepted = False
        result.refused.append("no financial statements were found (balance sheet, income or cash flow)")
        return result

    names = {"BS": "Balance sheet", "IS": "Income statement", "CF": "Cash flow"}
    result.statements = [{"statement": t.statement.value, "name": names.get(t.statement.value, t.statement.value),
                          "pages": t.pages} for t in tables]
    found = {t.statement.value for t in tables}
    missing = {"BS", "IS", "CF"} - found
    if missing:
        result.warnings.append("statement(s) not found: " + ", ".join(sorted(missing)))
    statement_pages = {p for t in tables for p in t.pages}
    auditor = _auditor_status(pdf_path, statement_pages)
    if auditor.status == "partial":
        result.warnings.append(f"auditor's report is {auditor.finding}")
    elif auditor.status == "missing":
        result.warnings.append("no independent auditor's report found; treat these numbers as unaudited")

    result.filing_id = f"{issuer_id}-UP-{result.sha256[:8]}"
    report = build(tables, issuer_id, result.filing_id, kind)
    result.checks_passed = sum(c.status == "pass" for c in report.checks)
    result.checks_failed = len(report.failed)
    if report.failed:
        result.warnings.append(f"{len(report.failed)} arithmetic check(s) fail; review before using these numbers")

    shutil.copyfile(pdf_path, stored_pdf)
    issuer = Issuer(id=issuer_id, name=issuer_name, reporting_currency=tables[0].columns[0].currency)
    filing = Filing(id=result.filing_id, issuer_id=issuer_id, kind=kind,
                    source_url=f"file:uploads/{result.sha256}.pdf", pdf_sha256=result.sha256,
                    published_on=date.today(), title=f"Uploaded {_clean_name(display_name or Path(pdf_path).name)}",
                    retrieved_at=datetime.now().replace(microsecond=0), origin=Origin.UPLOAD)
    result.facts_stored = save(conn, issuer, filing, report)
    return result


def _clean_name(name: str) -> str:
    """A file name for display only (never used as a path): base name, printable, short."""
    base = re.split(r"[\\/]", name)[-1]
    base = re.sub(r"[^\w .()\-]", "", base).strip() or "file.pdf"
    return base[:80]


def _auditor_status(pdf_path: Path, statement_pages: set[int]):
    """Readable, scanned (image pages just before the statements), or missing."""
    import pdfplumber
    from src.report.disclosure import auditor_check
    with pdfplumber.open(str(pdf_path)) as pdf:
        texts = [page.extract_text() or "" for page in pdf.pages]
    return auditor_check(texts, statement_pages)[0]
