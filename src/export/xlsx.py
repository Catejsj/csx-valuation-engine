"""A small .xlsx writer using only the standard library.

An .xlsx file is a zip of XML parts. This writes the few parts Excel, LibreOffice,
Numbers and Google Sheets need: text, exact numbers, live formulas, a handful of
number formats, column widths, a frozen header row, and links.

    wb = Workbook()
    ws = wb.sheet("Summary")
    ws.row(["Year", 2024, 2025], style="head")
    ws.row(["Revenue", Decimal("1200"), Formula("B2*1.1")], style="money")
    wb.save("out.xlsx")
"""
from __future__ import annotations

import re
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape

# style name -> index in cellXfs below
STYLES = {"text": 0, "head": 1, "money": 2, "pct": 3, "x": 4, "note": 5, "title": 6, "num2": 7,
          "date": 8, "bold_money": 9, "link": 10}

_STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="4">
<numFmt numFmtId="164" formatCode="#,##0;(#,##0);&quot;-&quot;"/>
<numFmt numFmtId="165" formatCode="0.0%"/>
<numFmt numFmtId="166" formatCode="0.00&quot;x&quot;"/>
<numFmt numFmtId="167" formatCode="yyyy-mm-dd"/>
</numFmts>
<fonts count="5">
<font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><name val="Calibri"/></font>
<font><i/><sz val="10"/><color rgb="FF6B6B6B"/><name val="Calibri"/></font>
<font><b/><sz val="14"/><name val="Calibri"/></font>
<font><u/><sz val="11"/><color rgb="FF1F5FBF"/><name val="Calibri"/></font>
</fonts>
<fills count="3">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFF1ECE0"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="2">
<border><left/><right/><top/><bottom/><diagonal/></border>
<border><left/><right/><top/><bottom style="thin"><color rgb="FF999999"/></bottom><diagonal/></border>
</borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="11">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"/>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="166" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="167" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="164" fontId="1" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1"/>
<xf numFmtId="0" fontId="4" fillId="0" borderId="0" xfId="0" applyFont="1"/>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""

_BAD_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_SHEET_NAME_BAD = re.compile(r"[\[\]\*\?/\\:]")
_EPOCH = date(1899, 12, 30)


@dataclass(frozen=True)
class Formula:
    """A live Excel formula, written without the leading '='."""
    text: str


def col_letter(n: int) -> str:
    """1 -> A, 27 -> AA."""
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def ref(col: int, row: int, absolute: bool = False) -> str:
    return f"${col_letter(col)}${row}" if absolute else f"{col_letter(col)}{row}"


def _clean(text: str) -> str:
    return escape(_BAD_XML.sub("", text))


