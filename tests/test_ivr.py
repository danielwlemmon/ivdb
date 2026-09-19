from datetime import date, timedelta

import pytest

from ivdb.ivr import format_report, ivr_report, rank_and_percentile
from ivdb.store import DAILY_COLUMNS, DAILY_KEY, upsert_rows


def test_rank_and_percentile():
    values = [0.10, 0.20, 0.30, 0.40, 0.25]
    ivr, pct = rank_and_percentile(values)
    assert ivr == pytest.approx((0.25 - 0.10) / (0.40 - 0.10) * 100)
    assert pct == pytest.approx(2 / 4 * 100)  # 0.10 and 0.20 are below 0.25
    assert rank_and_percentile([0.2, 0.2, 0.2]) == (None, 0.0)
    assert rank_and_percentile([0.2]) == (None, None)


def test_report_requires_min_rows_and_uses_window(tmp_path):
    data_dir = tmp_path
    rows = []
    start = date(2026, 1, 2)
    for i in range(200):
        rows.append({"date": start + timedelta(days=i), "iv30_atm": 0.10 + 0.001 * i})
    upsert_rows(data_dir / "daily" / "SPY.csv", DAILY_COLUMNS, DAILY_KEY, rows)
    upsert_rows(data_dir / "daily" / "TINY.csv", DAILY_COLUMNS, DAILY_KEY, rows[:10])

    rep = {r["symbol"]: r for r in ivr_report(data_dir, ["SPY", "TINY", "MISSING"])}
    assert rep["SPY"]["n"] == 200 and rep["SPY"]["ivr"] == pytest.approx(100.0) and rep["SPY"]["iv_pct"] == pytest.approx(100.0)
    assert rep["TINY"]["ivr"] is None and rep["TINY"]["n"] == 10
    assert rep["MISSING"]["n"] == 0

    windowed = ivr_report(data_dir, ["SPY"], window=150)[0]
    assert windowed["n"] == 150 and windowed["low"] == pytest.approx(0.15)  # rows 50..199 only

    text = format_report(list(rep.values()))
    assert "n/a (n=10)" in text and "SPY" in text


def test_confidence_thresholds(tmp_path):
    """Below 120 rows the rank is refused; below 180 it is labelled provisional."""
    start = date(2026, 1, 2)

    def build(sym, n):
        rows = [{"date": start + timedelta(days=i), "iv30_atm": 0.10 + 0.001 * i} for i in range(n)]
        upsert_rows(tmp_path / "daily" / f"{sym}.csv", DAILY_COLUMNS, DAILY_KEY, rows)

    build("THIN", 119)
    build("EDGE", 120)
    build("PROV", 179)
    build("FIRM", 180)
    rep = {r["symbol"]: r for r in ivr_report(tmp_path, ["THIN", "EDGE", "PROV", "FIRM"])}
    assert rep["THIN"]["confidence"] == "insufficient" and rep["THIN"]["ivr"] is None
    assert rep["EDGE"]["confidence"] == "provisional" and rep["EDGE"]["ivr"] is not None
    assert rep["PROV"]["confidence"] == "provisional"
    assert rep["FIRM"]["confidence"] == "firm"

    text = format_report(list(rep.values()))
    assert "provisional = fewer than 180 rows" in text
    assert "insufficient" in text
    # the caller can still override the floor
    assert ivr_report(tmp_path, ["THIN"], min_rows=60)[0]["ivr"] is not None
