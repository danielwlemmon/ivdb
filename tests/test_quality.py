from datetime import date

from ivdb.compute import ChainSummary, ExpirationMetrics
from ivdb.quality import ATM_COLUMNS, TickerResult, atm_reason, classify, gate_job, hard_reason, validate_file
from ivdb.store import DAILY_COLUMNS, DAILY_KEY


def exp(dte=14, atm_iv=0.11, n_usable=10, atm_n=4, expiry=date(2026, 9, 18)):
    return ExpirationMetrics(expiry, dte, 100.0, atm_iv, None, None, None, None, 0, n_usable, atm_n)


def summary(sym="SPY", iv30=0.12, iv60=0.13, spot=100.0, cboe=0.12, exps=None):
    daily = {"spot": spot, "iv30_atm": iv30, "iv60_atm": iv60, "iv90_atm": 0.14, "cboe_iv30": cboe,
             "put25_iv_30": 0.13, "call25_iv_30": 0.11, "skew_25d_30": 0.02,
             "front_expiry": date(2026, 9, 18), "front_dte": 14, "front_atm_iv": 0.11}
    return ChainSummary(sym, date(2026, 9, 4), daily, [], exps if exps is not None else [exp()])


def ok(sym="SPY", **kw):
    return TickerResult(sym, True, None, summary(sym, **kw))


def bad(sym, err="HTTP 404"):
    return TickerResult(sym, False, err, None)


def no_history(_sym):
    return []


def test_hard_and_atm_reasons():
    assert hard_reason(summary()) is None
    assert hard_reason(summary(spot=0)) == "spot<=0"
    assert atm_reason(summary()) is None
    assert atm_reason(summary(iv30=None)) == "iv30_atm missing"
    assert "out of range" in atm_reason(summary(iv30=5.0))
    # an illiquid expiration (2 usable quotes) cannot anchor the 30-day point
    assert "no reliable expiration" in atm_reason(summary(exps=[exp(n_usable=2, atm_n=2)]))
    # a liquid expiration outside 7-60 DTE does not count either
    assert "no reliable expiration" in atm_reason(summary(exps=[exp(dte=105, expiry=date(2026, 12, 18))]))


def test_classify_blanks_atm_columns_but_keeps_row():
    r = classify(summary(exps=[exp(n_usable=2, atm_n=2)]))
    assert r.ok and not r.atm_ok and "no reliable" in r.atm_note
    assert all(r.summary.daily[c] is None for c in ATM_COLUMNS)
    assert r.summary.daily["cboe_iv30"] == 0.12 and r.summary.daily["spot"] == 100.0
    r = classify(summary(spot=-1))
    assert not r.ok and r.summary is None and r.error == "spot<=0"
    r = classify(summary())
    assert r.ok and r.atm_ok and r.summary.daily["iv30_atm"] == 0.12


def test_ratio_gate():
    results = [ok(f"S{i}") for i in range(9)] + [bad("B1")]
    assert gate_job(results, no_history) == []  # 9/10 = 0.90 passes
    results.append(bad("B2"))
    problems = gate_job(results, no_history)
    assert len(problems) == 1 and "success ratio" in problems[0] and "B1(HTTP 404)" in problems[0]
    assert gate_job([], no_history) == ["no tickers attempted"]


def test_atm_blank_ratio_gate():
    blank = [classify(summary(f"B{i}", exps=[exp(n_usable=1, atm_n=1)])) for i in range(6)]
    good = [ok(f"G{i}") for i in range(4)]
    problems = gate_job(good + blank, no_history)
    assert any("ATM IV unreliable for 6/10" in p for p in problems)
    assert gate_job(good + blank[:3], no_history) == []  # 3/7 < 50%


def test_range_gate_belt_and_suspenders():
    problems = gate_job([ok(iv60=4.5)], no_history)
    assert any("iv60_atm" in p for p in problems)


def test_jump_gate_requires_history_and_is_confirmed_by_cboe():
    hist = [{"iv30_atm": "0.12", "cboe_iv30": "0.12"} for _ in range(20)]
    # 4x jump with CBOE flat -> parser regression
    problems = gate_job([ok(iv30=0.48, cboe=0.12)], lambda s: hist)
    assert any("parser regression" in p for p in problems)
    # 4x jump with CBOE confirming -> real vol event, allowed
    assert gate_job([ok(iv30=0.48, cboe=0.45)], lambda s: hist) == []
    # not enough history -> gate inactive
    assert gate_job([ok(iv30=0.48, cboe=0.12)], lambda s: hist[:10]) == []


def test_validate_file(tmp_path):
    p = tmp_path / "X.csv"
    p.write_text(",".join(DAILY_COLUMNS) + "\n" + "2026-09-04" + "," * (len(DAILY_COLUMNS) - 1) + "\n" + "2026-09-03" + "," * (len(DAILY_COLUMNS) - 1) + "\n")
    problems = validate_file(p, DAILY_COLUMNS, DAILY_KEY)
    assert any("not sorted" in x for x in problems)
    p.write_text(",".join(DAILY_COLUMNS) + "\n" + ("2026-09-04" + "," * (len(DAILY_COLUMNS) - 1) + "\n") * 2)
    assert any("duplicate" in x for x in validate_file(p, DAILY_COLUMNS, DAILY_KEY))
    p.write_text("date,foo\n")
    assert validate_file(p, DAILY_COLUMNS, DAILY_KEY)
