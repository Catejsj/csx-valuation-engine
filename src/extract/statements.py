"""Read the primary financial statements out of a text PDF. Rules only, no AI.

Pipeline: find statement pages -> read the column header (years, currencies, scale)
-> parse each row (label, optional note number, one value per column) -> keep the page.

Row values are copied as printed: "(1,234)" is negative, "-" is a printed nil (zero).
Nothing is guessed. A row whose numbers do not fit the column count is returned with
`problem` set, so the review step sees it instead of it silently disappearing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from src.core.schema import Currency, Scale, Statement

TITLES = {
    Statement.BALANCE_SHEET: ("STATEMENT OF FINANCIAL POSITION", "BALANCE SHEET"),
    Statement.INCOME: ("STATEMENT OF PROFIT OR LOSS", "INCOME STATEMENT", "STATEMENT OF COMPREHENSIVE INCOME",
                       "STATEMENT OF INCOME"),
    Statement.CASH_FLOW: ("STATEMENT OF CASH FLOWS", "CASH FLOW STATEMENT", "STATEMENT OF CASH FLOW"),
}
TITLE_LINES = 4        # where a title must be on its own
TITLE_LINES_WIDE = 12  # how far down it may be when the page also has a column header (registration lines above)
MONTHS = {m: i for i, m in enumerate(
    ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST",
     "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"], start=1)}

NUM = r"\(?-?\d{1,3}(?:,\d{3})*(?:\.\d+)?\)?|-"
NUM_RE = re.compile(rf"^(?:{NUM})$")
YEAR_LINE = re.compile(r"^\s*(?:notes?\s+)?((?:(?:19|20)\d{2}\s+){1,7}(?:19|20)\d{2})\s*$", re.IGNORECASE)
DOT_DATE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.((?:19|20)\d{2})\b")
LONG_DATE = re.compile(r"\b(\d{1,2})\s+(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+((?:19|20)\d{2})\b")
UNIT_RE = re.compile(r"(US\$|USD|KHR|RIEL)(\s*(?:million|mn|['’‘`´]\s?000|thousand|000s?))?", re.IGNORECASE)
DURATION_RE = re.compile(r"\b(three|six|nine|twelve|3|6|9|12)[\s-]+months?\b|\bquarter\b", re.IGNORECASE)
DURATION_WORDS = {"three": 3, "six": 6, "nine": 9, "twelve": 12, "3": 3, "6": 6, "9": 9, "12": 12}
DAY_MONTH = re.compile(r"\b(\d{1,2})\s+(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\b")
SKIPPABLE = re.compile(r"^(?:[\s()]*(?:notes?|un-?audited|audited|reviewed|but|restated|and|reclassified|[\d.,]{1,4}))*[\s()]*$",
                       re.IGNORECASE)
PERIOD_RE = re.compile(r"(?:AS AT|ENDED)\s+(\d{1,2})\s+([A-Z]+)\s+((?:19|20)\d{2})")


@dataclass(frozen=True)
class Column:
    year: int
    currency: Currency
    scale: Scale
    entity: str = ""     # "group" / "company" when both are printed side by side, else ""
    end_month: int | None = None   # the column's own date when the header prints one ("31 December 2025")
    end_day: int | None = None
    months: int | None = None      # period length when printed ("three-month period ended", "9 months to")


@dataclass
class Row:
    page: int                      # 1-based
    statement: Statement
    section: str                   # last section header seen ("" if none)
    label: str
    note: str                      # note reference as printed, "" if none
    values: list[Decimal | None]   # one per column; None only when `problem` is set
    problem: str = ""


@dataclass
class StatementTable:
    statement: Statement
    period_end: date
    columns: list[Column]
    rows: list[Row] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)


def parse_number(token: str) -> Decimal:
    """'1,234' -> 1234; '(1,234)' -> -1234; '-' -> 0 (printed nil)."""
    if not NUM_RE.match(token):
        raise ValueError(f"not a number token: {token!r}")
    if token == "-":
        return Decimal(0)
    negative = token.startswith("(") and token.endswith(")")
    core = token.strip("()").replace(",", "")
    value = Decimal(core)
    return -value if negative else value


def _title_in(text: str) -> Statement | None:
    head = re.sub(r"\bSTATEMENTS\b", "STATEMENT", text.upper())
    head = re.sub(r"\s+", " ", head)
    for stmt, titles in TITLES.items():
        if any(t in head for t in titles):
            return stmt
    return None


def detect_statement(lines: list[str]) -> Statement | None:
    """A statement page has its title near the top (a contents page does not).

    The title may sit below a registration block ("Registration No: ...", page number,
    company name); further down it only counts when the page also has a column header.
    """
    found = _title_in(" ".join(lines[:TITLE_LINES]))
    if found is not None:
        return found
    found = _title_in(" ".join(lines[:TITLE_LINES_WIDE]))
    if found is not None and parse_columns(lines) is not None:
        return found
    return None


def parse_period_end(lines: list[str]) -> date | None:
    head = lines[:TITLE_LINES_WIDE]
    for line in head:
        m = PERIOD_RE.search(line.upper())
        if m and m.group(2) in MONTHS:
            return date(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)))
    # "... AS AT 31" / "DECEMBER 2020" split over two lines
    joined = " ".join(l.upper() for l in head)
    m = PERIOD_RE.search(joined)
    if m and m.group(2) in MONTHS:
        return date(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)))
    return None


def _scale_of(text: str) -> Scale:
    t = text.lower()
    if "million" in t or "mn" in t.split():
        return Scale.MILLIONS
    if re.search(r"['’‘`´]\s?000|thousand|\b000s?\b", t):
        return Scale.THOUSANDS
    return Scale.UNITS


def _years_in(line: str) -> list[int] | None:
    """Column years from a header line: '2024 2023', 'Notes 2024 2023',
    '31.12.2024 31.12.2023 1.1.2023' (an opening 1 January column is the year before's end),
    or '31 December 2020 31 December 2019'. Anything else on the line means it is not a header."""
    m = YEAR_LINE.match(line)
    if m:
        return [int(y) for y in m.group(1).split()]
    rest = re.sub(r"(?i)^\s*notes?\b", "", line)
    for pattern in (DOT_DATE, LONG_DATE):
        found = list(pattern.finditer(rest.upper()))
        if len(found) >= 2 and not pattern.sub("", rest.upper()).strip():
            years = []
            for d in found:
                day, month, year = d.group(1), d.group(2), int(d.group(3))
                is_jan1 = day.lstrip("0") == "1" and month.upper().lstrip("0") in ("1", "JANUARY")
                years.append(year - 1 if is_jan1 else year)
            return years
    return None


def _column_dates(line: str, above: str, n: int) -> list[tuple[int, int]] | None:
    """(month, day) per year group, from the year line itself or a 'DD Month' line above it."""
    for text in (line, above):
        found = DAY_MONTH.findall(text.upper())
        if len(found) == n and not DAY_MONTH.sub("", re.sub(r"(?:19|20)\d{2}", "", text.upper())
                                               ).replace("NOTES", "").replace("NOTE", "").strip():
            return [(MONTHS[m], int(d)) for d, m in found]
    return None


def _durations(head: list[str], i: int, n: int) -> list[int | None]:
    """Months per year group: the nearest line above the years that names period lengths."""
    for line in reversed(head[max(0, i - 3):i]):
        found = [DURATION_WORDS[m.group(1).lower()] if m.group(1) else 3 for m in DURATION_RE.finditer(line)]
        if found and n % len(found) == 0:
            per = n // len(found)
            return [found[k // per] for k in range(n)]
    return [None] * n


def parse_columns(lines: list[str]) -> list[Column] | None:
    """Find the year line and the currency line under it, e.g.
        2024 2023
        Notes US$ KHR million US$ KHR million
    Each year spans the same number of currency columns.
    """
    head = lines[:TITLE_LINES_WIDE + 6]
    for i, line in enumerate(head):
        years = _years_in(line)
        if not years or i + 1 >= len(lines):
            continue
        unit_line = i + 1
        while unit_line < min(len(lines), i + 3) and SKIPPABLE.match(lines[unit_line]) \
                and not UNIT_RE.search(lines[unit_line]):
            unit_line += 1     # "(Unaudited) (Audited)" between the dates and the currencies
        cols_per_year = []
        for cm in UNIT_RE.finditer(lines[unit_line] if unit_line < len(lines) else ""):
            code = cm.group(1).upper()
            currency = Currency.USD if code in ("US$", "USD") else Currency.KHR
            cols_per_year.append((currency, _scale_of(cm.group(2) or "")))
        if not cols_per_year:
            # one currency for the whole page, stated in a line above the years ("KHR'000" on its own)
            for back in reversed(head[:i]):
                found = list(UNIT_RE.finditer(back))
                if len(found) == 1 and not re.search(r"\d{1,3},\d{3}", back):
                    code = found[0].group(1).upper()
                    one = (Currency.USD if code in ("US$", "USD") else Currency.KHR, _scale_of(found[0].group(2) or ""))
                    cols_per_year = [one] * len(years)
                    break
        if not cols_per_year or len(cols_per_year) % len(years):
            continue
        per = len(cols_per_year) // len(years)
        cols = [Column(years[k // per], *cols_per_year[k]) for k in range(len(cols_per_year))]
        dates = _column_dates(line, head[i - 1] if i else "", len(years))
        spans = _durations(head, i, len(years))
        cols = [replace(c, end_month=dates[k // per][0] if dates else None,
                        end_day=dates[k // per][1] if dates else None, months=spans[k // per])
                for k, c in enumerate(cols)]
        # "Group  Company" printed above the years: the first half is the group (consolidated)
        above = " ".join(head[max(0, i - 2):i]).lower()
        if re.search(r"\bgroup\b", above) and re.search(r"\bcompany\b", above) and len(cols) % 2 == 0:
            half = len(cols) // 2
            cols = [replace(c, entity="group" if k < half else "company") for k, c in enumerate(cols)]
        else:
            # a whole page for one of them: a line that is just "Group" or "Company" above the years
            for prev in head[max(0, i - 3):i]:
                word = prev.strip().lower()
                if word in ("group", "company", "the group", "the company"):
                    entity = word.replace("the ", "")
                    cols = [replace(c, entity=entity) for c in cols]
                    break
        return cols
    return None


def _split_row(line: str, n_cols: int) -> tuple[str, str, list[str]] | None:
    """Split 'Label text 6 104,804,858 421,840 ...' into (label, note, value tokens)."""
    tokens = line.split()
    values: list[str] = []
    while tokens and NUM_RE.match(tokens[-1]) and len(values) < n_cols:
        values.insert(0, tokens.pop())
    if not values:
        return None
    note = ""
    if tokens and re.fullmatch(r"\d{1,2}(?:\.\d{1,2})?|\d{1,2}\([a-z]\)", tokens[-1]):
        note = tokens.pop()
    return " ".join(tokens), note, values


def _is_footer(line: str) -> bool:
    low = line.lower()
    return low.startswith("the accompanying notes") or bool(re.fullmatch(r"\d{1,3}", line.strip()))


CONNECTORS = ("of", "the", "for", "and", "to", "from", "in", "on", "attributable", "by", "with")


def _ends_with_connector(text: str) -> bool:
    words = text.lower().split()
    return bool(words) and words[-1] in CONNECTORS


def parse_page(lines: list[str], page_no: int, statement: Statement, n_cols: int,
               start_section: str = "") -> tuple[list[Row], str]:
    """Parse the body of one statement page. Returns rows and the section in force at the end.

    Lines without numbers are buffered. When the next numeric row starts with a capital
    letter (and the buffer does not end in a connector like "of the"), the buffer was a
    section header; otherwise it was the first half of a wrapped label and is joined.
    """
    rows: list[Row] = []
    section = start_section
    pending = ""
    body_started = False
    for raw in lines:
        # "( 12,081,555)" -> "(12,081,555)": some PDFs put a space inside the brackets
        line = re.sub(r"\(\s+(?=\d)", "(", raw.strip())
        line = re.sub(r"(?<=\d)\s+\)", ")", line)
        if not line:
            continue
        if not body_started:
            if re.search(r"US\$|USD|KHR|RIEL", line, re.IGNORECASE) and not re.search(r"\d{1,3},\d{3}", line):
                body_started = True
            continue
        if re.fullmatch(r"(\(?notes?\s*[\d&, ]+\)?\s*)+", line, re.IGNORECASE):
            continue   # "(Note 4) (Note 4)" under the currency line
        if _is_footer(line):
            continue
        split = _split_row(line, n_cols)
        if split is None:
            # A new capitalised line closes a finished header ("Adjustments for:", "ASSETS").
            if pending and (pending.endswith(":") or (line[:1].isupper() and not _ends_with_connector(pending))):
                section = pending.lower().rstrip(":")
                pending = ""
            pending = f"{pending} {line}".strip()
            continue
        if pending.endswith(":"):
            section, pending = pending.lower().rstrip(":"), ""
        label, note, tokens = split
        if pending:
            joins = (not label) or (not label[:1].isupper()) or _ends_with_connector(pending)
            if joins:
                label = f"{pending} {label}".strip()
            else:
                section = pending.lower().rstrip(":")
            pending = ""
        problem = ""
        if len(tokens) != n_cols:
            problem = f"expected {n_cols} numbers, found {len(tokens)}"
        elif "per share" in label.lower():
            problem = "per-share row: printed in full amounts, not the column scale"
        values = [None] * n_cols if len(tokens) != n_cols else [parse_number(t) for t in tokens]
        if not label:
            label, problem = UNLABELLED, problem or "unlabelled subtotal"
        rows.append(Row(page_no, statement, section, label, note, values, problem))
    return rows, section


UNLABELLED = "Subtotal (no label printed)"


def _close(total: Decimal, parts: list[Decimal]) -> bool:
    """Equal within one printed unit per term (rounding in the printed figures)."""
    return abs(sum(parts, Decimal(0)) - total) <= len(parts) + 1


def resolve_subtotals(rows: list[Row]) -> None:
    """Name unlabelled subtotals that are provably the total of their section.

    Many reports print a section total as a bare number under the section's rows
    (no "Total current assets" label). If, in every column, it equals the rows above it
    since the last total in the same section (or the last subtotal plus those rows),
    it is named "Total <section>" and no longer flagged. Anything else stays flagged.
    """
    run: list[Row] = []
    last_sub: Row | None = None
    section = None
    for row in rows:
        if row.section != section:
            run, last_sub, section = [], None, row.section
        if row.label == UNLABELLED and row.problem == "unlabelled subtotal" and run:
            n = len(row.values)
            plain = all(_close(row.values[c], [r.values[c] for r in run]) for c in range(n))
            chained = last_sub is not None and all(
                _close(row.values[c], [last_sub.values[c]] + [r.values[c] for r in run]) for c in range(n))
            if (plain or chained) and section:
                if chained and not plain and last_sub is not None and last_sub.label.startswith("Total "):
                    last_sub.label = "Subtotal " + last_sub.label[6:]   # the later line is the real total
                row.label = "Total " + section.strip()
                row.problem = ""
                run, last_sub = [], row
                continue
        if row.problem:
            continue
        if row.label.lower().startswith("total"):
            run, last_sub = [], row
            continue
        run.append(row)


def extract_statements(pdf_path: Path | str,
                       force_pages: dict[int, Statement | None] | None = None,
                       only_listed: bool = False) -> list[StatementTable]:
    """Extract balance sheet, income statement and cash flow statement (with continuation pages).

    force_pages lets a person correct the detection: {page: Statement} treats that page as that
    statement whatever its title says; {page: None} means "this page is not a statement".
    """
    force_pages = force_pages or {}
    import pdfplumber  # imported here so the rest of the engine runs without it

    tables: dict[Statement, StatementTable] = {}
    with pdfplumber.open(str(pdf_path)) as pdf:
        for idx, page in enumerate(pdf.pages):
            lines = (page.extract_text() or "").splitlines()
            if only_listed:     # a person listed the statement pages: read exactly those, nothing else
                stmt = force_pages.get(idx + 1)
            else:
                stmt = force_pages[idx + 1] if idx + 1 in force_pages else detect_statement(lines)
            if stmt is None:
                continue
            columns = parse_columns(lines)
            period = parse_period_end(lines)
            if columns is None or period is None:
                continue
            table = tables.get(stmt)
            if table is not None and not table.rows:
                table = None   # an earlier page had the title and header but no rows (e.g. a summary page)
            if table is None:
                start_rows, _ = parse_page(lines, idx + 1, stmt, len(columns), "")
                if not start_rows:
                    continue
                table = tables[stmt] = StatementTable(stmt, period, columns)
            elif table.columns != columns or table.period_end != period:
                continue   # a different table with a similar title (e.g. company-only statements)
            elif table.pages and idx + 1 != table.pages[-1] + 1 and idx + 1 not in force_pages:
                continue   # only directly following pages continue a statement (unless a person said so)
            start_section = table.rows[-1].section if table.rows else ""
            rows, _ = parse_page(lines, idx + 1, stmt, len(columns), start_section)
            if not rows:
                continue
            table.rows.extend(rows)
            table.pages.append(idx + 1)
    for table in tables.values():
        resolve_subtotals(table.rows)
    return list(tables.values())
