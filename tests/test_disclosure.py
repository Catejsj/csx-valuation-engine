"""The disclosure checklist, on made-up page texts (no PDF needed)."""
import unittest

from src.report import disclosure as d

FILLER = "Some ordinary text about the year. " * 20


def checks(texts, statement_pages=frozenset()):
    return {c.key: c for c in d.analyse(texts, set(statement_pages))}


class AuditorReport(unittest.TestCase):
    def test_readable_clean_opinion(self):
        pages = [FILLER, "INDEPENDENT AUDITOR'S REPORT\nOpinion\nThe statements present fairly ...\nKey Audit Matters\n...",
                 "STATEMENT OF FINANCIAL POSITION"]
        c = checks(pages, {3})
        self.assertEqual((c["auditor_report"].status, c["auditor_report"].pages), ("good", [2]))
        self.assertEqual(c["auditor_opinion"].status, "good")
        self.assertEqual(c["key_audit_matters"].status, "good")

    def test_modified_opinion_flagged_as_wording_found(self):
        pages = [FILLER, "INDEPENDENT AUDITOR'S REPORT\nQualified Opinion\nExcept for ...\nBasis for qualified opinion"]
        c = checks(pages, {3})
        self.assertEqual(c["auditor_opinion"].status, "partial")
        self.assertIn("qualified opinion", c["auditor_opinion"].finding)

    def test_scanned_auditor_report_before_statements(self):
        pages = [FILLER, FILLER, "", "", "", "", "CONSOLIDATED STATEMENT OF FINANCIAL POSITION ..."]
        c = checks(pages, {7})
        self.assertEqual(c["auditor_report"].status, "partial")
        self.assertIn("scanned image (pages 3–6)", c["auditor_report"].finding)
        self.assertIn("scanned", c["key_audit_matters"].finding)

    def test_missing(self):
        c = checks([FILLER, "STATEMENT OF FINANCIAL POSITION"], {2})
        self.assertEqual(c["auditor_report"].status, "missing")


class OtherChecks(unittest.TestCase):
    def test_segments_mdna_kpis_risks(self):
        pages = ([FILLER] * 2 + ["PART 8 - MANAGEMENT'S DISCUSSION AND ANALYSIS\n" + FILLER] + [FILLER] * 9
                 + ["PART 9 - OTHER\n" + FILLER, "PART 4. RISK MANAGEMENT AND INTERNAL CONTROL\n" + FILLER,
                    "Note 30 Operating segments\nThe Group has two reportable segments", "subscribers grew; ARPU fell"])
        c = checks(pages)
        self.assertEqual(c["segments"].status, "good")
        self.assertEqual(c["mdna"].status, "good")            # 10 pages
        self.assertEqual(c["mdna"].pages, list(range(3, 13)))
        self.assertEqual(c["kpis"].status, "good")
        self.assertEqual(c["risks"].status, "good")

    def test_short_mdna_no_segments_only_financial_risk_notes(self):
        pages = ["MANAGEMENT DISCUSSION AND ANALYSIS\n" + FILLER, "PART 2 X\n" + FILLER,
                 "5. FINANCIAL RISK MANAGEMENT\ncredit risk, liquidity risk"]
        c = checks(pages)
        self.assertEqual((c["mdna"].status, c["mdna"].pages), ("partial", [1]))
        self.assertEqual(c["segments"].status, "missing")
        self.assertEqual(c["risks"].status, "partial")        # notes only, no risk section

    def test_bullet_mentioning_risk_management_is_not_a_section(self):
        self.assertEqual(d.risk_section_pages(["- To review and oversee the risk management processes"]), [])

    def test_scanned_pages_and_structured_data(self):
        c = checks([FILLER, "", FILLER, "", ""])
        self.assertIn("3 of 5 pages", c["scanned_pages"].finding)
        self.assertEqual(c["structured_data"].status, "missing")

    def test_runs(self):
        self.assertEqual(d._runs([63, 64, 65, 66, 112, 113, 120]), "63–66, 112–113, 120")


if __name__ == "__main__":
    unittest.main()


class BothStyles(unittest.TestCase):
    """The same rules must judge a US 10-K and a Cambodian annual report correctly."""

    def test_us_10k_style(self):
        hdr = "Table of Contents\n"                      # every 10-K page carries this link
        toc = hdr + "\n".join(f"Item {n}. Heading {n} {p}" for n, p in
                              [("1", 4), ("1A", 13), ("2", 30), ("3", 31), ("5", 32), ("7", 34), ("7A", 43), ("8", 44), ("9", 45)])
        pages = [hdr + "FORM 10-K\nInteractive Data File ... Yes", toc, hdr + FILLER,
                 hdr + FILLER + "\nItem 1A. Risk Factors\n" + FILLER,
                 hdr + "Item 7. Management's Discussion and Analysis of Financial Condition\n" + FILLER,
                 hdr + FILLER, hdr + "Item 7A. Quantitative and Qualitative Disclosures about Market Risk",
                 hdr + "Report of Independent Registered Public Accounting Firm\nOpinion\npresent fairly\nCritical Audit Matters",
                 hdr + "Note 17 - Segment Information", hdr + "embedded within the Inline XBRL document"]
        c = checks(pages)
        self.assertEqual(c["auditor_report"].status, "good")
        self.assertEqual(c["auditor_report"].pages, [8])        # not the contents page
        self.assertEqual(c["risks"].pages[0], 4)                # heading mid-page, not page 2 (contents)
        self.assertEqual(c["mdna"].pages, [5, 6])               # stops at Item 7A
        self.assertEqual(c["structured_data"].status, "good")

    def test_cambodian_style_with_dotted_contents_and_typo(self):
        toc = "TABLE OF CONTENTS\n" + "\n".join(f"PART {n} - SECTION ........................ {p}" for n, p in
                                                 [(1, 3), (2, 9), (3, 14), (4, 20), (5, 22), (8, 27)])
        toc += "\nPART 8 - MANAGEMENT'S DICUSSION AND ANALYSIS (MD&A) ........ 27"
        pages = [FILLER, toc] + [FILLER] * 3 + ["PART 8 - MANAGEMENT'S DICUSSION AND ANALYSIS (MD&A)\n" + FILLER,
                                               FILLER, FILLER, "PART 9 - OTHER\n" + FILLER, "H. Risks Factors\n" + FILLER]
        c = checks(pages)
        self.assertTrue(d.is_contents_page(toc))
        self.assertEqual(c["mdna"].pages, [6, 7, 8])            # not the contents page
        self.assertEqual(c["risks"].status, "good")
        self.assertEqual(c["structured_data"].status, "missing")
