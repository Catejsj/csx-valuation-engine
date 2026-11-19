"""Value lens: formulas against hand-worked numbers, directions, and the engine's refusals."""
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.core import store
from src.core.schema import Filing, Issuer
from src.value import models
from tests.test_schema import make_fact

REAL_CONFIG = Path(__file__).resolve().parents[1] / "config"


class Formulas(unittest.TestCase):
    def test_hand_worked(self):
        self.assertAlmostEqual(models.cost_of_equity(0.05, 1.0, 0.04, 0.07), 0.16)
        self.assertAlmostEqual(models.relever(0.5, 0.2, 1.0), 0.9)
        self.assertAlmostEqual(models.wacc(0.16, 0.10, 0.2, 50, 50), 0.12)
        self.assertAlmostEqual(models.cagr(100, 121, 2), 0.10)

    def test_no_growth_is_a_perpetuity(self):
        # 100 a year forever at 10% = 1,000, whichever way it is split into stages
        self.assertAlmostEqual(models.fcff_value(100, 0.0, 1, 0.0, 0.10).value, 1000)
        self.assertAlmostEqual(models.fcff_value(100, 0.0, 5, 0.0, 0.10).value, 1000)
        self.assertAlmostEqual(models.dividend_value(10, 0.0, 3, 0.0, 0.10).value, 100)

    def test_residual_income_matches_justified_pb(self):
        self.assertAlmostEqual(models.residual_income(100, 0.10, 0.10, 0.0, 5, 0.0).value, 100)   # no excess return
        self.assertAlmostEqual(models.residual_income(100, 0.20, 0.10, 0.0, 1, 0.0).value, 200)
        self.assertAlmostEqual(models.justified_pb(0.20, 0.10, 0.0) * 100, 200)

    def test_directions(self):
        base = models.fcff_value(100, 0.05, 5, 0.025, 0.12).value
        self.assertLess(models.fcff_value(100, 0.05, 5, 0.025, 0.13).value, base)    # dearer capital, lower value
        self.assertGreater(models.fcff_value(100, 0.05, 5, 0.03, 0.12).value, base)  # more growth, higher value
        self.assertGreater(models.fcff_value(100, 0.06, 5, 0.025, 0.12).value, base)
        rb = models.residual_income(100, 0.15, 0.12, 0.05, 5, 0.025).value
        self.assertLess(models.residual_income(100, 0.15, 0.13, 0.05, 5, 0.025).value, rb)

    def test_growth_at_or_above_the_rate_is_refused(self):
        with self.assertRaises(ValueError):
            models.fcff_value(100, 0.05, 5, 0.10, 0.10)


def _company(conn, cid, model_years, lines, origin="official"):
    store.add_issuer(conn, Issuer(id=cid, name=f"{cid} Plc"))
    for y in model_years:
        fid = f"{cid}-AR{y}"
        store.add_filing(conn, Filing(id=fid, issuer_id=cid, kind="annual", source_url=f"https://example.org/{fid}.pdf",
                                      pdf_sha256=__import__("hashlib").sha256(fid.encode()).hexdigest(),
                                      published_on=date(y + 1, 3, 31), origin=origin))
        for st, item, val in lines(y):
            store.add_fact(conn, make_fact(issuer_id=cid, filing_id=fid, statement=st, period_end=date(y, 12, 31),
                                           line_item=item, label_as_printed=item, value_as_reported=str(val),
                                           currency="USD", scale=1, page=5))


def _industrial(y):
    k = y - 2021
    return [("BS", "total_assets", 1000), ("BS", "total_equity", 600 + 10 * k), ("BS", "borrowings", 200),
            ("BS", "cash_and_equivalents", 50), ("IS", "revenue", 500 + 25 * k), ("IS", "net_income", 60),
            ("IS", "finance_costs", -16), ("CF", "cfo", 120), ("CF", "purchase_of_property_and_equipment", -40),
            ("CF", "interest_paid", -16)]


class Engine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(Path(self.tmp.name) / "t.sqlite")
        store.init_db(self.conn)
        self.cfg = Path(self.tmp.name) / "config"
        shutil.copytree(REAL_CONFIG, self.cfg)
        self.add_sector('IND = { sector = "Test", model = "fcff", industry = "Total Market" }')
        _company(self.conn, "IND", [2022, 2023, 2024], _industrial)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add_sector(self, line):
        p = self.cfg / "sectors.toml"
        p.write_text(p.read_text().replace("[companies]\n", f"[companies]\n{line}\n", 1))

    def run_value(self, cid, **kw):
        from src.value.assumptions import load
        from src.value.engine import value
        return value(self.conn, cid, assumptions=load(config_dir=self.cfg), **kw)

    def test_unchecked_inputs_are_used_and_listed(self):
        r = self.run_value("IND")
        self.assertEqual(r["status"], "ok")
        v = r["value"]
        self.assertLess(v["bear"], v["base"])
        self.assertLess(v["base"], v["bull"])
        self.assertIn("risk_free_rate", r["unverified_inputs"])
        self.assertTrue(r["notes"][0].startswith("Inputs not checked by a person yet"))
        self.assertTrue(any(s.startswith("FCFF 2024 = CFO 120 - capex 40") for s, _ in r["steps"]))
        # no share price: value only, the unlisted liquidity band, no market comparison
        self.assertIsNone(r["market"])
        self.assertEqual(r["liquidity"]["band"], "unlisted")

    def test_checked_inputs_carry_no_note(self):
        for name in ("market.toml", "countries/KH.toml", "sectors.toml"):
            p = self.cfg / name
            p.write_text(p.read_text().replace("verified = false", "verified = true"))
        r = self.run_value("IND")
        self.assertEqual((r["status"], r["unverified_inputs"]), ("ok", []))
        self.assertFalse(any(n.startswith("Inputs not checked") for n in r["notes"]))

    def test_no_sector_is_not_handled(self):
        _company(self.conn, "XYZ", [2022, 2023, 2024], _industrial)
        r = self.run_value("XYZ")
        self.assertEqual(r["status"], "not handled")
        self.assertIsNone(r["value"])

    def test_sector_picked_in_the_app_chooses_the_model(self):
        _company(self.conn, "UPL", [2022, 2023, 2024], _industrial, origin="upload")
        self.assertEqual(self.run_value("UPL", official_only=False)["status"], "not handled")
        self.conn.execute("UPDATE issuers SET sector='Distribution and trading' WHERE id='UPL'")
        r = self.run_value("UPL", official_only=False)
        self.assertEqual((r["status"], r["model"], r["sector_source"]), ("ok", "fcff", "app"))
        self.assertIn("Distribution and trading", r["sector_choices"])

    def test_too_few_years(self):
        self.add_sector('NEW = { sector = "Test", model = "fcff", industry = "Total Market" }')
        _company(self.conn, "NEW", [2024], _industrial)
        r = self.run_value("NEW")
        self.assertEqual(r["status"], "not enough data")
        self.assertIn("3 full years needed", r["missing"][0])

    def test_higher_risk_free_rate_lowers_value(self):
        low = self.run_value("IND")["value"]["base"]
        p = self.cfg / "market.toml"
        p.write_text(p.read_text().replace("value = 0.0507", "value = 0.0607"))
        self.assertLess(self.run_value("IND")["value"]["base"], low)


if __name__ == "__main__":
    unittest.main()
