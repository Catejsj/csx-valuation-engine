# Developing

How the code is laid out and how the parts work. For using the app, see the [tutorial](TUTORIAL.md).

Run the tests with `./csx test` (Windows: `csx test`). `python tools/release.py FOLDER` makes the public copy:
only the files it lists, and only if they pass the leak check and a scan for home-folder paths, email addresses
and tokens. The leak check (`python tools/leakcheck.py`) blocks
private terms before anything is published; its denylist is stored as hashes.

The data lives in `data/` inside this folder (or the folder set in `CSX_DATA_DIR`). The database is SQLite in
WAL mode: back it up with `sqlite3 data/facts.sqlite ".backup copy.sqlite"`, not a plain file copy.

## Layout

- `src/core/schema.py`: Issuer, Filing, Fact, FxRate, Price; every value carries its page, currency and scale
- `src/core/store.py`: SQLite store; restatements resolved by the latest-published filing
- `src/core/paths.py`: data folder per OS (project `data/` from source; user data folder when packaged)
- `src/extract/`: statement pages -> rows (label, note, values, page) -> standard line items; rules only, no AI
- `src/core/checks.py`: balance identities, rows-add-up-to-total, cash ties; run on every printed column
- `src/review/`: local review screen (confirm / correct / reject; every action logged in `fact_events`)
- `src/intake/upload.py`: user uploads — safe-PDF gate, statements gate, checks gate; stored by hash, origin=upload, never public
- `src/core/coverage.py` + `config/requirements.toml`: minimum years per calculation
- `tools/leakcheck.py`: blocks private terms; the denylist is stored as hashes

## What the reader can and cannot read

The reader uses rules, not AI, so it reads the layouts it has been taught and reports clearly
when it cannot read something. **No layout is guaranteed.** Every report it reads is checked:
balance sheet totals, rows adding up to subtotals, and cash flow tying to the balance sheet.
A report is switched on only when all three statements are read and every check passes.
Anything else stays switched off until a person has looked at it.

Reads:
- Text PDFs (not scans) in English: balance sheet, income statement, cash flow statement.
- Annual, half-year and quarterly reports. Quarterly columns are stored by period length:
  Q1-Q4 (three months), H1 (six months), 9M (nine months year to date), 12M (a full year
  printed in a quarterly report, unaudited). FY is kept for audited annual reports.
  A period type is a length ending on the value's `period_end`: for a company whose year
  ends in June, H1 is the six months to 31 March. Quarter names (Q1-Q4) follow the calendar
  month of the period end, not the company's own numbering.
- USD and KHR columns side by side, units in full or thousands (`KHR'000`), group and company
  columns (the group is stored), titles below a registration block, dates written as
  `31.12.2024` or `31 December 2024`, an opening 1 January column, totals printed without a label.

Does not read (yet):
- Scanned pages (needs OCR), Khmer-language reports, statements attached as a separate file.
- Statement of changes in equity and the notes (only checked for, not stored).
- Layouts it has not seen. These show up as "no statements found" or as failed checks, never as silent numbers.

When a report is not read, the reason is recorded per document (`documents.read_status`).

## Standardised statements (for programs)

`export ID --format json|csv`, or the API: `/v1/companies/{id}/statements.json` / `.csv`.
One row per printed value, the same columns for every company. `value` is the full amount as
an exact decimal string. `line_item` is a standard code (`/v1/standard-items`) when the printed
line matches one, otherwise the company's own line with `is_standard = false`. Nothing is
estimated or merged. JSON Schema: `/v1/schema/statements.json`.

## Value lens

`value ID` picks the model that fits the company's sector (`config/sectors.toml`): residual
income for banks; dividend discount, free cash flow to equity or a regulated-return view for
utilities and ports; book NAV for property; a two-stage FCFF DCF for the rest; "not handled"
when nothing fits. The result is a range (bear / base / bull), with every step and every input.

Every market and country input lives in `config/market.toml` and `config/countries/KH.toml`
with its source and date; edit a value and the valuation follows. `verified = true` is
optional (a person checked it against the source); unchecked inputs are used and listed next
to the result. The share price is only compared with the
range, never used to build it. Educational estimates, not investment advice.

## Overview (what the statements say)

`src/report/overview.py` turns the report's own numbers into plain sentences, one card per topic:
growth, profitability, cash, debt and safety, and what it means for the value (return on equity against
the cost of equity, price to book, price to earnings, market cap against the model's range). Code writes
every sentence; a topic without the numbers it needs is named as missing, never guessed. Rules of thumb
(net debt above 3x EBITDA, interest cover below 2x) are labelled as such. No buy or sell words.

There is no AI-written text in the app: every sentence is built by code from the numbers. For a written
report, give the statements (the CSV or Excel export) to a strong AI model of your choice.

## How it stays fast

Work is done once, when the data changes, and the app only ever loads what the page shows:

- **One company at a time.** A page loads that company's values only; other companies are never touched.
- **Built once per change.** Each company's page and Excel workbook are kept in memory and rebuilt only
  when its data changes. A fingerprint of everything the page depends on (values, review actions,
  files switched on or off, sector, prices, settings files) is checked in about a millisecond.
- **Heavy PDF work is never in the way.** Reading a PDF for the disclosure checklist runs in a separate,
  low-priority worker process, once per file; the result is saved under `data/cache/` and reused after
  restarts. A page that is still waiting for it opens anyway and fills that section in when it is ready.
- **Page images** for "Check numbers" are drawn once per page and saved; the browser keeps them too.
- **Database:** SQLite in WAL mode (reading and writing never block each other), indexed by company,
  and "newest report wins" is a single join rather than a lookup per value.

Measured 2026-11-19 on a stress copy with a made-up company holding ten times ACLEDA's data (310 reports,
41,570 values): a cached page 4 ms, first build 0.34 s; real companies 0.02-0.08 s first build, under 1 ms
cached; switching companies during start-up 0.02-0.06 s.

## One report at a time

Uploads are added one file at a time, on purpose: each file goes through the three checks (safe file,
statements found, sums) and you see what was read before adding the next. Batch upload is left out so a
wrong or unreadable file never slips in unseen.
