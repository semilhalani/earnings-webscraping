"""Tests for parse_number and parse_percent: the two functions that turn
the text Finviz shows ("124.30B", "-12.96%", "5,828.00") into numbers.

Every value in the sheet passes through one of these, so a mistake here
quietly corrupts or empties cells across thousands of rows.

pytest.approx: computers store decimals like 124.3 slightly imprecisely, so
124.3 * 1,000,000,000 can come out as 124299999999.99998. approx() means
"equal, allowing for that tiny rounding error".
"""

import pytest

from finviz_earnings_scraper import parse_number, parse_percent


# --- parse_number ------------------------------------------------------------

@pytest.mark.parametrize(
    "text, expected",
    [
        ("2.40", 2.40),            # EPS
        ("-0.18", -0.18),          # negative EPS
        ("+1.48", 1.48),           # an explicit plus sign
        ("72", 72.0),              # RSI, a whole number
        (" 2.40 ", 2.40),          # stray spaces around the value
        ("124.30B", 124.30e9),     # B = billion (revenue)
        ("46.60M", 46.60e6),       # M = million
        ("-163.63M", -163.63e6),   # negative million (revenue surprise)
        ("-1.36K", -1360.0),       # K = thousand
        ("1.50T", 1.50e12),        # T = trillion
    ],
)
def test_parse_number_reads_finviz_values(text, expected):
    assert parse_number(text) == pytest.approx(expected)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("5,828.00", 5828.00),     # NVR's price: Finviz adds a comma at 1,000+
        ("-1,234.50", -1234.50),   # negative, with a comma
        ("12,345,678.90", 12345678.90),  # more than one comma
    ],
)
def test_parse_number_reads_numbers_with_commas(text, expected):
    # Found 2026-11-12: these used to come back as None, so 588 price rows
    # across 124 stocks (NVR, AZO, MKL, ...) had every price cell blank.
    assert parse_number(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", [None, "", "—", "-", "N/A", "abc"])
def test_parse_number_returns_none_for_missing_values(text):
    # "—" is the long dash Finviz shows for "no value yet".
    # None (an empty cell) is right for these; 0 would be a wrong number.
    assert parse_number(text) is None


# --- parse_percent -----------------------------------------------------------

@pytest.mark.parametrize(
    "text, expected",
    [
        ("+11.91%", 11.91),        # stored as 11.91, not 0.1191
        ("-12.96%", -12.96),
        ("0.00%", 0.0),            # a real zero stays zero (not None)
        ("-2.06K%", -2060.0),      # Finviz's shorthand for -2,060%
        ("1.5M%", 1_500_000.0),
        ("1,234.56%", 1234.56),    # a comma, same as in parse_number
    ],
)
def test_parse_percent_reads_finviz_values(text, expected):
    assert parse_percent(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", [None, "", "—", "-", "abc%"])
def test_parse_percent_returns_none_for_missing_values(text):
    assert parse_percent(text) is None
