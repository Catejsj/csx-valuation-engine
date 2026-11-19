# What a useful filing contains

This tool reads the annual reports of companies listed on the Cambodia Securities Exchange. Building it
showed, page by page, what makes a report easy or hard to use. This guide sets that down: what the
reports usually look like today, why the US annual report format (the 10-K) works better for investors,
and what any company can do to make its reports more useful.

No company is named here. The figures come from the tool's own checks (the "What the report tells you"
section in the app), run on the reports as published.

## The accounting is fine. The reporting is the problem.

Cambodian listed companies prepare their statements under CIFRS, which follows the international
standard (IFRS). Once the balance sheet, income statement and cash flow statement are found, they
generally add up and tie together. The tool checks this on every report.

The trouble is everything around those statements: how they are published, where they sit in the
report, and how much explanation comes with them.

## What the reports usually look like

**Half of them can't be read by software at all.** Of the 138 annual reports published on the exchange,
68 gave the tool no readable financial statements. Some are scanned pages (pictures of paper, not text),
some are in Khmer only, and some put the statements in a separate file or in a layout no reader expects.

**Even the readable ones are hard to use.** Taking the newest annual report of each of the 13 companies
whose statements could be read (so the best of the set):

| What the check looks for | How many of the 13 have it |
|---|---|
| Auditor's report as searchable text (not a scanned picture) | 3 |
| The auditor's key audit matters, readable | 4 |
| Machine-readable data (XBRL) filed with the report | 0 |
| Every page as text (no picture-only pages) | 1 |
| Segment information (which part of the business earns the money) | 8 |
| A management discussion of several pages that explains the results | 4 (4 have none) |
| Operating figures beyond the accounts (customers, volumes, sites) | 6 |
| A risk section | 13 |

On the tool's nine-point checklist, these reports score between 1 and 7, with 3 the most common score. A
US 10-K, checked the same way, scores 9 out of 9.

**Other patterns seen while building the reader:**

- **The statements move around.** They can sit near the front or past page 150, after "financial
  highlights" pages, bar charts and staff lists. Each report has to be searched by hand.
- **The auditor's opinion is often a scan.** The one page that says whether the numbers can be trusted is
  the page a computer can't read, and often the one a reader can't search either.
- **Layouts change from year to year**, even within one company: column order, units (US dollars, riel,
  thousands of riel), dates written several ways, labels split across lines.
- **Last year's figures sometimes come back different.** A number printed for one year is printed
  differently in the next year's report. The tool keeps both and uses the newer one; a reader comparing two
  reports by hand may not notice.
- **There is no central, structured source.** The reports are PDFs on a website. To compare companies,
  someone has to download each file and copy the numbers out.

## Why the 10-K structure works better

Every company listed in the United States files a 10-K each year, in the same structure:

| Part of the 10-K | What it holds | Why it helps |
|---|---|---|
| Item 1, Business | What the company does, its segments, customers, competition | You understand the business before the numbers |
| Item 1A, Risk factors | What could go wrong, in management's words | Risks are in one known place |
| Item 7, Management's discussion and analysis (MD&A) | Why revenue, margins and cash moved, segment by segment, and how the company is funded | Management has to explain the numbers, not only show them |
| Item 7A, Market risk | Exposure to interest rates, currencies, prices | The sensitivities are stated, not guessed |
| Item 8, Financial statements | The statements, the notes, and the auditor's report as text, including critical audit matters | The core is complete, searchable and in one place |
| Item 9A, Controls | Whether internal controls over reporting work | A check on the process behind the numbers |

What makes the difference:

1. **The same order, every company, every year.** A reader (or a program) knows where the MD&A is
   before opening the file. Comparing two companies is comparing the same sections.
2. **Every number is tagged.** Inline XBRL labels each figure in the filing with a standard name ("Revenue",
   "Total assets"), its period and its unit, so software reads it exactly, with no copying and no guessing.
3. **Everything is text.** The auditor's report, the critical audit matters and the notes can all be
   searched, quoted and checked.
4. **Explanation is required, not optional.** Segment results and a real MD&A are part of the filing, so
   the reasons behind the numbers come from the company itself.
5. **One public, free source.** All filings sit in one database (EDGAR) with an index and a free
   interface for programs. Anyone can pull ten years of statements for any company in minutes.

The 10-K is not perfect: it is long, and parts of it (risk factors especially) grow into boilerplate. But
its structure is why US company data is cheap to analyse and hard to hide in. As an example, NVIDIA's
10-K for its 2024 fiscal year has its risk factors from page 13, about 12 pages of MD&A, the auditor's
report and critical audit matters as text, segment notes, every page searchable, and XBRL filed with it.

## What a company can do (most of it costs nothing)

1. **Publish text, not scans.** Export the report straight from the document software. Sign the auditor's
   report digitally, or add a text version beside the signed scan.
2. **Put the statements in a fixed, findable place**, with standard titles ("Statement of financial
   position", "Statement of profit or loss", "Statement of cash flows"), in the same order every year.
3. **Add a spreadsheet of the statements** beside the PDF, and XBRL when the exchange supports it.
4. **Explain the year.** A few pages of management discussion: what drove revenue, costs and cash, by
   segment, with the operating figures that matter for the business (customers, volumes, occupancy, loans).
5. **Report segments.** Which part of the business earns what, and with which assets.
6. **Flag changed figures.** When last year's numbers are reprinted differently, say which, by how much
   and why, next to the statement.
7. **Keep the layout stable** from year to year: units, columns and labels.
8. **Publish English and Khmer both as text**, so both audiences, and software, can read them.

## How the tool measures this

The "What the report tells you" section in each company's page runs these checks on the newest annual
report. Green means provided, yellow means partly, red means not found. They describe the document, not
the company.

| Check | What it looks for |
|---|---|
| Auditor's report | Found, and readable as text or only a scanned picture |
| Auditor's opinion | The standard opinion wording, when the report is text |
| Key audit matters | The auditor's list of the hardest areas |
| Segment information | A segment note or segment table |
| Management discussion | A section discussing the results, and how long it is |
| Operating figures | Numbers about the business beyond the accounts |
| Risk section | A section on risks or risk management |
| Pages without text | Pages that are pictures only |
| Machine-readable data | XBRL or similar structured data |

The tool also lists figures that a later report printed differently, with the old and new values.
