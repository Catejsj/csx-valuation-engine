"""Statement layouts seen across the exchange's listed companies (2026-11-19 survey)."""
import unittest
from decimal import Decimal

from src.core import checks
from src.core.schema import Currency, Scale, Statement
from src.extract.statements import (UNLABELLED, Row, detect_statement, parse_columns, parse_period_end,
                                    resolve_subtotals)


class Headers(unittest.TestCase):
    def test_curly_apostrophe_means_thousands(self):
        cols = parse_columns(["STATEMENT OF FINANCIAL POSITION", "as at 31 December 2023",
                              "Notes 2023 2022", "KHR’000 KHR’000"])
        self.assertEqual([(c.year, c.currency, c.scale) for c in cols],
                         [(2023, Currency.KHR, Scale.THOUSANDS), (2022, Currency.KHR, Scale.THOUSANDS)])

    def test_straight_apostrophe_and_million(self):
        cols = parse_columns(["2024 2023", "US$ KHR'000 US$ KHR million"])
        self.assertEqual([c.scale for c in cols], [Scale.UNITS, Scale.THOUSANDS, Scale.UNITS, Scale.MILLIONS])

    def test_dot_dates_with_opening_column(self):
        cols = parse_columns(["Group", "31.12.2024 31.12.2023 1.1.2023", "USD USD USD"])
        self.assertEqual([c.year for c in cols], [2024, 2023, 2022])
        self.assertEqual({c.entity for c in cols}, {"group"})

    def test_long_dates(self):
        cols = parse_columns(["Note Unaudited Audited", "31 December 2020 31 December 2019",
                              "USD KHR’000 USD KHR’000"])
        self.assertEqual([(c.year, c.currency) for c in cols],
                         [(2020, Currency.USD), (2020, Currency.KHR), (2019, Currency.USD), (2019, Currency.KHR)])

    def test_group_and_company_side_by_side(self):
        cols = parse_columns(["Note Group Company", "2017 2016 2017 2016", "US$ US$ US$ US$"])
        self.assertEqual([c.entity for c in cols], ["group", "group", "company", "company"])

    def test_company_only_page(self):
        cols = parse_columns(["STATEMENTS OF FINANCIAL POSITION", "AS AT 31 DECEMBER 2024 (continued)",
                              "Company", "31.12.2024 31.12.2023", "USD USD"])
        self.assertEqual({c.entity for c in cols}, {"company"})


class Titles(unittest.TestCase):
    PAGE = ["Registration No:", "Co.0839 Et/2012", "1404", "PHNOM PENH WATER SUPPLY AUTHORITY",
            "(Incorporated in Cambodia)", "STATEMENTS OF FINANCIAL POSITION", "AS AT 31 DECEMBER 2020",
            "Note 2020 2019", "KHR’000 KHR’000", "Property and equipment 5 1,000 900"]

    def test_title_below_registration_block_and_plural(self):
        self.assertIs(detect_statement(self.PAGE), Statement.BALANCE_SHEET)
        self.assertEqual(str(parse_period_end(self.PAGE)), "2020-12-31")

    def test_contents_page_is_not_a_statement(self):
        contents = ["Contents", "Directors' report 1", "Independent auditors' report 4", "Report of x 5",
                    "Statement of financial position 10", "Statement of profit or loss 11"]
        self.assertIsNone(detect_statement(contents))


def row(label, values, section="current assets", problem=""):
    return Row(1, Statement.BALANCE_SHEET, section, label, "", [Decimal(v) for v in values], problem)


