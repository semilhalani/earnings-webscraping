# Finviz Earnings Scraper

A Python scraper that collects quarterly earnings data from Finviz for about 5,300 US tickers and loads it into Google Sheets. It runs continuously on GitHub Actions, picks up where the last run stopped, and is checked by tests and CI on every change.

This is the second phase of my [US Equity Screener & Scoring Engine](https://github.com/semilhalani/us-equity-screener), not a separate project. It reads its ticker list from the screener's `Raw_Universe` sheet, but writes to a separate test spreadsheet, so the live scoring workbook stays untouched until this phase is proven and merged in.

## What it collects

For each ticker, the scraper reads four widgets on the Finviz financials page and writes each one to its own table:

| Table | What it holds |
|---|---|
| `Raw_EPSHistory` | EPS estimate, reported value, surprise and surprise % per quarter |
| `Raw_GAAPEPSHistory` | The same fields for GAAP EPS, which can differ from adjusted EPS |
| `Raw_RevenueHistory` | Revenue estimate, reported value, surprise and surprise % per quarter |
| `Raw_PostEarningsMoves` | Report date, session (before or after market), RSI, open, high, low and close, and the stock's move from 3 days before to 1 week after each report, alongside SPY's move |

Four more tables keep track of the runs and the data quality:

| Table | What it holds |
|---|---|
| `ScraperProgress` | One row per workflow run: where it started, how far it got, the last ticker processed and how it ended (`running`, `stopped on time`, `sweep finished`, `limit reached` or `crashed`). Each run resumes from the newest row |
| `Raw_LastChecked` | One row per ticker: when it was last checked and whether that worked (`OK`, `NO_DATA` or `FAILED`) |
| `Raw_DataFlags` | Values that look wrong on Finviz (see below), with when each problem was first and last seen. A row deletes itself once Finviz shows a normal value again |
| `Raw_ScrapeIssues` | Every ticker in the current sweep that failed or had no earnings data, with the reason. Cleared at the start of each sweep |

## Why Selenium

Finviz builds these earnings widgets in the browser with JavaScript after the page loads. A plain HTTP request only gets the page before that happens, so the data is not there. The scraper uses Selenium with headless Chrome to load the page as a real browser would, waits for the widgets to appear, then parses the finished HTML with BeautifulSoup.

## How the data is loaded

Rows are matched on **ticker and quarter**, never on their position, so a skipped or failed ticker cannot shift data into the wrong row. On every sweep, every quarter Finviz shows is compared with what the sheet already has:

| Finviz now shows | What happens |
|---|---|
| A quarter the sheet does not have | The row is added |
| The same values | Nothing is written |
| A changed value (a revision, or a quarter that just reported) | The row is updated |
| A blank where the sheet has a value | The sheet keeps its value, and the cell is flagged `KEPT_OLD_VALUE` |
| 0 where the sheet had a non-zero value | The 0 is written, and the cell is flagged `CHANGED_TO_ZERO` |
| Nothing for a quarter (it has scrolled off the page) | The row is kept as it is |

- **`last_updated` means "last changed".** It moves only when a value in that row changes, so an old date can still be today's data. `Raw_LastChecked` shows when each ticker was last checked. Both are explained in a note on the column headers in the sheet.
- **Tiny rounding differences are not changes.** Numbers are compared as numbers (`"102.77"` equals `102.77`), and differences smaller than a billionth of the value (computer rounding, such as 124.30B stored as 124,299,999,999.99998) are ignored. The smallest change Finviz can display is millions of times larger, so no real revision is skipped.
- **Correct ordering across years and reporting styles.** Sorting labels like `Q4 '24` and `Q1 '25` as text puts them in the wrong order, so every row gets a `quarter_sort` number: year × 10 + a slot. Q1 to Q4 use slots 1 to 4. Semi-annual reporters (`S1`/`S2`, or `H1`/`H2`) use 5 and 6, and annual reporters (`FY`) use 9. These slots never collide. Any label the scraper does not recognise is kept and printed as a loud warning, never silently dropped.
- **No data is not the same as failed.** ETFs and some unlisted tickers have no earnings widgets at all. These are recorded as `NO_DATA`, which is expected, and kept separate from `FAILED`, which is worth investigating.
- **A safe receiver.** Rows are sent to a Google Apps Script web app (`finviz_data_receiver.gs`), which applies the rule above. It writes under `LockService`, so two requests arriving at once cannot both add the same row. It also matches columns by name, so adding a new field never shifts data into the wrong column. The same rule exists in Python for the local CSV copy, and both are tested against one shared set of cases.

## Schedule and reliability

The workflow in `.github/workflows/scrape-earnings.yml` runs continuously as a chain: each run scrapes for 45 minutes, stops cleanly, and starts the next run itself. GitHub's own schedule proved unreliable on this repo: an hourly schedule actually starts only 3 to 5 runs a day, at irregular times, while runs started by the chain begin immediately. If a run fails, the next one is still started, unless the two runs before it also failed: three failures in a row stop the chain, so a real fault cannot turn into a loop. The hourly schedule stays in place only as a backup, to restart a stopped chain. The workflow can also be started by hand from the Actions tab; a manual run limited to the first N tickers is a quick test and does not start the chain.

- **Resumable checkpoint.** Results are saved every 20 tickers, together with the resume cursor. Each run continues from the cursor, so chained runs cover the whole universe and an interrupted run loses at most one batch. A new sweep starts only once the previous one has actually finished.
- **Clean stop.** Each run scrapes for a 45-minute budget, then saves its progress, sorts the sheets and exits normally, so a healthy run always ends green.
- **Hard limits.** The budget is only checked between tickers, so two more limits stop a run from freezing: one ticker may take at most 3 minutes (its browser is then force-closed, the ticker counts as `FAILED` and the run moves on), and all the writes of one checkpoint share a 5-minute save window (a save that keeps failing stops retrying, and its rows are collected again on the next sweep). The step and job `timeout-minutes` (58 and 65) are only a last safety net; a run cut off by them leaves its progress row at `running`.
- **Concurrency group, and the incident it fixed.** A manual run and a scheduled run once overlapped. Both read the same cursor, scraped the same tickers, doubled the load on Finviz and ran until both hit the 5-hour 40-minute timeout. The workflow now uses one fixed concurrency group shared by both trigger types, with `cancel-in-progress: false`, so at most one run is going and one is waiting.
- **Retries.** Writes to the web app wait up to 120 seconds and retry with longer waits each time, within the save window. A table write that still fails is counted and reported at the end of the run, and its rows stay in the run's CSV artifact.
- **Logs.** The scraper step runs with `PYTHONUNBUFFERED=1`, so every line reaches the log immediately, even if the machine dies mid-run.
- **Fixed environment.** Both workflows run on `ubuntu-24.04` rather than `ubuntu-latest`, so a new Ubuntu release cannot change the system, Chrome or Python under the scraper without a deliberate change.
- **Secrets.** The web app URL and the ticker spreadsheet ID are stored in GitHub Secrets, never in the code.
- **Run output.** Each run's CSV copies of the data are uploaded as a downloadable artifact, kept for 14 days. A failed upload does not fail the run.

### Bugs found and fixed in production

- Progress used to reset to the first ticker every midnight, so a sweep longer than a day never reached later tickers. Now it resets only when a sweep is complete.
- The resume cursor was read from the ticker spreadsheet instead of the data spreadsheet. That file has no progress tab, which looks exactly like "no progress yet", so every run quietly restarted from the first ticker. It now reads the spreadsheet it writes to.
- Google Sheets turned a date-like progress key into a real date, so it no longer matched, and every checkpoint added a new row. Each run now writes its own row, keyed `run-<number>`.
- A full-sheet sort ran after every checkpoint, over 267 times per sweep, and caused write timeouts. Sorting now happens once per run, from a `finally` block.
- Web app writes crashed whole runs on a 30-second timeout, and the reads at start-up were not retried at all. Both now use longer timeouts and retries.
- Price reaction rows were frozen once first saved, so a row saved on report day never got its later prices. They were then frozen once the one-week price existed, which still kept prices scraped mid-day (about 284 rows) and later revisions (for example a GAAP EPS saved as 6.77 that Finviz later showed as 6.98). The compare-every-sweep rule above replaced freezing.
- Finviz writes prices of 1,000 and above with commas (`5,828.00`), which Python could not read, so 588 price rows across 124 tickers were blank. Commas are now removed first. Rows for old quarters that Finviz no longer shows could not be recovered.
- A run froze for 45 minutes until GitHub's machine gave up, and its log was empty because Python had not written its output yet. This led to the hard limits and unbuffered logs above.

## Tests and CI

Every push and pull request runs one CI job, `checks` (`.github/workflows/ci.yml`), in about a minute. The `main` branch is protected: changes arrive through pull requests, and a pull request can only be merged once `checks` passes. Merges are squashed, and auto-merge merges a pull request by itself once it goes green.

The job runs four checks. None of them touches the network, Finviz, Chrome or Google: the tests use saved pages, temporary files and stand-ins.

| Check | What it does |
|---|---|
| `ruff check .` | Lints the Python code (the rules are in `pyproject.toml`) |
| `actionlint` | Lints both workflow files, including the shell commands inside them (with `shellcheck`) |
| `pytest` | Runs the Python tests below |
| `node --test tests/receiver.test.js` | Tests the Apps Script receiver with Node, the same JavaScript engine Apps Script uses |

| Test file | What it covers |
|---|---|
| `tests/test_quarters.py` | Period labels (straight and curly apostrophes, `S1`, `H2`, `FY`, unknown labels) and their sort numbers, including the `Q4 '24` before `Q1 '25` year boundary |
| `tests/test_numbers.py` | Turning Finviz text into numbers: `K`/`M`/`B`/`T`, signs, percentages, dashes and commas |
| `tests/test_parse_tables.py` | A saved, trimmed copy of a real Finviz page (`tests/fixtures/finviz_nvr_earnings.html`), checked value by value through the parsers and row builders |
| `tests/test_refresh_rule.py` | The compare-every-sweep rule against shared cases (`tests/fixtures/refresh_cases.json`), the local CSV copy using temporary files, and the direct Google Sheets path using a stand-in worksheet |
| `tests/receiver.test.js` | The real `finviz_data_receiver.gs`: the same shared cases, dates, the `Raw_DataFlags` lifecycle and a full write into a stand-in sheet |
| `tests/test_run_limits.py` | The 3-minute ticker limit (including killing a stand-in browser's processes) and the checkpoint save window, using a pretend clock |

The tool versions are pinned in `requirements-dev.txt`, so a new release cannot turn CI red without a change to the code. Bug fixes were added test first: the new tests were pushed, shown failing in CI, and then the fix turned them green.

To run the same checks on your own machine (the Node test needs [Node.js](https://nodejs.org)):

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
ruff check .
actionlint
python -m pytest
node --test tests/receiver.test.js
```

## Running it

Requires Python 3.9 or newer and Google Chrome.

```bash
pip install -r requirements.txt

# Check one ticker in a visible browser window. Nothing is written to Sheets.
python finviz_earnings_scraper.py --test LUNR

# Check a small mixed set of tickers. Nothing is written to Sheets.
python finviz_earnings_scraper.py --test-batch

# Scrape real tickers but only write local CSV previews.
python finviz_earnings_scraper.py AAPL NVDA --dry-run

# Full run, as the workflow does it.
python finviz_earnings_scraper.py --tickers-from-sheet --web-app-url YOUR_WEB_APP_URL
```

Every option can also be set with an environment variable or a local `.env` file (kept out of git), for example `FINVIZ_WEB_APP_URL`, `FINVIZ_TICKERS_FROM_SHEET`, `FINVIZ_TICKERS_SHEET_NAME` and `FINVIZ_TICKERS_LIMIT`. The test flags (`--test`, `--test-batch` and `--test-baseline`) can only be set on the command line, on purpose. If one were left switched on in a settings file, every scheduled run would quietly stay in test mode and write nothing.

`--test-baseline` is a speed check. It times browser start-up, a simple unrelated page and one Finviz page in the same session, to tell a slow machine apart from Finviz responding slowly.

To write to Google Sheets, deploy `finviz_data_receiver.gs` as a web app from the Apps Script editor (Deploy, New deployment, Web app) and pass its URL. To update it later, use Deploy, Manage deployments, edit the existing deployment and choose a new version, so the URL stays the same. The script also supports a Google service account route through `--sheet-id` and `--creds`. That route needs `gspread` and `google-auth`, which are not in `requirements.txt`.

## Known limitations

- A ticker takes about 3 to 4 seconds on GitHub's runners, plus a pause between tickers out of courtesy to Finviz. One 45-minute run covers about 240 tickers, so a full sweep takes about 22 chained runs, roughly 17 hours.
- When GitHub's machine itself dies mid-run, only the backup schedule restarts the chain, which can take a few hours.
- Not built yet: skipping tickers that never have data, detecting stock splits, and splitting a sweep across parallel jobs.
- Finviz can change its page layout without notice, which would break the selectors. Finviz's terms also restrict automated scraping, so the scraper waits between tickers.

## Files

| File | Purpose |
|---|---|
| `finviz_earnings_scraper.py` | The scraper: fetch, parse, the compare-every-sweep rule, checkpointing, hard limits and the command line |
| `finviz_data_receiver.gs` | Apps Script web app that receives rows, applies the same rule, keeps `Raw_DataFlags`, and serves the ticker list and progress cursor |
| `.github/workflows/scrape-earnings.yml` | The scraping workflow (the chain, plus the backup schedule) |
| `.github/workflows/ci.yml` | The CI checks on every push and pull request |
| `tests/` | The tests and their saved inputs (`tests/fixtures/`) |
| `pyproject.toml` | Settings for ruff and pytest |
| `requirements.txt` | The scraper's Python dependencies |
| `requirements-dev.txt` | The pinned check tools: pytest, ruff, actionlint, shellcheck |
