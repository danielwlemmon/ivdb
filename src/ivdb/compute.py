"""Turn one CBOE option-chain payload into a compact daily summary.

All IVs here are annualized decimals (0.2053). CBOE's per-contract `iv` is
already a decimal; the per-underlying `iv30` is in percent points and is
divided by 100 on ingest.
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from datetime import date

from .occ import OccContract, parse_occ
from .timeutil import is_third_friday, parse_cboe_datetime

USABLE_IV_MIN = 0.01
USABLE_IV_MAX = 5.0
# CBOE's after-close snapshots contain placeholder quotes such as bid 0.00 / ask 4.80 whose
# IVs are meaningless. A contract must be two-sided with a spread no wider than the larger
# of MAX_ABS_SPREAD (cheap options) and MAX_REL_SPREAD x mid.
MAX_ABS_SPREAD = 0.10
MAX_REL_SPREAD = 0.50
DELTA_TARGET = 0.25
DELTA_TOL = 0.10
CM_TARGETS = (30, 60, 90)
SKEW_TARGET_DTE = 30
TERM_NEAREST_N = 8
TERM_MONTHLY_MAX_DTE = 120
# An expiration drives the constant-maturity curve only if its ATM estimate rests on enough
# quotes. Illiquid weeklies on sector ETFs otherwise dominate the 30-day point.
RELIABLE_MIN_ATM_N = 3
RELIABLE_MIN_USABLE = 6
# Strikes used for the ATM estimate must sit within max(ATM_MAX_MONEYNESS x spot, one strike step)
# of spot. Bridging distant strikes inherits their skew (XLF 2026-09-30 read 0.25 vs 0.14).
ATM_MAX_MONEYNESS = 0.03


@dataclass(slots=True)
class Contract:
    occ: OccContract
    iv: float | None
    delta: float | None
    bid: float
    ask: float
    oi: int


@dataclass(slots=True)
class ExpirationMetrics:
    expiry: date
    dte: int
    atm_strike: float | None
    atm_iv: float | None
    put25_iv: float | None
    put25_strike: float | None
    call25_iv: float | None
    call25_strike: float | None
    oi: int
    n_usable: int
    atm_n: int = 0  # contracts behind atm_iv (2-4)


def is_reliable(e: ExpirationMetrics) -> bool:
    return e.atm_iv is not None and e.atm_n >= RELIABLE_MIN_ATM_N and e.n_usable >= RELIABLE_MIN_USABLE


@dataclass(slots=True)
class ChainSummary:
    symbol: str
    trade_date: date
    daily: dict
    term: list[dict]
    expirations: list[ExpirationMetrics]  # every expiration with DTE >= 1, sorted by DTE


def _num(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def is_usable(c: Contract) -> bool:
    """A contract contributes to IV metrics only if it has a real two-sided market and a sane IV."""
    if c.iv is None or not (USABLE_IV_MIN < c.iv < USABLE_IV_MAX):
        return False
    if c.bid <= 0 or c.ask <= 0 or c.ask < c.bid:
        return False
    mid = (c.bid + c.ask) / 2.0
    return (c.ask - c.bid) <= max(MAX_ABS_SPREAD, MAX_REL_SPREAD * mid)


def parse_contracts(options: list[dict]) -> list[Contract]:
    out: list[Contract] = []
    for o in options:
        occ = parse_occ(str(o.get("option", "")))
        if occ is None:
            continue
        out.append(
            Contract(
                occ=occ,
                iv=_num(o.get("iv")),
                delta=_num(o.get("delta")),
                bid=_num(o.get("bid")) or 0.0,
                ask=_num(o.get("ask")) or 0.0,
                oi=int(_num(o.get("open_interest")) or 0),
            )
        )
    return out


def strike_step(contracts: list[Contract], spot: float) -> float:
    """Smallest gap between listed strikes near spot (any contract, usable or not)."""
    strikes = sorted({c.occ.strike for c in contracts if abs(c.occ.strike - spot) <= 0.10 * spot})
    gaps = [b - a for a, b in zip(strikes, strikes[1:]) if b > a]
    return min(gaps) if gaps else ATM_MAX_MONEYNESS * spot


def _interp_side(side: list[Contract], spot: float, max_dist: float) -> tuple[float | None, int]:
    """IV of one option type interpolated linearly to spot between the bracketing strikes.

    Only strikes within `max_dist` of spot are considered. Returns (iv, number of contracts
    used). Duplicate contracts at a strike (e.g. SPX and SPXW sharing an expiration) are
    collapsed with a median.
    """
    by_strike: dict[float, list[float]] = {}
    for c in side:
        if c.iv is not None and abs(c.occ.strike - spot) <= max_dist:
            by_strike.setdefault(c.occ.strike, []).append(c.iv)
    if not by_strike:
        return None, 0
    strikes = sorted(by_strike)
    below = [k for k in strikes if k <= spot]
    above = [k for k in strikes if k >= spot]
    if below and above:
        k1, k2 = below[-1], above[0]
        iv1 = statistics.median(by_strike[k1])
        if k1 == k2:
            return iv1, len(by_strike[k1])
        iv2 = statistics.median(by_strike[k2])
        w = (spot - k1) / (k2 - k1)
        return iv1 + (iv2 - iv1) * w, len(by_strike[k1]) + len(by_strike[k2])
    k = below[-1] if below else above[0]
    return statistics.median(by_strike[k]), len(by_strike[k])


def atm_iv(usable: list[Contract], spot: float, max_dist: float | None = None) -> tuple[float | None, float | None, int]:
    """ATM IV: interpolate call IV and put IV to spot, then average the two.

    Returns (atm_strike, atm_iv, n_contracts_used). At least two contracts must contribute,
    all within `max_dist` of spot (default 3% of spot). Interpolating to spot (rather than
    averaging the nearest strikes) matters for low-vol names with coarse strikes, where the
    nearest OTM strike already carries visible skew.
    """
    if max_dist is None:
        max_dist = ATM_MAX_MONEYNESS * spot
    strikes = sorted({c.occ.strike for c in usable}, key=lambda k: (abs(k - spot), k))
    if not strikes:
        return None, None, 0
    atm_strike = strikes[0]
    c_iv, c_n = _interp_side([c for c in usable if c.occ.right == "C"], spot, max_dist)
    p_iv, p_n = _interp_side([c for c in usable if c.occ.right == "P"], spot, max_dist)
    vals = [v for v in (c_iv, p_iv) if v is not None]
    n = c_n + p_n
    if not vals or n < 2:
        return atm_strike, None, n
    return atm_strike, sum(vals) / len(vals), n


def pick_delta(
    usable: list[Contract], right: str, target: float = DELTA_TARGET, tol: float = DELTA_TOL
) -> tuple[float | None, float | None]:
    """IV and strike of the contract whose |delta| is nearest `target` (within `tol`)."""
    best: Contract | None = None
    best_dist = math.inf
    for c in usable:
        if c.occ.right != right or c.delta is None:
            continue
        if right == "P" and c.delta >= 0:
            continue
        if right == "C" and c.delta <= 0:
            continue
        dist = abs(abs(c.delta) - target)
        if dist < best_dist:
            best, best_dist = c, dist
    if best is None or best_dist > tol:
        return None, None
    return best.iv, best.occ.strike


def expiration_metrics(contracts: list[Contract], spot: float, trade_date: date) -> list[ExpirationMetrics]:
    by_expiry: dict[date, list[Contract]] = {}
    for c in contracts:
        by_expiry.setdefault(c.occ.expiry, []).append(c)
    out: list[ExpirationMetrics] = []
    for expiry in sorted(by_expiry):
        dte = (expiry - trade_date).days
        if dte < 1:
            continue
        group = by_expiry[expiry]
        usable = [c for c in group if is_usable(c)]
        max_dist = max(ATM_MAX_MONEYNESS * spot, strike_step(group, spot))
        atm_strike, atm, atm_n = atm_iv(usable, spot, max_dist)
        p_iv, p_k = pick_delta(usable, "P")
        c_iv, c_k = pick_delta(usable, "C")
        out.append(
            ExpirationMetrics(
                expiry=expiry,
                dte=dte,
                atm_strike=atm_strike,
                atm_iv=atm,
                put25_iv=p_iv,
                put25_strike=p_k,
                call25_iv=c_iv,
                call25_strike=c_k,
                oi=sum(c.oi for c in group),
                n_usable=len(usable),
                atm_n=atm_n,
            )
        )
    return out


def interpolate_cm(exps: list[ExpirationMetrics], target_dte: int) -> float | None:
    """Constant-maturity ATM IV, linear in total variance between bracketing expirations.

    Only reliable expirations (enough quotes) are used; if there are none, fall back to every
    expiration with an ATM IV. One-sided (all expirations on the same side of the target) ->
    nearest expiration's IV.
    """
    cands = [e for e in exps if is_reliable(e)] or [e for e in exps if e.atm_iv is not None]
    pts = sorted((e.dte, e.atm_iv) for e in cands)
    if not pts:
        return None
    lower = [p for p in pts if p[0] <= target_dte]
    upper = [p for p in pts if p[0] >= target_dte]
    if lower and lower[-1][0] == target_dte:
        return lower[-1][1]
    if lower and upper:
        t1, s1 = lower[-1]
        t2, s2 = upper[0]
        w1 = s1 * s1 * t1
        w2 = s2 * s2 * t2
        var = w1 + (w2 - w1) * (target_dte - t1) / (t2 - t1)
        return math.sqrt(var / target_dte) if var > 0 else None
    _, s = upper[0] if upper else lower[-1]
    return s


def skew_expiration(exps: list[ExpirationMetrics], target_dte: int = SKEW_TARGET_DTE) -> ExpirationMetrics | None:
    """Expiration nearest the target that has both 25-delta IVs."""
    cands = [e for e in exps if e.put25_iv is not None and e.call25_iv is not None]
    if not cands:
        return None
    return min(cands, key=lambda e: (abs(e.dte - target_dte), e.dte))


def select_term_subset(exps: list[ExpirationMetrics]) -> list[ExpirationMetrics]:
    """Nearest N expirations plus standard monthlies out to TERM_MONTHLY_MAX_DTE."""
    chosen = list(exps[:TERM_NEAREST_N])
    chosen += [e for e in exps if is_third_friday(e.expiry) and e.dte <= TERM_MONTHLY_MAX_DTE]
    seen: set[date] = set()
    out: list[ExpirationMetrics] = []
    for e in sorted(chosen, key=lambda e: e.dte):
        if e.expiry not in seen:
            seen.add(e.expiry)
            out.append(e)
    return out


def chain_trade_date(payload: dict) -> date:
    """The session a chain snapshot belongs to: the date of the underlying's last trade."""
    return parse_cboe_datetime(str(payload["data"]["last_trade_time"])).date()


