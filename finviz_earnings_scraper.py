"""
finviz_earnings_scraper.py

Scrapes three widgets from a Finviz stock financials page
(e.g. https://finviz.com/stock?t=lunr&p=d&ty=ea):

1. "EPS Performance and Forecast"       -> estimate, reported, surprise, surprise_pct   per quarter
2. "Revenue Performance and Forecast"   -> estimate, reported, surprise, surprise_pct   per quarter
3. "Price Reaction to Earnings Reports" -> report date, session, RSI, and price-reaction
                                            data (-3d..+1wk, vs SPY) per quarter

Storage: every run writes a local CSV preview (Raw_EPSHistory.csv,
Raw_RevenueHistory.csv, Raw_PostEarningsMoves.csv) you can open and check
before trusting anything to Sheets — no per-ticker files, one growing file
per dataset. Optionally, pass --sheet-id/--creds to ALSO write directly
into a Google Sheet via a service account. Both storage paths use the same
upsert logic: existing (ticker, quarter) rows are never duplicated, and a
forecast-only row is updated in place once its "reported" value comes in.

Some tickers (ETFs, some OTC/unlisted tickers) genuinely have no earnings
widgets on Finviz at all — these are detected and reported as NO_DATA, a
normal expected outcome, distinct from a real FAILED (worth investigating).

WHY SELENIUM: the EPS/Revenue/Price-Reaction widgets are rendered
client-side by Finviz's JavaScript (React) — a plain HTTP GET cannot see
them. This has been directly confirmed in prior testing on this project;
see the project history if you need the full explanation.

REQUIREMENTS:
    pip install selenium beautifulsoup4 gspread google-auth

USAGE:
    # 1. Sanity-check on ONE ticker first — always do this before anything else.
    #    Opens a visible browser window so you can watch it work.
    python finviz_earnings_scraper.py --test

    # 2. Test on a small, deliberately varied set of tickers (mega-cap, ETF,
    #    penny stock, etc.) — still no Sheets write, just prints a summary.
    python finviz_earnings_scraper.py --test-batch
    python finviz_earnings_scraper.py --test-batch LUNR AAPL SPY GME KO

    # 3. Real run, dry — scrapes for real but does NOT write to Sheets.
    python finviz_earnings_scraper.py LUNR AAPL NVDA --dry-run

    # 4. Real run, writes to your Google Sheet.
    python finviz_earnings_scraper.py LUNR AAPL NVDA --sheet-id YOUR_SHEET_ID --creds service_account.json

    # 5. Full universe (once 1-4 above are all confirmed working).
    python finviz_earnings_scraper.py $(cat tickers.txt) --sheet-id YOUR_SHEET_ID --creds service_account.json

GOOGLE SHEETS SETUP (one-time):
    1. Create a service account in Google Cloud Console, download its JSON key.
    2. Share your target Google Sheet with that service account's email
       address (found in the JSON file, looks like ...@...iam.gserviceaccount.com),
       giving it Editor access.
    3. Pass the JSON key file's path via --creds, and the Sheet's ID
       (the long string in its URL between /d/ and /edit) via --sheet-id.

CAVEATS:
- Finviz's Terms of Service restrict automated scraping — read them before
  running this at scale, and keep --delay reasonable (default 3s/ticker).
- Finviz's frontend HTML/class names can change without notice; the
  selectors below are current as of this project's testing but are not
  guaranteed to stay that way.
- Not every ticker will have all three widgets (e.g. ETFs typically have no
  EPS/Revenue estimates). Missing widgets are logged and skipped per-ticker,
  not treated as a fatal error — see scrape_ticker().
"""

import argparse
import csv
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException


class NoFinancialsDataError(Exception):
    """Raised when a ticker's page loads fine but has zero financials
    widgets on it — expected for ETFs (no earnings) and some OTC/unlisted
    tickers Finviz doesn't cover. Not a bug, and not worth retrying."""


def load_env_file(path: str = ".env") -> None:
    """
    Loads simple KEY=value pairs from a local .env file into the process
    environment, WITHOUT overriding any variable already set — so a real
    environment variable (e.g. injected by GitHub Actions from a secret)
    always wins over whatever's in the file. A missing file is completely
    normal and silently ignored; most CI runs won't have one at all.

    This is what lets you keep your Web App URL, spreadsheet ID, etc. out
    of your command history and (as long as .env is gitignored) out of
    any repo you push — instead of retyping the full command every time.
    Every value below can still be overridden per-run with its matching
    --flag without editing the file.
    """
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def _env_int(name: str, default: Optional[int] = None) -> Optional[int]:
    val = os.environ.get(name)
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _env_float(name: str, default: Optional[float] = None) -> Optional[float]:
    val = os.environ.get(name)
    if not val:
        return default
    try:
        return float(val)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    """Parses a boolean-flag env var (e.g. FINVIZ_DRY_RUN=true). Accepts
    1/0, true/false, yes/no, on/off, case-insensitively. An unset or
    empty var falls back to `default` rather than raising."""
    val = os.environ.get(name)
    if not val:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str) -> List[str]:
    """Parses a comma-separated env var into a list of trimmed,
    uppercased, non-empty ticker symbols — e.g.
    FINVIZ_TICKERS=lunr, aapl, spy -> ['LUNR', 'AAPL', 'SPY']. Returns []
    if unset or empty, so it's a safe default for the positional
    `tickers` argument (only used when nothing is typed on the command
    line AND --tickers-from-sheet is off)."""
    val = os.environ.get(name)
    if not val:
        return []
    return [t.strip().upper() for t in val.split(",") if t.strip()]


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# A deliberately varied default test set: a normal small-cap (LUNR), a
# mega-cap (AAPL), an ETF with no earnings data at all (SPY), a volatile
# meme stock (GME), and a stable dividend payer (KO). Widget-availability
# and parsing edge cases are far more likely to show up across a set like
# this than across five similar large-caps.
DEFAULT_TEST_TICKERS = ["LUNR", "AAPL", "SPY", "GME", "KO"]

PRICE_REACTION_COLUMNS = [
    "-3 Days", "-2 Days", "-1 Day", "",
    "Open", "High", "Low", "Close", "",
    "+1 Day", "+2 Days", "+3 Days", "+1 Week", "-1 Week",
]

COLUMN_KEY_MAP = {
    "-3 Days": "m3d", "-2 Days": "m2d", "-1 Day": "m1d",
    "Open": "open", "High": "high", "Low": "low", "Close": "close",
    "+1 Day": "p1d", "+2 Days": "p2d", "+3 Days": "p3d",
    "+1 Week": "p1wk", "-1 Week": "m1wk",
}

EPS_HEADERS = ["ticker", "quarter", "quarter_sort", "estimate", "reported", "surprise", "surprise_pct", "last_updated"]
GAAP_EPS_HEADERS = EPS_HEADERS  # identical shape
REVENUE_HEADERS = EPS_HEADERS  # identical shape
PRICE_HEADERS = (
    ["ticker", "quarter", "quarter_sort", "report_date", "session", "rsi"]
    + [f"{p}_{k}" for p in ("price", "stockpct", "spypct") for k in COLUMN_KEY_MAP.values()]
    + ["last_updated"]
)

DATE_SESSION_PATTERN = re.compile(r"^(.*\d{4})\s+(BMO|AMC)$")


def build_url(ticker: str) -> str:
    return f"https://finviz.com/stock?t={ticker.lower()}&p=d&ty=ea"


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def get_driver(headless: bool = True) -> webdriver.Chrome:
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1400,1200")
    options.add_argument(f"user-agent={USER_AGENT}")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.page_load_strategy = "eager"

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(30)
    return driver


def fetch_html(ticker: str, headless: bool = True) -> str:
    """
    NOTE on this function's history, so this mistake isn't repeated:

    v1 waited for `div.js-financials-widget` to appear (bounded 15s
    timeout). That worked correctly for every real ticker but produced a
    scary-looking (harmless) stack trace for tickers with no widgets at
    all (SPY, ALRT) — a timeout, not a crash.

    v2 "fixed" that by waiting for `document.readyState == "complete"`
    instead. This was WRONG and caused every ticker, including ones with
    real data, to fail — because `page_load_strategy = "eager"` was
    already deliberately chosen (see get_driver() above) specifically to
    AVOID waiting for full page completion, since real-world pages with
    ads/trackers/analytics often never fire the "complete" event at all.
    Explicitly polling for readyState=="complete" reintroduces exactly
    the hang eager was chosen to prevent.

    v3 (this version): back to waiting on the specific widget selector
    (proven to work), but the timeout is now caught and converted into
    NoFinancialsDataError instead of propagating as a raw, scary-looking
    exception. Same wait strategy that worked before; friendlier handling
    of the one case (no widgets exist) that used to look like a crash.
    """
    url = build_url(ticker)
    driver = get_driver(headless=headless)
    try:
        driver.get(url)
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.js-financials-widget"))
            )
        except TimeoutException:
            raise NoFinancialsDataError(
                f"{ticker}: no financials widgets appeared within 15s — "
                "likely an ETF or a ticker Finviz has no earnings coverage for."
            )
        time.sleep(1)  # let charts finish animating in
        return driver.page_source
    finally:
        driver.quit()


