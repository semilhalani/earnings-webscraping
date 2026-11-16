"""Tests for the two hard limits added after run #553 froze for 45 minutes:

  1. ticker_time_limit: one ticker may take at most TICKER_TIME_LIMIT_SECONDS.
     When time is up, its browser is force-closed, the ticker is FAILED and
     the run moves on.
  2. the checkpoint save window: retries of the Apps Script writes stop once
     CHECKPOINT_SAVE_LIMIT_SECONDS is used up.

No Chrome and no network: a "hung ticker" is just time.sleep, a "browser"
is a harmless `sleep` process, and Google's Apps Script is a fake function.
"""

import signal
import subprocess
import sys
import time

import pytest
import requests

import finviz_earnings_scraper as scraper
from finviz_earnings_scraper import (
    TickerTimeLimitError,
    fetch_html_with_retry,
    force_close_browsers,
    run_batch,
    ticker_time_limit,
    write_via_webapp,
)

needs_alarm = pytest.mark.skipif(
    not hasattr(signal, "SIGALRM") or sys.platform == "win32",
    reason="the time limit uses the operating system's alarm signal (Linux/macOS only)",
)


# --- 1. The per-ticker time limit ----------------------------------------------

@needs_alarm
def test_a_hung_ticker_is_stopped_at_the_limit():
    started = time.monotonic()
    with pytest.raises(TickerTimeLimitError):
        with ticker_time_limit(0.3):
            time.sleep(10)  # stands in for a page load that never finishes
    assert time.monotonic() - started < 2


@needs_alarm
def test_a_ticker_that_finishes_in_time_is_not_affected():
    with ticker_time_limit(2):
        value = 1 + 1
    assert value == 2
    # The alarm is switched off again afterwards.
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


class FakeService:
    """Stands in for Selenium's chromedriver Service: just has a .process."""

    def __init__(self, process):
        self.process = process


@needs_alarm
def test_time_limit_force_closes_the_browser_processes():
    # A harmless stand-in "browser": a shell running two sleeps in its own
    # process group, like chromedriver plus the Chrome it starts.
    browser = subprocess.Popen(["sh", "-c", "sleep 60 & sleep 60"], start_new_session=True)
    scraper._OPEN_BROWSER_SERVICES.append(FakeService(browser))
    try:
        with pytest.raises(TickerTimeLimitError):
            with ticker_time_limit(0.3):
                time.sleep(10)
        assert browser.wait(timeout=5) == -signal.SIGKILL  # killed, not left running
        assert scraper._OPEN_BROWSER_SERVICES == []
    finally:
        if browser.poll() is None:
            browser.kill()


def test_force_close_skips_browsers_that_already_closed():
    finished = subprocess.Popen(["true"])
    finished.wait()
    scraper._OPEN_BROWSER_SERVICES.extend([FakeService(finished), FakeService(None)])
    assert force_close_browsers() == 0
    assert scraper._OPEN_BROWSER_SERVICES == []


def test_get_driver_gives_the_browser_its_own_process_group(monkeypatch):
    # No real Chrome: a fake stands in for webdriver.Chrome and keeps what
    # it was given.
    started = {}

    class FakeChrome:
        def __init__(self, options=None, service=None):
            started["service"] = service

        def set_page_load_timeout(self, seconds):
            pass

    monkeypatch.setattr(scraper.webdriver, "Chrome", FakeChrome)
    scraper.get_driver()
    service = started["service"]
    assert service.popen_kw == {"start_new_session": True}
    assert scraper._OPEN_BROWSER_SERVICES == [service]  # known, so it can be killed
    scraper._OPEN_BROWSER_SERVICES.clear()


def test_get_driver_forgets_a_browser_that_failed_to_start(monkeypatch):
    def broken_chrome(options=None, service=None):
        raise RuntimeError("Chrome failed to start")

    monkeypatch.setattr(scraper.webdriver, "Chrome", broken_chrome)
    with pytest.raises(RuntimeError):
        scraper.get_driver()
    assert scraper._OPEN_BROWSER_SERVICES == []


