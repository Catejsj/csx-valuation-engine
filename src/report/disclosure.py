"""What a filing tells you: a factual disclosure checklist, measured from the document itself.

Each check states what was found and where ("no segment note found", "auditor's report is a
scanned image on pages 63-66"). It never judges the company. The reference points are common
international practice for listed companies: a readable auditor's opinion with key audit
matters, segment information, a management discussion, operating figures, a risk section,
and machine-readable data.

`analyse()` works on plain page texts so it can be tested without PDFs; `analyse_pdf()` reads
the file once and caches the result by its hash.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

GOOD, PARTIAL, MISSING, INFO = "good", "partial", "missing", "info"

KPI_TERMS = {
    "subscribers": r"\bsubscribers?\b", "customers (count)": r"\bnumber of customers\b|\bcustomer base\b",
    "ARPU": r"\bARPU\b|average revenue per user", "churn": r"\bchurn\b",
    "loans and deposits": r"\bloan portfolio\b|\btotal deposits\b|\bdeposit base\b",
    "non-performing loans": r"non-performing loans?|\bNPL\b", "volumes": r"\bvolume of\b|\btonnes?\b|\bTEU\b|\bm3\b",
    "network / sites": r"\bbase stations?\b|\btowers?\b|\bsites\b", "occupancy / units": r"\boccupancy\b|\bunits sold\b",
}


@dataclass
class Check:
    key: str
    label: str
    status: str                    # good | partial | missing | info
    finding: str
    pages: list[int] = field(default_factory=list)
    why: str = ""                  # why this matters, one line, for students


def _head(text: str, lines: int = 4) -> str:
    return " ".join(text.strip().splitlines()[:lines])


def _runs(pages: list[int]) -> str:
    """[63,64,65,66,112] -> '63–66, 112'"""
    out, start, prev = [], None, None
    for p in sorted(pages):
        if start is None:
            start = prev = p
        elif p == prev + 1:
            prev = p
        else:
            out.append(f"{start}–{prev}" if prev != start else f"{start}")
            start = prev = p
    if start is not None:
        out.append(f"{start}–{prev}" if prev != start else f"{start}")
    return ", ".join(out)


def find_pages(texts: list[str], pattern: str, head_only: bool = False) -> list[int]:
    rx = re.compile(pattern, re.IGNORECASE)
    return [i for i, t in enumerate(texts, start=1) if rx.search(_head(t) if head_only else t)]


PREFIX = r"^(?:item\s+\d+[a-z]?\.?\s*|part\s+\w+\.?\s*[–\-:]?\s*|\d+(?:\.\d+)*[.)]?\s+|[a-z][.)]\s+)?"


def is_contents_page(text: str) -> bool:
    """A contents page lists many headings with page numbers (dotted leaders, or 10-K 'Item' lines).

    A 'Table of Contents' link alone is not enough: 10-K pages carry one in every header.
    """
    low = text.lower()
    items = len(re.findall(r"(?m)^\s*item\s+\d+[a-z]?\.", low))
    leaders = len(re.findall(r"(?m)(?:\.{4,}|…{2,})\s*\d{1,3}\s*$", text))
    return leaders >= 5 or items >= 8 or ("table of contents" in low[:600] and (items >= 5 or leaders >= 3))


def heading_pages(texts: list[str], pattern: str) -> list[int]:
    """Pages with a heading line (anywhere on the page) matching pattern; contents pages skipped."""
    rx = re.compile(PREFIX + pattern, re.IGNORECASE)
    out = []
    for i, t in enumerate(texts, start=1):
        if is_contents_page(t):
            continue
        for line in t.splitlines():
            line = line.strip()
            if 3 < len(line) < 130 and line[0] not in "-•–" and not line[0].islower() and rx.match(line):
                out.append(i)
                break
    return out


def image_only_pages(texts: list[str]) -> list[int]:
    return [i for i, t in enumerate(texts, start=1) if not t.strip()]


def auditor_check(texts: list[str], statement_pages: set[int]) -> tuple[Check, Check]:
    """(auditor's report readable?, opinion wording) — handles a scanned auditor's report."""
    why = "The auditor's opinion says whether the statements can be relied on; it should be readable and searchable."
    pages = [p for p in find_pages(texts, r"independent auditor|independent registered public accounting firm")
             if re.search(r"\bopinion\b", texts[p - 1], re.I) and not is_contents_page(texts[p - 1])]
    if pages:
        text = " ".join(texts[p - 1] for p in pages).lower()
        modified = [name for name, rx in (("qualified opinion", r"qualified opinion"),
                                          ("adverse opinion", r"adverse opinion"),
                                          ("disclaimer of opinion", r"disclaimer of opinion"),
                                          ("emphasis of matter", r"emphasis of matter"),
                                          ("material uncertainty about going concern",
                                           r"material uncertainty related to going concern"))
                    if re.search(rx, text) and not re.search(r"(unqualified|un-qualified) opinion", text)]
        readable = Check("auditor_report", "Auditor's report", GOOD, f"readable text on page {_runs(pages)}", pages, why)
        if modified:
            opinion = Check("auditor_opinion", "Auditor's opinion", PARTIAL,
                            "wording found: " + ", ".join(modified) + " — read it on the page", pages,
                            "A modified opinion or an emphasis of matter tells you where the auditor had concerns.")
        elif re.search(r"present fairly|give a true and fair view", text):
            opinion = Check("auditor_opinion", "Auditor's opinion", GOOD, "standard clean-opinion wording found",
                            pages, "A clean opinion is the minimum; it does not mean the business is good.")
        else:
            opinion = Check("auditor_opinion", "Auditor's opinion", INFO, "opinion wording not recognised — read it on the page",
                            pages, "")
        return readable, opinion
    first = min(statement_pages) if statement_pages else None
    scanned = image_only_pages(texts)
    near = [p for p in scanned if first is not None and first - 8 <= p < first]
    if near:
        return (Check("auditor_report", "Auditor's report", PARTIAL,
                      f"a scanned image (pages {_runs(near)}) — not searchable; read the opinion by eye", near, why),
                Check("auditor_opinion", "Auditor's opinion", INFO, "cannot be read automatically from a scan", near, ""))
    return (Check("auditor_report", "Auditor's report", MISSING, "not found in this document", [], why),
            Check("auditor_opinion", "Auditor's opinion", MISSING, "no auditor's opinion found", [], ""))


RISK_HEADING = re.compile(r"^(?:PART\s+\w+[.:]?\s*|\d+(?:\.\d+)*[.)]?\s+|[A-Z][.)]\s+)?[A-Z ,&]*"
                          r"(RISK FACTORS|PRINCIPAL RISKS|KEY RISKS|RISK MANAGEMENT)", re.IGNORECASE)


def risk_section_pages(texts: list[str]) -> list[int]:
    """Pages that open a real risk section: a heading line, not a bullet or the financial-risk note."""
    pages = heading_pages(texts, r"(?:[a-z ,&]*)(risks? factors|principal risks|key risks|risk management)")
    out = []
    for p in pages:
        lines = [l.strip() for l in texts[p - 1].splitlines() if l.strip()]
        heads = [l for l in lines if len(l) < 130 and re.search(r"risks? factors|principal risks|key risks|risk management", l, re.I)]
        if any("FINANCIAL RISK" not in h.upper() and not h[0] in "-•–" and not h[0].islower() for h in heads):
            out.append(p)
    return out


def mdna_pages(texts: list[str]) -> list[int]:
    """Pages of the management discussion: from its heading to the next major heading."""
    starts = heading_pages(texts, r"management.{0,3}s?\s+dis?c?u?ssion|.*\bmd&a\b")
    if not starts:
        return []
    start = starts[0]
    nxt = re.compile(PREFIX.replace("|\\d+(?:\\.\\d+)*[.)]?\\s+|[a-z][.)]\\s+", "") +
                     r"(?:quantitative and qualitative|financial statements|statements? of financial position|"
                     r"report of independent|independent auditor|directors|corporate governance)|^(?:item\s+\d+[a-z]?\.|part\s+\w+\b)",
                     re.IGNORECASE)
    out = [start]
    for p in range(start + 1, min(start + 30, len(texts) + 1)):
        text = texts[p - 1]
        if not text.strip() or any(nxt.match(l.strip()) for l in text.splitlines()[:6]):
            break
        out.append(p)
    return out


def analyse(texts: list[str], statement_pages: set[int] | None = None) -> list[Check]:
    statement_pages = statement_pages or set()
    checks: list[Check] = []
    readable, opinion = auditor_check(texts, statement_pages)
    checks += [readable, opinion]

    kam = find_pages(texts, r"key audit matters?|critical audit matters?")
    checks.append(Check("key_audit_matters", "Key audit matters", GOOD if kam else
                        (INFO if readable.status != GOOD else MISSING),
                        f"found on page {_runs(kam)}" if kam else
                        ("not found — may be inside the scanned auditor's report" if readable.status != GOOD else "not found"),
                        kam, "The areas the auditor found hardest to check; listed-company audits normally include them."))

    seg = find_pages(texts, r"operating segments?|segment information|segment report|reportable segments?")
    checks.append(Check("segments", "Segment information", GOOD if seg else MISSING,
                        f"found on page {_runs(seg)}" if seg else "no segment note found — revenue and profit are not split by business line",
                        seg, "Without segments you cannot tell which part of the business earns the money."))

    md = mdna_pages(texts)
    if md:
        status = GOOD if len(md) >= 8 else PARTIAL
        finding = f"about {len(md)} page{'s' if len(md) != 1 else ''} (pages {_runs(md)})"
    else:
        status, finding = MISSING, "no management discussion section found"
    checks.append(Check("mdna", "Management discussion", status, finding, md,
                        "Management's own explanation of why the numbers moved; short ones explain little."))

    found = {name: find_pages(texts, rx) for name, rx in KPI_TERMS.items()}
    found = {k: v for k, v in found.items() if v}
    checks.append(Check("kpis", "Operating figures", GOOD if len(found) >= 2 else PARTIAL if found else MISSING,
                        ("mentions " + ", ".join(found)) if found else "no operating figures found (customers, volumes, units…)",
                        sorted({p for v in found.values() for p in v})[:12],
                        "Operating figures connect the accounts to the real business."))

    risk_head = risk_section_pages(texts)
    risk_any = find_pages(texts, r"interest rate risk|credit risk|liquidity risk")
    if risk_head:
        checks.append(Check("risks", "Risk section", GOOD, f"a risk section starts on page {risk_head[0]}",
                            risk_head[:6], "What management thinks could go wrong."))
    else:
        checks.append(Check("risks", "Risk section", PARTIAL if risk_any else MISSING,
                            "only the standard financial-risk notes (interest, credit, liquidity)" if risk_any
                            else "no risk discussion found", risk_any[:6], "What management thinks could go wrong."))

    scanned = image_only_pages(texts)
    checks.append(Check("scanned_pages", "Pages without text", INFO if scanned else GOOD,
                        f"{len(scanned)} of {len(texts)} pages are images only (pages {_runs(scanned)})" if scanned
                        else "every page has searchable text", scanned,
                        "Image pages cannot be searched or read automatically."))
    xbrl = find_pages(texts, r"inline xbrl|interactive data file")
    checks.append(Check("structured_data", "Machine-readable data", GOOD if xbrl else MISSING,
                        f"structured data (XBRL) filed with it — see page {_runs(xbrl[:4])}" if xbrl else
                        "PDF only — no structured data file (such as XBRL) published with it", xbrl[:4],
                        "Structured data lets anyone load the numbers without retyping them."))
    return checks


_cache: dict[str, list[dict]] = {}
CACHE_VERSION = 1   # raise when analyse() changes, so saved results are recomputed


def _cache_key(sha256: str, statement_pages: set[int]) -> str:
    import hashlib
    pages = ",".join(str(p) for p in sorted(statement_pages))
    return f"{sha256}-{hashlib.sha256(pages.encode()).hexdigest()[:12]}-v{CACHE_VERSION}"


def _cache_file(key: str) -> Path:
    from src.core import paths
    return paths.data_dir() / "cache" / "disclosure" / f"{key}.json"


def cached(sha256: str, statement_pages: set[int]) -> list[dict] | None:
    """The saved result, if this file was analysed before (never reads the PDF)."""
    key = _cache_key(sha256, statement_pages)
    if key in _cache:
        return _cache[key]
    f = _cache_file(key)
    if f.is_file():
        import json
        try:
            _cache[key] = json.loads(f.read_text(encoding="utf-8"))
            return _cache[key]
        except (OSError, ValueError):
            return None
    return None


def analyse_pdf(pdf_path: Path, sha256: str, statement_pages: set[int]) -> list[dict]:
    """Analyse once per file: the result is kept in memory and on disk (data/cache/disclosure)."""
    hit = cached(sha256, statement_pages)
    if hit is not None:
        return hit
    import json
    import pdfplumber
    with pdfplumber.open(str(pdf_path)) as pdf:
        texts = [page.extract_text() or "" for page in pdf.pages]
    result = [asdict(c) for c in analyse(texts, statement_pages)]
    key = _cache_key(sha256, statement_pages)
    _cache[key] = result
    try:
        f = _cache_file(key)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return result


def restated_figures(conn, issuer: str) -> list[dict]:
    """Figures printed differently for the same year in two switched-on filings."""
    rows = conn.execute("""
        SELECT a.statement, a.line_item, a.period_end, a.label_as_printed,
               a.value_as_reported AS old_value, a.filing_id AS old_filing,
               b.value_as_reported AS new_value, b.filing_id AS new_filing
        FROM facts a JOIN facts b ON a.issuer_id = b.issuer_id AND a.statement = b.statement
             AND a.line_item = b.line_item AND a.period_end = b.period_end AND a.period_type = b.period_type
        JOIN filings fa ON fa.id = a.filing_id JOIN filings fb ON fb.id = b.filing_id
        WHERE a.issuer_id = ?
          AND ((SELECT MAX(x.period_end) FROM facts x WHERE x.filing_id = fa.id), fa.published_on)
            < ((SELECT MAX(x.period_end) FROM facts x WHERE x.filing_id = fb.id), fb.published_on)
          AND fa.included = 1 AND fb.included = 1
          AND a.value_as_reported != b.value_as_reported AND a.scale = b.scale AND a.currency = b.currency
        ORDER BY a.period_end, a.statement, a.line_item""", (issuer,)).fetchall()
    return [dict(r) for r in rows]