class Subtotals(unittest.TestCase):
    def test_unlabelled_total_named_when_it_adds_up(self):
        rows = [row("Inventories", [10, 20]), row("Cash", [5, 5]), row(UNLABELLED, [15, 25], problem="unlabelled subtotal")]
        resolve_subtotals(rows)
        self.assertEqual((rows[2].label, rows[2].problem), ("Total current assets", ""))

    def test_unlabelled_number_that_does_not_add_up_stays_flagged(self):
        rows = [row("Inventories", [10, 20]), row("Cash", [5, 5]), row(UNLABELLED, [19, 25], problem="unlabelled subtotal")]
        resolve_subtotals(rows)
        self.assertEqual(rows[2].problem, "unlabelled subtotal")

    def test_chained_subtotal_after_held_for_sale(self):
        rows = [row("Inventories", [10]), row("Cash", [5]), row(UNLABELLED, [15], problem="unlabelled subtotal"),
                row("Assets held for sale", [3]), row(UNLABELLED, [18], problem="unlabelled subtotal")]
        resolve_subtotals(rows)
        self.assertEqual(rows[2].label, "Subtotal current assets")
        self.assertEqual(rows[4].label, "Total current assets")

    def test_total_equity_is_owners_total_plus_minority(self):
        rows = [("share_capital", "Share capital", "equity", Decimal(100), 1),
                ("retained", "Retained earnings", "equity", Decimal(50), 1),
                ("t1", "Total equity attributable to owners", "equity", Decimal(150), 1),
                ("nci", "Non-controlling interests", "equity", Decimal(-7), 1),
                ("total_equity", "TOTAL EQUITY", "equity", Decimal(143), 1)]
        results = checks.component_checks(Statement.BALANCE_SHEET, "2024 USD", rows)
        self.assertEqual([r.status for r in results], ["pass", "pass"])

    def test_wrong_total_still_fails(self):
        rows = [("a", "A", "equity", Decimal(100), 1), ("t1", "Total owners", "equity", Decimal(100), 1),
                ("nci", "NCI", "equity", Decimal(5), 1), ("t", "TOTAL EQUITY", "equity", Decimal(200), 1)]
        results = checks.component_checks(Statement.BALANCE_SHEET, "2024 USD", rows)
        self.assertEqual(results[-1].status, "fail")


class IncomeSubtotals(unittest.TestCase):
    def test_printed_revenue_total_not_counted_twice(self):
        rows = [("r1", "Revenue from customers", "revenue", Decimal(100), 1),
                ("r2", "Rent income", "revenue", Decimal(20), 1),
                ("tr", "Total revenue", "revenue", Decimal(120), 1),
                ("ex", "Operating expenses", "", Decimal(-70), 1),
                ("operating_profit", "Operating profit", "", Decimal(50), 1)]
        [result] = checks.component_checks(Statement.INCOME, "2023 KHR", rows)
        self.assertEqual(result.status, "pass")


