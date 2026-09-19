"""CBOE delayed-quote chain fetching, market-session probe, and the collect loop.

CBOE's CDN sits behind Cloudflare and rate-limits bursts (HTTP 429 after roughly 40
requests in a minute at concurrency 6, observed 2026-09-05). The fetcher therefore paces
request starts globally and, on a 429, pauses every worker rather than just retrying the
one request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dtime
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from . import __version__
from .compute import chain_trade_date, parse_summary_tail, summarize, summarize_underlying, summary_trade_date
from .quality import SurveillanceResult, TickerResult, classify, classify_surveillance
from .timeutil import parse_cboe_datetime
from .universe import Ticker

log = logging.getLogger("ivdb.cboe")

BASE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"
USER_AGENT = f"ivdb/{__version__} (personal IV history collector)"
DEFAULT_CONCURRENCY = 2
DEFAULT_MIN_INTERVAL_S = 1.5  # minimum gap between request starts, across all workers
DEFAULT_ATTEMPTS = 5
RATE_LIMIT_COOLDOWN_S = (30.0, 60.0, 120.0, 180.0)  # after the 1st, 2nd, ... 429 for a request
TRANSIENT_BACKOFF_S = (1.0, 3.0, 10.0, 30.0)

# Regular session ends 16:00 ET (index options 16:15). A chain whose last trade is
# earlier than this on today's date is either an early-close day or not final yet.
SESSION_FINAL = dtime(15, 45)
# After this wall-clock time the market cannot still be open, so a short session is an early close.
LATE_ENOUGH = dtime(17, 0)

Fetcher = Callable[[str], Awaitable[dict]]


class MarketStatus(str, Enum):
    OK = "ok"
    CLOSED = "closed"
    TOO_EARLY = "too_early"
    EARLY_CLOSE = "early_close"


@dataclass(slots=True)
class Probe:
    status: MarketStatus
    trade_date: date
    last_trade: datetime
    cboe_ts: str


class FetchError(Exception):
    def __init__(self, message: str, retryable: bool = True, rate_limited: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.rate_limited = rate_limited
        self.retry_after = retry_after


def decide_market(last_trade: datetime, now: datetime) -> MarketStatus:
    """Classify the session from the probe symbol's last trade time and the current ET clock."""
    if last_trade.date() != now.date():
        return MarketStatus.CLOSED
    if last_trade.time() < SESSION_FINAL:
        return MarketStatus.TOO_EARLY if now.time() < LATE_ENOUGH else MarketStatus.EARLY_CLOSE
    return MarketStatus.OK


class Pacer:
    """Global request pacing shared by all workers: a minimum start interval plus cooldowns."""

    def __init__(self, min_interval_s: float):
        self.min_interval_s = max(0.0, min_interval_s)
        self._lock = asyncio.Lock()
        self._next_start = 0.0
        self._pause_until = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            start = max(now, self._next_start, self._pause_until)
            self._next_start = start + self.min_interval_s
        delay = start - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    def pause(self, seconds: float) -> None:
        """Hold every worker for `seconds` (extends, never shortens, an existing pause)."""
        until = time.monotonic() + seconds
        if until > self._pause_until:
            self._pause_until = until
            log.warning("rate limited by CBOE; pausing all requests for %.0fs", seconds)


def make_client(timeout_s: float = 60.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_s, connect=10.0),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def make_http_fetcher(
    client: httpx.AsyncClient,
    attempts: int = DEFAULT_ATTEMPTS,
    min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
    pacer: Pacer | None = None,
) -> Fetcher:
    pacer = pacer or Pacer(min_interval_s)

    async def fetch(cboe_symbol: str) -> dict:
        url = BASE_URL.format(symbol=cboe_symbol)
        last_exc: FetchError | None = None
        rate_limit_hits = 0
        for attempt in range(1, attempts + 1):
            await pacer.wait()
            try:
                resp = await client.get(url)
                if resp.status_code == 404:
                    raise FetchError("HTTP 404 (unknown CBOE symbol)", retryable=False)
                if resp.status_code == 429:
                    raise FetchError("HTTP 429 (rate limited)", rate_limited=True, retry_after=_retry_after_seconds(resp))
                if resp.status_code >= 500:
                    raise FetchError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                payload = resp.json()
                if not isinstance(payload, dict) or "data" not in payload or "options" not in payload["data"]:
                    raise FetchError("unexpected payload shape")
                return payload
            except FetchError as exc:
                last_exc = exc
                if not exc.retryable:
                    raise
            except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
                last_exc = FetchError(f"{type(exc).__name__}: {exc}")
            if attempt >= attempts:
                break
            if last_exc.rate_limited:
                cooldown = last_exc.retry_after or RATE_LIMIT_COOLDOWN_S[min(rate_limit_hits, len(RATE_LIMIT_COOLDOWN_S) - 1)]
                rate_limit_hits += 1
                pacer.pause(cooldown + random.uniform(0, 5))
            else:
                delay = TRANSIENT_BACKOFF_S[min(attempt - 1, len(TRANSIENT_BACKOFF_S) - 1)] + random.uniform(0, 1)
                log.debug("%s: attempt %d failed (%s); retrying in %.1fs", cboe_symbol, attempt, last_exc, delay)
                await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc

    return fetch


