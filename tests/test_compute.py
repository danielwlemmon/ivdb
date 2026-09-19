import math
from datetime import date

import pytest

from ivdb.compute import (
    Contract,
    ExpirationMetrics,
    atm_iv,
    chain_trade_date,
    interpolate_cm,
    is_usable,
    pick_delta,
    select_term_subset,
    summarize,
)
from ivdb.occ import OccContract
from tests.conftest import FIXTURE_TRADE_DATE


def mk(strike, right, iv, delta, bid=1.0, ask=1.2, expiry=date(2026, 10, 16), oi=10):
    return Contract(OccContract("X", expiry, right, strike), iv, delta, bid, ask, oi)


def test_usable_filter():
    assert is_usable(mk(100, "C", 0.25, 0.5))
    assert not is_usable(mk(100, "C", 0.25, 0.5, bid=0, ask=0))  # unquoted
    assert not is_usable(mk(100, "C", 0.25, 0.5, bid=0, ask=4.8))  # one-sided placeholder
    assert not is_usable(mk(100, "C", 0.25, 0.5, bid=0.06, ask=4.95))  # absurd spread
    assert is_usable(mk(100, "C", 0.25, 0.5, bid=0.02, ask=0.11))  # cheap option, abs spread rule
    assert not is_usable(mk(100, "C", 0.25, 0.5, bid=0.08, ask=0.48))  # 0.40 wide on a 0.28 mid
    assert is_usable(mk(100, "C", 0.25, 0.5, bid=1.0, ask=1.5))  # 0.50 wide on a 1.25 mid: within 50%
    assert not is_usable(mk(100, "C", 0.25, 0.5, bid=1.0, ask=1.8))  # 0.80 wide on a 1.40 mid: too wide
    assert not is_usable(mk(100, "C", 0.005, 0.5))  # iv too small
    assert not is_usable(mk(100, "C", 6.0, 0.5))  # iv absurd
    assert not is_usable(mk(100, "C", None, 0.5))


def test_atm_iv_interpolates_each_side_to_spot():
    spot = 101.0
    cs = [mk(100, "C", 0.20, 0.55), mk(102, "C", 0.22, 0.45), mk(105, "C", 0.30, 0.2),
          mk(100, "P", 0.24, -0.45), mk(102, "P", 0.26, -0.55), mk(95, "P", 0.40, -0.1)]
    strike, iv, n = atm_iv(cs, spot)
    assert strike == 100.0  # nearest overall (tie broken toward lower strike)
    assert iv == pytest.approx((0.21 + 0.25) / 2)  # calls -> 0.21 at 101, puts -> 0.25 at 101
    assert n == 4


def test_atm_iv_coarse_strikes_low_vol():
    # spot 79.16, strikes 79 and 80: interpolation should sit close to the 79 strike IVs
    cs = [mk(79, "C", 0.036, 0.55), mk(80, "C", 0.046, 0.25), mk(79, "P", 0.038, -0.45), mk(80, "P", 0.034, -0.75)]
    _, iv, _ = atm_iv(cs, 79.16)
    assert iv == pytest.approx(((0.036 + 0.16 * 0.010) + (0.038 - 0.16 * 0.004)) / 2)


def test_atm_iv_one_sided_and_survivor_rules():
    strike, iv, n = atm_iv([mk(100, "C", 0.20, 0.5)], 100.0)
    assert strike == 100.0 and iv is None and n == 1
    # both sides present at the single ATM strike -> usable with n == 2
    _, iv, n = atm_iv([mk(100, "C", 0.20, 0.5), mk(100, "P", 0.22, -0.5)], 100.0)
    assert iv == pytest.approx(0.21) and n == 2
    # only strikes far above spot -> rejected as not ATM
    _, iv, _ = atm_iv([mk(110, "C", 0.20, 0.5), mk(110, "P", 0.22, -0.5)], 100.0)
    assert iv is None
    # bracketing strikes that are too far apart (55 and 61 around 58.1) must not be bridged
    _, iv, n = atm_iv([mk(55, "C", 0.30, 0.8), mk(61, "C", 0.12, 0.2), mk(55, "P", 0.28, -0.2), mk(61, "P", 0.14, -0.8)], 58.1)
    assert iv is None and n == 0
    # ...unless the caller widens max_dist (e.g. because the strike step is that coarse)
    _, iv, n = atm_iv([mk(55, "C", 0.30, 0.8), mk(61, "C", 0.12, 0.2)], 58.1, max_dist=3.5)
    assert iv is not None and n == 2
    assert atm_iv([], 100.0) == (None, None, 0)


def test_strike_step():
    from ivdb.compute import strike_step

    cs = [mk(k, "C", 0.2, 0.5) for k in (14.0, 14.5, 15.0, 15.5, 16.0)]
    assert strike_step(cs, 14.62) == 0.5
    assert strike_step([], 100.0) == pytest.approx(3.0)  # fallback: 3% of spot