def test_a_timed_out_ticker_is_not_retried(monkeypatch):
    # A retry would start a new browser with no limit left, so it must not happen.
    calls = []

    def fake_fetch_html(ticker, headless=True):
        calls.append(ticker)
        raise TickerTimeLimitError("took too long")

    monkeypatch.setattr(scraper, "fetch_html", fake_fetch_html)
    with pytest.raises(TickerTimeLimitError):
        fetch_html_with_retry("HANG", retry_delay=0)
    assert calls == ["HANG"]


@needs_alarm
def test_run_marks_a_hung_ticker_failed_and_carries_on(monkeypatch, tmp_path):
    def fake_scrape_ticker(ticker, headless=True):
        if ticker == "HANG":
            time.sleep(10)
        return {"eps": [], "gaap_eps": [], "revenue": [], "price_reaction": []}

    saved = {}

    def fake_write(url, sheet_name=None, headers=None, rows=None, **kwargs):
        saved.setdefault(sheet_name, []).extend(rows or [])
        return {"ok": True, "appended": 0, "updated": 0}

    monkeypatch.setattr(scraper, "scrape_ticker", fake_scrape_ticker)
    monkeypatch.setattr(scraper, "write_via_webapp", fake_write)
    monkeypatch.setattr(scraper, "TICKER_TIME_LIMIT_SECONDS", 0.3)

    started = time.monotonic()
    result = run_batch(["AAPL", "HANG", "MSFT"], web_app_url="https://example.invalid",
                       delay=0, run_date="2026-11-16", output_dir=str(tmp_path))

    assert time.monotonic() - started < 5
    assert result["ok_count"] == 2
    assert result["failed_tickers"] == ["HANG"]
    assert result["status"] == "sweep finished"
    checked = {row["ticker"]: row["result"] for row in saved["Raw_LastChecked"]}
    assert checked == {"AAPL": "OK", "HANG": "FAILED", "MSFT": "OK"}
    issue = saved["Raw_ScrapeIssues"][0]
    assert (issue["ticker"], issue["status"]) == ("HANG", "FAILED")
    assert "took longer than" in issue["detail"]


# --- 2. The checkpoint save window --------------------------------------------

class FakeClock:
    """Pretend time, so a "2-minute timeout" takes no real time in the test."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def unreachable_apps_script(monkeypatch):
    """Every write waits for its full timeout and then fails, the way it
    looks when Google's Apps Script stops answering. Records each attempt's
    timeout."""
    clock = FakeClock()
    attempts = []

    def fake_post(url, json=None, timeout=None):
        attempts.append(timeout)
        clock.sleep(timeout)
        raise requests.ConnectionError("Apps Script did not answer")

    monkeypatch.setattr(scraper, "time", clock)
    monkeypatch.setattr(requests, "post", fake_post)
    return clock, attempts


def test_without_a_save_window_the_old_retries_are_unchanged(unreachable_apps_script):
    clock, attempts = unreachable_apps_script
    with pytest.raises(requests.ConnectionError):
        write_via_webapp("https://example.invalid", "Raw_EPSHistory", [], [], timeout=120)
    assert attempts == [120, 120, 120, 120, 120]  # 5 attempts
    assert clock.now - 1000.0 == 5 * 120 + (5 + 15 + 30 + 60)  # ~12 minutes


def test_retries_stop_when_the_save_window_is_used_up(unreachable_apps_script):
    clock, attempts = unreachable_apps_script
    with pytest.raises(requests.ConnectionError):
        write_via_webapp("https://example.invalid", "Raw_EPSHistory", [], [], timeout=120,
                         deadline=clock.monotonic() + 300)
    assert len(attempts) < 5
    assert clock.now - 1000.0 <= 300 + 30  # the window, plus at most one short last try


def test_a_write_after_the_window_still_gets_one_short_try(unreachable_apps_script):
    clock, attempts = unreachable_apps_script
    with pytest.raises(requests.ConnectionError):
        write_via_webapp("https://example.invalid", "ScraperProgress", [], [], timeout=120,
                         deadline=clock.monotonic() - 1)
    assert attempts == [30]