def make_fixture_fetcher(fixture_dir: Path) -> Fetcher:
    """Offline fetcher for tests and dry runs: reads {fixture_dir}/{cboe_symbol}.json."""

    async def fetch(cboe_symbol: str) -> dict:
        path = fixture_dir / f"{cboe_symbol}.json"
        if not path.exists():
            raise FetchError(f"fixture not found: {path.name}", retryable=False)
        return json.loads(path.read_text(encoding="utf-8"))

    return fetch


async def probe_market(fetch: Fetcher, probe_symbol: str, now: datetime) -> Probe:
    payload = await fetch(probe_symbol)
    last_trade = parse_cboe_datetime(str(payload["data"]["last_trade_time"]))
    status = decide_market(last_trade, now)
    return Probe(status=status, trade_date=last_trade.date(), last_trade=last_trade, cboe_ts=str(payload.get("timestamp", "")))


async def collect(
    tickers: list[Ticker], trade_date: date, fetch: Fetcher, concurrency: int = DEFAULT_CONCURRENCY
) -> list[TickerResult]:
    """Fetch and summarize every ticker. Each payload is discarded as soon as it is summarized."""
    sem = asyncio.Semaphore(max(1, concurrency))
    done = 0

    async def one(t: Ticker) -> TickerResult:
        nonlocal done
        async with sem:
            try:
                payload = await fetch(t.cboe_symbol)
            except FetchError as exc:
                log.warning("%s: fetch failed: %s", t.symbol, exc)
                return TickerResult(t.symbol, False, str(exc), None)
            finally:
                done += 1
                if done % 25 == 0:
                    log.info("fetched %d/%d", done, len(tickers))
        try:
            chain_date = chain_trade_date(payload)
        except (KeyError, ValueError) as exc:
            return TickerResult(t.symbol, False, f"bad last_trade_time: {exc}", None)
        if chain_date != trade_date:
            return TickerResult(t.symbol, False, f"stale chain (last trade {chain_date})", None)
        try:
            summary = summarize(t.symbol, payload, trade_date)
        except Exception as exc:  # a parser bug must not take down the whole run
            log.exception("%s: summarize failed", t.symbol)
            return TickerResult(t.symbol, False, f"summarize error: {type(exc).__name__}: {exc}", None)
        del payload
        return classify(summary)

    return list(await asyncio.gather(*(one(t) for t in tickers)))


# --- surveillance tier -------------------------------------------------------
# A suffix Range request pulls only the tail of the chain file, where the per-underlying
# summary lives. That is ~900 bytes instead of several MB, which is what makes watching
# a thousand extra tickers affordable.

SUMMARY_TAIL_BYTES = 4096
SUMMARY_TAIL_MAX = 32768

# CBOE regenerates the chain file of a quiet name only a few times a day, so at run time
# roughly a third of surveillance tickers still show the previous session's close (measured
# 2026-09-10: 348/784). Those are accepted and stored under their own date; the next run
# fills today. Five calendar days spans a long weekend. Anything older is a dead symbol.
SURVEILLANCE_MAX_LAG_DAYS = 5


