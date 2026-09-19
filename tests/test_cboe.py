import asyncio
from datetime import date, datetime

import pytest

from ivdb.cboe import FetchError, MarketStatus, collect, decide_market, make_fixture_fetcher, probe_market
from ivdb.universe import Ticker
from tests.conftest import FIXTURE_TRADE_DATE


def dt(s):
    return datetime.fromisoformat(s)


def test_decide_market_cases():
    full = dt("2026-09-04T15:59:59")
    assert decide_market(full, dt("2026-09-04T18:17:00")) is MarketStatus.OK
    assert decide_market(full, dt("2026-09-05T18:17:00")) is MarketStatus.CLOSED  # Saturday
    assert decide_market(full, dt("2026-09-07T18:17:00")) is MarketStatus.CLOSED  # holiday Monday
    live = dt("2026-09-04T13:59:00")
    assert decide_market(live, dt("2026-09-04T14:00:00")) is MarketStatus.TOO_EARLY
    early_close = dt("2026-09-04T12:59:59")
    assert decide_market(early_close, dt("2026-09-04T18:17:00")) is MarketStatus.EARLY_CLOSE
    # index options print until 16:15
    assert decide_market(dt("2026-09-04T16:14:59"), dt("2026-09-04T17:17:00")) is MarketStatus.OK


def test_probe_uses_fixture(fixtures_dir):
    fetch = make_fixture_fetcher(fixtures_dir)
    probe = asyncio.run(probe_market(fetch, "SPY", dt("2026-09-04T18:17:00")))
    assert probe.status is MarketStatus.OK
    assert probe.trade_date == FIXTURE_TRADE_DATE


def test_collect_marks_missing_and_stale(fixtures_dir):
    fetch = make_fixture_fetcher(fixtures_dir)
    tickers = [Ticker("SPY", "SPY", "etf", True), Ticker("MU", "MU", "equity", True), Ticker("BOGUS", "BOGUS", "equity", True)]
    results = asyncio.run(collect(tickers, FIXTURE_TRADE_DATE, fetch, concurrency=2))
    by = {r.symbol: r for r in results}
    assert by["SPY"].ok and by["MU"].ok
    assert not by["BOGUS"].ok and "fixture not found" in by["BOGUS"].error
    assert by["SPY"].summary.daily["iv30_atm"] is not None

    stale = asyncio.run(collect(tickers[:1], date(2026, 9, 8), fetch))
    assert not stale[0].ok and "stale chain" in stale[0].error


def test_fixture_fetcher_missing_is_not_retryable(fixtures_dir):
    fetch = make_fixture_fetcher(fixtures_dir)
    with pytest.raises(FetchError) as exc:
        asyncio.run(fetch("NOPE"))
    assert exc.value.retryable is False
