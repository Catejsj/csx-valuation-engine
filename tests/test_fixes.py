"""Fixing upload mistakes, quarterly periods, the report numbers, and the AI number guard."""
import os
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from src.core import store
from src.core.schema import Filing, Issuer
from src.intake import upload
from src.report.build import build_report, fmt_money, placeholders
from tests.test_extract import BS_PAGE
from tests.test_intake import make_pdf
from tests.test_schema import SHA, make_fact


class UploadFixes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("CSX_DATA_DIR")
        os.environ["CSX_DATA_DIR"] = self.tmp.name
        self.conn = store.connect(Path(self.tmp.name) / "facts.sqlite")
        store.init_db(self.conn)
        pdf = Path(self.tmp.name) / "r.pdf"
        pdf.write_bytes(make_pdf(BS_PAGE))
        self.r = upload.ingest(pdf, "TYPO", "Tset Plc", conn=self.conn)
        self.assertTrue(self.r.accepted)

    def tearDown(self):
        self.conn.close()
        if self.old is None:
            os.environ.pop("CSX_DATA_DIR", None)
        else:
            os.environ["CSX_DATA_DIR"] = self.old
        self.tmp.cleanup()

    def test_wrong_company_code_moved(self):
        new_id = store.move_filing(self.conn, self.r.filing_id, "test", "Test Plc")
        self.assertTrue(new_id.startswith("TEST-UP-"))
        self.assertIsNone(self.conn.execute("SELECT 1 FROM issuers WHERE id='TYPO'").fetchone())   # empty one removed
        n = self.conn.execute("SELECT COUNT(*) FROM facts WHERE filing_id=? AND issuer_id='TEST'", (new_id,)).fetchone()[0]
        self.assertEqual(n, self.r.facts_stored)

    def test_bad_code_rejected(self):
        with self.assertRaises(ValueError):
            store.move_filing(self.conn, self.r.filing_id, "../x", "X")
        with self.assertRaises(ValueError):
            store.move_filing(self.conn, self.r.filing_id, "NEWCO", "")   # new company needs a name

    def test_rename_and_kind(self):
        store.rename_company(self.conn, "TYPO", "Test Plc")
        self.assertEqual(self.conn.execute("SELECT name FROM issuers WHERE id='TYPO'").fetchone()[0], "Test Plc")
        store.update_filing(self.conn, self.r.filing_id, kind="quarterly")
        types = {r[0] for r in self.conn.execute("SELECT DISTINCT period_type FROM facts WHERE filing_id=?",
                                                  (self.r.filing_id,))}
        self.assertEqual(types, {"Q4"})        # 31 December -> Q4

    def test_delete_upload(self):
        store.delete_upload(self.conn, self.r.filing_id)
        self.assertEqual(store.counts(self.conn)["facts"], 0)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM issuers WHERE id='TYPO'").fetchone())

    def test_official_filings_are_protected(self):
        store.add_issuer(self.conn, Issuer(id="OFF", name="Official Plc"))
        store.add_filing(self.conn, Filing(id="OFF-AR", issuer_id="OFF", kind="annual",
                                           source_url="https://example.org/a.pdf", pdf_sha256=SHA,
                                           published_on=date(2026, 3, 31)))
        for action in (lambda: store.delete_upload(self.conn, "OFF-AR"),
                       lambda: store.move_filing(self.conn, "OFF-AR", "X", "X"),
                       lambda: store.update_filing(self.conn, "OFF-AR", kind="quarterly"),
                       lambda: store.rename_company(self.conn, "OFF", "Other")):
            with self.assertRaises(store.NotEditable):
                action()


class PeriodTypes(unittest.TestCase):
    def test_kind_to_period(self):
        self.assertEqual(store.period_type_for("annual", date(2025, 12, 31)), "FY")
        self.assertEqual(store.period_type_for("semiannual", date(2025, 6, 30)), "H1")
        self.assertEqual(store.period_type_for("quarterly", date(2025, 3, 31)), "Q1")
        self.assertEqual(store.period_type_for("quarterly", date(2025, 9, 30)), "Q3")