def fetch_html_with_retry(
    ticker: str, headless: bool = True, retries: int = 2, retry_delay: float = 2.0
) -> str:
    """Wraps fetch_html() with a couple of retries for genuine transient
    failures (a real chromedriver hiccup, a slow network blip). Does NOT
    retry NoFinancialsDataError — if a ticker has no widgets, retrying
    won't change that, so it's re-raised immediately."""
    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            return fetch_html(ticker, headless=headless)
        except NoFinancialsDataError:
            raise
        except Exception as e:
            last_error = e
            if attempt < retries:
                print(f"  [{ticker}] Fetch failed (attempt {attempt + 1}/{retries + 1}): {e} — retrying...")
                time.sleep(retry_delay)
    raise last_error


# ---------------------------------------------------------------------------
# Numeric / date parsing helpers
# ---------------------------------------------------------------------------

def parse_number(value: Optional[str]) -> Optional[float]:
    """Parses plain numbers and K/M/B/T-suffixed numbers into a float.
    Handles EPS values ("-0.18"), revenue values ("46.60M"), prices
    ("32.42"), and RSI ("72"). Returns None for empty/placeholder values."""
    if value in (None, "", "\u2014", "-"):
        return None
    v = value.strip()
    negative = v.startswith("-")
    v = v.lstrip("+-")
    multiplier = 1.0
    for suffix, mult in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if v.endswith(suffix):
            multiplier = mult
            v = v[:-1]
            break
    try:
        num = float(v) * multiplier
    except ValueError:
        return None
    return -num if negative else num


def parse_percent(value: Optional[str]) -> Optional[float]:
    """Parses percentages, including Finviz's "K%" shorthand for very large
    surprise percentages (e.g. "-2.06K%" -> -2060.0, meaning -2060%).
    Returns the percentage as a plain number (11.91 for "+11.91%"), not a
    fraction — decide at read time whether you want to divide by 100."""
    if value in (None, "", "\u2014", "-"):
        return None
    v = value.strip().rstrip("%")
    negative = v.startswith("-")
    v = v.lstrip("+-")
    multiplier = 1.0
    if v.endswith("K"):
        multiplier = 1000.0
        v = v[:-1]
    elif v.endswith("M"):
        multiplier = 1_000_000.0
        v = v[:-1]
    try:
        num = float(v) * multiplier
    except ValueError:
        return None
    return -num if negative else num