def test_pick_delta_nearest_within_tolerance():
    puts = [mk(90, "P", 0.30, -0.18), mk(92, "P", 0.28, -0.24), mk(95, "P", 0.26, -0.33)]
    assert pick_delta(puts, "P") == (0.28, 92.0)
    calls = [mk(110, "C", 0.18, 0.12)]  # 0.13 away from 0.25 -> outside tolerance
    assert pick_delta(calls, "C") == (None, None)
    # wrong-signed delta is ignored
    assert pick_delta([mk(90, "P", 0.30, 0.25)], "P") == (None, None)


def em(dte, iv, expiry=None, p=None, c=None, n_usable=20, atm_n=4):
    expiry = expiry or (date(2026, 9, 4) + __import__("datetime").timedelta(days=dte))
    return ExpirationMetrics(expiry, dte, 100.0, iv, p, None, c, None, 0, n_usable, atm_n)


def test_interpolate_prefers_reliable_expirations():
    # an illiquid weekly at 30d with a junk IV must not override liquid neighbours
    exps = [em(21, 0.15), em(30, 0.35, n_usable=2, atm_n=2), em(42, 0.15)]
    assert interpolate_cm(exps, 30) == pytest.approx(0.15)
    # ...unless nothing reliable exists at all
    exps = [em(30, 0.35, n_usable=2, atm_n=2)]
    assert interpolate_cm(exps, 30) == pytest.approx(0.35)


def test_interpolate_total_variance_hand_check():
    # sigma 0.20 @ 20d, 0.24 @ 48d -> 30d
    exps = [em(20, 0.20), em(48, 0.24)]
    w1, w2 = 0.20**2 * 20, 0.24**2 * 48
    expected = math.sqrt((w1 + (w2 - w1) * 10 / 28) / 30)
    assert interpolate_cm(exps, 30) == pytest.approx(expected)
    assert interpolate_cm(exps, 30) == pytest.approx(0.2237, abs=1e-4)


def test_interpolate_exact_and_one_sided():
    exps = [em(20, 0.20), em(30, 0.21), em(48, 0.24)]
    assert interpolate_cm(exps, 30) == 0.21
    assert interpolate_cm(exps, 90) == 0.24  # beyond last -> nearest
    assert interpolate_cm(exps, 5) == 0.20  # before first -> nearest
    assert interpolate_cm([em(20, None)], 30) is None


def test_term_subset_nearest_8_plus_monthlies():
    # 12 non-Friday "weeklies" (Saturdays, so never third Fridays) plus monthlies at 42, 105, 133, 168 days
    weekly = [em(7 * k + 1, 0.2) for k in range(12)]  # 1, 8, 15, ... 78
    monthlies = [em(42, 0.2, expiry=date(2026, 10, 16)), em(105, 0.2, expiry=date(2026, 12, 18)), em(133, 0.2, expiry=date(2027, 1, 15)), em(168, 0.2, expiry=date(2027, 2, 19))]
    exps = sorted(weekly + monthlies, key=lambda e: e.dte)
    chosen = select_term_subset(exps)
    dtes = [e.dte for e in chosen]
    assert dtes[:8] == [1, 8, 15, 22, 29, 36, 42, 43]
    assert 105 in dtes  # monthly <= 120 kept even though it is beyond the nearest 8
    assert 133 not in dtes and 168 not in dtes
    assert dtes == sorted(dtes) and len(set(e.expiry for e in chosen)) == len(chosen)


def test_summarize_spy_fixture(spy_payload):
    assert chain_trade_date(spy_payload) == FIXTURE_TRADE_DATE
    s = summarize("SPY", spy_payload, FIXTURE_TRADE_DATE)
    d = s.daily
    assert d["spot"] == pytest.approx(770.19)
    assert d["cboe_iv30"] == pytest.approx(0.11605)  # percent points -> decimal
    assert 0.05 < d["iv30_atm"] < 0.30
    assert abs(d["iv30_atm"] - d["cboe_iv30"]) < 0.03  # own ATM IV tracks CBOE's index
    assert d["iv60_atm"] > d["iv30_atm"] > d["front_atm_iv"]  # upward-sloping term structure in fixture
    assert d["skew_25d_30"] > 0  # equity index puts richer than calls
    assert d["front_dte"] >= 1  # the same-day expiration (DTE 0) is excluded
    assert all(e.dte >= 1 for e in s.expirations)
    assert len(s.term) >= 4
    assert {r["date"] for r in s.term} == {FIXTURE_TRADE_DATE}
    assert d["n_contracts"] == len(spy_payload["data"]["options"])


def test_summarize_vix_index_fixture(vix_payload):
    s = summarize("VIX", vix_payload, FIXTURE_TRADE_DATE)
    assert s.daily["spot"] == pytest.approx(14.53)
    assert s.daily["iv30_atm"] is not None and 0.3 < s.daily["iv30_atm"] < 1.5
    assert s.daily["cboe_iv30"] == pytest.approx(0.60163)


def test_summarize_handles_empty_chain():
    payload = {"timestamp": "x", "data": {"close": 100.0, "current_price": 100.0, "last_trade_time": "2026-09-04T15:59:59", "options": []}}
    s = summarize("EMPTY", payload, FIXTURE_TRADE_DATE)
    assert s.daily["iv30_atm"] is None and s.term == [] and s.daily["n_contracts"] == 0
