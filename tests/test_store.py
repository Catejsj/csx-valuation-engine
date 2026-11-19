import os
import sqlite3
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from src.core import paths, store
from src.core.schema import Filing, FxRate, Issuer, Price
from tests.test_schema import SHA, make_fact


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(Path(self.tmp.name) / "t.sqlite")
        store.init_db(self.conn)
        store.add_issuer(self.conn, Issuer(id="TEST", name="Test Plc", reporting_currency="KHR"))
        for fid, pub in (("TEST-AR2025", date(2026, 3, 31)), ("TEST-AR2026", date(2027, 3, 31))):
            store.add_filing(self.conn, Filing(
                id=fid, issuer_id="TEST", kind="annual", source_url=f"https://example.org/{fid}.pdf",
                pdf_sha256=SHA, published_on=pub))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_init_twice_is_safe(self):
        store.init_db(self.conn)
        self.assertEqual(store.counts(self.conn)["issuers"], 1)

    def test_roundtrip_exact(self):
        fact = make_fact(value_as_reported="1234567.891")
        store.add_fact(self.conn, fact)
        [back] = store.facts_for(self.conn, "TEST")
        self.assertEqual(back, fact)
        self.assertEqual(back.value_as_reported, Decimal("1234567.891"))

    def test_duplicate_rejected(self):
        store.add_fact(self.conn, make_fact())
        with self.assertRaises(sqlite3.IntegrityError):
            store.add_fact(self.conn, make_fact(value_as_reported="1"))

    def test_same_line_in_two_statements_allowed(self):
        store.add_fact(self.conn, make_fact(statement="BS", line_item="cash"))
        store.add_fact(self.conn, make_fact(statement="CF", line_item="cash"))
        self.assertEqual(len(store.facts_for(self.conn, "TEST", "cash")), 2)

    def test_restatement_latest_filing_wins(self):
        # FY2025 printed in the 2025 report, then restated as a comparative in the 2026 report.
        store.add_fact(self.conn, make_fact(filing_id="TEST-AR2025", value_as_reported="100", page=10))
        store.add_fact(self.conn, make_fact(filing_id="TEST-AR2026", value_as_reported="105", page=12))
        latest = store.latest_fact(self.conn, "TEST", "total_assets", date(2025, 12, 31))
        self.assertEqual(latest.value_as_reported, Decimal("105"))
        self.assertEqual(latest.filing_id, "TEST-AR2026")
        self.assertEqual(len(store.facts_for(self.conn, "TEST", "total_assets")), 2)  # old value kept

    def test_failed_fact_skipped(self):
        store.add_fact(self.conn, make_fact(filing_id="TEST-AR2025", value_as_reported="100"))
        store.add_fact(self.conn, make_fact(filing_id="TEST-AR2026", value_as_reported="999999", check_status="failed"))
        self.assertEqual(store.latest_fact(self.conn, "TEST", "total_assets", date(2025, 12, 31)).value_as_reported,
                         Decimal("100"))

    def test_missing_returns_none(self):
        self.assertIsNone(store.latest_fact(self.conn, "TEST", "revenue", date(2025, 12, 31)))

    def test_unknown_filing_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            store.add_fact(self.conn, make_fact(filing_id="NOPE"))

    def test_fx_and_price(self):
        store.add_fx_rate(self.conn, FxRate(on=date(2025, 12, 31), base="USD", quote="KHR",
                                            rate="4010.5", source_url="https://example.org/fx"))
        store.add_price(self.conn, Price(issuer_id="TEST", on=date(2026, 1, 5), close="6200",
                                         volume=120, currency="KHR", source_url="https://example.org/p"))
        self.assertEqual(store.counts(self.conn)["fx_rates"], 1)
        self.assertEqual(store.counts(self.conn)["prices"], 1)


class PathTests(unittest.TestCase):
    def test_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested"
            old = os.environ.get("CSX_DATA_DIR")
            os.environ["CSX_DATA_DIR"] = str(target)
            try:
                self.assertEqual(paths.data_dir(), target)
                self.assertTrue(target.is_dir())
                self.assertEqual(paths.db_path(), target / "facts.sqlite")
            finally:
                if old is None:
                    os.environ.pop("CSX_DATA_DIR", None)
                else:
                    os.environ["CSX_DATA_DIR"] = old


if __name__ == "__main__":
    unittest.main()