class FailedReportRules(unittest.TestCase):
    """Rules found reviewing the 31 annual reports whose checks failed (2026-11-19)."""

    def test_section_totals_without_the_word_total(self):
        from src.extract.labels import line_items
        labels = [("Property, plant and equipment", "non-current assets", 10), ("Intangible assets", "non-current assets", 2),
                  ("Non-current assets", "non-current assets", 12), ("Inventories", "current assets", 5),
                  ("Cash and cash equivalents", "current assets", 3), ("Current assets", "current assets", 8),
                  ("Total assets", "current assets", 20)]
        rows = [Row(1, Statement.BALANCE_SHEET, sec, lab, "", [Decimal(v)], "") for lab, sec, v in labels]
        names = line_items(Statement.BALANCE_SHEET, rows)
        results = checks.component_checks(Statement.BALANCE_SHEET, "2024 USD",
                                          [(n, r.label, r.section, r.values[0], 1) for n, r in zip(names, rows)])
        self.assertEqual([r.status for r in results], ["pass", "pass", "pass"])

    def test_total_liabilities_and_equity_from_earlier_totals(self):
        rows = [("a", "Share capital", "equity", Decimal(70), 1), ("te", "Total equity", "equity", Decimal(70), 1),
                ("b", "Borrowings", "non-current", Decimal(5), 1), ("t1", "Non-current liabilities", "non-current", Decimal(5), 1),
                ("c", "Payables", "current", Decimal(20), 1), ("t2", "Current liabilities", "current", Decimal(20), 1),
                ("tl", "Total liabilities", "current", Decimal(25), 1),
                ("tel", "Total equity and liabilities", "current", Decimal(95), 1)]
        rows[3] = ("total_non_current_liabilities",) + rows[3][1:]
        rows[5] = ("total_current_liabilities",) + rows[5][1:]
        self.assertEqual({r.status for r in checks.component_checks(Statement.BALANCE_SHEET, "c", rows)}, {"pass"})

    def test_attributable_to_owners_is_a_subtotal(self):
        rows = [("a", "Share capital", "equity", Decimal(100), 1), ("b", "Reserves", "equity", Decimal(50), 1),
                ("o", "Attributable to owners of the Bank", "equity", Decimal(150), 1),
                ("n", "Non-controlling interests", "equity", Decimal(6), 1), ("t", "TOTAL EQUITY", "equity", Decimal(156), 1)]
        self.assertEqual({r.status for r in checks.component_checks(Statement.BALANCE_SHEET, "c", rows)}, {"pass"})

    def test_total_expenses_below_revenue(self):
        rows = [("revenue", "Revenue", "", Decimal(300), 1), ("e1", "Supplies", "", Decimal(-80), 1),
                ("e2", "Salaries", "", Decimal(-90), 1), ("te", "Total expenses", "", Decimal(-170), 1),
                ("operating_profit", "Operating profit", "", Decimal(130), 1)]
        [result] = checks.component_checks(Statement.INCOME, "c", rows)
        self.assertEqual(result.status, "pass")

    def test_revenue_printed_as_a_subtotal_then_gross_profit(self):
        rows = [("r1", "Revenue from contracts with customers", "", Decimal(44), 1), ("r2", "Rental income", "", Decimal(1), 1),
                ("revenue", "Revenue", "", Decimal(45), 1), ("cos", "Cost of sales", "", Decimal(-22), 1),
                ("gp", "Gross profit", "", Decimal(23), 1), ("ad", "Administrative expenses", "", Decimal(-10), 1),
                ("operating_profit", "Operating profit", "", Decimal(13), 1)]
        [result] = checks.component_checks(Statement.INCOME, "c", rows)
        self.assertEqual(result.status, "pass")

    def test_investing_rows_named_net_still_count(self):
        rows = [("ppe", "Purchase of property, plant and equipment", "investing", Decimal(-10), 1),
                ("x", "Net repayment from shareholder", "investing", Decimal(2), 1),
                ("y", "Net cash inflow on acquisition of a subsidiary", "investing", Decimal(1), 1),
                ("cfi", "Net cash used in investing activities", "investing", Decimal(-7), 1)]
        [result] = checks.component_checks(Statement.CASH_FLOW, "c", rows)
        self.assertEqual(result.status, "pass")

    def test_fx_adjustment_in_operating_section_is_not_the_cash_effect(self):
        from src.extract.labels import line_items
        labels = ["Net currency translation differences", "Net increase in cash and cash equivalents",
                  "Cash and cash equivalents at the beginning of the year", "Exchange differences"]
        rows = [Row(1, Statement.CASH_FLOW, "", lab, "", [Decimal(1)], "") for lab in labels]
        names = line_items(Statement.CASH_FLOW, rows)
        self.assertNotEqual(names[0], "fx_effect_on_cash")
        self.assertEqual(names[3], "fx_effect_on_cash")


class WrappedLabelHeading(unittest.TestCase):
    def test_total_adds_up_across_a_mistaken_heading(self):
        # "Placements with the National" / "Bank of Cambodia 213 ..." split the asset rows (Woori 2024)
        rows = [("cash", "Cash on hand", "assets", Decimal(18), 12),
                ("bank_of_cambodia", "Bank of Cambodia", "placements with the national", Decimal(213), 12),
                ("loans", "Loans", "placements with the national", Decimal(1186), 12),
                ("total_assets", "TOTAL ASSETS", "placements with the national", Decimal(1417), 12)]
        self.assertEqual([r.status for r in checks.component_checks(Statement.BALANCE_SHEET, "c", rows)], ["pass"])

    def test_wrong_total_still_fails_across_headings(self):
        rows = [("cash", "Cash", "assets", Decimal(18), 1), ("x", "Loans", "other", Decimal(100), 1),
                ("total_assets", "TOTAL ASSETS", "other", Decimal(200), 1)]
        self.assertEqual(checks.component_checks(Statement.BALANCE_SHEET, "c", rows)[-1].status, "fail")


