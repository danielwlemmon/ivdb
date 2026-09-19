from datetime import date

import pytest

from ivdb.store import DAILY_COLUMNS, DAILY_KEY, TERM_COLUMNS, TERM_KEY, SchemaError, append_run, completed_dates, fmt, read_rows, upsert_rows


def test_fmt_canonical():
    assert fmt(None) == ""
    assert fmt(0.2053) == "0.2053"
    assert fmt(770.0) == "770"
    assert fmt(0.12345678) == "0.123457"
    assert fmt(1e-9) == "0"
    assert fmt(date(2026, 9, 4)) == "2026-09-04"
    assert fmt(12) == "12"


def test_upsert_is_idempotent_and_sorted(tmp_path):
    p = tmp_path / "SPY.csv"
    row = {"date": date(2026, 9, 4), "spot": 770.19, "iv30_atm": 0.1157}
    upsert_rows(p, DAILY_COLUMNS, DAILY_KEY, [row])
    upsert_rows(p, DAILY_COLUMNS, DAILY_KEY, [row])
    rows = read_rows(p, DAILY_COLUMNS)
    assert len(rows) == 1 and rows[0]["spot"] == "770.19" and rows[0]["iv60_atm"] == ""

    upsert_rows(p, DAILY_COLUMNS, DAILY_KEY, [{"date": date(2026, 9, 3), "spot": 765.0}])
    upsert_rows(p, DAILY_COLUMNS, DAILY_KEY, [{"date": date(2026, 9, 4), "spot": 771.0}])  # replaces
    rows = read_rows(p, DAILY_COLUMNS)
    assert [r["date"] for r in rows] == ["2026-09-03", "2026-09-04"]
    assert rows[1]["spot"] == "771"
    assert not (tmp_path / "SPY.csv.tmp").exists()


def test_upsert_term_composite_key(tmp_path):
    p = tmp_path / "SPY.csv"
    d = date(2026, 9, 4)
    rows = [{"date": d, "expiry": date(2026, 10, 16), "dte": 42, "atm_iv": 0.12}, {"date": d, "expiry": date(2026, 9, 18), "dte": 14, "atm_iv": 0.10}]
    upsert_rows(p, TERM_COLUMNS, TERM_KEY, rows)
    upsert_rows(p, TERM_COLUMNS, TERM_KEY, [{"date": d, "expiry": date(2026, 9, 18), "dte": 14, "atm_iv": 0.11}])
    out = read_rows(p, TERM_COLUMNS)
    assert [(r["expiry"], r["atm_iv"]) for r in out] == [("2026-09-18", "0.11"), ("2026-10-16", "0.12")]


def test_header_mismatch_raises(tmp_path):
    p = tmp_path / "X.csv"
    p.write_text("date,foo\n2026-09-04,1\n")
    with pytest.raises(SchemaError):
        upsert_rows(p, DAILY_COLUMNS, DAILY_KEY, [{"date": date(2026, 9, 4)}])


def test_runs_log(tmp_path):
    p = tmp_path / "runs.csv"
    append_run(p, {"run_ts_utc": "t", "trade_date": date(2026, 9, 4), "status": "ok", "attempted": 2, "succeeded": 2})
    append_run(p, {"run_ts_utc": "t", "trade_date": date(2026, 9, 3), "status": "fail", "attempted": 2, "succeeded": 0})
    assert completed_dates(p) == {"2026-09-04"}
    assert p.read_text().splitlines()[0].startswith("run_ts_utc,trade_date,status")
