import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.core import store
from src.core.coverage import full_years, readiness
from src.core.schema import Filing, Issuer
from src.intake import upload
from tests.test_extract import BS_PAGE
from tests.test_schema import SHA, make_fact


def make_pdf(lines: list[str]) -> bytes:
    """A minimal, valid one-page text PDF (Helvetica), built by hand so tests need no PDF writer."""
    def esc(t):
        return t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    ops = ["BT", "/F1 9 Tf", "11 TL", "40 800 Td"] + [f"({esc(l)}) Tj T*" for l in lines] + ["ET"]
    stream = "\n".join(ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


class FileGate(unittest.TestCase):
    def test_refusals(self):
        good = make_pdf(["hello"])
        cases = {
            "empty": b"",
            "not a PDF": b"Revenue 100\nTotal assets 200\n",
            "encrypted": good.replace(b"/Root 1 0 R", b"/Root 1 0 R /Encrypt 9 0 R"),
            "active content": good.replace(b"/Type /Catalog", b"/Type /Catalog /OpenAction << /S /JavaScript /JS (x) >>"),
            "embedded file": good.replace(b"/Type /Catalog", b"/Type /Catalog /Names << /EmbeddedFiles 9 0 R >>"),
        }
        for name, data in cases.items():
            with self.subTest(name):
                r = upload.check_file(data)
                self.assertFalse(r.accepted)
                self.assertTrue(r.refused)

    def test_too_big(self):
        old = upload.MAX_BYTES
        upload.MAX_BYTES = 100
        try:
            self.assertIn("larger", upload.check_file(make_pdf(["x" * 200])).refused[0])
        finally:
            upload.MAX_BYTES = old

    def test_no_text_layer(self):
        blank = make_pdf([])
        r = upload.check_file(blank)
        self.assertFalse(r.accepted)
        self.assertIn("scanned", r.refused[0])

    def test_ordinary_pdf_accepted(self):
        r = upload.check_file(make_pdf(["Some text"]))
        self.assertTrue(r.accepted, r.refused)
        self.assertEqual((r.pages, r.text_pages), (1, 1))


class Ingest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old = os.environ.get("CSX_DATA_DIR")
        os.environ["CSX_DATA_DIR"] = self.tmp.name
        self.conn = store.connect(Path(self.tmp.name) / "facts.sqlite")
        store.init_db(self.conn)
        self.pdf = Path(self.tmp.name) / "my report.pdf"
        self.pdf.write_bytes(make_pdf(BS_PAGE))

    def tearDown(self):
        self.conn.close()
        if self.old is None:
            os.environ.pop("CSX_DATA_DIR", None)
        else:
            os.environ["CSX_DATA_DIR"] = self.old
        self.tmp.cleanup()

    def test_upload_stored_as_private(self):
        r = upload.ingest(self.pdf, "TEST", "Test Plc", conn=self.conn)
        self.assertTrue(r.accepted, r.refused)
        self.assertGreater(r.facts_stored, 0)
        self.assertEqual(r.checks_failed, 0)
        self.assertTrue(any("auditor" in w for w in r.warnings))        # no auditor's report in it
        self.assertTrue(any("not found" in w for w in r.warnings))      # only a balance sheet
        row = self.conn.execute("SELECT origin, source_url FROM filings WHERE id=?", (r.filing_id,)).fetchone()
        self.assertEqual(row["origin"], "upload")
        self.assertTrue((Path(self.tmp.name) / "uploads" / f"{r.sha256}.pdf").exists())  # stored by hash, not name
        self.assertEqual(store.public_facts(self.conn), [])             # never public

    def test_same_file_twice(self):
        upload.ingest(self.pdf, "TEST", "Test Plc", conn=self.conn)
        again = upload.ingest(self.pdf, "TEST", "Test Plc", conn=self.conn)
        self.assertEqual(again.facts_stored, 0)
        self.assertTrue(any("already loaded" in w for w in again.warnings))

    def test_non_statement_pdf_refused(self):
        self.pdf.write_bytes(make_pdf(["Dear shareholders,", "It was a good year."]))
        r = upload.ingest(self.pdf, "TEST", "Test Plc", conn=self.conn)
        self.assertFalse(r.accepted)
        self.assertIn("no financial statements", r.refused[0])


class PublicAndCoverage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.conn = store.connect(Path(self.tmp.name) / "t.sqlite")
        store.init_db(self.conn)
        store.add_issuer(self.conn, Issuer(id="TEST", name="Test Plc"))
        store.add_filing(self.conn, Filing(id="TEST-AR2025", issuer_id="TEST", kind="annual",
                                           source_url="https://example.org/a.pdf", pdf_sha256=SHA,
                                           published_on=date(2026, 3, 31)))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add_year(self, year, status="proposed"):
        for stmt, item in (("BS", "total_assets"), ("IS", "net_income"), ("CF", "cfo")):
            store.add_fact(self.conn, make_fact(statement=stmt, line_item=item, period_end=date(year, 12, 31),
                                                check_status=status))

    def test_public_facts_hide_reviewer_and_failed(self):
        self.add_year(2025)
        fid = self.conn.execute("SELECT MIN(id) FROM facts").fetchone()[0]
        store.confirm_fact(self.conn, fid, "someone")
        store.fail_fact(self.conn, fid + 1, "someone")
        rows = store.public_facts(self.conn)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertNotIn("confirmed_by", row)
            self.assertNotIn("confirmed_at", row)
            self.assertNotIn("someone", str(row))

    def test_years_and_readiness(self):
        self.add_year(2024)
        self.add_year(2025)
        store.add_fact(self.conn, make_fact(statement="BS", line_item="total_assets", period_end=date(2023, 12, 31)))
        self.assertEqual(full_years(self.conn, "TEST"), [2024, 2025])     # 2023 has no IS/CF
        ready = {r.calculation: r for r in readiness(self.conn, "TEST")}
        self.assertFalse(ready["value.dcf"].ready)
        self.assertEqual((ready["value.dcf"].have, ready["value.dcf"].need), (2, 3))
        self.assertTrue(ready["quality.beneish_m"].ready)
        self.assertFalse(ready["market.liquidity"].ready)

    def test_rejected_year_does_not_count(self):
        self.add_year(2025, status="failed")
        self.assertEqual(full_years(self.conn, "TEST"), [])


if __name__ == "__main__":
    unittest.main()


class ActiveContentScan(unittest.TestCase):
    """The safety gate looks at the PDF's structure, not at image bytes."""

    def test_js_bytes_inside_an_image_are_not_a_script(self):
        from src.intake.upload import _structure
        import zlib
        image = b"1 0 obj << /Type /XObject /Subtype /Image /Length 9 >> stream\n" + b"\x00/JS\xff/JS\x01" + b"\nendstream endobj"
        self.assertNotIn(b"/JS", _structure(b"%PDF-1.7\n" + image))

    def test_script_hidden_in_an_object_stream_is_found(self):
        from src.intake.upload import _structure
        import zlib
        body = zlib.compress(b"5 0 obj << /S /JavaScript /JS (app.alert(1)) >> endobj")
        pdf = b"%PDF-1.7\n2 0 obj << /Type /ObjStm /N 1 /Filter /FlateDecode >> stream\n" + body + b"\nendstream endobj"
        self.assertIn(b"/JavaScript", _structure(pdf))

    def test_script_in_plain_structure_is_found(self):
        from src.intake.upload import _structure
        self.assertIn(b"/JS", _structure(b"%PDF-1.7\n1 0 obj << /OpenAction << /S /JavaScript /JS (x) >> >> endobj"))

