"""End-to-end CLI runs against the offline fixtures."""

import json
from pathlib import Path

import pytest

from ivdb.cli import main
from ivdb.store import DAILY_COLUMNS, TERM_COLUMNS, read_rows

TICKERS = "symbol,cboe_symbol,kind,active\nSPY,SPY,etf,1\nMU,MU,equity,1\nVIX,_VIX,index,1\nOFF,OFF,equity,0\n"


@pytest.fixture
def env(tmp_path, fixtures_dir, monkeypatch):
    (tmp_path / "tickers.csv").write_text(TICKERS)
    data = tmp_path / "data"
    gh_out = tmp_path / "gh_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))

    def run(*extra, now="2026-09-04T18:17:00"):
        args = ["collect", "--tickers", str(tmp_path / "tickers.csv"), "--data-dir", str(data), "--fixture-dir", str(fixtures_dir), "--probe-symbol", "SPY", "--now", now, *extra]
        return main(args)

    return tmp_path, data, gh_out, run


def outputs(gh_out: Path) -> dict:
    return dict(line.split("=", 1) for line in gh_out.read_text().splitlines() if "=" in line)


def test_full_write_then_idempotent_rerun(env):
    tmp_path, data, gh_out, run = env
    assert run() == 0
    assert outputs(gh_out)["market_status"] == "ok"
    daily = read_rows(data / "daily" / "SPY.csv", DAILY_COLUMNS)
    assert len(daily) == 1 and daily[0]["date"] == "2026-09-04" and daily[0]["iv30_atm"]
    term = read_rows(data / "term" / "SPY.csv", TERM_COLUMNS)
    assert len(term) >= 4
    assert (data / "daily" / "VIX.csv").exists()
    assert not (data / "daily" / "OFF.csv").exists()  # inactive skipped
    runs = read_rows(data / "runs.csv")
    assert runs[-1]["status"] == "ok" and runs[-1]["attempted"] == "3" and runs[-1]["succeeded"] == "3"

    before = {p.name: p.read_text() for p in (data / "daily").glob("*.csv")}
    # same day again without --force -> skipped, nothing changes
    assert run() == 0
    assert outputs(gh_out).get("market_status") == "skipped"
    # with --force -> rewritten, still identical content, one more run row
    assert run("--force") == 0
    after = {p.name: p.read_text() for p in (data / "daily").glob("*.csv")}
    assert before == after
    assert len(read_rows(data / "runs.csv")) == 2

    assert main(["validate", "--data-dir", str(data)]) == 0


def test_closed_day_writes_nothing(env):
    tmp_path, data, gh_out, run = env
    assert run(now="2026-09-05T18:17:00") == 0  # Saturday
    assert outputs(gh_out)["market_status"] == "closed"
    assert not (data / "daily").exists()
    assert read_rows(data / "runs.csv")[-1]["status"] == "closed"


def test_too_early_exit_2(env, fixtures_dir):
    tmp_path, data, gh_out, run = env
    # a probe chain whose last trade is mid-session (the market is still open)
    live_dir = tmp_path / "live"
    live_dir.mkdir()
    for p in fixtures_dir.glob("*.json"):
        (live_dir / p.name).symlink_to(p)
    live = json.loads((fixtures_dir / "SPY.json").read_text())
    live["data"]["last_trade_time"] = "2026-09-04T13:59:00"
    (live_dir / "LIVE.json").write_text(json.dumps(live))
    args = ["collect", "--tickers", str(tmp_path / "tickers.csv"), "--data-dir", str(data), "--fixture-dir", str(live_dir), "--probe-symbol", "LIVE", "--now", "2026-09-04T14:00:00"]
    assert main(args) == 2
    assert not (data / "daily").exists()
    # same probe after 17:00 ET is treated as an early close and proceeds
    assert main(args[:-1] + ["2026-09-04T18:17:00"]) == 0
    assert (data / "daily" / "SPY.csv").exists()


def test_quality_failure_exit_1_and_no_symbol_files(env):
    tmp_path, data, gh_out, run = env
    assert run("--symbols", "SPY,BOGUS1,BOGUS2") == 1
    assert outputs(gh_out)["market_status"] == "fail"
    assert not (data / "daily").exists()
    assert read_rows(data / "runs.csv")[-1]["status"] == "fail"


def test_runs_path_keeps_the_log_outside_the_data_dir(env):
    """Public code repo + private data repo: the run log is written where --runs-path says,
    and the already-collected check reads it from there too."""
    tmp_path, data, gh_out, run = env
    log_path = tmp_path / "code-repo" / "runs.csv"
    assert run("--runs-path", str(log_path)) == 0
    assert read_rows(log_path)[-1]["status"] == "ok"
    assert not (data / "runs.csv").exists()
    assert (data / "daily" / "SPY.csv").exists()
    assert run("--runs-path", str(log_path)) == 0
    assert outputs(gh_out).get("market_status") == "skipped"
    assert len(read_rows(log_path)) == 1


def test_dry_run_writes_nothing(env):
    tmp_path, data, gh_out, run = env
    assert run("--dry-run") == 0
    assert not data.exists()


def test_config_error_exit_3(env, tmp_path):
    tmp_path, data, gh_out, run = env
    assert main(["collect", "--tickers", str(tmp_path / "missing.csv"), "--fixture-dir", str(tmp_path)]) == 3