def summarize(symbol: str, payload: dict, trade_date: date) -> ChainSummary:
    data = payload["data"]
    contracts = parse_contracts(data.get("options") or [])
    close = _num(data.get("close"))
    last = _num(data.get("current_price"))
    spot = close if close and close > 0 else last

    exps = expiration_metrics(contracts, spot, trade_date) if spot and spot > 0 else []
    cm = {t: interpolate_cm(exps, t) for t in CM_TARGETS}
    sk = skew_expiration(exps)
    front = next((e for e in exps if is_reliable(e)), None) or next((e for e in exps if e.atm_iv is not None), None)
    iv30_pct = _num(data.get("iv30"))

    daily = {
        "date": trade_date,
        "spot": spot,
        "last": last,
        "close": close,
        "prev_close": _num(data.get("prev_day_close")),
        "volume": int(_num(data.get("volume")) or 0),
        "cboe_iv30": iv30_pct / 100.0 if iv30_pct is not None else None,
        "iv30_atm": cm[30],
        "iv60_atm": cm[60],
        "iv90_atm": cm[90],
        "put25_iv_30": sk.put25_iv if sk else None,
        "call25_iv_30": sk.call25_iv if sk else None,
        "skew_25d_30": (sk.put25_iv - sk.call25_iv) if sk else None,
        "front_expiry": front.expiry if front else None,
        "front_dte": front.dte if front else None,
        "front_atm_iv": front.atm_iv if front else None,
        "n_expirations": len({c.occ.expiry for c in contracts}),
        "n_contracts": len(contracts),
        "total_oi": sum(c.oi for c in contracts),
        "source_ts": str(payload.get("timestamp", "")),
    }
    term = [
        {
            "date": trade_date,
            "expiry": e.expiry,
            "dte": e.dte,
            "atm_strike": e.atm_strike,
            "atm_iv": e.atm_iv,
            "atm_n": e.atm_n,
            "put25_iv": e.put25_iv,
            "put25_strike": e.put25_strike,
            "call25_iv": e.call25_iv,
            "call25_strike": e.call25_strike,
            "oi": e.oi,
            "n_usable": e.n_usable,
        }
        for e in select_term_subset(exps)
    ]
    return ChainSummary(symbol=symbol, trade_date=trade_date, daily=daily, term=term, expirations=exps)


