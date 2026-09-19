"""Surveillance tier: tail parsing, tiering, gates, and an end-to-end CLI run."""

import asyncio
import csv
import json
from datetime import date, timedelta

import pytest

from ivdb.cli import main
from ivdb.compute import parse_summary_tail, summarize_underlying, summary_trade_date
from ivdb.ivr import ivr_report, symbol_series
from ivdb.quality import classify_surveillance, gate_surveillance
from ivdb.store import SURVEILLANCE_COLUMNS, SURVEILLANCE_KEY, read_rows, upsert_rows
from ivdb.universe import ConfigError, load_tickers, split_tiers
from tests.conftest import FIXTURE_TRADE_DATE

TAIL = (
    '"prev_day_close": 162.75}], "symbol": "BE", "security_type": "stock", "current_price": 274.79, '
    '"open": 267.85, "high": 283.83, "low": 259.78, "close": 277.22, "prev_day_close": 252.87, '
    '"volume": 27451897, "iv30": 88.136, "last_trade_time": "2026-09-08T16:00:00", "tick": "down"}, "symbol": "BE"}'
)


def test_parse_summary_tail_recovers_the_block():
    s = parse_summary_tail(TAIL)
    assert s["symbol"] == "BE" and s["iv30"] == 88.136 and s["close"] == 277.22
    assert summary_trade_date(s) == date(2026, 9, 8)


def test_parse_summary_tail_rejects_junk():
    assert parse_summary_tail("") is None
    assert parse_summary_tail("not json at all") is None
    assert parse_summary_tail('], "broken": ') is None          # unparseable fragment
    assert parse_summary_tail('], "a": 1}') is None             # parses but has no last_trade_time


def test_summarize_underlying_converts_iv_to_decimal():
    row = summarize_underlying(parse_summary_tail(TAIL), date(2026, 9, 8))
    assert row["cboe_iv30"] == pytest.approx(0.88136)   # percent points -> decimal
    assert row["spot"] == 277.22 and row["prev_close"] == 252.87 and row["volume"] == 27451897
    assert set(row) == set(SURVEILLANCE_COLUMNS)


def test_summarize_underlying_falls_back_to_current_price():
    row = summarize_underlying({"current_price": 10.0, "close": 0, "iv30": None, "last_trade_time": "x"}, date(2026, 9, 8))
    assert row["spot"] == 10.0 and row["cboe_iv30"] is None


TIERED = ("symbol,cboe_symbol,kind,active,tier\n"
          "SPY,SPY,etf,1,active\nMU,MU,equity,1,active\nVIX,_VIX,index,1,surveillance\nOFF,OFF,equity,0,surveillance\n")


def test_tier_column(tmp_path):
    p = tmp_path / "t.csv"; p.write_text(TIERED)
    active, surveil = split_tiers(load_tickers(p))
    assert [t.symbol for t in active] == ["SPY", "MU"]
    assert [t.symbol for t in surveil] == ["VIX"]          # OFF is inactive


def test_tier_column_is_optional(tmp_path):
    p = tmp_path / "t.csv"; p.write_text("symbol,cboe_symbol,kind,active\nSPY,SPY,etf,1\n")
    active, surveil = split_tiers(load_tickers(p))
    assert len(active) == 1 and surveil == []              # old files read as all-active


def test_bad_tier_rejected(tmp_path):
    p = tmp_path / "t.csv"; p.write_text("symbol,cboe_symbol,kind,active,tier\nSPY,SPY,etf,1,watching\n")
    with pytest.raises(ConfigError, match="tier must be one of"):
        load_tickers(p)


def test_surveillance_gates():
    good = classify_surveillance("BE", {"spot": 277.0, "cboe_iv30": 0.88})
    assert good.ok and good.row is not None
    assert classify_surveillance("X", {"spot": 0, "cboe_iv30": 0.5}).error == "spot<=0"
    assert classify_surveillance("X", {"spot": 1, "cboe_iv30": None}).error == "cboe_iv30 missing"
    assert "out of range" in classify_surveillance("X", {"spot": 1, "cboe_iv30": 9.9}).error
    assert gate_surveillance([good] * 6 + [classify_surveillance("X", {"spot": 0})] * 4) == []
    problems = gate_surveillance([good] * 4 + [classify_surveillance("X", {"spot": 0})] * 6)
    assert problems and "surveillance success ratio" in problems[0]
    assert gate_surveillance([]) == []                     # no surveillance tier configured


def test_ivr_reads_surveillance_history(tmp_path):
    rows = [{"date": date(2026, 1, 2) + timedelta(days=i), "cboe_iv30": 0.10 + 0.001 * i} for i in range(200)]
    upsert_rows(tmp_path / "surveillance" / "BE.csv", SURVEILLANCE_COLUMNS, SURVEILLANCE_KEY, rows)
    series, source = symbol_series(tmp_path, "BE", "iv30_atm")   # not a surveillance column
    assert len(series) == 200 and source == "surveillance:cboe_iv30"
    rep = ivr_report(tmp_path, ["BE"])[0]
    assert rep["n"] == 200 and rep["ivr"] == pytest.approx(100.0)
    assert rep["source"] == "surveillance:cboe_iv30" and rep["confidence"] == "firm"


