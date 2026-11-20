"""Regression tests for the 2026-11-20 calculation audit: each test names the problem it guards against."""
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.core import store
from src.core.schema import Filing, Issuer
from src.export.standard import standard_code
from src.value import models
from tests.test_schema import make_fact
from tests.test_value import REAL_CONFIG, _company, _industrial


class CodeMapping(unittest.TestCase):
    """Wordings the valuation depends on must map to the standard codes; look-alikes must not."""

    def test_cash_flow_wordings(self):
        cases = {
            "acquisition_of_property_and_equipment": "capex",
            "acquisitions_of_property_plant_and_equipment": "capex",
            "acquisition_of_intangible_asset": "purchase_of_intangibles",
            "purchases_of_investment_properties": "purchase_of_investment_property",
            "proceed_from_borrowing": "proceeds_from_borrowings",
            "drawdown_of_loans": "proceeds_from_borrowings",
            "repayment_of_bank_borrowings": "repayments_of_borrowings",
            "proceeds_from_issuance_of_green_bond": "proceeds_from_debt_securities",
            "repayments_of_corporate_bonds": "repayments_of_debt_securities",
            "payment_of_principal_portion_of_lease_liabilities": "lease_payments",
            "dividends_paid_4_2_1": "dividends_paid",
        }
        for stored, code in cases.items():
            self.assertEqual(standard_code("CF", stored), (code, True), stored)

    def test_look_alikes_are_not_caught(self):
        for stored in ("proceeds_from_disposal_of_property_plant_and_equipment",
                       "acquisition_of_property_plant_and_equipment_through_liabilities_from_joint_management",
                       "depreciation_of_property_plant_and_equipment"):
            self.assertFalse(standard_code("CF", stored)[1], stored)

    def test_balance_sheet_wordings(self):
        self.assertEqual(standard_code("BS", "loans_and_borrowings_current"), ("borrowings_current", True))
        self.assertEqual(standard_code("BS", "cash_and_bank_balances_current"), ("cash_and_equivalents_current", True))
        self.assertEqual(standard_code("BS", "corporate_bond_payable"), ("debt_securities", True))
        self.assertEqual(standard_code("BS", "total_current_assets_current"), ("total_current_assets", True))
        self.assertEqual(standard_code("BS", "borrowings_net_of_current_portion"), ("borrowings_non_current", True))


class OneReportPerStatement(unittest.TestCase):
    """Never mix lines from two versions of the same statement (MJQE 2023, GTI 2022, DBDE 2024)."""

    def test_newer_report_supplies_every_line(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        conn = store.connect(Path(tmp.name) / "t.sqlite")
        store.init_db(conn)
        store.add_issuer(conn, Issuer(id="MIX", name="Mix Plc"))
        for fid, pub, rows in (("MIX-AR2023", date(2024, 3, 31), [("total_assets", 100), ("borrowings", 40)]),
                               ("MIX-AR2024", date(2025, 3, 31), [("total_assets", 98), ("borrowings_current", 10),
                                                                  ("borrowings_non_current", 30)])):
            store.add_filing(conn, Filing(id=fid, issuer_id="MIX", kind="annual", source_url="https://example.org/x.pdf",
                                          pdf_sha256=__import__("hashlib").sha256(fid.encode()).hexdigest(), published_on=pub))
            for item, v in rows:
                store.add_fact(conn, make_fact(issuer_id="MIX", filing_id=fid, statement="BS", period_end=date(2023, 12, 31),
                                               line_item=item, value_as_reported=str(v), currency="USD", scale=1))
            store.add_fact(conn, make_fact(issuer_id="MIX", filing_id=fid, statement="BS", period_end=date(pub.year - 1, 12, 31),
                                           line_item="cash_and_equivalents", value_as_reported="1", currency="USD", scale=1)) \
                if pub.year == 2025 else None
        from src.value import inputs as inp
        y2023 = next(y for y in inp.years(conn, "MIX", official_only=False) if y.end.startswith("2023"))
        self.assertEqual(y2023.get("total_assets"), 98)                 # the restated figure
        self.assertEqual(y2023.sum_of("borrowings"), 40)                # not 40 + 10 + 30
        conn.close()
        tmp.cleanup()


class Models(unittest.TestCase):
    def test_fading_excess_return(self):
        # one year of excess return 10, then fading by 0.5 a year at ke 10%: terminal = 10 x 0.5 / (1.1 - 0.5)
        r = models.residual_income(100, 0.20, 0.10, 0.0, 1, 0.0, persistence=0.5)
        self.assertAlmostEqual(r.value, 100 + 10 / 1.1 + (10 * 0.5 / 0.6) / 1.1)
        self.assertLess(r.value, models.residual_income(100, 0.20, 0.10, 0.0, 1, 0.0).value)   # less than forever


class EngineFixes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.conn = store.connect(Path(self.tmp.name) / "t.sqlite")
        store.init_db(self.conn)
        self.cfg = Path(self.tmp.name) / "config"
        shutil.copytree(REAL_CONFIG, self.cfg)
        with open(self.cfg / "sectors.toml", "a") as f:
            f.write("")
        text = (self.cfg / "sectors.toml").read_text()
        text = text.replace("[companies]", '[companies]\nIND = { sector = "Test", model = "fcff", industry = "Total Market" }\n'
                                           'INT = { sector = "Test", model = "fcff", industry = "Total Market" }\n'
                                           'DEC = { sector = "Test", model = "fcff", industry = "Total Market" }', 1)
        (self.cfg / "sectors.toml").write_text(text)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def run_value(self, cid):
        from src.value.assumptions import load
        from src.value.engine import value
        return value(self.conn, cid, assumptions=load(config_dir=self.cfg))

    def test_intangibles_bought_are_investment(self):
        _company(self.conn, "IND", [2022, 2023, 2024], _industrial)
        _company(self.conn, "INT", [2022, 2023, 2024], lambda y: _industrial(y) + [("CF", "purchase_of_intangible_assets", -30)])
        self.assertLess(self.run_value("INT")["value"]["base"], self.run_value("IND")["value"]["base"])
        self.assertTrue(any("intangibles" in s for s, _ in self.run_value("INT")["steps"]))

    def test_shrinking_revenue_is_projected_to_shrink(self):
        _company(self.conn, "DEC", [2022, 2023, 2024],
                 lambda y: [x if x[1] != "revenue" else ("IS", "revenue", 600 - 40 * (y - 2021)) for x in _industrial(y)])
        g = next(v for s, v in self.run_value("DEC")["steps"] if s.startswith("first-stage"))
        self.assertLess(g, 0)