class ReportNumbers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(Path(self.tmp.name) / "t.sqlite")
        store.init_db(self.conn)
        store.add_issuer(self.conn, Issuer(id="TEST", name="Test Plc", reporting_currency="USD"))
        store.add_filing(self.conn, Filing(id="F", issuer_id="TEST", kind="annual",
                                           source_url="https://example.org/a.pdf", pdf_sha256=SHA,
                                           published_on=date(2026, 3, 31)))
        rows = {("IS", "revenue"): ("1000", "800"), ("IS", "net_income"): ("100", "80"),
                ("IS", "operating_profit"): ("200", "150"), ("CF", "cfo"): ("150", "120"),
                ("CF", "capex"): ("-50", "-40"), ("BS", "total_assets"): ("2000", "1800"),
                ("BS", "total_equity"): ("1000", "900"), ("BS", "cash_and_equivalents"): ("100", "50"),
                ("BS", "borrowings_current"): ("100", "100"), ("BS", "borrowings_non_current"): ("300", "200")}
        for (stmt, item), (now, before) in rows.items():
            for year, v in ((2025, now), (2024, before)):
                store.add_fact(self.conn, make_fact(filing_id="F", statement=stmt, line_item=item,
                                                    period_end=date(year, 12, 31), value_as_reported=v, scale=1))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def metric(self, report, key, year=2025):
        m = next(x for x in report["lines"] + report["ratios"] if x["key"] == key)
        v = m["values"][str(year)]
        return None if v is None else Decimal(v)

    def test_derived_values(self):
        r = build_report(self.conn, "TEST")
        self.assertEqual(r["years"], [2024, 2025])
        self.assertEqual(self.metric(r, "fcf"), Decimal(100))            # 150 + (-50)
        self.assertEqual(self.metric(r, "debt"), Decimal(400))           # 100 + 300
        self.assertEqual(self.metric(r, "net_debt"), Decimal(300))       # 400 - 100
        self.assertEqual(self.metric(r, "revenue_growth"), Decimal("0.25"))
        self.assertEqual(self.metric(r, "net_margin"), Decimal("0.1"))
        self.assertAlmostEqual(float(self.metric(r, "roe")), 100 / 950)  # average equity
        self.assertIsNone(self.metric(r, "revenue_growth", 2024))        # no 2023: not guessed
        self.assertIn("finance_costs", r["not_found"])                   # interest cover needs it

    def test_placeholders_are_formatted_by_code(self):
        ph = placeholders(build_report(self.conn, "TEST"))
        self.assertEqual(ph["revenue"], "$1.0K")
        self.assertEqual(ph["revenue_growth"], "25.0%")
        self.assertEqual(fmt_money(Decimal("172066105"), "USD"), "$172.1M")
        self.assertEqual(fmt_money(Decimal("-30551701"), "USD"), "-$30.6M")


class IncludeSwitch(unittest.TestCase):
    """Switched-off files stay saved but stop counting in numbers and coverage."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(Path(self.tmp.name) / "t.sqlite")
        store.init_db(self.conn)
        store.add_issuer(self.conn, Issuer(id="TEST", name="Test Plc", reporting_currency="USD"))
        for fid, pub, years, rev in (("OLD", date(2025, 3, 31), (2024, 2023), ("800", "700")),
                                     ("NEW", date(2026, 3, 31), (2025, 2024), ("1000", "820"))):
            store.add_filing(self.conn, Filing(id=fid, issuer_id="TEST", kind="annual",
                                               source_url="https://example.org/a.pdf", pdf_sha256=SHA,
                                               published_on=pub))
            for y, v in zip(years, rev):
                for stmt, item, val in (("IS", "revenue", v), ("IS", "net_income", "10"), ("BS", "total_assets", "99"),
                                        ("CF", "cfo", "5")):
                    store.add_fact(self.conn, make_fact(filing_id=fid, statement=stmt, line_item=item,
                                                        period_end=date(y, 12, 31), value_as_reported=val, scale=1))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def revenue(self, year):
        r = build_report(self.conn, "TEST")
        return r["years"], next(m for m in r["lines"] if m["key"] == "revenue")["values"].get(str(year))

    def test_newest_filing_wins_and_switch_off_falls_back(self):
        years, v2024 = self.revenue(2024)
        self.assertEqual(years, [2023, 2024, 2025])
        self.assertEqual(v2024, "820")                 # restated value from the newer report
        store.set_included(self.conn, "NEW", False)
        years, v2024 = self.revenue(2024)
        self.assertEqual(years, [2023, 2024])          # 2025 gone with the switched-off report
        self.assertEqual(v2024, "800")                 # back to the older report's figure
        self.assertEqual(store.counts(self.conn)["facts"], 16)   # nothing deleted
        store.set_included(self.conn, "NEW", True)
        self.assertEqual(self.revenue(2024)[1], "820")

    def test_unknown_filing(self):
        with self.assertRaises(KeyError):
            store.set_included(self.conn, "NOPE", False)


class ValueFixes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(Path(self.tmp.name) / "t.sqlite")
        store.init_db(self.conn)
        store.add_issuer(self.conn, Issuer(id="TEST", name="Test Plc"))
        store.add_filing(self.conn, Filing(id="F", issuer_id="TEST", kind="annual",
                                           source_url="https://example.org/a.pdf", pdf_sha256=SHA,
                                           published_on=date(2026, 3, 31)))
        self.fid = store.add_fact(self.conn, make_fact(filing_id="F", value_as_reported="1000"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_saving_the_same_value_changes_nothing(self):
        with self.assertRaises(store.Unchanged):
            store.correct_fact(self.conn, self.fid, "1,000", "me")
        self.assertEqual(store.events_for(self.conn, self.fid), [])
        self.assertEqual(store.fact_by_id(self.conn, self.fid).check_status.value, "proposed")

    def test_restore_original_after_two_fixes(self):
        store.correct_fact(self.conn, self.fid, "1600", "me")
        store.correct_fact(self.conn, self.fid, "1700", "me")
        self.assertEqual(store.original_reading(self.conn, self.fid), "1000")
        store.restore_original(self.conn, self.fid, "me")
        fact = store.fact_by_id(self.conn, self.fid)
        self.assertEqual(fact.value_as_reported, Decimal(1000))
        self.assertEqual(fact.check_status.value, "proposed")      # back to "not checked yet"
        with self.assertRaises(store.Unchanged):
            store.restore_original(self.conn, self.fid, "me")       # already original

    def test_restore_without_fix(self):
        with self.assertRaises(store.Unchanged):
            store.restore_original(self.conn, self.fid, "me")


class StatementLabels(unittest.TestCase):
    """Detected statements are shown, and a person can relabel pages of an upload."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("CSX_DATA_DIR")
        os.environ["CSX_DATA_DIR"] = self.tmp.name
        self.conn = store.connect(Path(self.tmp.name) / "facts.sqlite")
        store.init_db(self.conn)
        self.pdf = Path(self.tmp.name) / "r.pdf"
        self.pdf.write_bytes(make_pdf(BS_PAGE))
        self.r = upload.ingest(self.pdf, "TEST", "Test Plc", conn=self.conn)

    def tearDown(self):
        self.conn.close()
        if self.old is None:
            os.environ.pop("CSX_DATA_DIR", None)
        else:
            os.environ["CSX_DATA_DIR"] = self.old
        self.tmp.cleanup()

    def test_found_statements_reported(self):
        self.assertEqual([s["statement"] for s in self.r.statements], ["BS"])
        summary = store.statements_summary(self.conn, self.r.filing_id)
        self.assertEqual(summary[0]["name"], "Balance sheet")
        self.assertEqual(summary[0]["pages"], [1])
        self.assertEqual(summary[0]["years"], ["2024", "2025"])

    def test_force_pages(self):
        from src.core.schema import Statement
        from src.extract.statements import extract_statements
        self.assertEqual(extract_statements(self.pdf, {1: None}), [])                       # "not a statement"
        [t] = extract_statements(self.pdf, {1: Statement.INCOME})                           # relabelled
        self.assertIs(t.statement, Statement.INCOME)

    def test_remove_one_statement_from_upload(self):
        n = store.remove_statement(self.conn, self.r.filing_id, "BS")
        self.assertEqual(n, self.r.facts_stored)
        self.assertEqual(store.statements_summary(self.conn, self.r.filing_id), [])
        with self.assertRaises(KeyError):
            store.remove_statement(self.conn, self.r.filing_id, "BS")


