import unittest
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

from src.core.schema import (
    CheckStatus, Currency, Fact, Filing, FxRate, Issuer, Price, Scale,
)

SHA = "a" * 64


def make_fact(**overrides) -> Fact:
    base = dict(
        issuer_id="TEST", filing_id="TEST-AR2025", statement="BS",
        period_end=date(2025, 12, 31), period_type="FY", line_item="total_assets",
        label_as_printed="Total assets", value_as_reported="1,234,567", currency="KHR",
        scale=1_000, page=42, method="table",
    )
    base.update(overrides)
    return Fact(**base)


class FactRules(unittest.TestCase):
    def test_value_is_required(self):
        # The old engine invented share_price=5.0 when a number was missing. A Fact cannot.
        with self.assertRaises(TypeError):
            Fact(issuer_id="TEST", filing_id="F", statement="BS", period_end=date(2025, 12, 31),
                 period_type="FY", line_item="cash", label_as_printed="Cash",
                 currency="USD", scale=1, page=3, method="table")

    def test_exact_decimal_and_commas(self):
        fact = make_fact()
        self.assertEqual(fact.value_as_reported, Decimal("1234567"))
        self.assertEqual(fact.value_in_units(), Decimal("1234567000"))

    def test_scale_changes_the_amount_1000x(self):
        thousands = make_fact(scale=1_000).value_in_units()
        units = make_fact(scale=1).value_in_units()
        self.assertEqual(thousands, units * 1000)

    def test_float_refused(self):
        with self.assertRaises(TypeError):
            make_fact(value_as_reported=0.1)

    def test_negative_and_parenthesis_free_values(self):
        self.assertEqual(make_fact(value_as_reported="-500").value_as_reported, Decimal("-500"))
        with self.assertRaises(ValueError):
            make_fact(value_as_reported="(500)")  # sign rules belong to the extractor, not guessed here

    def test_not_a_number(self):
        for bad in ("", "n/a", "NaN", "Infinity"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                make_fact(value_as_reported=bad)

    def test_page_must_be_positive(self):
        for bad in (0, -1, True, "3"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                make_fact(page=bad)

    def test_line_item_snake_case(self):
        with self.assertRaises(ValueError):
            make_fact(line_item="Total Assets")

    def test_label_required(self):
        with self.assertRaises(ValueError):
            make_fact(label_as_printed="  ")

    def test_unknown_enums_rejected(self):
        for field, bad in (("currency", "EUR"), ("scale", 100), ("statement", "XX"),
                           ("method", "llm"), ("period_type", "FY2")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                make_fact(**{field: bad})

    def test_confirmed_needs_who_and_when(self):
        with self.assertRaises(ValueError):
            make_fact(check_status="confirmed")
        ok = make_fact(check_status="confirmed", confirmed_by="owner", confirmed_at=datetime(2026, 11, 18, 20, 0))
        self.assertIs(ok.check_status, CheckStatus.CONFIRMED)

    def test_enums_normalised(self):
        fact = make_fact()
        self.assertIs(fact.currency, Currency.KHR)
        self.assertIs(fact.scale, Scale.THOUSANDS)

    def test_frozen(self):
        fact = make_fact()
        with self.assertRaises(Exception):
            fact.page = 1  # type: ignore[misc]
        self.assertEqual(replace(fact, page=1).page, 1)


class OtherRecords(unittest.TestCase):
    def test_issuer(self):
        Issuer(id="TEST", name="Test Plc", reporting_currency="USD")
        with self.assertRaises(ValueError):
            Issuer(id="bad id", name="X")
        with self.assertRaises(ValueError):
            Issuer(id="TEST", name=" ")

    def test_filing_hash_and_url(self):
        Filing(id="TEST-AR2025", issuer_id="TEST", kind="annual",
               source_url="https://example.org/ar.pdf", pdf_sha256=SHA, published_on=date(2026, 3, 31))
        with self.assertRaises(ValueError):
            Filing(id="F", issuer_id="TEST", kind="annual", source_url="https://x/y.pdf",
                   pdf_sha256="ABC", published_on=date(2026, 3, 31))
        with self.assertRaises(ValueError):
            Filing(id="F", issuer_id="TEST", kind="annual", source_url="ar.pdf",
                   pdf_sha256=SHA, published_on=date(2026, 3, 31))

    def test_fx_rate(self):
        FxRate(on=date(2025, 12, 31), base="USD", quote="KHR", rate="4010", source_url="https://example.org")
        with self.assertRaises(ValueError):
            FxRate(on=date(2025, 12, 31), base="USD", quote="USD", rate="1", source_url="x")
        with self.assertRaises(ValueError):
            FxRate(on=date(2025, 12, 31), base="USD", quote="KHR", rate="0", source_url="x")
        with self.assertRaises(ValueError):
            FxRate(on=date(2025, 12, 31), base="USD", quote="KHR", rate="4000", source_url="")

    def test_price(self):
        Price(issuer_id="TEST", on=date(2026, 1, 5), close="6200", volume=0, currency="KHR", source_url="x")
        with self.assertRaises(ValueError):
            Price(issuer_id="TEST", on=date(2026, 1, 5), close="6200", volume=-1, currency="KHR", source_url="x")


if __name__ == "__main__":
    unittest.main()