def test_cli_collects_both_tiers(tmp_path, fixtures_dir, monkeypatch):
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out.txt"))
    (tmp_path / "t.csv").write_text(
        "symbol,cboe_symbol,kind,active,tier\nSPY,SPY,etf,1,active\nMU,MU,equity,1,surveillance\nVIX,_VIX,index,1,surveillance\n")
    data = tmp_path / "data"
    args = ["collect", "--tickers", str(tmp_path / "t.csv"), "--data-dir", str(data),
            "--fixture-dir", str(fixtures_dir), "--probe-symbol", "SPY", "--now", "2026-09-04T18:17:00"]
    assert main(args) == 0

    assert (data / "daily" / "SPY.csv").exists() and (data / "term" / "SPY.csv").exists()
    assert not (data / "surveillance" / "SPY.csv").exists()      # active tier never writes here
    for sym in ("MU", "VIX"):
        rows = read_rows(data / "surveillance" / f"{sym}.csv", SURVEILLANCE_COLUMNS)
        assert len(rows) == 1 and rows[0]["date"] == str(FIXTURE_TRADE_DATE) and rows[0]["cboe_iv30"]
        assert not (data / "daily" / f"{sym}.csv").exists()      # surveillance never writes daily
    run = list(csv.DictReader((data / "runs.csv").open()))[-1]
    assert run["attempted"] == "1" and run["surv_attempted"] == "2" and run["surv_succeeded"] == "2"

    out = dict(l.split("=", 1) for l in (tmp_path / "out.txt").read_text().splitlines() if "=" in l)
    assert out["surv_succeeded"] == "2"
    assert main(["validate", "--data-dir", str(data)]) == 0

    before = (data / "surveillance" / "MU.csv").read_text()
    assert main(args + ["--force"]) == 0                          # idempotent
    assert (data / "surveillance" / "MU.csv").read_text() == before


def test_cli_no_surveillance_flag(tmp_path, fixtures_dir, monkeypatch):
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out.txt"))
    (tmp_path / "t.csv").write_text(
        "symbol,cboe_symbol,kind,active,tier\nSPY,SPY,etf,1,active\nMU,MU,equity,1,surveillance\n")
    data = tmp_path / "data"
    assert main(["collect", "--tickers", str(tmp_path / "t.csv"), "--data-dir", str(data),
                 "--fixture-dir", str(fixtures_dir), "--probe-symbol", "SPY",
                 "--now", "2026-09-04T18:17:00", "--no-surveillance"]) == 0
    assert (data / "daily" / "SPY.csv").exists()
    assert not (data / "surveillance").exists()


def test_cli_surveillance_gate_does_not_block_active_tier(tmp_path, fixtures_dir, monkeypatch):
    """A night where most surveillance summaries are stale must still write the active rows:
    CBOE serves only today's snapshot, so a skipped day can never be re-collected."""
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out.txt"))
    (tmp_path / "t.csv").write_text(
        "symbol,cboe_symbol,kind,active,tier\nSPY,SPY,etf,1,active\nMU,MU,equity,1,surveillance\n"
        "NOPE1,NOPE1,equity,1,surveillance\nNOPE2,NOPE2,equity,1,surveillance\nNOPE3,NOPE3,equity,1,surveillance\n")
    data = tmp_path / "data"
    assert main(["collect", "--tickers", str(tmp_path / "t.csv"), "--data-dir", str(data),
                 "--fixture-dir", str(fixtures_dir), "--probe-symbol", "SPY", "--now", "2026-09-04T18:17:00"]) == 0
    assert (data / "daily" / "SPY.csv").exists()
    assert (data / "surveillance" / "MU.csv").exists()
    run = list(csv.DictReader((data / "runs.csv").open()))[-1]
    assert run["status"] == "ok" and run["surv_attempted"] == "4" and run["surv_succeeded"] == "1"
    assert "surveillance gate" in run["note"]
    out = dict(l.split("=", 1) for l in (tmp_path / "out.txt").read_text().splitlines() if "=" in l)
    assert out["market_status"] == "ok"


def test_surveillance_accepts_lagged_summary_under_its_own_date(fixtures_dir):
    """CBOE regenerates quiet names' files only a few times a day, so at run time a summary
    is often still the previous close. It is kept and dated by its own session."""
    from ivdb.cboe import collect_surveillance, make_fixture_summary_fetcher
    from ivdb.universe import Ticker
    fetch = make_fixture_summary_fetcher(fixtures_dir)
    mu = [Ticker("MU", "MU", "equity", True, "surveillance")]

    same = asyncio.run(collect_surveillance(mu, FIXTURE_TRADE_DATE, fetch))[0]
    assert same.ok and same.lag_days == 0 and same.row["date"] == FIXTURE_TRADE_DATE

    lagged = asyncio.run(collect_surveillance(mu, FIXTURE_TRADE_DATE + timedelta(days=4), fetch))[0]
    assert lagged.ok and lagged.lag_days == 4 and lagged.row["date"] == FIXTURE_TRADE_DATE

    stale = asyncio.run(collect_surveillance(mu, FIXTURE_TRADE_DATE + timedelta(days=6), fetch))[0]
    assert not stale.ok and "stale chain" in stale.error

    future = asyncio.run(collect_surveillance(mu, FIXTURE_TRADE_DATE - timedelta(days=1), fetch))[0]
    assert not future.ok and "stale chain" in future.error