class BankReport(unittest.TestCase):
    """A bank prints no revenue: it gets operating income, cost to income, cost of risk and the like."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(Path(self.tmp.name) / "t.sqlite")
        store.init_db(self.conn)
        store.add_issuer(self.conn, Issuer(id="BNKX", name="Bank Plc", reporting_currency="USD"))
        store.add_filing(self.conn, Filing(id="F", issuer_id="BNKX", kind="annual",
                                           source_url="https://example.org/a.pdf", pdf_sha256=SHA,
                                           published_on=date(2026, 3, 31)))
        rows = {("IS", "net_interest_income"): ("500", "400"), ("IS", "net_fee_and_commission_income"): ("80", "70"),
                ("IS", "other_income_net"): ("20", "30"), ("IS", "net_impairment_losses"): ("-50", "-40"),
                ("IS", "profit_before_tax"): ("250", "200"), ("IS", "net_income"): ("200", "160"),
                ("BS", "loans_and_advances_net"): ("6000", "5000"), ("BS", "deposits_from_customers"): ("7500", "6000"),
                ("BS", "total_assets"): ("10000", "9000"), ("BS", "total_equity"): ("1500", "1300"),
                ("CF", "cfo"): ("900", "-300")}
        for (stmt, item), (now, before) in rows.items():
            for year, v in ((2025, now), (2024, before)):
                store.add_fact(self.conn, make_fact(issuer_id="BNKX", filing_id="F", statement=stmt, line_item=item,
                                                    period_end=date(year, 12, 31), value_as_reported=v, scale=1))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_bank_measures(self):
        r = build_report(self.conn, "BNKX")
        self.assertEqual(r["kind"], "bank")
        get = lambda k: Decimal(next(m for m in r["lines"] + r["ratios"] if m["key"] == k)["values"]["2025"])  # noqa: E731
        self.assertEqual(get("operating_income"), Decimal(600))            # 500 + 80 + 20
        self.assertEqual(get("operating_costs"), Decimal(-300))            # -(600 - 50 - 250)
        self.assertEqual(get("cost_to_income"), Decimal("0.5"))
        self.assertAlmostEqual(float(get("cost_of_risk")), 50 / 5500)      # average loans
        self.assertEqual(get("loans_to_deposits"), Decimal("0.8"))
        keys = {m["key"] for m in r["lines"] + r["ratios"]}
        self.assertFalse(keys & {"revenue", "ebitda", "net_debt", "current_ratio", "cash_conversion"})
        self.assertNotIn("revenue", r["not_found"])
