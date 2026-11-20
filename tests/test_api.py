import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path

from src.collect.csx import Document, classify, page_url, safe_name
from src.core import store
from src.core.schema import Filing, Issuer
from tests.test_schema import SHA, make_fact


def seed(conn):
    store.add_issuer(conn, Issuer(id="TEST", name="Test Plc"))
    store.add_issuer(conn, Issuer(id="MINE", name="Only Uploaded Plc"))
    store.add_filing(conn, Filing(id="TEST-AR2024", issuer_id="TEST", kind="annual",
                                  source_url="https://example.org/2024.pdf", pdf_sha256=SHA,
                                  published_on=date(2025, 3, 31)))
    store.add_filing(conn, Filing(id="TEST-AR2025", issuer_id="TEST", kind="annual",
                                  source_url="https://example.org/2025.pdf", pdf_sha256="b" * 64,
                                  published_on=date(2026, 3, 31)))
    store.add_filing(conn, Filing(id="MINE-UP", issuer_id="MINE", kind="annual",
                                  source_url="file:uploads/x.pdf", pdf_sha256="c" * 64,
                                  published_on=date(2026, 3, 31), origin="upload"))
    # 2024 total assets as first printed, then reprinted differently in the 2025 report
    store.add_fact(conn, make_fact(filing_id="TEST-AR2024", period_end=date(2024, 12, 31), value_as_reported="900"))
    store.add_fact(conn, make_fact(filing_id="TEST-AR2025", period_end=date(2024, 12, 31), value_as_reported="950"))
    store.add_fact(conn, make_fact(filing_id="TEST-AR2025", period_end=date(2025, 12, 31), value_as_reported="1,000"))
    store.add_fact(conn, make_fact(issuer_id="MINE", filing_id="MINE-UP", value_as_reported="5"))
    fid = store.add_fact(conn, make_fact(filing_id="TEST-AR2025", line_item="cash", value_as_reported="7"))
    store.confirm_fact(conn, fid, "Some Reviewer")


