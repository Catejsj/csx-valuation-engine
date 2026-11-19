import unittest

from src.report.overview import overview


def report(currency="USD", **series):
    lines = [{"key": k, "kind": "money", "values": {str(y): str(v) for y, v in vals.items()}}
             for k, vals in series.items() if not k.startswith("r_")]
    ratios = [{"key": k[2:], "kind": "pct", "values": {str(y): str(v) for y, v in vals.items()}}
              for k, vals in series.items() if k.startswith("r_")]
    return {"currency": currency, "lines": lines, "ratios": ratios}


class Overview(unittest.TestCase):
    def test_growth_rate_and_direction(self):
        o = overview(report(revenue={2021: 100, 2023: 121}))
        g = o["topics"][0]
        self.assertEqual(g["key"], "growth")
        self.assertIn("grew", g["headline"])
        self.assertIn("10.0% a year", g["headline"])
        self.assertEqual(g["tone"], "up")

    def test_profit_used_when_revenue_missing(self):
        g = overview(report(net_income={2022: 50, 2023: 40}))["topics"][0]
        self.assertIn("Profit fell", g["headline"])
        self.assertIn("profit is used", " ".join(g["points"]))

    def test_missing_topics_are_named_not_guessed(self):
        o = overview(report())
        self.assertEqual(o["topics"], [])
        self.assertEqual(o["missing"], ["growth", "profitability", "cash", "debt", "value"])

    def test_cash_conversion_below_one_is_flagged(self):
        c = [t for t in overview(report(cfo={2022: 12, 2023: 12}, net_income={2022: 40, 2023: 40}))["topics"]
             if t["key"] == "cash"][0]
        self.assertIn("0.3x profit", c["headline"])
        self.assertEqual(c["tone"], "watch")

    def test_bank_cash_is_not_compared_with_profit(self):
        val = {"model": "bank", "status": "ok", "cost_of_capital": {"cost_of_equity": 0.15}}
        c = [t for t in overview(report(cfo={2023: 900}, net_income={2023: 100}), val)["topics"]
             if t["key"] == "cash"][0]
        self.assertIn("not compared", c["headline"])

    def test_return_against_cost_of_equity(self):
        val = {"model": "fcff", "status": "ok", "cost_of_capital": {"cost_of_equity": 0.12},
               "market": {"market_cap_usd": 300, "market_cap_vs_range": "above the range"},
               "value": {"bear": 100, "base": 150, "bull": 200}}
        r = report(r_roe={2022: 0.20, 2023: 0.20}, total_equity={2023: 100}, net_income={2023: 20})
        m = [t for t in overview(r, val)["topics"] if t["key"] == "meaning"][0]
        self.assertIn("8.0 points above", m["headline"])
        self.assertIn("worth more than its book value", m["headline"])
        joined = " ".join(m["points"])
        self.assertIn("3.0x its book equity", joined)
        self.assertIn("15.0 times last year's profit", joined)
        self.assertIn("assumes more growth", joined)
        self.assertNotRegex(joined.lower(), r"\b(buy|sell|undervalued|overvalued)\b")

    def test_equal_returns_say_about_book_value(self):
        val = {"model": "fcff", "status": "ok", "cost_of_capital": {"cost_of_equity": 0.154}}
        m = [t for t in overview(report(r_roe={2023: 0.152}), val)["topics"] if t["key"] == "meaning"][0]
        self.assertIn("about the same", m["headline"])

    def test_debt_card_with_only_current_ratio_has_a_headline(self):
        r = report()
        r["ratios"].append({"key": "current_ratio", "kind": "x", "values": {"2023": "2.5"}})
        d = [t for t in overview(r)["topics"] if t["key"] == "debt"][0]
        self.assertIn("2.5x the bills due", d["headline"])

    def test_two_years_give_a_change_not_an_average(self):
        g = overview(report(revenue={2022: 100, 2023: 80}))["topics"][0]
        self.assertIn("(−20.0%)", g["headline"])
        self.assertNotIn("on average", g["headline"])


if __name__ == "__main__":
    unittest.main()
