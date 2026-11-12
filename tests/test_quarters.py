"""Tests for the two functions that turn Finviz period labels into
something we can store and sort: normalize_quarter and quarter_sort_key.

How pytest finds these: it looks in the tests/ folder (see pyproject.toml),
opens every file whose name starts with "test_", and runs every function
in it whose name starts with "test_". Each test calls a real function with
a known input and uses `assert` to check the answer. If an assert is false,
that test fails and CI turns red.
"""

import pytest

# Import ONLY the functions under test. Never "from finviz_earnings_scraper
# import *": the scraper has its own functions named test_baseline_speed,
# test_single_ticker and test_small_batch, and pytest would try to run them
# as tests (opening Chrome and contacting Finviz).
from finviz_earnings_scraper import normalize_quarter, quarter_sort_key


# --- normalize_quarter -------------------------------------------------------
# Finviz writes the same period in slightly different ways in different
# tables. normalize_quarter turns all of them into one standard form,
# "Q1 '26", so rows from different tables match up.

def test_straight_apostrophe_is_kept():
    # The EPS and revenue tables use a normal apostrophe.
    assert normalize_quarter("Q1 '26") == "Q1 '26"


def test_curly_apostrophes_become_straight():
    # The price-reaction table uses curly apostrophes (both directions).
    assert normalize_quarter("Q1 ’26") == "Q1 '26"
    assert normalize_quarter("Q1 ‘26") == "Q1 '26"


def test_extra_text_after_the_label_is_ignored():
    # The price-reaction table adds the fiscal month: "Q4 '26 (Sep '26)".
    # Only the first period label counts.
    assert normalize_quarter("Q4 ‘26 (Sep ‘26)") == "Q4 '26"


@pytest.mark.parametrize(
    "label, expected",
    [
        ("S1 '25", "S1 '25"),  # semi-annual reporters (e.g. BTI)
        ("H2 '24", "H2 '24"),  # half-year, another semi-annual style
        ("FY '26", "FY '26"),  # annual reporters
    ],
)
def test_non_quarterly_periods_are_recognised(label, expected):
    # parametrize = run this one test once per (label, expected) pair above.
    assert normalize_quarter(label) == expected


@pytest.mark.parametrize("label", ["2014", "Q5 '26", "Annual", "", None])
def test_unknown_labels_return_none(label):
    # None means "not a period I recognise". The caller then keeps the raw
    # label and warns, rather than silently dropping the row.
    assert normalize_quarter(label) is None


# --- quarter_sort_key ----------------------------------------------------------
# Turns a normalized label into a number that sorts in date order:
# year * 10 + slot, where Q1-Q4 are slots 1-4, S1/H1 = 5, S2/H2 = 6, FY = 9.

@pytest.mark.parametrize(
    "quarter, expected",
    [
        ("Q2 '24", 242),
        ("Q4 '24", 244),
        ("Q1 '25", 251),
        ("S1 '25", 255),
        ("H2 '25", 256),
        ("FY '26", 269),
    ],
)
def test_sort_key_values(quarter, expected):
    assert quarter_sort_key(quarter) == expected


def test_year_boundary_sorts_in_date_order():
    # THE reason quarter_sort exists. Sorted as plain text, "Q4 '24" comes
    # AFTER "Q1 '25" (because the character "4" > "1"), which is wrong:
    # Q4 2024 happened before Q1 2025.
    labels = ["Q1 '25", "Q4 '24", "Q2 '25", "Q3 '24"]
    assert sorted(labels) == ["Q1 '25", "Q2 '25", "Q3 '24", "Q4 '24"]  # wrong order
    assert sorted(labels, key=quarter_sort_key) == ["Q3 '24", "Q4 '24", "Q1 '25", "Q2 '25"]


def test_different_period_types_never_share_a_number():
    # Q1, S1 and FY of the same year must all get different numbers.
    keys = [quarter_sort_key(q) for q in ("Q1 '26", "Q2 '26", "Q3 '26", "Q4 '26", "S1 '26", "S2 '26", "FY '26")]
    assert len(set(keys)) == len(keys)


@pytest.mark.parametrize("quarter", ["2014", "Q5 '26", "", None])
def test_unknown_periods_have_no_sort_key(quarter):
    assert quarter_sort_key(quarter) is None