def make_summary_fetcher(
    client: httpx.AsyncClient,
    attempts: int = DEFAULT_ATTEMPTS,
    min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
    pacer: Pacer | None = None,
) -> Fetcher:
    """Fetch just the underlying summary. Widens the tail window, then falls back to a
    full download, if the fragment cannot be parsed (e.g. CBOE reorders the JSON)."""
    pacer = pacer or Pacer(min_interval_s)

    async def fetch(cboe_symbol: str) -> dict:
        url = BASE_URL.format(symbol=cboe_symbol)
        last_exc: FetchError | None = None
        rate_limit_hits = 0
        for attempt in range(1, attempts + 1):
            window = SUMMARY_TAIL_BYTES if attempt == 1 else SUMMARY_TAIL_MAX
            full = attempt >= 3  # give up on Range and take the whole file
            await pacer.wait()
            try:
                headers = {} if full else {"Range": f"bytes=-{window}"}
                resp = await client.get(url, headers=headers)
                if resp.status_code == 404:
                    raise FetchError("HTTP 404 (unknown CBOE symbol)", retryable=False)
                if resp.status_code == 429:
                    raise FetchError("HTTP 429 (rate limited)", rate_limited=True, retry_after=_retry_after_seconds(resp))
                if resp.status_code >= 500:
                    raise FetchError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                if resp.status_code == 206:
                    summary = parse_summary_tail(resp.text)
                    if summary is not None:
                        return summary
                    raise FetchError("could not parse summary from tail")
                payload = resp.json()  # server ignored Range and sent everything
                if not isinstance(payload, dict) or "data" not in payload:
                    raise FetchError("unexpected payload shape")
                return payload["data"]
            except FetchError as exc:
                last_exc = exc
                if not exc.retryable:
                    raise
            except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
                last_exc = FetchError(f"{type(exc).__name__}: {exc}")
            if attempt >= attempts:
                break
            if last_exc.rate_limited:
                cooldown = last_exc.retry_after or RATE_LIMIT_COOLDOWN_S[min(rate_limit_hits, len(RATE_LIMIT_COOLDOWN_S) - 1)]
                rate_limit_hits += 1
                pacer.pause(cooldown + random.uniform(0, 5))
            else:
                await asyncio.sleep(TRANSIENT_BACKOFF_S[min(attempt - 1, len(TRANSIENT_BACKOFF_S) - 1)] + random.uniform(0, 1))
        assert last_exc is not None
        raise last_exc

    return fetch


def make_fixture_summary_fetcher(fixture_dir: Path) -> Fetcher:
    """Offline equivalent: read the fixture and hand back its summary fields."""

    async def fetch(cboe_symbol: str) -> dict:
        path = fixture_dir / f"{cboe_symbol}.json"
        if not path.exists():
            raise FetchError(f"fixture not found: {path.name}", retryable=False)
        data = json.loads(path.read_text(encoding="utf-8"))["data"]
        return {k: v for k, v in data.items() if k != "options"}

    return fetch


async def collect_surveillance(
    tickers: list[Ticker], trade_date: date, fetch: Fetcher, concurrency: int = DEFAULT_CONCURRENCY
) -> list[SurveillanceResult]:
    """Fetch the underlying summary for every surveillance ticker and build one row each."""
    sem = asyncio.Semaphore(max(1, concurrency))
    done = 0

    async def one(t: Ticker) -> SurveillanceResult:
        nonlocal done
        async with sem:
            try:
                summary = await fetch(t.cboe_symbol)
            except FetchError as exc:
                log.warning("%s: surveillance fetch failed: %s", t.symbol, exc)
                return SurveillanceResult(t.symbol, False, str(exc), None)
            finally:
                done += 1
                if done % 100 == 0:
                    log.info("surveillance: fetched %d/%d", done, len(tickers))
        try:
            chain_date = summary_trade_date(summary)
        except (KeyError, ValueError) as exc:
            return SurveillanceResult(t.symbol, False, f"bad last_trade_time: {exc}", None)
        lag = (trade_date - chain_date).days
        if lag < 0 or lag > SURVEILLANCE_MAX_LAG_DAYS:
            return SurveillanceResult(t.symbol, False, f"stale chain (last trade {chain_date})", None)
        # A lagged summary is still that session's close, so it is stored under its own date.
        result = classify_surveillance(t.symbol, summarize_underlying(summary, chain_date))
        result.lag_days = lag
        return result

    return list(await asyncio.gather(*(one(t) for t in tickers)))