# --- surveillance tier -------------------------------------------------------
# The summary block sits at the END of a chain file, after the options array, so a
# suffix Range request (a few KB instead of several MB) is enough to read it.

def parse_summary_tail(text: str) -> dict | None:
    """Recover the per-underlying summary object from the tail of a chain file.

    The tail looks like `... "prev_day_close": 1.2}], "symbol": "BE", ... "tick": "down"}, ...`.
    Everything after the options array and before the next `}` is the summary, and all of
    those fields are scalars. Returns None if the fragment cannot be recovered, so the
    caller can fall back to a full fetch rather than guessing.
    """
    idx = text.rfind('], "')
    if idx < 0:
        return None
    rest = text[idx + 1 :]
    end = rest.find("}")
    if end < 0:
        return None
    body = rest[:end].strip().lstrip(",").strip()
    try:
        obj = json.loads("{" + body + "}")
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) and "last_trade_time" in obj else None


def summary_trade_date(summary: dict) -> date:
    """The session a summary belongs to, from the underlying's last trade."""
    return parse_cboe_datetime(str(summary["last_trade_time"])).date()


def summarize_underlying(summary: dict, trade_date: date) -> dict:
    """Build one surveillance row. `cboe_iv30` is converted from percent points to a decimal."""
    close = _num(summary.get("close"))
    last = _num(summary.get("current_price"))
    iv30 = _num(summary.get("iv30"))
    return {
        "date": trade_date,
        "spot": close if close and close > 0 else last,
        "close": close,
        "prev_close": _num(summary.get("prev_day_close")),
        "volume": int(_num(summary.get("volume")) or 0),
        "cboe_iv30": iv30 / 100.0 if iv30 is not None else None,
    }
