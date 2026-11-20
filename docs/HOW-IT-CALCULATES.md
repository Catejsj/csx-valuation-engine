# How it calculates

Every number in the app comes from a formula on this page. Each part starts with the idea in plain words,
then the formula, then a real example you can follow with a calculator. Nothing is estimated by AI, and
when a number is missing the app says so instead of filling it in.

You can check everything yourself: the Excel workbook has the ratios as live formulas, and
`csx value ABC` prints every step of a valuation.

**Contents:** [1. Where the numbers come from](#1-where-the-numbers-come-from) ·
[2. The checks](#2-the-checks) · [3. Figures and ratios](#3-figures-and-ratios) ·
[4. The overview](#4-the-overview) · [5. The valuation](#5-the-valuation) ·
[6. When there is no value](#6-when-there-is-no-value)

---

## 1. Where the numbers come from

- **Read from the page.** Each value is read from the financial statements in a report, with its page
  number, so you can always go back and look.
- **Newest report wins.** A report usually shows this year and last year. When two reports give a
  different figure for the same year (a company sometimes changes last year's numbers), the app uses the
  report that covers the later period, and shows you the old and new figures.
- **Only switched-on reports count.** Reports you switch off in Files are left out of everything.
- **Riel to dollars.** Reports in riel are turned into US dollars for the valuation, using the published
  exchange rate: balance sheet figures at the rate on the last day of the year, income and cash flow
  figures at the average rate over the year. The ratios on the company page stay in the report's currency.

## 2. The checks

Before a report is used, the app checks that its numbers hang together:

- **The balance sheet balances:** total assets = total liabilities + total equity.
- **Rows add up:** the lines above a total add up to that total (for example, current assets).
- **Cash ties:** the cash at the end of the cash flow statement matches the balance sheet.

A report where a check fails starts switched off, so a misread number never goes into the valuation
unnoticed.

## 3. Figures and ratios

These are the rows in "The numbers" on each company page. "Average" means the average of this year and
last year, which is fairer when a company grew during the year.

### Most companies

| Row | Formula |
|---|---|
| Revenue, operating profit, profit, cash from operations, total assets, equity, cash | As printed |
| EBITDA | operating profit + depreciation and amortisation |
| Capital spending | payments for property, plant and equipment (negative = money out) |
| **Free cash flow** | cash from operations + capital spending |
| Borrowings and bonds | all borrowings + bonds issued (leases are shown separately) |
| **Net debt** | borrowings and bonds − cash |
| Revenue growth | revenue ÷ last year's revenue − 1 |
| Operating margin | operating profit ÷ revenue |
| EBITDA margin | EBITDA ÷ revenue |
| Net margin | profit ÷ revenue |
| **Return on equity** | profit ÷ average equity |
| Return on assets | profit ÷ total assets |
| Debt to equity | borrowings and bonds ÷ equity |
| Net debt to EBITDA | net debt ÷ EBITDA |
| Interest cover | operating profit ÷ finance costs |
| Current ratio | current assets ÷ current liabilities |
| Cash conversion | cash from operations ÷ profit |

### Banks

Banks don't report revenue, EBITDA or current assets, and their customers' deposits are what they lend
out, not ordinary debt. So they get the measures analysts use for banks:

| Row | Formula |
|---|---|
| **Operating income** (a bank's revenue) | net interest income + net fees and commissions + other income |
| Loan loss charges | as printed (negative = a charge) |
| Operating costs | −(operating income + loan loss charges − profit before tax) |
| Operating income growth, profit growth | this year ÷ last year − 1 |
| Net interest income to assets | net interest income ÷ average total assets |
| **Cost to income** | operating costs ÷ operating income (the share of income spent running the bank) |
| **Cost of risk** | loan losses ÷ average loans |
| Loans to deposits | loans ÷ customer deposits |
| Equity to assets | equity ÷ total assets (the owners' cushion against losses) |
| Return on equity | profit ÷ average equity |
| Return on assets | profit ÷ average total assets |

Operating costs are worked out from the totals, because banks label their expense lines differently from
year to year.

## 4. The overview

The "What the statements say" cards are sentences built from the rows above:

| Card | What it calculates |
|---|---|
| Growth | Yearly growth from the first year to the last: (last ÷ first)^(1 ÷ years) − 1. Uses revenue, operating income for banks, or profit if neither is printed. Also last year's change and how many years it rose. |
| Profitability | Operating and net margin now against the first year, in percentage points; return on equity now and on average. Banks: cost to income and cost of risk. |
| Cash | Cash from operations ÷ profit, added up over all years; how many years free cash flow was positive. Not used for banks. |
| Debt and safety | Net debt, debt to equity, net debt to EBITDA, interest cover, current ratio. Banks: equity to assets, loans to deposits. |
| What it means for the value | Return on equity (average of the last 3 years) against the cost of equity (section 5.1); price to book = market cap ÷ equity; price to earnings = market cap ÷ last year's profit; the model's range against the market cap. |

The coloured dot follows simple rules. Green means rising or comfortable; red means falling; yellow
("worth a closer look") means one of these common rules of thumb is crossed: cash from operations below
profit, free cash flow negative in most years, net debt above 3× EBITDA, interest cover below 2×, or return
on equity below the cost of equity. They're rules of thumb, not verdicts.

---

## 5. The valuation

**The idea.** A company is worth the cash it will produce in the future, brought back to today. Money in
the future is worth less than money now, because investors could put it somewhere else and want a return
for the risk. That required return is the **discount rate**: the higher the risk, the higher the rate,
and the lower the value today.

The app does this in four steps: find the discount rate, pick the model that fits the company's industry,
work out the value, then give a range around it.

### 5.1 The discount rate (cost of equity)

What investors expect to earn for owning the shares:

> **cost of equity = risk-free rate + beta × equity risk premium + country risk premium**

| Input | Value | What it means | Source |
|---|---|---|---|
| Risk-free rate | 5.07% | What a US government bond pays (10-year) | US Treasury, 18 Nov 2026 |
| Equity risk premium | 4.23% | The extra return investors want for shares over safe bonds, in a mature market | Damodaran (NYU) |
| Country risk premium | 7.12% | The extra return for investing in Cambodia | Damodaran (NYU) |
| Beta | by industry | How strongly the industry moves with the market (1 = like the market) | Damodaran, emerging-market industry betas |

All values live in `config/market.toml` and `config/countries/KH.toml` with their source and date. Change
one and the next valuation follows.

**Example, ACLEDA (a bank, beta 0.586):** 5.07% + 0.586 × 4.23% + 7.12% = **14.67%**.

**Adjusting beta for debt.** A company with more debt is riskier for its owners. The industry beta is
first stripped of debt ("unlevered"), then adjusted to this company's own debt:

> **beta = unlevered beta × (1 + (1 − tax rate) × debt ÷ market value of equity)**

The tax rate is Cambodia's 20%. Banks keep the industry beta as it is, because for a bank debt is part of
the business, not financing.

**The rate for the whole company (WACC).** When a model values the whole business (owners and lenders
together), it uses the average cost of both, weighted by size:

> **WACC = equity share × cost of equity + debt share × cost of debt × (1 − tax rate)**

The cost of debt is the company's own finance costs ÷ its debt, if that looks sensible; otherwise the
risk-free rate + Cambodia's default spread (4.67%). Interest is tax-deductible, hence the (1 − tax).

### 5.2 Which model

| Industry | Model | Why |
|---|---|---|
| Banks | Residual income | A bank's value is its book equity plus whatever it earns above what investors require |
| Utilities and ports | Dividend model; or cash flow to equity if it pays out under 30% of profit; or regulated return if that cash flow is negative | These pay steady dividends, or invest heavily in assets that earn a set return |
| Property | Book net asset value | Value sits in land and buildings on the balance sheet |
| Everyone else | Free cash flow to the firm (two stages) | Values the cash the whole business produces |

Companies on the exchange have their industry set in `config/sectors.toml`. For your own uploads, you
pick the sector in the app.

### 5.3 Free cash flow to the firm (most companies)

**The idea:** take the cash the business makes after investing, assume it grows for five years, then
grows slowly forever; bring all of it back to today at the WACC; then take away what is owed to lenders.

1. **Free cash flow** each of the last 3 years = cash from operations − capital spending. The **base** is
   their average (one good or bad year doesn't decide the value).
2. **Growth for years 1 to 5** = the company's own yearly revenue growth, kept between 0% and 10%.
3. **Each year's value today** = that year's cash ÷ (1 + WACC)^year.
4. **Terminal value** (everything after year 5) = year-5 cash × (1 + g) ÷ (WACC − g), where g = 2.5% a
   year forever, then brought back to today like the other years.
5. **Enterprise value** = the sum of steps 3 and 4.
6. **Equity value** = enterprise value − debt (borrowings, bonds and leases) + cash.

**Worked example, CamGSM (FY2025, US dollars):**

| Step | Calculation | Result |
|---|---|---|
| Cost of equity | 5.07% + 0.761 × 4.23% + 7.12% | 15.41% |
| WACC | with cost of debt 8.81% (finance costs ÷ debt), tax 20% | 14.17% |
| Free cash flow 2023 | 76.4M − 17.6M | 58.8M |
| Free cash flow 2024 | 47.3M − 30.6M | 16.7M |
| Free cash flow 2025 | 65.4M − 25.7M | 39.6M |
| Base | average of the three | 38.4M |
| Growth years 1–5 | revenue fell 2.96% a year, kept at the 0% floor | 0% |
| Years 1–5 today | 38.4M a year, each ÷ 1.1417^year | 131.2M |
| Terminal value | 38.4M × 1.025 ÷ (0.1417 − 0.025) | 337.1M |
| …today | 337.1M ÷ 1.1417^5 | 173.8M |
| Enterprise value | 131.2M + 173.8M | 305.0M |
| Equity value | 305.0M − debt 338.5M + cash 101.9M | **68.4M** |

The market valued CamGSM at about 1.95bn, far above this. The overview card says what that implies: the
price assumes much more growth, higher returns or lower risk than the model does.

### 5.4 Residual income (banks)

**The idea:** a bank is worth its book equity, plus the value of earning more than investors require on it
(or minus, if it earns less).

1. **Book equity** = the owners' equity from the balance sheet.
2. **Return on equity** = average of the last 3 years.
3. **Excess return** each year = (return on equity − cost of equity) × book equity at the start of the year.
4. Book equity grows each year by return on equity × the share of profit kept (not paid as dividends).
5. **Value** = book equity + the excess returns of years 1 to 5 brought back to today + a terminal value
   for the years after, worked out the same way as in 5.3.

**Worked example, ACLEDA (FY2025):** book equity 1,657M; return on equity 10.70% (average of 10.97%,
8.45%, 12.68%); cost of equity 14.67%. ACLEDA earns about 4 points less than investors require, so each
year's excess return is negative (year 1: (10.70% − 14.67%) × 1,657M = −65.8M). The value comes out
below book: **1,021M**, against a market cap of 1,088M ("within the range").

**Cross-check:** justified price-to-book = (return on equity − g) ÷ (cost of equity − g) = 0.67, so
0.67 × book = 1,117M. When two methods disagree by more than 2×, the app says the value is uncertain.

### 5.5 Dividend, cash flow to equity and regulated return (utilities, ports)

- **Dividend model:** the same five-years-then-forever steps as 5.3, on dividends paid, discounted at the
  cost of equity. Growth = return on equity × the share of profit kept, between 0% and 10%.
- **Cash flow to equity** (when less than 30% of profit is paid out): cash from operations − capital
  spending + new borrowing − repayments − interest, then the same steps.
- **Regulated return** (when that cash flow is negative because the company is building a lot): the
  residual income steps of 5.4, since a regulated utility earns a return on the assets it owns.

### 5.6 Book net asset value (property)

Value = the owners' book equity. The property's market value is in the notes, which the app doesn't read
yet, so it shows a single book value and invents no range.

### 5.7 The range

The value depends on two guesses, so the app shows what happens when they are a bit worse or better:

| | Careful (bear) | Base | Hopeful (bull) |
|---|---|---|---|
| Discount rate | +1 point | as worked out | −1 point |
| Long-term growth | 2.0% | 2.5% | 3.0% |

The full 3 × 3 table is in "How it is worked out" in the app.

**Market cap against the range.** Market cap = share price × number of shares. The app only says whether
it is below, within or above the range, never "buy" or "sell".

**Liquidity.** Shares that rarely trade are harder to sell, so buyers may pay less. The band comes from the
share of trading days with no trades in the last year:

| Days with no trades | Band | Possible discount |
|---|---|---|
| under 20% | low | 0–10% |
| 20% to 50% | medium | 10–20% |
| over 50% | high | 20–30% |
| not listed (your uploads) | unlisted | 25–35% |

These bands are judgement ranges (no local study exists). They're shown next to the value, never applied
silently.

## 6. When there is no value

The app says so instead of forcing a number:

| Message | Why |
|---|---|
| No model fits yet | No industry is set; pick a sector in the Value section |
| Not enough history | The cash flow, bank and dividend models need 3 full years of statements (property needs 1) |
| No meaningful value | Free cash flow is negative on average, book equity is zero or negative, or debt is larger than the value of the cash flows |

**Every input not yet checked by a person** is listed next to the result, with its source and date.