class QuarterlyColumns(unittest.TestCase):
    """Interim reports: column dates, period lengths, and which columns are stored."""

    def test_three_and_nine_month_columns(self):
        cols = parse_columns(["Three-month period ended Nine-month period ended",
                              "30 September 2026 30 September 2025 30 September 2026 30 September 2025",
                              "Notes USD KHR’000 USD KHR’000 USD KHR’000 USD KHR’000"])
        self.assertEqual([c.months for c in cols], [3, 3, 3, 3, 9, 9, 9, 9])
        self.assertEqual({(c.end_month, c.end_day) for c in cols}, {(9, 30)})

    def test_balance_sheet_against_year_end_with_audit_line_between(self):
        cols = parse_columns(["Notes 30 September 2026 31 December 2025", "Unaudited Audited",
                              "US$ KHR’000 US$ KHR’000"])
        self.assertEqual([(c.year, c.end_month, c.end_day) for c in cols],
                         [(2026, 9, 30), (2026, 9, 30), (2025, 12, 31), (2025, 12, 31)])

    def test_months_on_a_line_above_the_years(self):
        cols = parse_columns(["Three-month period ended Nine-month period ended",
                              "30 September 30 September 30 September 30 September", "2026 2025 2026 2025",
                              "Notes KHR’000 KHR’000 KHR’000 KHR’000"])
        self.assertEqual([(c.year, c.months, c.end_month) for c in cols],
                         [(2026, 3, 9), (2025, 3, 9), (2026, 9, 9), (2025, 9, 9)])

    def test_only_the_current_quarter_is_stored(self):
        from datetime import date
        from src.extract.load import build
        from src.extract.statements import StatementTable
        cols = parse_columns(["Three-month period ended Six-month period ended",
                              "30 June 2026 30 June 2025 30 June 2026 30 June 2025", "USD USD USD USD"])
        table = StatementTable(Statement.INCOME, date(2026, 6, 30), cols,
                               [Row(5, Statement.INCOME, "", "Revenue", "", [Decimal(v) for v in (10, 9, 25, 20)])], [5])
        facts = build([table], "T", "T-Q2-2026", "quarterly").facts
        self.assertEqual(sorted((f.period_type.value if hasattr(f.period_type, "value") else f.period_type,
                                 str(f.period_end), f.value_as_reported) for f in facts),
                         [("H1", "2026-06-30", Decimal(25)), ("Q2", "2026-06-30", Decimal(10))])

    def test_nine_month_year_to_date_stored(self):
        from datetime import date
        from src.extract.load import build
        from src.extract.statements import StatementTable
        cols = parse_columns(["Three-month period ended Nine-month period ended",
                              "30 September 2026 30 September 2025 30 September 2026 30 September 2025",
                              "USD USD USD USD"])
        table = StatementTable(Statement.INCOME, date(2026, 9, 30), cols,
                               [Row(5, Statement.INCOME, "", "Revenue", "", [Decimal(v) for v in (10, 9, 30, 27)])], [5])
        got = sorted((str(getattr(f.period_type, "value", f.period_type)), f.value_as_reported)
                     for f in build([table], "T", "T-Q3-2026", "quarterly").facts)
        self.assertEqual(got, [("9M", Decimal(30)), ("Q3", Decimal(10))])

    def test_half_year_and_year_end_comparative(self):
        from datetime import date
        from src.extract.load import build
        from src.extract.statements import StatementTable
        bs_cols = parse_columns(["30 June 2026 31 December 2025", "USD USD"])
        is_cols = parse_columns(["Six-month period ended", "30 June 2026 30 June 2025", "USD USD"])
        bs = StatementTable(Statement.BALANCE_SHEET, date(2026, 6, 30), bs_cols,
                            [Row(3, Statement.BALANCE_SHEET, "", "Cash", "", [Decimal(5), Decimal(4)])], [3])
        inc = StatementTable(Statement.INCOME, date(2026, 6, 30), is_cols,
                             [Row(4, Statement.INCOME, "", "Revenue", "", [Decimal(7), Decimal(6)])], [4])
        facts = build([bs, inc], "T", "T-H1-2026", "semiannual").facts
        got = sorted((f.statement.value, str(f.period_end), str(getattr(f.period_type, "value", f.period_type))) for f in facts)
        self.assertEqual(got, [("BS", "2026-06-30", "H1"), ("IS", "2026-06-30", "H1")])


if __name__ == "__main__":
    unittest.main()