class ApiData(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["CSX_DATA_DIR"] = self.tmp.name
        from src.core import paths
        self.conn = store.connect(paths.db_path())
        store.init_db(self.conn)
        seed(self.conn)

    def tearDown(self):
        self.conn.close()
        os.environ.pop("CSX_DATA_DIR", None)
        self.tmp.cleanup()

    def test_uploads_never_appear(self):
        from src.api import data
        ids = [c["id"] for c in data.companies(self.conn)]
        self.assertEqual(ids, ["TEST"])
        with self.assertRaises(data.NotFound):
            data.company(self.conn, "MINE")
        self.assertNotIn("MINE", json.dumps(data.line_items(self.conn)) + json.dumps(data.documents(self.conn)))

    def test_newest_version_wins_and_all_versions_kept(self):
        from src.api import data
        rows = [r for r in data.facts(self.conn, "TEST", line_item="total_assets") if r["period_end"] == "2024-12-31"]
        self.assertEqual([r["value_as_reported"] for r in rows], ["950"])
        every = [r for r in data.facts(self.conn, "TEST", line_item="total_assets", all_versions=True)
                 if r["period_end"] == "2024-12-31"]
        self.assertEqual([r["value_as_reported"] for r in every], ["900", "950"])
        self.assertEqual(rows[0]["source"]["page"], 42)
        self.assertEqual(rows[0]["source"]["url"], "https://example.org/2025.pdf")

    def test_series_is_exact_and_scaled(self):
        from src.api import data
        s = data.series(self.conn, "TEST", "total_assets")
        self.assertEqual([p["value"] for p in s["points"]], ["950000", "1000000"])

    def test_no_reviewer_names_or_local_paths(self):
        from src.api import data
        blob = json.dumps([data.company(self.conn, "TEST"), data.facts(self.conn, "TEST"),
                           data.report(self.conn, "TEST")])
        self.assertNotIn("Some Reviewer", blob)
        self.assertNotIn("confirmed_by", blob)
        self.assertNotIn(self.tmp.name, blob)
        self.assertNotIn("upload", data.report(self.conn, "TEST")["filings"][0]["source"]["url"])

    def test_exchange_pages_cited_as_home_page(self):
        from src.api.data import public_link
        deep = "https://csx.com.kh/en/company-information/stock/annual-financial-reports/CGSM/14"
        link = public_link(deep, "CGSM", "Annual report 2025", "2026-04-21")
        self.assertEqual(link["url"], "https://csx.com.kh")
        self.assertIn("CGSM > Annual report 2025", link["where"])
        self.assertEqual(public_link("https://example.org/a.pdf")["url"], "https://example.org/a.pdf")

    def test_export_writes_static_files(self):
        from src.api.server import export
        out = Path(self.tmp.name) / "site"
        n = export(out)
        self.assertGreater(n, 5)
        self.assertTrue((out / "v1/companies/TEST/series/total_assets.json").is_file())
        self.assertFalse((out / "v1/companies/MINE.json").exists())


class ApiServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["CSX_DATA_DIR"] = cls.tmp.name
        from src.core import paths
        conn = store.connect(paths.db_path())
        store.init_db(conn)
        seed(conn)
        conn.close()
        from src.api.server import Handler
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        os.environ.pop("CSX_DATA_DIR", None)
        cls.tmp.cleanup()

    def get(self, path, method="GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=b"{}" if method != "GET" else None)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read()), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), e.headers

    def test_routes(self):
        status, body, headers = self.get("/v1")
        self.assertEqual(status, 200)
        self.assertIn("licence", body)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        status, body, _ = self.get("/v1/companies/test/facts?statement=BS&period_type=FY")
        self.assertEqual(status, 200)
        self.assertTrue(body)
        self.assertEqual(self.get("/v1/companies/MINE")[0], 404)
        self.assertEqual(self.get("/v1/nothing")[0], 404)
        status, body, _ = self.get("/v1/companies/TEST/statements.json")
        self.assertEqual((status, body["format"]), (200, "csx.statements/1"))
        self.assertEqual(self.get("/v1/companies/MINE/statements.json")[0], 404)
        self.assertEqual(self.get("/v1/schema/statements.json")[1]["$id"], "csx.statements/1")
        self.assertTrue(any(x["code"] == "revenue" for x in self.get("/v1/standard-items")[1]))
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/v1/companies/TEST/statements.csv") as r:
            self.assertTrue(r.headers["Content-Type"].startswith("text/csv"))
            self.assertTrue(r.read().decode().startswith("company_id,statement,"))
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/v1/companies/MINE/statements.csv")

    def test_read_only(self):
        self.assertEqual(self.get("/v1/companies", method="POST")[0], 405)
        self.assertEqual(self.get("/v1/companies", method="DELETE")[0], 405)


class Workbook(unittest.TestCase):
    def test_xlsx_parts_and_formulas(self):
        import io
        import xml.etree.ElementTree as ET
        import zipfile
        from decimal import Decimal
        from src.export.xlsx import Formula, Workbook as WB, col_letter
        self.assertEqual([col_letter(n) for n in (1, 26, 27, 52, 703)], ["A", "Z", "AA", "AZ", "AAA"])
        wb = WB()
        ws = wb.sheet("Summary")
        ws.row(["Figure", "FY2024"], style="head")
        ws.row(["=cmd|' /C calc'!A0", Decimal("1234.50"), Formula("B2*2")])
        z = zipfile.ZipFile(io.BytesIO(wb.to_bytes()))
        for name in z.namelist():
            ET.fromstring(z.read(name))
        sheet = z.read("xl/worksheets/sheet1.xml").decode()
        self.assertIn("<f>B2*2</f>", sheet)
        self.assertIn("<v>1234.50</v>", sheet)
        self.assertIn("'=cmd", sheet)          # labels can never become formulas
        with self.assertRaises(ValueError):
            wb.sheet("summary")

    def test_company_workbook_public_has_no_uploads(self):
        import io
        import zipfile
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        conn = store.connect(Path(tmp.name) / "t.sqlite")
        store.init_db(conn)
        seed(conn)
        from src.export.workbook import build_workbook
        wb = build_workbook(conn, "TEST", official_only=True)
        names = [s.name for s in wb.sheets]
        self.assertEqual(names, ["Read me", "Balance sheet", "Income statement", "Cash flow", "Summary", "Market", "Sources"])
        blob = b"".join(zipfile.ZipFile(io.BytesIO(wb.to_bytes())).read(n) for n in
                        zipfile.ZipFile(io.BytesIO(wb.to_bytes())).namelist())
        self.assertNotIn(b"Some Reviewer", blob)
        self.assertIn(b"No share price", blob)     # unlisted / no prices: valuation still works
        conn.close()


class Collector(unittest.TestCase):
    def test_titles(self):
        cases = {
            "Third Quarterly Report of 2026 of CAMGSM PLC.": ("quarterly", 2026, "Q3"),
            "The​ First Quarterly Report 2026 of CAMGSM PLC.": ("quarterly", 2026, "Q1"),
            "Annual report of CAMGSM PLC. in 2025": ("annual", 2025, "FY"),
            "Semi-Annual Report 2024": ("semiannual", 2024, "H1"),
            "Notice of meeting": ("other", None, ""),
        }
        for title, want in cases.items():
            self.assertEqual(classify(title), want, title)

    def test_price_text(self):
        from src.collect.market import _num
        self.assertEqual(_num("4,040"), "4040")
        self.assertEqual(_num("2930(SQ)"), "2930")
        self.assertIsNone(_num("-"))
        self.assertIsNone(_num(None))

    def test_safe_names_and_links(self):
        self.assertEqual(safe_name("csx-14-../../etc/passwd"), "csx-14-etc-passwd")
        self.assertEqual(safe_name("csx-9-Annual Report 2024 (EN).pdf"), "csx-9-Annual-Report-2024-EN-.pdf")
        doc = Document("stock", "CGSM", 14, "t", date(2026, 4, 21), "annual", 2025, "FY")
        self.assertTrue(page_url(doc).startswith("https://csx.com.kh/en/company-information/stock/"))


if __name__ == "__main__":
    unittest.main()


class ProfitLabels(unittest.TestCase):
    def test_profit_variants_are_net_income(self):
        from src.extract.labels import line_items
        from src.core.schema import Statement

        class R:
            def __init__(self, label):
                self.label, self.section = label, ""
        for label in ["Profit for the year, representing total comprehensive income for the year",
                      "Profit for the year carried forward to next page", "Net profit for the year"]:
            self.assertEqual(line_items(Statement.INCOME, [R(label)]), ["net_income"], label)
        # A breakdown line after the real total is not the profit...
        names = line_items(Statement.INCOME, [R("Profit for the year"), R("Profit for the year attributable to owners")])
        self.assertEqual(names[0], "net_income")
        self.assertNotEqual(names[1], "net_income")
        # ...but when it is the only profit line printed, it is (MJQE, PAS quarterly reports).
        for label in ("Profit for the period attributable to shareholders",
                      "Profit for the periods attributable to shareholders"):
            self.assertEqual(line_items(Statement.INCOME, [R("Profit before tax"), R(label)])[1], "net_income", label)
        # A business was sold: the stated total, not the continuing-operations line (PPSP 2022).
        names = line_items(Statement.INCOME, [R("Profit for the financial period from continuing operation"),
                                              R("Owners of the Company"), R("Total profit attributable to")])
        self.assertEqual(names.index("net_income"), 2)


class StandardExport(unittest.TestCase):
    """Standardised statements for programs: JSON, CSV, schema."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.conn = store.connect(Path(self.tmp.name) / "t.sqlite")
        store.init_db(self.conn)
        seed(self.conn)
        store.add_fact(self.conn, make_fact(filing_id="TEST-AR2025", statement="IS", line_item="revenues",
                                            label_as_printed="=HYPERLINK(\"x\")", value_as_reported="-12.5"))
        store.add_fact(self.conn, make_fact(filing_id="TEST-AR2025", line_item="borrowings_non_current",
                                            label_as_printed="Borrowings", value_as_reported="3"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_codes_values_and_periods(self):
        from src.export.standard import rows
        got = {(r["statement"], r["period_end"], r["line_item"]): r for r in rows(self.conn, "TEST")}
        rev = got[("IS", "2025-12-31", "revenue")]
        self.assertTrue(rev["is_standard"])
        self.assertEqual(rev["value"], "-12500.0")
        self.assertEqual((rev["period_start"], rev["months"]), ("2025-01-01", 12))
        self.assertEqual(got[("BS", "2025-12-31", "borrowings_non_current")]["is_standard"], True)
        self.assertEqual(got[("BS", "2024-12-31", "total_assets")]["value_as_printed"], "950")  # newest version
        self.assertEqual(got[("BS", "2024-12-31", "total_assets")]["months"], "")

    def test_period_start_for_interim_and_june_year_end(self):
        from datetime import date as d
        from src.export.standard import _period_start
        self.assertEqual(_period_start(d(2026, 9, 30), 9), d(2026, 1, 1))
        self.assertEqual(_period_start(d(2026, 3, 31), 6), d(2025, 10, 1))
        self.assertEqual(_period_start(d(2026, 6, 30), 3), d(2026, 4, 1))

    def test_public_has_no_uploads_or_reviewers(self):
        from src.export.standard import to_csv, to_json
        blob = json.dumps(to_json(self.conn, "TEST")) + to_csv(self.conn, "TEST")
        self.assertNotIn("MINE", blob)
        self.assertNotIn("Some Reviewer", blob)
        self.assertNotIn("example.org/x", blob)
        self.assertEqual(to_json(self.conn, "MINE")["facts"], [])

    def test_json_matches_its_schema(self):
        import re
        from src.export.standard import COLUMNS, json_schema, to_json
        doc, schema = to_json(self.conn, "TEST"), json_schema()
        props = schema["properties"]["facts"]["items"]["properties"]
        self.assertEqual(sorted(props), sorted(COLUMNS))
        for fact in doc["facts"]:
            self.assertEqual(sorted(fact), sorted(COLUMNS))
            for key, rule in props.items():
                if "enum" in rule:
                    self.assertIn(fact[key], rule["enum"], key)
                if "pattern" in rule:
                    self.assertTrue(re.match(rule["pattern"], str(fact[key])), (key, fact[key]))

    def test_csv_is_parseable_and_formula_safe(self):
        import csv
        import io
        from src.export.standard import COLUMNS, to_csv
        text = to_csv(self.conn, "TEST")
        parsed = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(list(parsed[0]), COLUMNS)
        labels = [r["label_as_printed"] for r in parsed]
        self.assertIn("'=HYPERLINK(\"x\")", labels)
        self.assertLessEqual({r["is_standard"] for r in parsed}, {"true", "false"})


class NewestReportWins(unittest.TestCase):
    def test_later_period_wins_even_when_uploaded_first(self):
        """Two uploads on the same day: the report covering the later year wins, whatever the upload order."""
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        conn = store.connect(Path(tmp.name) / "t.sqlite")
        store.init_db(conn)
        store.add_issuer(conn, Issuer(id="UP", name="Up Plc"))
        today = date(2026, 11, 19)
        for fid, sha in (("UP-2025", "d" * 64), ("UP-2024", "e" * 64)):     # newer report uploaded first
            store.add_filing(conn, Filing(id=fid, issuer_id="UP", kind="annual", source_url="file:x",
                                          pdf_sha256=sha, published_on=today, origin="upload"))
        store.add_fact(conn, make_fact(issuer_id="UP", filing_id="UP-2025", period_end=date(2025, 12, 31), value_as_reported="11"))
        store.add_fact(conn, make_fact(issuer_id="UP", filing_id="UP-2025", period_end=date(2024, 12, 31), value_as_reported="10"))
        store.add_fact(conn, make_fact(issuer_id="UP", filing_id="UP-2024", period_end=date(2024, 12, 31), value_as_reported="9"))
        from src.export.standard import rows
        got = {r["period_end"]: r["value_as_printed"] for r in rows(conn, "UP", official_only=False)}
        self.assertEqual(got["2024-12-31"], "10")          # the 2025 report's restated figure
        conn.close()


class RereadKeepsReviews(unittest.TestCase):
    def test_checked_and_fixed_values_survive_a_reread(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        conn = store.connect(Path(tmp.name) / "t.sqlite")
        store.init_db(conn)
        store.add_issuer(conn, Issuer(id="UP", name="Up Plc"))
        store.add_filing(conn, Filing(id="UP-1", issuer_id="UP", kind="annual", source_url="file:x",
                                      pdf_sha256="f" * 64, published_on=date(2026, 11, 19), origin="upload"))
        a = store.add_fact(conn, make_fact(issuer_id="UP", filing_id="UP-1", line_item="total_assets", value_as_reported="100"))
        b = store.add_fact(conn, make_fact(issuer_id="UP", filing_id="UP-1", line_item="cash", value_as_reported="7"))
        c = store.add_fact(conn, make_fact(issuer_id="UP", filing_id="UP-1", line_item="loans", value_as_reported="50"))
        store.confirm_fact(conn, a, "me")
        store.correct_fact(conn, b, "8", "me", "page says 8")
        store.confirm_fact(conn, c, "me")
        # the re-read finds the same total_assets and cash rows (same original numbers); loans now reads 55
        new = [make_fact(issuer_id="UP", filing_id="UP-1", line_item="total_assets", value_as_reported="100"),
               make_fact(issuer_id="UP", filing_id="UP-1", line_item="cash", value_as_reported="7"),
               make_fact(issuer_id="UP", filing_id="UP-1", line_item="loans", value_as_reported="55")]
        n, kept, dropped = store.replace_upload_facts(conn, "UP-1", new)
        self.assertEqual((n, kept, dropped), (3, 2, 1))
        rows = {r["line_item"]: r for r in conn.execute("SELECT * FROM facts WHERE filing_id='UP-1'")}
        self.assertEqual((rows["total_assets"]["check_status"], rows["cash"]["value_as_reported"]), ("confirmed", "8"))
        self.assertEqual(rows["loans"]["check_status"], "proposed")        # it reads differently now: not carried
        self.assertEqual(store.original_reading(conn, rows["cash"]["id"]), "7")   # history moved with the value
        conn.close()