@dataclass
class Sheet:
    name: str
    rows: list[list[tuple[object, str | None]]] = field(default_factory=list)
    widths: dict[int, float] = field(default_factory=dict)
    freeze_rows: int = 0
    freeze_cols: int = 0
    links: list[tuple[str, str]] = field(default_factory=list)   # (cell, url)

    @property
    def next_row(self) -> int:
        return len(self.rows) + 1

    def row(self, values: Sequence[object], style: str | None = None,
            styles: Sequence[str | None] | None = None) -> int:
        """Append a row; returns its 1-based row number."""
        cells = []
        for i, v in enumerate(values):
            st = styles[i] if styles and i < len(styles) and styles[i] else style
            cells.append((v, st))
        self.rows.append(cells)
        return len(self.rows)

    def blank(self) -> int:
        return self.row([])

    def link(self, row: int, col: int, url: str) -> None:
        self.links.append((ref(col, row), url))

    def width(self, col: int, chars: float) -> None:
        self.widths[col] = chars

    def _cell(self, r: int, c: int, value: object, style: str | None) -> str:
        at = ref(c, r)
        s = STYLES.get(style or "text", 0)
        sattr = f' s="{s}"' if s else ""
        if value is None or value == "":
            return f'<c r="{at}"{sattr}/>' if s else ""
        if isinstance(value, Formula):
            return f'<c r="{at}"{sattr}><f>{_clean(value.text)}</f></c>'
        if isinstance(value, bool):
            return f'<c r="{at}" t="b"{sattr}><v>{int(value)}</v></c>'
        if isinstance(value, (int, Decimal)) or (isinstance(value, float) and value == value):
            v = format(value, "f") if isinstance(value, Decimal) else repr(value)
            return f'<c r="{at}"{sattr}><v>{v}</v></c>'
        if isinstance(value, (date, datetime)):
            d = value.date() if isinstance(value, datetime) else value
            sattr = f' s="{STYLES["date"]}"' if not s else sattr
            return f'<c r="{at}"{sattr}><v>{(d - _EPOCH).days}</v></c>'
        text = str(value)
        if text[:1] in "=+-@" and not re.fullmatch(r"-?\d+(\.\d+)?", text):
            text = "'" + text   # never let a label turn into a formula
        return f'<c r="{at}" t="inlineStr"{sattr}><is><t xml:space="preserve">{_clean(text)}</t></is></c>'

    def xml(self) -> str:
        parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                 '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">']
        if self.freeze_rows or self.freeze_cols:
            top = ref(self.freeze_cols + 1, self.freeze_rows + 1)
            attrs = []
            if self.freeze_cols:
                attrs.append(f'xSplit="{self.freeze_cols}"')
            if self.freeze_rows:
                attrs.append(f'ySplit="{self.freeze_rows}"')
            parts.append(f'<sheetViews><sheetView workbookViewId="0"><pane {" ".join(attrs)} '
                         f'topLeftCell="{top}" activePane="bottomRight" state="frozen"/></sheetView></sheetViews>')
        if self.widths:
            parts.append("<cols>" + "".join(
                f'<col min="{c}" max="{c}" width="{w}" customWidth="1"/>' for c, w in sorted(self.widths.items()))
                + "</cols>")
        parts.append("<sheetData>")
        for r, cells in enumerate(self.rows, start=1):
            body = "".join(self._cell(r, c, v, st) for c, (v, st) in enumerate(cells, start=1))
            parts.append(f'<row r="{r}">{body}</row>')
        parts.append("</sheetData>")
        if self.links:
            parts.append("<hyperlinks>" + "".join(
                f'<hyperlink ref="{cell}" r:id="rId{i}"/>' for i, (cell, _) in enumerate(self.links, start=1))
                + "</hyperlinks>")
        parts.append("</worksheet>")
        return "".join(parts)

    def rels(self) -> str | None:
        if not self.links:
            return None
        body = "".join(
            f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            f'hyperlink" Target="{escape(url, {chr(34): "&quot;"})}" TargetMode="External"/>'
            for i, (_, url) in enumerate(self.links, start=1))
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                + body + "</Relationships>")


class Workbook:
    def __init__(self) -> None:
        self.sheets: list[Sheet] = []

    def sheet(self, name: str) -> Sheet:
        clean = _SHEET_NAME_BAD.sub("-", name)[:31] or f"Sheet{len(self.sheets) + 1}"
        if any(s.name.lower() == clean.lower() for s in self.sheets):
            raise ValueError(f"duplicate sheet name: {clean}")
        ws = Sheet(clean)
        self.sheets.append(ws)
        return ws

    def to_bytes(self) -> bytes:
        import io
        buf = io.BytesIO()
        self._write(buf)
        return buf.getvalue()

    def save(self, path: Path | str) -> None:
        with open(path, "wb") as f:
            self._write(f)

    def _write(self, f) -> None:
        n = len(self.sheets)
        if not n:
            raise ValueError("a workbook needs at least one sheet")
        ct = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
              '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
              '<Default Extension="xml" ContentType="application/xml"/>'
              '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
              '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
        ct += [f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
               for i in range(1, n + 1)]
        ct.append("</Types>")
        root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                     '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                     '</Relationships>')
        sheets_xml = "".join(f'<sheet name="{_clean(s.name)}" sheetId="{i}" r:id="rId{i}"/>'
                             for i, s in enumerate(self.sheets, start=1))
        workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                    f'<sheets>{sheets_xml}</sheets><calcPr calcId="191029" fullCalcOnLoad="1"/></workbook>')
        wb_rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
        wb_rels += [f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                    for i in range(1, n + 1)]
        wb_rels.append(f'<Relationship Id="rId{n + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>')
        wb_rels.append("</Relationships>")
        with zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", "".join(ct))
            z.writestr("_rels/.rels", root_rels)
            z.writestr("xl/workbook.xml", workbook)
            z.writestr("xl/_rels/workbook.xml.rels", "".join(wb_rels))
            z.writestr("xl/styles.xml", _STYLES_XML)
            for i, s in enumerate(self.sheets, start=1):
                z.writestr(f"xl/worksheets/sheet{i}.xml", s.xml())
                rels = s.rels()
                if rels:
                    z.writestr(f"xl/worksheets/_rels/sheet{i}.xml.rels", rels)
