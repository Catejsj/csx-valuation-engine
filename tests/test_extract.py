"""Extractor and checks on hand-written statement pages (made-up company, made-up numbers).

The real-PDF test at the bottom runs only when the downloaded report is present (it is
git-ignored and never redistributed).
"""
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from src.core.schema import Currency, Scale, Statement
from src.extract.labels import line_items
from src.extract.load import build
from src.extract.statements import (
    StatementTable, detect_statement, parse_columns, parse_number, parse_page, parse_period_end,
)

BS_PAGE = """TEST PLC.
STATEMENT OF FINANCIAL POSITION
AS AT 31 DECEMBER 2025
2025 2024
Notes US$ KHR million US$ KHR million
ASSETS
Non-current assets
Property, plant and equipment 5 1,000 4 900 4
Advances to suppliers for capital
expenditure 200 1 - -
Total non-current assets 1,200 5 900 4
Current assets
Cash and cash equivalents 6 300 1 100 -
Total current assets 300 1 100 -
TOTAL ASSETS 1,500 6 1,000 4
EQUITY AND LIABILITIES
EQUITY
Share capital 1,000 4 800 3
Retained earnings 100 - (50) -
TOTAL EQUITY 1,100 4 750 3
LIABILITIES
Non-current liabilities
Borrowings 7 300 1 200 1
Total non-current liabilities 300 1 200 1
Current liabilities
Borrowings 7 100 1 50 -
Total current liabilities 100 1 50 -
TOTAL LIABILITIES 400 2 250 1
TOTAL EQUITY AND LIABILITIES 1,500 6 1,000 4
The accompanying notes form an integral part of these financial statements.
5""".splitlines()


def table_from(lines, page=5):
    stmt = detect_statement(lines)
    cols = parse_columns(lines)
    rows, _ = parse_page(lines, page, stmt, len(cols))
    t = StatementTable(stmt, parse_period_end(lines), cols, rows, [page])
    return t


class Parsing(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(parse_number("1,234"), Decimal(1234))
        self.assertEqual(parse_number("(1,234)"), Decimal(-1234))
        self.assertEqual(parse_number("-"), Decimal(0))
        self.assertEqual(parse_number("0.014"), Decimal("0.014"))
        with self.assertRaises(ValueError):
            parse_number("12a")

    def test_header(self):
        self.assertIs(detect_statement(BS_PAGE), Statement.BALANCE_SHEET)
        self.assertEqual(parse_period_end(BS_PAGE), date(2025, 12, 31))
        cols = parse_columns(BS_PAGE)
        self.assertEqual([(c.year, c.currency, c.scale) for c in cols], [
            (2025, Currency.USD, Scale.UNITS), (2025, Currency.KHR, Scale.MILLIONS),
            (2024, Currency.USD, Scale.UNITS), (2024, Currency.KHR, Scale.MILLIONS)])

    def test_contents_page_is_not_a_statement(self):
        toc = ["ANNUAL REPORT", "TABLE OF CONTENTS", "Directors' report 3", "Chairman's message 4",
               "Statement of financial position 9"]
        self.assertIsNone(detect_statement(toc))

    def test_rows_labels_notes_sections(self):
        t = table_from(BS_PAGE)
        by_label = {r.label: r for r in t.rows}
        self.assertIn("Advances to suppliers for capital expenditure", by_label)   # wrapped label joined
        ppe = by_label["Property, plant and equipment"]
        self.assertEqual(ppe.note, "5")
        self.assertEqual(ppe.values, [Decimal(1000), Decimal(4), Decimal(900), Decimal(4)])
        self.assertEqual(ppe.page, 5)
        self.assertIn("non-current", ppe.section)
        self.assertEqual(by_label["Retained earnings"].values[2], Decimal(-50))
        self.assertTrue(all(not r.problem for r in t.rows))

    def test_duplicate_labels_get_section_suffix(self):
        t = table_from(BS_PAGE)
        names = line_items(t.statement, t.rows)
        self.assertIn("borrowings_non_current", names)
        self.assertIn("borrowings_current", names)
        self.assertIn("total_assets", names)
        self.assertEqual(len(names), len(set(names)))

    def test_wrong_count_flagged_not_guessed(self):
        lines = BS_PAGE[:6] + ["Mystery item 1,000 4"] + BS_PAGE[6:]
        t = table_from(lines)
        bad = [r for r in t.rows if r.label == "Mystery item"]
        self.assertEqual(len(bad), 1)
        self.assertIn("expected 4", bad[0].problem)
        self.assertEqual(bad[0].values, [None] * 4)


class Checks(unittest.TestCase):
    def test_clean_page_passes_everything(self):
        report = build([table_from(BS_PAGE)], "TEST", "TEST-AR2025")
        self.assertTrue(report.checks)
        self.assertEqual(report.failed, [], [c.name for c in report.failed])
        # only the USD columns are stored, both years
        self.assertEqual({f.currency for f in report.facts}, {Currency.USD})
        self.assertEqual({f.period_end for f in report.facts}, {date(2025, 12, 31), date(2024, 12, 31)})

    def test_misread_number_is_caught(self):
        # 1,000 misread as 1,600: the section sum and the balance identity must both fail.
        lines = [ln.replace("Property, plant and equipment 5 1,000", "Property, plant and equipment 5 1,600")
                 for ln in BS_PAGE]
        report = build([table_from(lines)], "TEST", "TEST-AR2025")
        failed = {c.name for c in report.failed if c.column == "2025 USD"}
        self.assertIn("rows add up to 'Total non-current assets'", failed)

    def test_unit_trap_caught(self):
        # A total read in thousands while its rows are in units breaks the sum.
        lines = [ln.replace("TOTAL ASSETS 1,500", "TOTAL ASSETS 1,500,000") for ln in BS_PAGE]
        report = build([table_from(lines)], "TEST", "TEST-AR2025")
        self.assertIn("assets = equity + liabilities",
                      {c.name for c in report.failed if c.column == "2025 USD"})


REAL_PDF = Path(__file__).resolve().parents[1] / "data" / "pdfs" / "CGSM" / "FS-011-annual-report-2024.pdf"


@unittest.skipUnless(REAL_PDF.exists(), "real report not downloaded")
class RealReport(unittest.TestCase):
    def test_cellcard_2024_all_checks_pass(self):
        from src.extract.statements import extract_statements
        tables = extract_statements(REAL_PDF)
        self.assertEqual({t.statement for t in tables},
                         {Statement.BALANCE_SHEET, Statement.INCOME, Statement.CASH_FLOW})
        report = build(tables, "CGSM", "CGSM-AR2024")
        self.assertEqual(report.failed, [])
        self.assertGreaterEqual(len(report.checks), 60)
        pages = {f.line_item: f.page for f in report.facts if f.period_end == date(2024, 12, 31)}
        # statement pages in that report: balance sheet 67, income 68, cash flow 70-71
        self.assertEqual(pages["total_assets"], 67)
        self.assertEqual(pages["net_income"], 68)
        self.assertEqual(pages["cash_end"], 71)


if __name__ == "__main__":
    unittest.main()
