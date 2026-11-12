"""Tests that read a SAVED copy of a real Finviz page (a "fixture") and check
that the parsers and row builders produce exactly the right values.

Why a saved page instead of the live site:
  - the same input every time, so a failure always means the CODE changed;
  - no network, Chrome or Finviz needed, so it runs in seconds in CI;
  - Finviz's numbers change daily; a saved page doesn't.

The fixture is tests/fixtures/finviz_nvr_earnings.html (NVR, captured
2026-11-12). The expected values below were read off that same page.
"""

from pathlib import Path

import pytest

from finviz_earnings_scraper import (
    build_performance_row,
    build_price_row,
    parse_performance_table,
    parse_price_reaction_table,
)

FIXTURE = Path(__file__).parent / "fixtures" / "finviz_nvr_earnings.html"


@pytest.fixture
def nvr_html():
    # A pytest "fixture" (same word, related idea): a function that prepares
    # input for tests. Any test that names `nvr_html` as a parameter gets
    # the page's HTML text passed in automatically.
    return FIXTURE.read_text(encoding="utf-8")


# --- EPS / GAAP EPS / Revenue widgets ----------------------------------------

def test_eps_table_has_all_twelve_quarters_in_page_order(nvr_html):
    eps = parse_performance_table(nvr_html, "EPS Performance and Forecast")
    assert list(eps) == [
        "Q4 '24", "Q1 '25", "Q2 '25", "Q3 '25", "Q4 '25", "Q1 '26",
        "Q2 '26", "Q3 '26", "Q4 '26", "Q1 '27", "Q2 '27", "Q3 '27",
    ]


def test_eps_reported_quarter_values(nvr_html):
    eps = parse_performance_table(nvr_html, "EPS Performance and Forecast")
    # The parser returns the text exactly as Finviz shows it ...
    assert eps["Q3 '26"] == {
        "estimate": "102.77",
        "reported": "89.45",
        "surprise": "-13.32",
        "surprise_pct": "-12.96%",
    }
    # ... and build_performance_row turns it into the numbers we store.
    row = build_performance_row("nvr", "Q3 '26", eps["Q3 '26"], "2026-11-12T00:00:00+00:00")
    assert row["ticker"] == "NVR"
    assert row["quarter_sort"] == 263
    assert row["estimate"] == pytest.approx(102.77)
    assert row["reported"] == pytest.approx(89.45)
    assert row["surprise"] == pytest.approx(-13.32)
    assert row["surprise_pct"] == pytest.approx(-12.96)


def test_future_quarter_has_estimate_but_no_reported_value(nvr_html):
    # Finviz shows "—" (a long dash) for a quarter not reported yet.
    # That must become an empty value, not 0 and not the dash itself.
    eps = parse_performance_table(nvr_html, "EPS Performance and Forecast")
    row = build_performance_row("NVR", "Q1 '27", eps["Q1 '27"], "t")
    assert row["estimate"] == pytest.approx(62.92)
    assert row["reported"] is None
    assert row["surprise"] is None
    assert row["surprise_pct"] is None


def test_gaap_widget_is_not_confused_with_eps_widget(nvr_html):
    # "EPS Performance and Forecast" is part of the text of "GAAP EPS
    # Performance and Forecast". The parser must match the heading EXACTLY,
    # or it could read the wrong table. On this page the two differ for
    # Q4 '25 (121.54 vs 121.56), which proves each read its own table.
    eps = parse_performance_table(nvr_html, "EPS Performance and Forecast")
    gaap = parse_performance_table(nvr_html, "GAAP EPS Performance and Forecast")
    assert eps["Q4 '25"]["reported"] == "121.54"
    assert gaap["Q4 '25"]["reported"] == "121.56"


def test_revenue_values_with_billion_and_million_suffixes(nvr_html):
    revenue = parse_performance_table(nvr_html, "Revenue Performance and Forecast")
    row = build_performance_row("NVR", "Q3 '26", revenue["Q3 '26"], "t")
    assert row["estimate"] == pytest.approx(2.57e9)
    assert row["reported"] == pytest.approx(2.40e9)
    assert row["surprise"] == pytest.approx(-163.63e6)
    assert row["surprise_pct"] == pytest.approx(-6.38)


def test_missing_widget_raises_value_error(nvr_html):
    # scrape_ticker relies on this: a page without a widget raises
    # ValueError, which it catches and logs instead of crashing.
    with pytest.raises(ValueError):
        parse_performance_table(nvr_html, "No Such Widget")


# --- Price Reaction widget ---------------------------------------------------

def test_price_reaction_reports_are_found_and_labelled(nvr_html):
    reactions = parse_price_reaction_table(nvr_html)
    # The page uses curly apostrophes ("Q3 ’26"); keys must be the
    # normalized form so they match the EPS table's quarters.
    assert list(reactions) == ["Q3 '26", "Q2 '26"]
    latest = reactions["Q3 '26"]
    assert latest["report_date"] == "Oct 22, 2026"
    assert latest["session"] == "BMO"
    assert latest["rsi"] == "45"


def test_price_row_has_every_value_as_a_number(nvr_html):
    reactions = parse_price_reaction_table(nvr_html)
    row = build_price_row("NVR", "Q3 '26", reactions["Q3 '26"], "t")

    assert row["report_date"] == "2026-10-22"
    assert row["session"] == "BMO"
    assert row["rsi"] == pytest.approx(45)

    # Prices. NVR trades above 1,000, so Finviz shows "6,110.63" with a
    # comma. Before the comma fix, every one of these came back as None.
    assert row["price_m3d"] == pytest.approx(5828.00)
    assert row["price_close"] == pytest.approx(6110.63)
    assert row["price_p1wk"] == pytest.approx(6018.95)
    assert row["price_m1wk"] == pytest.approx(5940.83)

    # The stock's % moves, and the S&P 500's (SPY) % moves.
    assert row["stockpct_close"] == pytest.approx(3.57)
    assert row["stockpct_p1wk"] == pytest.approx(3.91)
    assert row["spypct_close"] == pytest.approx(-1.18)
    assert row["spypct_p1wk"] == pytest.approx(-0.38)


def test_price_widget_missing_raises_value_error():
    with pytest.raises(ValueError):
        parse_price_reaction_table("<html><body>No widgets here</body></html>")
