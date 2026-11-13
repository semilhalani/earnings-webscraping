"""Tests for the refresh rule (CI Stage 4): every quarter Finviz shows is
compared with what we already have, on every sweep.

  - new quarter                   -> added
  - every value the same          -> nothing written (last_updated stays)
  - a value changed               -> row updated
  - Finviz now blank, we have one -> keep ours, flag KEPT_OLD_VALUE
  - non-zero becomes 0            -> write the 0, flag CHANGED_TO_ZERO
  - quarter no longer on Finviz   -> never touched (kept as it is)

The live sheet is written by Apps Script (finviz_data_receiver.gs); these
tests cover the Python side (merge_row, the local CSV copy and the direct
Google Sheets path). tests/receiver.test.js checks the Apps Script side
against the SAME cases in tests/fixtures/refresh_cases.json.

tmp_path: a pytest fixture that gives each test its own brand-new, empty
temporary folder and deletes it afterwards, so tests can write real CSV
files without touching anything of yours.
"""

import csv
import json
from pathlib import Path

import pytest

from finviz_earnings_scraper import EPS_HEADERS, upsert_csv, upsert_rows

CASES_FILE = Path(__file__).parent / "fixtures" / "refresh_cases.json"
CASES = json.loads(CASES_FILE.read_text(encoding="utf-8"))["cases"]


# --- The shared cases, against the Python merge_row ---------------------------

@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_merge_row_matches_shared_cases(case):
    # Imported here, not at the top, so that if merge_row is missing only
    # these tests fail and the rest of the file still runs.
    from finviz_earnings_scraper import merge_row

    result = merge_row(case["existing"], case["incoming"], case["columns"], case["open_flags"])
    expected = case["expected"]
    assert result["changed"] is expected["changed"]
    assert result["values"] == expected["values"]
    assert result["flags"] == expected["flags"]
    assert result["resolved"] == expected["resolved"]


# --- upsert_csv: the local CSV copy saved with each run ------------------------

def eps_row(quarter, estimate, reported, stamp):
    """One EPS row the way build_performance_row makes it."""
    return {
        "ticker": "AAPL", "quarter": quarter, "quarter_sort": None,
        "estimate": estimate, "reported": reported,
        "surprise": None, "surprise_pct": None, "last_updated": stamp,
    }


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return {row["quarter"]: row for row in csv.DictReader(f)}


@pytest.fixture
def csv_path(tmp_path):
    """A CSV that already holds two AAPL quarters, written on day 1."""
    path = tmp_path / "Raw_EPSHistory.csv"
    upsert_csv(str(path), EPS_HEADERS, [
        eps_row("Q2 '26", 1.60, 1.65, "day1"),
        eps_row("Q3 '26", 6.50, 6.77, "day1"),
    ])
    return path


def test_new_quarters_are_added(csv_path):
    appended, updated = upsert_csv(str(csv_path), EPS_HEADERS, [eps_row("Q4 '26", 2.10, None, "day2")])
    assert (appended, updated) == (1, 0)
    assert set(read_csv(csv_path)) == {"Q2 '26", "Q3 '26", "Q4 '26"}


def test_unchanged_rows_are_not_rewritten(csv_path):
    appended, updated = upsert_csv(str(csv_path), EPS_HEADERS, [eps_row("Q3 '26", 6.50, 6.77, "day2")])
    assert (appended, updated) == (0, 0)
    # last_updated means "when a value last changed", so it stays at day1.
    assert read_csv(csv_path)["Q3 '26"]["last_updated"] == "day1"


def test_revised_reported_value_is_updated(csv_path):
    # The real case: AAPL GAAP EPS Q3 '26 was saved as 6.77 and Finviz
    # later showed 6.98. The old rule froze rows once "reported" was
    # filled, so the sheet kept 6.77 forever.
    appended, updated = upsert_csv(str(csv_path), EPS_HEADERS, [eps_row("Q3 '26", 6.50, 6.98, "day2")])
    assert (appended, updated) == (0, 1)
    row = read_csv(csv_path)["Q3 '26"]
    assert float(row["reported"]) == pytest.approx(6.98)
    assert row["last_updated"] == "day2"


def test_blank_on_finviz_never_erases_a_value(csv_path):
    appended, updated = upsert_csv(str(csv_path), EPS_HEADERS, [eps_row("Q3 '26", 6.50, None, "day2")])
    assert (appended, updated) == (0, 0)
    assert float(read_csv(csv_path)["Q3 '26"]["reported"]) == pytest.approx(6.77)


def test_change_to_zero_is_written(csv_path):
    appended, updated = upsert_csv(str(csv_path), EPS_HEADERS, [eps_row("Q3 '26", 6.50, 0.0, "day2")])
    assert (appended, updated) == (0, 1)
    assert float(read_csv(csv_path)["Q3 '26"]["reported"]) == 0.0


def test_quarters_no_longer_shown_are_kept(csv_path):
    # Day 2's page no longer shows Q2 '26 at all: its row must stay.
    upsert_csv(str(csv_path), EPS_HEADERS, [eps_row("Q3 '26", 6.50, 6.77, "day2")])
    rows = read_csv(csv_path)
    assert float(rows["Q2 '26"]["reported"]) == pytest.approx(1.65)
    assert rows["Q2 '26"]["last_updated"] == "day1"


# --- upsert_rows: the direct Google Sheets path (not used by the live runs) --

class FakeWorksheet:
    """Stands in for a gspread worksheet: a list of rows, all text, the way
    gspread returns them. Records every write so tests can check them."""

    def __init__(self, rows):
        self.rows = [list(r) for r in rows]
        self.appended = []
        self.updates = []

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def append_rows(self, rows, value_input_option=None):
        self.appended.extend(rows)

    def update(self, cell, values, value_input_option=None):
        self.updates.append((cell, values[0]))


def sheet_with_one_row():
    return FakeWorksheet([
        EPS_HEADERS,
        ["AAPL", "Q3 '26", "263", "6.5", "6.77", "0.27", "4.15", "day1"],
    ])


def test_upsert_rows_updates_a_revised_value():
    ws = sheet_with_one_row()
    row = eps_row("Q3 '26", 6.50, 6.98, "day2") | {"quarter_sort": 263, "surprise": 0.27, "surprise_pct": 4.15}
    assert upsert_rows(ws, [row]) == (0, 1)
    cell, values = ws.updates[0]
    assert cell == "A2"
    assert values[EPS_HEADERS.index("reported")] == 6.98
    assert values[EPS_HEADERS.index("last_updated")] == "day2"


def test_upsert_rows_leaves_an_unchanged_row_alone():
    ws = sheet_with_one_row()
    row = eps_row("Q3 '26", 6.50, 6.77, "day2") | {"quarter_sort": 263, "surprise": 0.27, "surprise_pct": 4.15}
    assert upsert_rows(ws, [row]) == (0, 0)
    assert ws.updates == []
