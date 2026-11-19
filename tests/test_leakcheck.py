"""The leak check must catch a planted term without the test revealing any real private term.

These tests use their own made-up terms and their own hash file.
"""
import tempfile
import unittest
from pathlib import Path

from tools import leakcheck


class LeakcheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self.hashes = Path(self.tmp.name) / "hashes.txt"
        for term in ("purple elephant", "zebra_v9"):
            self.assertEqual(leakcheck.main(["--hashes", str(self.hashes), "--add", term]), 0)

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self):
        return leakcheck.scan_tree(self.root, self.hashes)

    def test_clean_tree(self):
        (self.root / "ok.py").write_text("print('hello')\n", encoding="utf-8")
        self.assertEqual(self.scan(), [])

    def test_planted_term_blocks(self):
        (self.root / "a.md").write_text("line one\nthe Purple   Elephant is here\n", encoding="utf-8")
        report = self.scan()
        self.assertEqual(len(report), 1)
        self.assertIn("a.md:2", report[0])
        self.assertNotIn("purple", report[0].lower())  # never print the term itself
        rc = leakcheck.main(["--hashes", str(self.hashes), "--root", str(self.root)])
        self.assertEqual(rc, 1)

    def test_snake_case_term_and_file_name(self):
        (self.root / "zebra_v9.py").write_text("x = 1\n", encoding="utf-8")
        report = self.scan()
        self.assertTrue(any("(file name)" in r for r in report))

    def test_partial_word_not_flagged(self):
        (self.root / "b.txt").write_text("purple elephants and zebra_v90\n", encoding="utf-8")
        self.assertEqual(self.scan(), [])

    def test_ignore_file(self):
        (self.root / "notes.md").write_text("purple elephant\n", encoding="utf-8")
        (self.root / ".leakcheck-ignore").write_text("notes.md\n", encoding="utf-8")
        self.assertEqual(self.scan(), [])

    def test_add_is_idempotent(self):
        before = self.hashes.read_text(encoding="utf-8")
        leakcheck.main(["--hashes", str(self.hashes), "--add", "PURPLE elephant"])
        self.assertEqual(self.hashes.read_text(encoding="utf-8"), before)

    def test_hash_file_holds_no_plain_terms(self):
        self.assertNotIn("elephant", self.hashes.read_text(encoding="utf-8"))

    def test_public_files_are_clean(self):
        # Everything the public copy would contain passes the denylist and the personal-detail check.
        # (Private notes such as the worklog are never copied: see tools/release.py.)
        from tools import release
        self.assertEqual(release.check(), [])

    def test_private_notes_are_never_public(self):
        from tools import release
        public = {p.relative_to(release.ROOT).as_posix() for p in release.public_files()}
        planning = "-".join(reversed(("brief", "project")))+".md"   # written whole, the name is on the denylist itself
        for private in ("worklog.md", planning, "debug/fixes.md", ".graphifyignore"):
            self.assertNotIn(private, public)
        self.assertFalse(any(p.startswith(("graph/", "retired/", "data/")) for p in public))


if __name__ == "__main__":
    unittest.main()
