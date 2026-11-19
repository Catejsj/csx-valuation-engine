# Commands

Run from this folder: `./csx <command>` on Mac and Linux, `csx <command>` on Windows (`.\csx` in
PowerShell). `csx` sets itself up the first time it runs.

## Everyday

| Command | What it does |
|---|---|
| `csx` | Open the app. Fetches new reports in the background once a week. Options: `--no-update`, `--no-open`, `--port 8770` |
| `csx update` | Download new annual reports, read them, refresh prices and the exchange rate |
| `csx update --all` | The same, plus half-year and quarterly reports |
| `csx value ABC` | Valuation with every step (`--json` for the full result) |
| `csx export ABC` | Excel workbook, saved in the current folder |
| `csx export ABC csv` | Standardised statements as CSV (`json` for JSON; `--public` leaves out your uploads; `--out FILE`) |
| `csx api` | Read-only JSON API at http://127.0.0.1:8780/v1 (official filings only) |
| `csx upload FILE.pdf --issuer ABC --name "ABC Plc"` | Add your own report from the terminal (the app's Files panel does the same) |
| `csx test` | Run the tests |
| `csx help` | Short list of commands |

## Less often

| Command | What it does |
|---|---|
| `csx collect` | Only download reports (`--kind annual quarterly`, `--symbol ABC`, `--index-only` to list without downloading) |
| `csx read` | Only read downloaded reports (`--kind annual semiannual quarterly`, `--symbol ABC`) |
| `csx market` | Only prices and the exchange rate (`--full` downloads the whole history again) |
| `csx coverage ABC` | Years of data and which calculations they allow |
| `csx export-api FOLDER` | Write the API as static files that any web host can serve |
| `csx extract FILE.pdf --issuer ... --name ... --filing-id ... --url ... --published YYYY-MM-DD` | Read one PDF directly (`--dry-run` to only check) |
| `csx status` | Where the data is and how much there is |
| `csx review` | The app without the weekly update |

## Settings

The valuation inputs live in `config/`. Change a value and the next valuation uses it.

- `market.toml`: risk-free rate, equity risk premium, terminal growth, industry betas
- `countries/KH.toml`: country risk premium, tax rate, liquidity discounts
- `sectors.toml`: each company's sector and model, and the sectors offered in the app
- `requirements.toml`: how many years each calculation needs