def parse_date_str(value: Optional[str]) -> Optional[str]:
    """"Aug 13, 2024" -> "2024-08-13" (ISO format, sorts correctly as text
    and is recognized as a date by Google Sheets)."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%b %d, %Y").date().isoformat()
    except ValueError:
        return value  # unexpected format — keep the raw string rather than lose it


# Reporting-period prefixes Finviz is known to use, and how each one
# maps to a sort slot within its year (see quarter_sort_key below).
#
# Q = quarterly. The overwhelming majority of tickers.
#
# S / H = semi-annual (half-year) reporters. CONFIRMED LIVE on this
# project (2026-08-19 run): BTI (British American Tobacco), BON
# (Bonduelle), IPX all show "S1 '24" / "S2 '24" instead of Q1-Q4 —
# these are foreign private issuers that file half-year reports
# instead of 10-Qs. Finviz has been observed using "S1"/"S2"; "H1"/
# "H2" is included defensively in case a different ticker uses that
# spelling for the same convention.
#
# FY = annual-only reporters (single report per year, no number).
# NOT yet confirmed live on this project, but the same population of
# foreign issuers that reports semi-annually sometimes reports
# annually instead — included defensively for the same reason S/H
# is, rather than waiting to be surprised by it the way S1/S2
# originally surprised this script (see quarter_sort_key docstring).
PERIOD_SORT_SLOT = {
    "Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4,
    "S1": 5, "S2": 6, "H1": 5, "H2": 6,
    "FY": 9,
}
# Slots are chosen so none of them share the same value mod 10 (1,2,3,4,
# 5,6,9 are all distinct mod 10). Under the year*10+slot scheme that
# guarantees zero collisions between different reporting conventions at
# ANY year offset -- e.g. this rules out "S1 '24" (24*10+5=245) ever
# numerically colliding with some future "Q-something '25/'26/...". An
# earlier draft used slots 11/12/21, which collided with next year's
# Q1-Q4 (e.g. S1 '24 = 251 == Q1 '25 = 251); harmless in practice since
# one ticker never mixes conventions, but not worth even the theoretical
# risk when avoiding it costs nothing.

QUARTER_PATTERN = re.compile(r"(Q[1-4]|S[1-2]|H[1-2]|FY)\s*'\s*(\d{2})")


def _warn_unrecognized_period(raw_label: str) -> None:
    """
    Called whenever a period label doesn't match ANY known prefix
    (Q1-4, S1-2, H1-2, FY). Prints a loud, visible warning instead of
    letting the row silently end up with a blank quarter_sort (or, in
    the price-reaction table, silently vanishing entirely) — that
    silent failure is exactly what happened with S1/S2 before this
    fix, and wasn't caught until someone noticed missing rows by hand.
    A future new convention will now show up in the run's console
    output immediately instead of quietly producing gaps.
    """
    print(f"  UNRECOGNIZED PERIOD FORMAT: {raw_label!r} — doesn't match Q#/S#/H#/FY. "
          f"Row will be kept with the raw label but won't sort correctly. "
          f"Add its prefix to PERIOD_SORT_SLOT once you've confirmed the pattern.")


def normalize_quarter(label: str) -> Optional[str]:
    """
    Normalizes period labels from any of the three tables to a common
    form, e.g. "Q1 '26" (curly apostrophe, price-reaction table) or
    "Q1 '26" (EPS table) -> "Q1 '26". Also recognizes semi-annual
    (S1/S2, H1/H2) and annual (FY) reporter labels — see
    PERIOD_SORT_SLOT above for why those exist.

    Returns None only for labels matching NONE of the known prefixes;
    callers should fall back to the raw label (never silently drop
    the row) and call _warn_unrecognized_period() so it's visible.
    """
    if not label:
        return None
    label = label.replace("\u2018", "'").replace("\u2019", "'")
    match = QUARTER_PATTERN.search(label)
    if not match:
        return None
    return f"{match.group(1)} '{match.group(2)}"


def quarter_sort_key(quarter: Optional[str]) -> Optional[int]:
    """
    Converts a normalized period label into a real, chronologically-
    sortable integer — valid for quarterly, semi-annual, and annual
    reporters alike (see PERIOD_SORT_SLOT above).

    Plain text-sorting "Q# 'YY" labels gets the year boundary WRONG —
    e.g. "Q4 '24" sorts after "Q1 '25" as plain text, since '4' > '1'
    character-by-character, even though Q4'24 is chronologically
    earlier. This gives every row a number where sorting ascending is
    always chronologically correct: year*10 + slot, e.g.
    Q2 '24 -> 242, Q4 '24 -> 244, Q1 '25 -> 251 (244 < 251, correct).

    Q1-Q4 keep their ORIGINAL slot values (1-4) unchanged from before
    this fix — nothing about existing quarterly data's quarter_sort
    numbers changes. S1/S2/H1/H2 get slots 5-6 and FY gets slot 9, all
    previously-unused within the *10 scheme and chosen so none of them
    share a value mod 10 with each other or with 1-4 — that guarantees
    zero collisions between different reporting conventions at ANY year
    offset, not just "shouldn't come up in practice." Sorting only ever
    compares rows from the SAME ticker (see _sort_rows_for_display),
    and no real ticker mixes reporting conventions for its own history
    anyway, but there's no reason not to rule the collision out for free.

    Returns None for anything not in PERIOD_SORT_SLOT — the row still
    gets written (via the "or raw label" fallback in the callers), it
    just won't sort correctly until the new prefix is added above.
    """
    if not quarter:
        return None
    m = re.match(r"(Q[1-4]|S[1-2]|H[1-2]|FY) '(\d{2})", quarter)
    if not m:
        return None
    label, yy = m.group(1), int(m.group(2))
    slot = PERIOD_SORT_SLOT.get(label)
    if slot is None:
        return None
    return yy * 10 + slot

# ---------------------------------------------------------------------------
# Widget 1 & 2: EPS / Revenue Performance and Forecast (identical structure)
# ---------------------------------------------------------------------------

def parse_performance_table(html: str, heading_text: str) -> Dict[str, Dict[str, Optional[str]]]:
    """
    Generalized parser for any "<X> Performance and Forecast" widget —
    covers EPS, GAAP EPS, and Revenue, which all share identical HTML.

    IMPORTANT: matches on the widget's <h4> heading by EXACT text, not
    substring. "EPS Performance and Forecast" is a substring of "GAAP EPS
    Performance and Forecast" — a substring match would sometimes grab the
    wrong widget depending on DOM order. Exact match avoids that entirely.
    """
    soup = BeautifulSoup(html, "html.parser")

    widget = None
    for candidate in soup.select("div.js-financials-widget"):
        heading = candidate.find("h4")
        if heading and heading.get_text(strip=True) == heading_text:
            widget = candidate
            break
    if widget is None:
        raise ValueError(f"Could not find the {heading_text!r} widget on the page.")

    header_cells = widget.select("table thead th")
    quarters_raw = [th.get_text(strip=True) for th in header_cells[1:]]
    quarters = []
    for q_raw in quarters_raw:
        normalized = normalize_quarter(q_raw)
        if normalized is None:
            _warn_unrecognized_period(q_raw)
        quarters.append(normalized or q_raw)

    data: Dict[str, Dict[str, Optional[str]]] = {
        q: {"estimate": None, "reported": None, "surprise": None, "surprise_pct": None}
        for q in quarters
    }

    for tbody in widget.select("table tbody"):
        for row in tbody.select("tr"):
            first_cell = row.select_one("td")
            if first_cell is None:
                continue
            label_span = first_cell.select_one("label span") or first_cell
            label = label_span.get_text(strip=True)

            value_cells = row.select("td")[1:]
            values = [c.get_text("\n", strip=True) for c in value_cells]

            if label == "Estimate":
                for q, v in zip(quarters, values):
                    data[q]["estimate"] = None if v in ("", "\u2014") else v
            elif label == "Reported":
                for q, v in zip(quarters, values):
                    data[q]["reported"] = None if v in ("", "\u2014") else v
            elif label == "Surprise":
                for q, v in zip(quarters, values):
                    if v in ("", "\u2014"):
                        continue
                    parts = [p.strip() for p in v.split("\n") if p.strip()]
                    if len(parts) == 2:
                        data[q]["surprise"], data[q]["surprise_pct"] = parts
                    else:
                        data[q]["surprise"] = v

    return data


def build_performance_row(ticker: str, quarter: str, metrics: Dict[str, Optional[str]], timestamp: str) -> Dict:
    return {
        "ticker": ticker.upper(),
        "quarter": quarter,
        "quarter_sort": quarter_sort_key(quarter),
        "estimate": parse_number(metrics["estimate"]),
        "reported": parse_number(metrics["reported"]),
        "surprise": parse_number(metrics["surprise"]),
        "surprise_pct": parse_percent(metrics["surprise_pct"]),
        "last_updated": timestamp,
    }


# ---------------------------------------------------------------------------
# Widget 3: Price Reaction to Earnings Reports
# ---------------------------------------------------------------------------

def _zip_row_values(row_tds, columns=PRICE_REACTION_COLUMNS) -> Dict[str, str]:
    values = [td.get_text(strip=True) for td in row_tds]
    return {col: val for col, val in zip(columns, values) if col}


def parse_price_reaction_table(html: str) -> Dict[str, Dict]:
    soup = BeautifulSoup(html, "html.parser")

    header_div = soup.select_one("#price-reaction-table")
    if header_div is None:
        raise ValueError("Could not find the Price Reaction to Earnings Reports widget.")

    widget = header_div.find_parent("div", class_="js-financials-widget")
    if widget is None:
        raise ValueError("Could not find the parent widget for the Price Reaction table.")

    table = widget.find("table")
    if table is None:
        raise ValueError("Could not find the Price Reaction table element.")

    results: Dict[str, Dict] = {}

    for tbody in table.select("tbody"):
        rows = tbody.select("tr")
        if len(rows) < 3:
            continue
        price_row, period_row, rsi_row = rows[0], rows[1], rows[2]

        # NOTE: "pr-0.5" is a Tailwind class with a literal dot — use
        # find(class_=...) here, not a CSS selector (a dot in a CSS
        # selector means "start of a new class", so div.pr-0.5 breaks).
        date_div = price_row.find("div", class_="pr-0.5")
        date_session_text = date_div.get_text(strip=True) if date_div else ""
        m = DATE_SESSION_PATTERN.match(date_session_text)
        report_date, session = (m.group(1), m.group(2)) if m else (date_session_text or None, None)

        period_label_el = period_row.select_one("td div.flex.justify-between div")
        period_label_raw = period_label_el.get_text(strip=True) if period_label_el else ""
        quarter = normalize_quarter(period_label_raw)
        if quarter is None:
            # FIX (was previously "continue", which silently DROPPED the
            # whole row — this is exactly why BTI/BON/IPX-style
            # semi-annual reporters were missing entirely from
            # Raw_PostEarningsMoves even though they had EPS/Revenue
            # rows. Now falls back to the raw label, same pattern
            # parse_performance_table already used, so the row is kept
            # (even if quarter_sort can't be computed for it yet) and the
            # gap is visible in the console instead of silent.
            if period_label_raw:
                _warn_unrecognized_period(period_label_raw)
            quarter = period_label_raw or None
        if not quarter:
            continue  # genuinely no period label at all — nothing to key this row on

        rsi_container = rsi_row.select_one("td div.flex.items-center")
        rsi_value = None
        if rsi_container:
            rsi_divs = rsi_container.select("div")
            if len(rsi_divs) >= 2:
                rsi_value = rsi_divs[-1].get_text(strip=True)

        price_data = _zip_row_values(price_row.select("td")[1:])
        stock_pct_data = _zip_row_values(period_row.select("td")[1:])
        spy_pct_data = _zip_row_values(rsi_row.select("td")[1:])

        results[quarter] = {
            "report_date": report_date,
            "session": session,
            "rsi": rsi_value,
            "price": price_data,
            "stock_pct": stock_pct_data,
            "spy_pct": spy_pct_data,
        }

    return results


def build_price_row(ticker: str, quarter: str, entry: Dict, timestamp: str) -> Dict:
    row = {
        "ticker": ticker.upper(),
        "quarter": quarter,
        "quarter_sort": quarter_sort_key(quarter),
        "report_date": parse_date_str(entry.get("report_date")),
        "session": entry.get("session"),
        "rsi": parse_number(entry.get("rsi")),
        "last_updated": timestamp,
    }
    for prefix, source_key, parser in (
        ("price", "price", parse_number),
        ("stockpct", "stock_pct", parse_percent),
        ("spypct", "spy_pct", parse_percent),
    ):
        for col_label, short_key in COLUMN_KEY_MAP.items():
            raw_val = entry.get(source_key, {}).get(col_label)
            row[f"{prefix}_{short_key}"] = parser(raw_val)
    return row


# ---------------------------------------------------------------------------
# Orchestration — one ticker
# ---------------------------------------------------------------------------

def scrape_ticker(ticker: str, headless: bool = True) -> Dict[str, List[Dict]]:
    """
    Returns {"eps": [...], "gaap_eps": [...], "revenue": [...], "price_reaction": [...]}.

    Two distinct failure modes callers should handle differently:
    - NoFinancialsDataError propagates up from here — the page loaded but
      has NO financials widgets at all (ETFs, some OTC/unlisted tickers).
      Expected, not a bug. Callers should record this separately from
      real failures (see test_small_batch/run_batch for the pattern).
    - A ticker missing just ONE of the three widgets (e.g. has EPS but no
      Revenue) is handled internally below — that widget's list comes
      back empty, the other two still populate normally.
    """
    html = fetch_html_with_retry(ticker, headless=headless)
    timestamp = datetime.now(timezone.utc).isoformat()
    result: Dict[str, List[Dict]] = {"eps": [], "gaap_eps": [], "revenue": [], "price_reaction": []}

    try:
        eps_raw = parse_performance_table(html, "EPS Performance and Forecast")
        result["eps"] = [build_performance_row(ticker, q, m, timestamp) for q, m in eps_raw.items()]
    except ValueError as e:
        print(f"  [{ticker}] No EPS data: {e}")

    try:
        # A DIFFERENT widget from plain EPS, not a duplicate — GAAP EPS
        # includes items (one-time charges, stock comp, etc.) that the
        # regular EPS estimate/reported figures typically exclude. For
        # many tickers the two are identical (no adjustments made), but
        # for others they genuinely diverge — e.g. BranchOut Food (BOF)
        # has real EPS coverage across 6 quarters but almost no GAAP EPS
        # analyst coverage at all (confirmed live on this project,
        # 2026-08-19). Was NOT scraped at all before this fix, even
        # though parse_performance_table already supported it generically.
        gaap_eps_raw = parse_performance_table(html, "GAAP EPS Performance and Forecast")
        result["gaap_eps"] = [build_performance_row(ticker, q, m, timestamp) for q, m in gaap_eps_raw.items()]
    except ValueError as e:
        print(f"  [{ticker}] No GAAP EPS data: {e}")

    try:
        revenue_raw = parse_performance_table(html, "Revenue Performance and Forecast")
        result["revenue"] = [build_performance_row(ticker, q, m, timestamp) for q, m in revenue_raw.items()]
    except ValueError as e:
        print(f"  [{ticker}] No Revenue data: {e}")

    try:
        price_raw = parse_price_reaction_table(html)
        result["price_reaction"] = [build_price_row(ticker, q, entry, timestamp) for q, entry in price_raw.items()]
    except ValueError as e:
        print(f"  [{ticker}] No Price Reaction data: {e}")

    return result


# ---------------------------------------------------------------------------
# Local CSV storage — a preview/audit trail you can open and check before
# (or alongside) any real Google Sheets write. Three files, matching
# exactly what would land in the three Sheets tabs.
# ---------------------------------------------------------------------------

def upsert_csv(
    path: str,
    headers: List[str],
    new_rows: List[Dict],
    key_columns=("ticker", "quarter"),
    finalize_column: Optional[str] = None,
):
    """
    Same upsert semantics as upsert_rows() (the Google Sheets version),
    applied to a local CSV file. Safe to call repeatedly across separate
    runs — reads the existing file if present, merges in memory, rewrites
    the whole file. Returns (num_appended, num_updated).

    Three cases per row:
    - Key not seen before -> appended.
    - Key seen before, finalize_column=None -> left alone. Used for
      Price Reaction: once a quarter's price data exists, it's a fixed
      historical fact and never needs re-writing.
    - Key seen before, finalize_column set (e.g. "reported") -> if the
      EXISTING row's value in that column is still empty, the ENTIRE row
      is refreshed with the new data (covers both "a forecast estimate
      just got revised" and "this quarter just actually reported"). Once
      that column holds a real value, the row is frozen forever — later
      scrapes never touch it again, even if Finviz's displayed quarter
      window later scrolls past it and stops showing it at all.
    """
    existing_rows: List[Dict] = []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            existing_rows = list(csv.DictReader(f))

    existing_map = {
        tuple(row.get(k, "") for k in key_columns): i
        for i, row in enumerate(existing_rows)
    }

    appended = updated = 0
    for row_dict in new_rows:
        row_str = {h: ("" if row_dict.get(h) is None else row_dict.get(h)) for h in headers}
        key = tuple(str(row_dict.get(k, "")) for k in key_columns)

        if key not in existing_map:
            existing_rows.append(row_str)
            existing_map[key] = len(existing_rows) - 1
            appended += 1
        elif finalize_column:
            idx = existing_map[key]
            existing_val = existing_rows[idx].get(finalize_column, "")
            if existing_val in ("", None):
                existing_rows[idx] = row_str
                updated += 1

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(_sort_rows_for_display(existing_rows))

    return appended, updated


def _sort_rows_for_display(rows: List[Dict]) -> List[Dict]:
    """Groups by ticker, then orders chronologically within each ticker
    using quarter_sort (a real number) rather than the "quarter" text
    label — sorting "Q4 '24" before "Q1 '25" as plain text gets the year
    boundary backwards, since '4' > '1' character-by-character. Rows
    missing quarter_sort (shouldn't normally happen, but could for very
    old rows written before this column existed, or an as-yet-unrecognized
    reporting-period prefix) sort last within their ticker rather than
    crashing.
    """
    def sort_key(row: Dict):
        ticker = row.get("ticker", "")
        raw = row.get("quarter_sort", "")
        try:
            qs = int(raw) if raw not in ("", None) else float("inf")
        except (TypeError, ValueError):
            qs = float("inf")
        return (ticker, qs)

    return sorted(rows, key=sort_key)


def save_to_csvs(
    output_dir: str,
    eps_rows: List[Dict],
    gaap_eps_rows: List[Dict],
    revenue_rows: List[Dict],
    price_rows: List[Dict],
) -> None:
    """Writes/updates the four local preview CSVs. Open these in Excel,
    Numbers, or Google Sheets to check the data looks right before ever
    pointing the script at a real Google Sheet."""
    os.makedirs(output_dir, exist_ok=True)

    eps_path = os.path.join(output_dir, "Raw_EPSHistory.csv")
    gaap_eps_path = os.path.join(output_dir, "Raw_GAAPEPSHistory.csv")
    revenue_path = os.path.join(output_dir, "Raw_RevenueHistory.csv")
    price_path = os.path.join(output_dir, "Raw_PostEarningsMoves.csv")

    a1, u1 = upsert_csv(eps_path, EPS_HEADERS, eps_rows, finalize_column="reported")
    a2, u2 = upsert_csv(gaap_eps_path, GAAP_EPS_HEADERS, gaap_eps_rows, finalize_column="reported")
    a3, u3 = upsert_csv(revenue_path, REVENUE_HEADERS, revenue_rows, finalize_column="reported")
    a4, u4 = upsert_csv(price_path, PRICE_HEADERS, price_rows, finalize_column=None)

    print(f"  {eps_path}: +{a1} new, {u1} updated")
    print(f"  {gaap_eps_path}: +{a2} new, {u2} updated")
    print(f"  {revenue_path}: +{a3} new, {u3} updated")
    print(f"  {price_path}: +{a4} new, {u4} updated")


# ---------------------------------------------------------------------------
# Google Sheets storage (direct write, no per-ticker files)
# ---------------------------------------------------------------------------

def get_sheets_client(credentials_path: str):
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(credentials_path, scopes=scopes)
    return gspread.authorize(creds)


def get_or_create_worksheet(spreadsheet, name: str, headers: List[str]):
    import gspread

    try:
        ws = spreadsheet.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=name, rows=1000, cols=max(len(headers), 10))
        ws.append_row(headers)
        return ws

    if not ws.row_values(1):
        ws.append_row(headers)
    return ws


def upsert_rows(
    worksheet,
    new_rows: List[Dict],
    key_columns=("ticker", "quarter"),
    finalize_column: Optional[str] = None,
):
    """
    Writes new_rows into worksheet without duplicating existing (ticker,
    quarter) rows.

    - A key not already present -> appended.
    - A key already present, finalize_column=None -> left alone (used for
      Price Reaction: once a quarter's price data exists, it's a fixed
      historical fact and never needs re-writing).
    - A key already present, finalize_column set (e.g. "reported") -> if
      the EXISTING row's value in that column is still empty, the ENTIRE
      row is refreshed with the new data. This covers both "a forecast
      estimate got revised since last scrape" and "this quarter just
      actually reported." Once that column holds a real value, the row
      is frozen forever and never touched again — even once Finviz's
      displayed quarter window scrolls past it and stops showing it.

    This is what lets you re-run the same tickers on a schedule
    indefinitely: rows only ever grow or get refreshed while still
    "open," never duplicated, and never lost once finalized — regardless
    of which 12 quarters Finviz happens to be displaying on any given day.

    Returns (num_appended, num_updated).
    """
    all_values = worksheet.get_all_values()
    if not all_values:
        raise ValueError("Worksheet has no header row — call get_or_create_worksheet() first.")

    headers = all_values[0]
    header_index = {h: i for i, h in enumerate(headers)}
    key_idx = [header_index[k] for k in key_columns]

    existing_map = {}
    for row_num, row in enumerate(all_values[1:], start=2):  # 1-indexed; +1 to skip header
        key = tuple(row[i] if i < len(row) else "" for i in key_idx)
        existing_map[key] = row_num

    rows_to_append = []
    updates = []  # (row_num, row_values)

    for row_dict in new_rows:
        key = tuple(str(row_dict.get(k, "")) for k in key_columns)
        row_values = ["" if row_dict.get(h) is None else row_dict.get(h) for h in headers]

        if key not in existing_map:
            rows_to_append.append(row_values)
            continue

        if finalize_column is None:
            continue  # already have it, nothing to do

        row_num = existing_map[key]
        existing_row = all_values[row_num - 1]
        check_idx = header_index.get(finalize_column)
        if check_idx is None:
            continue

        existing_val = existing_row[check_idx] if check_idx < len(existing_row) else ""
        if existing_val in ("", None):
            updates.append((row_num, row_values))

    if rows_to_append:
        worksheet.append_rows(rows_to_append, value_input_option="USER_ENTERED")
    for row_num, row_values in updates:
        worksheet.update(f"A{row_num}", [row_values], value_input_option="USER_ENTERED")

    return len(rows_to_append), len(updates)


def write_via_webapp(
    web_app_url: str,
    sheet_name: str,
    headers: List[str],
    rows: List[Dict],
    finalize_column: Optional[str] = None,
    key_columns: Optional[List[str]] = None,
    mode: Optional[str] = None,
    clear_before_write: bool = False,
    action: Optional[str] = None,
    timeout: Optional[float] = None,
    retries: int = 2,
    retry_delay: float = 5.0,
) -> Dict:
    """
    Alternative to upsert_rows() — sends rows to an Apps Script Web App
    (see finviz_data_receiver.gs) instead of using a Google Cloud service
    account. No Cloud Console, no credentials file; just an HTTP POST to
    a URL you got from Apps Script's own Deploy menu. Requires `requests`
    (pip install requests) in addition to this script's other dependencies.

    key_columns defaults to ["ticker", "quarter"] (every earnings table
    uses this). mode defaults to None, which the receiver treats as
    "finalize" (existing behavior: an existing row only updates while
    finalize_column is still blank). Pass mode="overwrite" to always
    replace an existing row regardless — used only by
    write_progress_via_webapp() below, since a progress cursor needs to
    be updated every checkpoint, not frozen after its first write.

    clear_before_write=True wipes all existing data rows in the target
    sheet (header kept) before applying `rows` — used only once, at the
    start of a fresh --tickers-from-sheet sweep, to reset
    Raw_ScrapeIssues so it reflects only today's failures, not an
    ever-growing history mixing in issues already fixed days ago.

    TIMEOUT/RETRIES — this used to be a plain, unprotected `timeout=30`
    HTTP call with no retry, and the checkpoint calls that use it (see
    flush() in run_batch) had no try/except around them either. That
    combination caused a real failure in production: a 20-ticker GitHub
    Actions test run failed after ~14 minutes because ONE write to the
    Apps Script Web App took longer than 30s and crashed the entire run
    — confirmed by raising the timeout to 180s, which then succeeded.

    Apps Script genuinely can be slower than a normal API call: the
    LockService added for write-safety (see finviz_data_receiver.gs)
    can briefly queue a caller behind another write, and sortSheet_()
    re-sorts the WHOLE sheet after every single write, which gets slower
    as a sheet grows into the thousands of rows this project produces.
    A fixed 30s budget for all of that was never generous enough.

    The real fix isn't just a bigger number, though — a single slow
    call, however rare, could still exceed any fixed timeout and take
    the whole run down with it, discarding a checkpoint's worth of
    already-scraped data along with it. So this now ALSO retries on
    failure (timeout, connection error, or a non-2xx response) before
    giving up, the same defensive pattern fetch_html_with_retry() uses
    for the scraping side. A truly broken Web App URL still surfaces as
    a real, loud error after retries are exhausted — this is deliberate
    for the four core data tables, where a persistent inability to save
    should stop the run rather than silently scraping data that can
    never be saved; see the try/except wrapping in flush() for how
    progress/issues tracking (non-critical metadata) differ, degrading
    gracefully instead.

    timeout defaults to FINVIZ_WEBAPP_TIMEOUT (120s) if not given —
    override per-call, or set that env var, if your sheets grow large
    enough that even 120s stops being enough headroom.
    """
    import requests

    if timeout is None:
        timeout = _env_float("FINVIZ_WEBAPP_TIMEOUT", 120.0)

    payload = {
        "sheet": sheet_name,
        "headers": headers,
        "key_columns": key_columns or ["ticker", "quarter"],
        "finalize_column": finalize_column,
        "rows": rows,
    }
    if mode:
        payload["mode"] = mode
    if clear_before_write:
        payload["clear_before_write"] = True
    if action:
        # Overrides the whole payload with just the action — used only by
        # trigger_sort_via_webapp() to send {"action": "sort_all"}, which
        # doesn't need sheet/headers/rows at all. Reuses this function's
        # retry/timeout handling rather than duplicating it.
        payload = {"action": action}

    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(web_app_url, json=payload, timeout=timeout)
            resp.raise_for_status()
            result = resp.json()
            if not result.get("ok"):
                raise RuntimeError(f"Web App reported an error: {result.get('error')}")
            return result
        except Exception as e:
            last_error = e
            if attempt < retries:
                print(f"  Write to {sheet_name!r} failed (attempt {attempt + 1}/{retries + 1}): "
                      f"{e} — retrying in {retry_delay}s...")
                time.sleep(retry_delay)
    raise last_error


def fetch_tickers_from_sheet(
    web_app_url: str,
    sheet_name: str = "Tickers",
    column: str = "ticker",
    spreadsheet_id: Optional[str] = None,
) -> List[str]:
    """
    Reads the ticker list FROM a Google Sheet via the Apps Script Web
    App's doGet() — instead of hardcoding tickers in a command line or
    workflow file. This is what makes a scheduled cloud run (GitHub
    Actions) able to just add or remove a row in the Sheet and have the
    next run automatically pick up the change, no code or workflow edit
    needed.

    spreadsheet_id is optional and lets tickers be read from a DIFFERENT
    spreadsheet than the one the Web App is deployed in — e.g. your real
    "US Stocks using Claude" spreadsheet's Raw_Universe tab (column name
    "Ticker", capitalized), while earnings data still writes into a
    separate sandbox spreadsheet you're testing in. Leave it unset to
    read from the same spreadsheet the Web App lives in.
    """
    import requests

    params = {"sheet": sheet_name, "column": column}
    if spreadsheet_id:
        params["spreadsheet_id"] = spreadsheet_id

    resp = requests.get(web_app_url, params=params, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Web App reported an error fetching tickers: {result.get('error')}")
    tickers = result.get("tickers", [])
    print(f"Fetched {len(tickers)} tickers from sheet {sheet_name!r}, column {column!r}.")
    return tickers


# ---------------------------------------------------------------------------
# Resumable progress tracking — lets a --tickers-from-sheet run pick up
# where a PREVIOUS run (today) left off, instead of restarting the full
# 5,336-ticker universe from ticker 1 every time. This is what makes
# chaining several ~5.5-hour scheduled runs across a day cover the whole
# universe without needing simultaneous/parallel writers at all — see the
# chat discussion this was designed in for the full reasoning. Progress
# lives in a small dedicated sheet tab ("ScraperProgress" by default) with
# exactly one data row: run_date, next_index, total_tickers, last_updated.
#
# Only ever used when --tickers-from-sheet is on — a manual/positional
# ticker list (e.g. quick debugging of one ticker) never reads or writes
# this cursor, so it can't accidentally eat into a real day's progress.
# Only ever ADVANCED on a real (non-dry-run) run, and only up to the last
# successfully-flushed checkpoint — a --dry-run run reads the cursor (so
# its preview shows what would actually run next) but never moves it,
# since nothing it does is actually saved.
# ---------------------------------------------------------------------------

PROGRESS_HEADERS = ["run_date", "next_index", "total_tickers", "last_ticker", "last_updated"]
ISSUES_SHEET_NAME = "Raw_ScrapeIssues"
ISSUES_HEADERS = ["ticker", "status", "detail", "run_date", "last_updated"]


def fetch_progress_via_webapp(
    web_app_url: str,
    sheet_name: str = "ScraperProgress",
    spreadsheet_id: Optional[str] = None,
) -> Optional[Dict]:
    """Returns the current progress row as a dict, or None if no progress
    has ever been recorded (first run ever, or the sheet is empty) —
    callers should treat None the same as "start from index 0"."""
    import requests

    params = {"mode": "progress", "sheet": sheet_name}
    if spreadsheet_id:
        params["spreadsheet_id"] = spreadsheet_id

    resp = requests.get(web_app_url, params=params, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Web App reported an error fetching progress: {result.get('error')}")
    if not result.get("found"):
        return None
    return result.get("progress")


def write_progress_via_webapp(
    web_app_url: str,
    next_index: int,
    total_tickers: int,
    run_date: str,
    last_ticker: str,
    sheet_name: str = "ScraperProgress",
) -> None:
    """Overwrites the single ScraperProgress row with the current cursor
    position. mode="overwrite" (see finviz_data_receiver.gs) is what lets
    this update the SAME row every checkpoint instead of the normal
    once-and-frozen upsert behavior every other table uses.

    last_ticker is the actual symbol most recently processed (i.e. the
    ticker at position next_index - 1) — purely for a human reading the
    sheet, so "next_index: 2000" doesn't require cross-referencing
    Raw_Universe to know what that number means. Not used by the resume
    logic itself, which only ever reads next_index."""
    row = {
        "run_date": run_date,
        "next_index": next_index,
        "total_tickers": total_tickers,
        "last_ticker": last_ticker,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    write_via_webapp(
        web_app_url, sheet_name, PROGRESS_HEADERS, [row],
        key_columns=["run_date"], mode="overwrite",
    )


def write_issues_via_webapp(
    web_app_url: str,
    issues: List[Dict],
    run_date: str,
    clear_first: bool = False,
    sheet_name: str = ISSUES_SHEET_NAME,
) -> None:
    """
    Appends failed/no-data tickers found so far to Raw_ScrapeIssues — the
    same information the terminal already prints as a summary at the end
    of a run ("N FAILED (TICKER1, TICKER2, ...)"), just also saved
    somewhere visible without needing the console log.

    clear_first=True wipes the sheet before writing — pass this ONLY on
    the very first write of a fresh full sweep (resume_offset==0). Note
    "fresh" means "the previous sweep just completed or never existed,"
    not "a new calendar day started" — a sweep may now legitimately span
    several days if scraping is slow (see resolve_resume_start()), so
    this sheet reflects the CURRENT sweep's issues only, not an
    ever-growing mix of old ones already fixed and new ones. Later
    checkpoints within the same sweep should pass clear_first=False so
    they add to, rather than wipe, what this sweep has found so far.

    Plain append (finalize_column=None), same pattern as
    Raw_PostEarningsMoves — a ticker shouldn't be scraped twice within
    one sweep (resume slicing guarantees each index is only visited
    once per sweep), so key collisions aren't expected here.
    """
    if not issues and not clear_first:
        return
    timestamp = datetime.now(timezone.utc).isoformat()
    rows = [
        {
            "ticker": ticker,
            "status": status,
            "detail": detail,
            "run_date": run_date,
            "last_updated": timestamp,
        }
        for ticker, status, detail in issues
    ]
    write_via_webapp(
        web_app_url, sheet_name, ISSUES_HEADERS, rows,
        finalize_column=None, key_columns=["ticker"],
        clear_before_write=clear_first,
    )


def trigger_sort_via_webapp(web_app_url: str) -> None:
    """
    Asks the Apps Script Web App to sort the four main data sheets, once
    — see sortAllDataSheets() in finviz_data_receiver.gs. Called once at
    the end of a real (non-dry-run) run_batch() run, replacing the old
    design where every single checkpoint write triggered its own
    full-sheet sort (267+ times per full sweep — confirmed in production
    to be slow enough, as sheets grew, to cause an HTTP timeout that
    crashed an entire run). Sorting once per script run is a deliberate
    middle ground: far less frequent than "every write" (the original
    bug), and — importantly — doesn't depend on a separately-configured
    time-driven trigger living outside version control that could be
    silently deleted with nothing to signal it's gone. Every run that
    reaches this point re-sorts, with no extra setup required.

    Uses write_via_webapp()'s own retry/timeout handling for free. If
    sorting fails even after retries, this only prints a warning — it
    never fails the run, since sort order is purely cosmetic and has no
    effect on the correctness of any data already safely written.
    """
    try:
        write_via_webapp(web_app_url, sheet_name="sort_all", headers=[], rows=[], action="sort_all")
    except Exception as e:
        print(f"  WARNING: could not sort sheets at end of run ({e}). "
              f"Data is unaffected — this is purely cosmetic and will be retried on the next run.")


def resolve_resume_start(progress: Optional[Dict], total_tickers: int) -> int:
    """
    Pure function (easy to test) implementing the actual resume rule.

    IMPORTANT — this used to reset to 0 whenever the calendar date had
    changed since the last recorded progress, regardless of whether that
    sweep had actually finished. That was a real, serious bug once a
    sweep turned out to genuinely take longer than a single day (confirmed
    in production: ~45s/ticker in GitHub Actions vs. ~8-13s/ticker
    locally meant one sweep would need ~2.8 days, not hours). With the
    old date-based rule, that meant progress got silently discarded and
    restarted from ticker 1 every single midnight, FOREVER — tickers
    past whatever a day's runs could reach would never be scraped, not
    once, permanently.

    The rule now: resume from wherever next_index says, for as many days
    as that takes, and only start a FRESH sweep once next_index actually
    reaches total_tickers — i.e. the previous sweep genuinely completed.
    The calendar date is no longer part of this decision at all; it's
    kept in the progress row purely for a human to see how long the
    current sweep has been running, not to drive any logic.

    - No progress recorded yet -> start at 0 (first run ever).
    - next_index < total_tickers -> resume from next_index, no matter how
      many days ago that was recorded — the sweep is still in progress.
    - next_index >= total_tickers -> the last sweep finished; start a
      fresh one from 0.
    """
    if not progress:
        return 0
    try:
        next_index = int(progress.get("next_index", 0))
    except (TypeError, ValueError):
        return 0
    if next_index >= total_tickers:
        return 0  # previous sweep genuinely complete -> start a fresh one
    return max(0, next_index)


# ---------------------------------------------------------------------------
# Test functions — ALWAYS run these before scaling to thousands of tickers
# ---------------------------------------------------------------------------

def test_single_ticker(ticker: str = "LUNR", output_dir: str = "test_output") -> Optional[Dict[str, List[Dict]]]:
    """Scrapes ONE ticker, prints full results, writes a local CSV preview
    for you to open and check — no Sheets write. Runs with a visible
    browser window so you can watch it actually work."""
    print(f"=== Testing single ticker: {ticker} (browser will be visible) ===\n")

    try:
        data = scrape_ticker(ticker, headless=False)
    except NoFinancialsDataError as e:
        print(f"\nNO DATA: {e}")
        print("This is expected for ETFs and some OTC/unlisted tickers — not a bug.")
        return None

    print(f"\nEPS rows ({len(data['eps'])}):")
    for r in data["eps"]:
        print(" ", r)

    print(f"\nGAAP EPS rows ({len(data['gaap_eps'])}):")
    for r in data["gaap_eps"]:
        print(" ", r)

    print(f"\nRevenue rows ({len(data['revenue'])}):")
    for r in data["revenue"]:
        print(" ", r)

    print(f"\nPrice Reaction rows ({len(data['price_reaction'])}):")
    for r in data["price_reaction"]:
        preview = {k: r[k] for k in list(r)[:8]}
        print(" ", preview, "...")

    print(f"\nWriting local CSV preview to {output_dir}/ ...")
    save_to_csvs(output_dir, data["eps"], data["gaap_eps"], data["revenue"], data["price_reaction"])
    print("Open those files to check the data before ever pointing this at Google Sheets.")

    return data


def test_small_batch(
    tickers: Optional[List[str]] = None, delay: float = 3.0, output_dir: str = "test_output"
) -> Dict[str, Dict]:
    """
    Scrapes a small, deliberately varied set of tickers (default: a normal
    stock, a mega-cap, an ETF, a meme stock, and a dividend payer), prints
    a pass/fail summary, and writes a local CSV preview. No Sheets write.
    Runs headless — use test_single_ticker() first if you want to watch
    it work.

    Three possible outcomes per ticker, tracked separately:
    - OK: scraped normally.
    - NO_DATA: page loaded fine, but Finviz has no earnings widgets for
      this ticker (ETFs, some OTC/unlisted tickers) — expected, not a bug.
    - FAILED: something unexpected happened — worth investigating.

    RUN THIS before ever pointing the script at your full ticker list.
    """
    tickers = tickers or DEFAULT_TEST_TICKERS
    results: Dict[str, Dict] = {}
    all_eps: List[Dict] = []
    all_gaap_eps: List[Dict] = []
    all_revenue: List[Dict] = []
    all_price: List[Dict] = []

    for i, ticker in enumerate(tickers):
        print(f"\n[{i + 1}/{len(tickers)}] {ticker}")
        try:
            data = scrape_ticker(ticker, headless=True)
            all_eps.extend(data["eps"])
            all_gaap_eps.extend(data["gaap_eps"])
            all_revenue.extend(data["revenue"])
            all_price.extend(data["price_reaction"])
            results[ticker] = {
                "eps_quarters": len(data["eps"]),
                "gaap_eps_quarters": len(data["gaap_eps"]),
                "revenue_quarters": len(data["revenue"]),
                "price_reaction_quarters": len(data["price_reaction"]),
                "status": "OK",
            }
            print(
                f"  EPS={len(data['eps'])}  GAAP_EPS={len(data['gaap_eps'])}  "
                f"Revenue={len(data['revenue'])}  PriceReaction={len(data['price_reaction'])}"
            )
        except NoFinancialsDataError as e:
            results[ticker] = {"status": "NO_DATA", "detail": str(e)}
            print(f"  NO DATA (expected for ETFs / unlisted tickers): {e}")
        except Exception as e:
            results[ticker] = {"status": "FAILED", "detail": str(e)}
            print(f"  FAILED (worth investigating): {e}")

        if i < len(tickers) - 1:
            time.sleep(delay)

    if all_eps or all_gaap_eps or all_revenue or all_price:
        print(f"\nWriting local CSV preview to {output_dir}/ ...")
        save_to_csvs(output_dir, all_eps, all_gaap_eps, all_revenue, all_price)

    print("\n=== SUMMARY ===")
    for ticker, r in results.items():
        print(f"{ticker}: {r}")

    ok = sum(1 for r in results.values() if r["status"] == "OK")
    no_data = sum(1 for r in results.values() if r["status"] == "NO_DATA")
    failed = [t for t, r in results.items() if r["status"] == "FAILED"]
    print(f"\n{ok} OK, {no_data} NO_DATA (expected for ETFs/unlisted), {len(failed)} FAILED", end="")
    print(f" ({', '.join(failed)})" if failed else "")

    return results


# ---------------------------------------------------------------------------
# Full run — writes to Google Sheets (or dry-runs if no credentials given)
# ---------------------------------------------------------------------------

def run_batch(
    tickers: List[str],
    spreadsheet_id: Optional[str] = None,
    credentials_path: Optional[str] = None,
    web_app_url: Optional[str] = None,
    headless: bool = True,
    delay: float = 3.0,
    dry_run: bool = False,
    output_dir: str = "scraped_data",
    batch_size: int = 20,
    resume_offset: int = 0,
    total_universe: Optional[int] = None,
    run_date: Optional[str] = None,
    progress_sheet_name: str = "ScraperProgress",
    is_fresh_sweep_start: bool = False,
) -> Dict:
    """
    Two ways to write to Google Sheets — pick ONE:
    - web_app_url: simplest, no Cloud Console at all. See
      finviz_data_receiver.gs for the Apps Script side.
    - spreadsheet_id + credentials_path: the Google Cloud service account
      route. More setup, but more robust for a fully unattended scheduled
      run later (e.g. GitHub Actions) since it doesn't depend on a Web
      App deployment staying reachable.
    Neither given -> dry_run (scrape only, local CSV, no Sheets write).

    CHECKPOINTING: results are written (local CSV always, and Sheets if
    configured) every `batch_size` tickers — not just once at the very
    end. If the run is interrupted (closed terminal, lost connection,
    crash) after ticker 140 of a 200-ticker run with batch_size=20,
    everything through ticker 140 is already safely written; only the
    unfinished 141-160 batch is lost, not the whole run.

    RESUME TRACKING (resume_offset / total_universe / run_date): only
    meaningful when `tickers` here is already a SLICE of a larger
    universe (set up by the --tickers-from-sheet CLI path — see
    resolve_resume_start()). resume_offset is how many tickers were
    already skipped before this slice started; after each successful,
    non-dry-run checkpoint, the ABSOLUTE position (resume_offset + how
    many of `tickers` have been processed so far) is written to the
    ScraperProgress sheet via web_app_url, so a LATER run today can
    resume from exactly this point instead of re-scraping from ticker 1.
    Only active when web_app_url and run_date are both given; silently
    skipped otherwise (e.g. the gspread/service-account route, or a
    manual/positional ticker list that was never resume-sliced to begin
    with).

    Returns a summary dict: {"ok_count", "no_data_count",
    "failed_tickers", "totals"} — not the full scraped rows, since with
    checkpointing those are written out incrementally rather than held
    in memory for the whole run.
    """
    use_webapp = bool(web_app_url)
    use_gspread = bool(spreadsheet_id and credentials_path)
    dry_run = dry_run or not (use_webapp or use_gspread)
    if dry_run and not use_webapp and not use_gspread:
        print("No --web-app-url or --sheet-id/--creds given — dry-run mode (scrape only, no write).\n")

    track_progress = bool(use_webapp and run_date and not dry_run)
    total_universe = total_universe if total_universe is not None else (resume_offset + len(tickers))

    if track_progress and is_fresh_sweep_start:
        try:
            write_issues_via_webapp(web_app_url, [], run_date, clear_first=True)
            print(f"Fresh sweep for {run_date}: cleared {ISSUES_SHEET_NAME} of yesterday's leftover issues.")
        except Exception as e:
            print(f"  WARNING: could not clear {ISSUES_SHEET_NAME} ({e}). "
                  f"It may contain stale entries from a previous day until this succeeds.")

    eps_ws = gaap_eps_ws = revenue_ws = price_ws = None
    if use_gspread:
        gc = get_sheets_client(credentials_path)
        spreadsheet = gc.open_by_key(spreadsheet_id)
        eps_ws = get_or_create_worksheet(spreadsheet, "Raw_EPSHistory", EPS_HEADERS)
        gaap_eps_ws = get_or_create_worksheet(spreadsheet, "Raw_GAAPEPSHistory", GAAP_EPS_HEADERS)
        revenue_ws = get_or_create_worksheet(spreadsheet, "Raw_RevenueHistory", REVENUE_HEADERS)
        price_ws = get_or_create_worksheet(spreadsheet, "Raw_PostEarningsMoves", PRICE_HEADERS)

    totals = {
        "eps_appended": 0, "eps_updated": 0,
        "gaap_eps_appended": 0, "gaap_eps_updated": 0,
        "revenue_appended": 0, "revenue_updated": 0,
        "price_appended": 0, "price_updated": 0,
    }
    ok_count = no_data_count = 0
    failed_tickers: List[str] = []
    batch_issues: List[tuple] = []  # (ticker, status, detail) since the last checkpoint

    batch_eps: List[Dict] = []
    batch_gaap_eps: List[Dict] = []
    batch_revenue: List[Dict] = []
    batch_price: List[Dict] = []

    def flush(checkpoint_desc: str) -> None:
        """Writes whatever's currently in the batch buffers to local CSV
        (always) and to Sheets (if configured), then clears the buffers.
        Safe to call with empty buffers."""
        nonlocal batch_eps, batch_gaap_eps, batch_revenue, batch_price

        print(f"\n--- Checkpoint: {checkpoint_desc} ---")
        print(f"Writing local CSV to {output_dir}/ ...")
        save_to_csvs(output_dir, batch_eps, batch_gaap_eps, batch_revenue, batch_price)

        if not dry_run:
            if use_webapp:
                r = write_via_webapp(web_app_url, "Raw_EPSHistory", EPS_HEADERS, batch_eps, finalize_column="reported")
                totals["eps_appended"] += r["appended"]; totals["eps_updated"] += r["updated"]
                print(f"  Raw_EPSHistory: +{r['appended']} new, {r['updated']} updated")

                r = write_via_webapp(web_app_url, "Raw_GAAPEPSHistory", GAAP_EPS_HEADERS, batch_gaap_eps, finalize_column="reported")
                totals["gaap_eps_appended"] += r["appended"]; totals["gaap_eps_updated"] += r["updated"]
                print(f"  Raw_GAAPEPSHistory: +{r['appended']} new, {r['updated']} updated")

                r = write_via_webapp(web_app_url, "Raw_RevenueHistory", REVENUE_HEADERS, batch_revenue, finalize_column="reported")
                totals["revenue_appended"] += r["appended"]; totals["revenue_updated"] += r["updated"]
                print(f"  Raw_RevenueHistory: +{r['appended']} new, {r['updated']} updated")

                r = write_via_webapp(web_app_url, "Raw_PostEarningsMoves", PRICE_HEADERS, batch_price, finalize_column=None)
                totals["price_appended"] += r["appended"]; totals["price_updated"] += r["updated"]
                print(f"  Raw_PostEarningsMoves: +{r['appended']} new, {r['updated']} updated")
            else:
                appended, updated = upsert_rows(eps_ws, batch_eps, finalize_column="reported")
                totals["eps_appended"] += appended; totals["eps_updated"] += updated
                print(f"  Raw_EPSHistory: +{appended} new, {updated} updated")

                appended, updated = upsert_rows(gaap_eps_ws, batch_gaap_eps, finalize_column="reported")
                totals["gaap_eps_appended"] += appended; totals["gaap_eps_updated"] += updated
                print(f"  Raw_GAAPEPSHistory: +{appended} new, {updated} updated")

                appended, updated = upsert_rows(revenue_ws, batch_revenue, finalize_column="reported")
                totals["revenue_appended"] += appended; totals["revenue_updated"] += updated
                print(f"  Raw_RevenueHistory: +{appended} new, {updated} updated")

                appended, updated = upsert_rows(price_ws, batch_price, finalize_column=None)
                totals["price_appended"] += appended; totals["price_updated"] += updated
                print(f"  Raw_PostEarningsMoves: +{appended} new, {updated} updated")

        batch_eps, batch_gaap_eps, batch_revenue, batch_price = [], [], [], []

    def _run_scrape_loop():
        nonlocal ok_count, no_data_count, batch_issues
        for i, ticker in enumerate(tickers):
            print(f"[{i + 1}/{len(tickers)}] Scraping {ticker}...")
            ticker_started = time.monotonic()
            try:
                data = scrape_ticker(ticker, headless=headless)
                elapsed = time.monotonic() - ticker_started
                batch_eps.extend(data["eps"])
                batch_gaap_eps.extend(data["gaap_eps"])
                batch_revenue.extend(data["revenue"])
                batch_price.extend(data["price_reaction"])
                ok_count += 1
                print(
                    f"  OK ({elapsed:.1f}s): {len(data['eps'])} EPS, {len(data['gaap_eps'])} GAAP EPS, "
                    f"{len(data['revenue'])} Revenue, {len(data['price_reaction'])} PriceReaction quarters"
                )
            except NoFinancialsDataError as e:
                elapsed = time.monotonic() - ticker_started
                no_data_count += 1
                batch_issues.append((ticker, "NO_DATA", str(e)))
                print(f"  NO DATA ({elapsed:.1f}s, expected for ETFs/unlisted tickers): {e}")
            except Exception as e:
                elapsed = time.monotonic() - ticker_started
                failed_tickers.append(ticker)
                batch_issues.append((ticker, "FAILED", str(e)))
                print(f"  FAILED ({elapsed:.1f}s, worth investigating): {e}")

            is_last = i == len(tickers) - 1
            if (i + 1) % batch_size == 0 or is_last:
                flush(f"after {i + 1}/{len(tickers)} tickers")

                if track_progress:
                    absolute_next_index = resume_offset + (i + 1)
                    try:
                        write_progress_via_webapp(
                            web_app_url, absolute_next_index, total_universe, run_date, ticker,
                            sheet_name=progress_sheet_name,
                        )
                        print(f"  Progress saved: {absolute_next_index}/{total_universe} tickers done today "
                              f"(last: {ticker}).")
                    except Exception as e:
                        # Never let a progress-tracking hiccup take down an
                        # otherwise-successful run — the actual data is
                        # already safely written above; worst case here is
                        # just that the NEXT run doesn't know to resume and
                        # re-scrapes from the top, which is wasteful but not
                        # harmful (the upsert logic is safe to repeat).
                        print(f"  WARNING: could not save progress cursor ({e}). "
                              f"Data above this line is still safe; next run may restart from the top.")

                    if batch_issues:
                        try:
                            write_issues_via_webapp(web_app_url, batch_issues, run_date, clear_first=False)
                            print(f"  {ISSUES_SHEET_NAME}: recorded {len(batch_issues)} issue(s) from this batch.")
                        except Exception as e:
                            print(f"  WARNING: could not save scrape issues ({e}). "
                                  f"They're still visible in this console output above.")
                        batch_issues = []

            if not is_last:
                time.sleep(delay)

    try:
        _run_scrape_loop()
    finally:
        if use_webapp and not dry_run:
            print("\nSorting sheets once at the end of this run...")
            trigger_sort_via_webapp(web_app_url)

    print(
        f"\n{ok_count} OK, {no_data_count} NO_DATA, {len(failed_tickers)} FAILED"
        + (f" ({', '.join(failed_tickers)})" if failed_tickers else "")
    )

    if dry_run:
        print("\nDRY RUN complete — local CSV checkpoints written, nothing sent to Sheets.")
    else:
        print(
            f"\nTotals across whole run — "
            f"EPS: +{totals['eps_appended']} new/{totals['eps_updated']} updated, "
            f"GAAP EPS: +{totals['gaap_eps_appended']} new/{totals['gaap_eps_updated']} updated, "
            f"Revenue: +{totals['revenue_appended']} new/{totals['revenue_updated']} updated, "
            f"PriceReaction: +{totals['price_appended']} new/{totals['price_updated']} updated"
        )

    return {
        "ok_count": ok_count,
        "no_data_count": no_data_count,
        "failed_tickers": failed_tickers,
        "totals": totals,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# WHY --test / --test-batch ARE NOT ENV-CONFIGURABLE (unlike everything else
# below): every other flag became env-configurable in this update so a real
# run can be fully driven by .env with zero command-line arguments — see
# env.example. --test and --test-batch were deliberately left OUT of that,
# on purpose: they're diagnostic modes that never write to Sheets. If one of
# them were left set to true in .env by accident (or copy-pasted into a
# GitHub Actions secret later), every future run — including a real
# scheduled cloud run meant to write thousands of rows — would silently stay
# in test mode forever, scraping a handful of tickers and writing nothing,
# with no error to signal anything was wrong. Requiring the actual --test /
# --test-batch flag on the command line every time makes that mistake
# structurally impossible to leave lying around.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if sys.version_info < (3, 9):
        print("This script requires Python 3.9+ (uses argparse.BooleanOptionalAction). "
              f"You're running {sys.version.split()[0]}.")
        sys.exit(1)

    load_env_file()  # must happen BEFORE argparse defaults below read os.environ

    parser = argparse.ArgumentParser(description="Finviz earnings/revenue/price-reaction scraper.")
    parser.add_argument(
        "tickers", nargs="*", default=_env_list("FINVIZ_TICKERS"),
        help="Ticker symbols, e.g. LUNR AAPL SPY. Only used when --tickers-from-sheet is off. "
        "If none are typed here, falls back to FINVIZ_TICKERS in .env (comma-separated) — e.g. "
        "FINVIZ_TICKERS=LUNR,AAPL,SPY,GME,KO — so you don't have to retype a test list every time. "
        "Typing tickers here always overrides the env var for that one run.",
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Test one ticker (default LUNR), visible browser, no Sheets write. "
        "Deliberately NOT env-configurable — see note above main().",
    )
    parser.add_argument(
        "--test-batch", action="store_true",
        help="Test a small varied set of tickers, headless, no Sheets write. "
        "Deliberately NOT env-configurable — see note above main().",
    )
    parser.add_argument(
        "--web-app-url",
        default=os.environ.get("FINVIZ_WEB_APP_URL"),
        help="Apps Script Web App URL — simplest option, no Cloud Console. Env var: FINVIZ_WEB_APP_URL",
    )
    parser.add_argument(
        "--sheet-id",
        default=os.environ.get("FINVIZ_SHEET_ID"),
        help="Google Sheet ID (alternative to --web-app-url: service account route). Env var: FINVIZ_SHEET_ID",
    )
    parser.add_argument(
        "--creds",
        default=os.environ.get("FINVIZ_CREDS_PATH"),
        help="Path to Google service account JSON credentials file, used with --sheet-id. Env var: FINVIZ_CREDS_PATH",
    )
    parser.add_argument(
        "--dry-run", action=argparse.BooleanOptionalAction,
        default=_env_bool("FINVIZ_DRY_RUN", False),
        help="Scrape but never write to Sheets. Env var: FINVIZ_DRY_RUN (true/false). "
        "Explicit --no-dry-run overrides an env var set to true for one run.",
    )
    parser.add_argument(
        "--show", action=argparse.BooleanOptionalAction,
        default=_env_bool("FINVIZ_SHOW", False),
        help="Show the browser window (default: headless). Env var: FINVIZ_SHOW (true/false).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=_env_float("FINVIZ_DELAY", 3.0),
        help="Seconds between tickers (default 3). Env var: FINVIZ_DELAY",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_env_int("FINVIZ_BATCH_SIZE", 20),
        help="Write a checkpoint (local CSV + Sheets) every N tickers (default 20). Env var: FINVIZ_BATCH_SIZE",
    )
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("FINVIZ_OUTPUT_DIR"),
        help="Where local CSV previews are written (default: test_output/ for --test/--test-batch, scraped_data/ otherwise). "
        "Env var: FINVIZ_OUTPUT_DIR",
    )
    parser.add_argument(
        "--tickers-from-sheet", action=argparse.BooleanOptionalAction,
        default=_env_bool("FINVIZ_TICKERS_FROM_SHEET", False),
        help="Fetch the ticker list from the Sheet itself (via doGet) instead of using positional tickers "
        "— requires --web-app-url. Env var: FINVIZ_TICKERS_FROM_SHEET (true/false).",
    )
    parser.add_argument(
        "--resume", action=argparse.BooleanOptionalAction,
        default=_env_bool("FINVIZ_RESUME", True),
        help="Only relevant with --tickers-from-sheet: pick up from today's saved progress cursor "
        "(ScraperProgress sheet) instead of starting at ticker 1. On by default. Use --no-resume to "
        "force starting the current sweep over from the beginning regardless of what's saved. "
        "Env var: FINVIZ_RESUME (true/false).",
    )
    parser.add_argument(
        "--tickers-sheet-name",
        default=os.environ.get("FINVIZ_TICKERS_SHEET_NAME", "Tickers"),
        help="Which sheet tab to read tickers from, used with --tickers-from-sheet (default 'Tickers'). "
        "Env var: FINVIZ_TICKERS_SHEET_NAME",
    )
    parser.add_argument(
        "--tickers-column",
        default=os.environ.get("FINVIZ_TICKERS_COLUMN", "ticker"),
        help="Which column header holds ticker symbols, used with --tickers-from-sheet (default 'ticker'). "
        "Env var: FINVIZ_TICKERS_COLUMN",
    )
    parser.add_argument(
        "--tickers-spreadsheet-id",
        default=os.environ.get("FINVIZ_TICKERS_SPREADSHEET_ID"),
        help="Read tickers from a DIFFERENT spreadsheet than the one the Web App is deployed in "
        "(e.g. your real Raw_Universe tab). Env var: FINVIZ_TICKERS_SPREADSHEET_ID",
    )
    parser.add_argument(
        "--tickers-limit",
        type=int,
        default=_env_int("FINVIZ_TICKERS_LIMIT"),
        help="Only use the first N tickers from the fetched/given list — e.g. --tickers-limit 20 "
        "to test against a small slice of a large sheet. Env var: FINVIZ_TICKERS_LIMIT",
    )
    args = parser.parse_args()

    output_kwargs = {"output_dir": args.output_dir} if args.output_dir else {}

    if args.test:
        test_single_ticker(args.tickers[0] if args.tickers else "LUNR", **output_kwargs)
    elif args.test_batch:
        test_small_batch(args.tickers or None, delay=args.delay, **output_kwargs)
    else:
        resume_offset = 0
        total_universe: Optional[int] = None
        run_date: Optional[str] = None

        if args.tickers_from_sheet:
            if not args.web_app_url:
                print("--tickers-from-sheet requires --web-app-url (that's where the ticker list is read from).")
                sys.exit(1)
            full_tickers = fetch_tickers_from_sheet(
                args.web_app_url,
                args.tickers_sheet_name,
                args.tickers_column,
                spreadsheet_id=args.tickers_spreadsheet_id,
            )
            total_universe = len(full_tickers)
            # UTC, not local time — used only as a "when did this happen"
            # label written to ScraperProgress/Raw_ScrapeIssues, purely
            # for a human to read. Does NOT drive the resume decision —
            # see resolve_resume_start() for why a sweep now correctly
            # continues across day boundaries however long it takes,
            # instead of restarting every midnight regardless of whether
            # the previous sweep actually finished.
            run_date = datetime.now(timezone.utc).date().isoformat()

            if args.resume:
                try:
                    progress = fetch_progress_via_webapp(
                        args.web_app_url, spreadsheet_id=args.tickers_spreadsheet_id
                    )
                except Exception as e:
                    print(f"Could not fetch progress cursor ({e}) — starting from the top as a fallback.")
                    progress = None
                resume_offset = resolve_resume_start(progress, total_universe)
            else:
                print("--no-resume: ignoring any saved progress, starting a fresh sweep from the top.")

            tickers = full_tickers[resume_offset:]

            if resume_offset > 0:
                print(f"Resuming the in-progress sweep from ticker {resume_offset + 1}/{total_universe} "
                      f"({resume_offset} done so far this sweep).")
            elif resume_offset >= total_universe:
                print(f"The current sweep ({total_universe} tickers) is already complete — nothing to do.")
            else:
                print(f"Starting a fresh sweep of all {total_universe} tickers (started {run_date}).")
        else:
            tickers = args.tickers

        if args.tickers_limit:
            tickers = tickers[: args.tickers_limit]
            print(f"Limiting to the first {args.tickers_limit} tickers (--tickers-limit / FINVIZ_TICKERS_LIMIT).")

        if not tickers:
            print("No tickers given. Use --test, --test-batch, --tickers-from-sheet, or pass ticker symbols.")
            sys.exit(1)

        run_batch(
            tickers,
            spreadsheet_id=args.sheet_id,
            credentials_path=args.creds,
            web_app_url=args.web_app_url,
            headless=not args.show,
            delay=args.delay,
            dry_run=args.dry_run,
            batch_size=args.batch_size,
            resume_offset=resume_offset,
            total_universe=total_universe,
            run_date=run_date,
            is_fresh_sweep_start=(args.tickers_from_sheet and resume_offset == 0),
            **output_kwargs,
        )