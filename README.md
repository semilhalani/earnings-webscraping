# Finviz Earnings Scraper

A Python scraper that collects quarterly earnings data from Finviz for about 5,300 US tickers and loads it into Google Sheets. It runs on a schedule in GitHub Actions and picks up where the last run stopped.

This is the second phase of my [US Equity Screener & Scoring Engine](https://github.com/semilhalani/us-equity-screener), not a separate project. It reads its ticker list from the screener's `Raw_Universe` sheet, but writes to a separate test spreadsheet, so the live scoring workbook stays untouched until this phase is proven and merged in.

## What it collects

For each ticker, the scraper reads four widgets on the Finviz financials page and writes each one to its own table:

| Table | What it holds |
|---|---|
| `Raw_EPSHistory` | EPS estimate, reported value, surprise and surprise % per quarter |
| `Raw_GAAPEPSHistory` | The same fields for GAAP EPS, which can differ from adjusted EPS |
| `Raw_RevenueHistory` | Revenue estimate, reported value, surprise and surprise % per quarter |
| `Raw_PostEarningsMoves` | Report date, session (before or after market), RSI, open, high, low and close, and the stock's move from 3 days before to 1 week after each report, alongside SPY's move |

Two more tables keep track of the runs themselves:

| Table | What it holds |
|---|---|
| `ScraperProgress` | One row holding the resume cursor: how far the current sweep has got and the last ticker processed |
| `Raw_ScrapeIssues` | Every ticker in the current sweep that failed or had no earnings data, with the reason |

## Why Selenium

Finviz builds these earnings widgets in the browser with JavaScript after the page loads. A plain HTTP request only gets the page before that happens, so the data is not there. The scraper uses Selenium with headless Chrome to load the page as a real browser would, waits for the widgets to appear, then parses the finished HTML with BeautifulSoup.

## How the data is loaded

- **Upsert on ticker and quarter.** A new ticker and quarter pair is added as a new row. An existing row is never duplicated. A forecast row (estimate only) is updated in place until the reported figure arrives, then it is frozen. Price reaction rows are written once and never changed.
- **Correct ordering across years and reporting styles.** Sorting labels like `Q4 '24` and `Q1 '25` as text puts them in the wrong order, so every row gets a `quarter_sort` number: year × 10 + a slot. Q1 to Q4 use slots 1 to 4. Semi-annual reporters (`S1`/`S2`, or `H1`/`H2`) use 5 and 6, and annual reporters (`FY`) use 9. These slots never collide. Any label the scraper does not recognise is kept and printed as a loud warning, never silently dropped.
- **No data is not the same as failed.** ETFs and some unlisted tickers have no earnings widgets at all. These are recorded as `NO_DATA`, which is expected, and kept separate from `FAILED`, which is worth investigating.
- **A safe receiver.** Rows are sent to a Google Apps Script web app (`finviz_data_receiver.gs`). It writes under `LockService`, so two requests arriving at once cannot both add the same row. It also matches columns by name, so adding a new field never shifts data into the wrong column.

## Schedule and reliability

The workflow in `.github/workflows/scrape-earnings.yml` runs four times a day, at 00:00, 06:00, 12:00 and 18:00 UTC, and can also be started by hand from the Actions tab. A manual run can be limited to the first N tickers for a quick test.

- **Resumable checkpoint.** Results are saved every 20 tickers, together with the resume cursor. Each run continues from the cursor, so chained runs cover the whole universe and an interrupted run loses at most one batch. A new sweep starts only once the previous one has actually finished, however many days that takes.
- **Timeout.** Each run stops at 340 minutes, inside GitHub's 6-hour limit per job, and the next run resumes from the last checkpoint.
- **Concurrency group, and the incident it fixed.** A manual run and a scheduled run once overlapped. Both read the same cursor, scraped the same tickers, doubled the load on Finviz and ran until both hit the 5-hour 40-minute timeout. The workflow now uses one fixed concurrency group shared by both trigger types, with `cancel-in-progress: false`. A new run waits in a queue behind the running one instead of starting alongside it.
- **Retries.** Writes to the web app wait up to 120 seconds by default and retry up to four times, with longer waits each time. A table write that still fails is counted and reported at the end of the run, and its rows stay in the run's CSV artifact.
- **Secrets.** The web app URL and the ticker spreadsheet ID are stored in GitHub Secrets, never in the code.
- **Run output.** Each run's CSV copies of the data are uploaded as a downloadable artifact, kept for 14 days.

### Bugs found and fixed in production

- Progress used to reset to the first ticker every midnight, so a sweep longer than a day never reached later tickers. Now it resets only when a sweep is complete.
- Google Sheets turned a date-like progress key into a real date, so it no longer matched, and every checkpoint added a new row. Progress is now keyed on a fixed value, `current`.
- A full-sheet sort ran after every checkpoint, over 267 times per sweep, and caused write timeouts. Sorting now happens once per run, from a `finally` block.
- Web app writes crashed whole runs on a 30-second timeout. They now use the longer timeout and retries described above.

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

# Full run, as the scheduled workflow does it.
python finviz_earnings_scraper.py --tickers-from-sheet --web-app-url YOUR_WEB_APP_URL
```

Every option can also be set with an environment variable or a local `.env` file (kept out of git), for example `FINVIZ_WEB_APP_URL`, `FINVIZ_TICKERS_FROM_SHEET`, `FINVIZ_TICKERS_SHEET_NAME` and `FINVIZ_TICKERS_LIMIT`. The test flags (`--test`, `--test-batch` and `--test-baseline`) can only be set on the command line, on purpose. If one were left switched on in a settings file, every scheduled run would quietly stay in test mode and write nothing.

`--test-baseline` is a speed check. It times browser start-up, a simple unrelated page and one Finviz page in the same session, to tell a slow machine apart from Finviz responding slowly.

To write to Google Sheets, deploy `finviz_data_receiver.gs` as a web app from the Apps Script editor (Deploy, New deployment, Web app) and pass its URL. The script also supports a Google service account route through `--sheet-id` and `--creds`. That route needs `gspread` and `google-auth`, which are not in `requirements.txt`.

## Known limitations

- On GitHub's shared runners, a ticker takes about 45 seconds, against roughly 8 to 13 seconds locally. A full sweep therefore takes more than a day of chained runs.
- Not built yet: skipping tickers that never have data, and splitting a sweep across parallel jobs.
- Finviz can change its page layout without notice, which would break the selectors. Finviz's terms also restrict automated scraping, so the scraper waits between tickers.

## Files

| File | Purpose |
|---|---|
| `finviz_earnings_scraper.py` | The scraper: fetch, parse, upsert, checkpointing and the command line |
| `finviz_data_receiver.gs` | Apps Script web app that receives rows and serves the ticker list and progress cursor |
| `.github/workflows/scrape-earnings.yml` | The scheduled GitHub Actions workflow |
| `requirements.txt` | Python dependencies |
