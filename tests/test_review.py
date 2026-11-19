import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path

from src.core import store
from src.core.schema import CheckStatus, Filing, Issuer, Method
from src.review.locate import printed_forms
from tests.test_schema import SHA, make_fact


class ReviewActions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.conn = store.connect(Path(self.tmp.name) / "t.sqlite")
        store.init_db(self.conn)
        store.add_issuer(self.conn, Issuer(id="TEST", name="Test Plc"))
        store.add_filing(self.conn, Filing(id="TEST-AR2025", issuer_id="TEST", kind="annual",
                                           source_url="https://example.org/a.pdf", pdf_sha256=SHA,
                                           published_on=date(2026, 3, 31)))
        self.fid = store.add_fact(self.conn, make_fact(value_as_reported="1000"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_confirm_needs_reviewer(self):
        with self.assertRaises(ValueError):
            store.confirm_fact(self.conn, self.fid, "  ")
        store.confirm_fact(self.conn, self.fid, "owner")
        fact = store.fact_by_id(self.conn, self.fid)
        self.assertIs(fact.check_status, CheckStatus.CONFIRMED)
        self.assertEqual(fact.confirmed_by, "owner")
        self.assertIsNotNone(fact.confirmed_at)

    def test_correction_keeps_original(self):
        store.correct_fact(self.conn, self.fid, "1,600", "owner", "misread digit")
        fact = store.fact_by_id(self.conn, self.fid)
        self.assertEqual(fact.value_as_reported, Decimal(1600))
        self.assertIs(fact.method, Method.MANUAL)
        self.assertIs(fact.check_status, CheckStatus.CONFIRMED)
        [event] = store.events_for(self.conn, self.fid)
        self.assertEqual((event["old_value"], event["new_value"], event["note"]), ("1000", "1600", "misread digit"))

    def test_bad_correction_changes_nothing(self):
        with self.assertRaises(ValueError):
            store.correct_fact(self.conn, self.fid, "e1600", "owner")
        self.assertEqual(store.fact_by_id(self.conn, self.fid).value_as_reported, Decimal(1000))
        self.assertEqual(store.events_for(self.conn, self.fid), [])

    def test_fail_and_unknown_id(self):
        store.fail_fact(self.conn, self.fid, "owner", "wrong row")
        self.assertIs(store.fact_by_id(self.conn, self.fid).check_status, CheckStatus.FAILED)
        with self.assertRaises(KeyError):
            store.confirm_fact(self.conn, 9999, "owner")

    def test_manual_add_is_logged(self):
        fid = store.add_manual_fact(self.conn, make_fact(line_item="eps", label_as_printed="EPS",
                                                         value_as_reported="0.014", scale=1, method="manual"),
                                    "owner")
        self.assertEqual(store.events_for(self.conn, fid)[0]["action"], "add")

    def test_printed_forms(self):
        self.assertEqual(printed_forms(Decimal("1234567")), {"1,234,567"})
        self.assertEqual(printed_forms(Decimal("-500")), {"(500)", "-500"})
        self.assertEqual(printed_forms(Decimal("0")), {"-", "0"})
        self.assertEqual(printed_forms(Decimal("0.0140")), {"0.014"})


class ServerGuards(unittest.TestCase):
    """The HTTP layer: local-only, JSON-only writes, reviewer required."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.old_env = os.environ.get("CSX_DATA_DIR")
        os.environ["CSX_DATA_DIR"] = cls.tmp.name
        from src.review.server import Handler
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        if cls.old_env is None:
            os.environ.pop("CSX_DATA_DIR", None)
        else:
            os.environ["CSX_DATA_DIR"] = cls.old_env
        cls.tmp.cleanup()

    def call(self, path, data=None, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"null") if "json" in r.headers.get("Content-Type", "") else None
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def test_page_and_empty_list(self):
        status, body = self.call("/api/filings")
        self.assertEqual((status, body), (200, []))
        status, _ = self.call("/")
        self.assertEqual(status, 200)

    def test_foreign_host_refused(self):
        status, body = self.call("/api/filings", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_write_needs_json(self):
        status, body = self.call("/api/fact/1/confirm", data=b"by=x")
        self.assertEqual(status, 400)

    def test_unknown_fact(self):
        status, _ = self.call("/api/fact/12345/confirm", data=b'{"by":"x"}',
                              headers={"Content-Type": "application/json"})
        self.assertEqual(status, 404)

    def test_downloads_in_three_formats(self):
        from datetime import date
        from src.core import paths, store
        from src.core.schema import Filing, Issuer
        from tests.test_schema import SHA, make_fact
        conn = store.connect(paths.db_path())
        store.init_db(conn)
        store.add_issuer(conn, Issuer(id="DLX", name="Download Plc", reporting_currency="USD"))
        store.add_filing(conn, Filing(id="DLX-AR", issuer_id="DLX", kind="annual", source_url="https://example.org/a.pdf",
                                      pdf_sha256=SHA, published_on=date(2026, 3, 31)))
        store.add_fact(conn, make_fact(issuer_id="DLX", filing_id="DLX-AR", line_item="total_assets",
                                       value_as_reported="1000", scale=1))
        conn.commit()
        conn.close()
        for fmt, kind in (("csv", "text/csv"), ("json", "application/json"),
                          ("xlsx", "application/vnd.openxmlformats")):
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/export/DLX.{fmt}") as r:
                body = r.read()
                self.assertEqual(r.status, 200)
                self.assertTrue(r.headers["Content-Type"].startswith(kind), fmt)
                self.assertIn(f".{fmt}", r.headers["Content-Disposition"])
            if fmt == "csv":
                self.assertIn(b"total_assets", body)
        status, _ = self.call("/api/export/NOPE.csv")
        self.assertEqual(status, 404)
        conn = store.connect(paths.db_path())   # leave the shared test database empty for the other tests
        for sql in ("DELETE FROM facts WHERE issuer_id='DLX'", "DELETE FROM filings WHERE issuer_id='DLX'",
                    "DELETE FROM issuers WHERE id='DLX'"):
            conn.execute(sql)
        conn.commit()
        conn.close()


if __name__ == "__main__":
    unittest.main()
