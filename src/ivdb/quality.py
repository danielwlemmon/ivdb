"""Quality gates.

Two tiers per ticker:
- hard failure (fetch error, stale chain, bad spot): the ticker gets no row today;
- ATM failure (no liquid expiration anchoring the 30-day point, IV out of range): the row
  is still written with spot/volume/`cboe_iv30`, but every ATM-derived column is blank.

A run that fails a job-level gate writes nothing at all.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .compute import ChainSummary, is_reliable
from .store import read_rows

IV_MIN = 0.03
IV_MAX = 4.0
MIN_SUCCESS_RATIO = 0.90
MAX_ATM_BLANK_RATIO = 0.50
JUMP_FACTOR = 3.0
JUMP_LOOKBACK = 20
CBOE_MOVE_FACTOR = 1.5
# The 30-day point must be anchored by at least one liquid expiration in this DTE range.
ANCHOR_DTE_RANGE = (7, 60)

ATM_COLUMNS = (
    "iv30_atm", "iv60_atm", "iv90_atm",
    "put25_iv_30", "call25_iv_30", "skew_25d_30",
    "front_expiry", "front_dte", "front_atm_iv",
)


@dataclass(slots=True)
class TickerResult:
    symbol: str
    ok: bool  # a daily row will be written
    error: str | None  # hard-failure reason
    summary: ChainSummary | None
    atm_ok: bool = True  # ATM-derived columns populated
    atm_note: str | None = None


def hard_reason(summary: ChainSummary) -> str | None:
    """Reasons the whole row is untrustworthy."""
    d = summary.daily
    if not d["spot"] or d["spot"] <= 0:
        return "spot<=0"
    return None


def atm_reason(summary: ChainSummary) -> str | None:
    """Reasons the ATM-derived columns must be blanked (row still written)."""
    d = summary.daily
    lo, hi = ANCHOR_DTE_RANGE
    if not any(is_reliable(e) and lo <= e.dte <= hi for e in summary.expirations):
        return f"no reliable expiration within {lo}-{hi} DTE"
    if d["iv30_atm"] is None:
        return "iv30_atm missing"
    if not (IV_MIN < d["iv30_atm"] < IV_MAX):
        return f"iv30_atm out of range ({d['iv30_atm']:.4f})"
    return None


def blank_atm(summary: ChainSummary) -> None:
    for col in ATM_COLUMNS:
        summary.daily[col] = None


def classify(summary: ChainSummary) -> TickerResult:
    hard = hard_reason(summary)
    if hard:
        return TickerResult(summary.symbol, False, hard, None)
    note = atm_reason(summary)
    if note:
        blank_atm(summary)
    return TickerResult(summary.symbol, True, None, summary, atm_ok=note is None, atm_note=note)


def _floats(rows: list[dict[str, str]], col: str) -> list[float]:
    out = []
    for r in rows:
        try:
            out.append(float(r[col]))
        except (KeyError, ValueError, TypeError):
            pass
    return out


def gate_job(
    results: list[TickerResult],
    load_history: Callable[[str], list[dict[str, str]]],
    min_ratio: float = MIN_SUCCESS_RATIO,
    max_atm_blank_ratio: float = MAX_ATM_BLANK_RATIO,
) -> list[str]:
    """Job-level gates. Returns a list of problems; empty means the run may be written."""
    problems: list[str] = []
    attempted = len(results)
    ok = [r for r in results if r.ok and r.summary is not None]
    if attempted == 0:
        return ["no tickers attempted"]
    ratio = len(ok) / attempted
    if ratio < min_ratio:
        failed = ", ".join(f"{r.symbol}({r.error})" for r in results if not r.ok)
        problems.append(f"success ratio {len(ok)}/{attempted}={ratio:.2f} < {min_ratio}: {failed}")

    blank = [r for r in ok if not r.atm_ok]
    if ok and len(blank) / len(ok) > max_atm_blank_ratio:
        problems.append(
            f"ATM IV unreliable for {len(blank)}/{len(ok)} tickers (> {max_atm_blank_ratio:.0%}); "
            "likely a CBOE quote-format change or a filter regression"
        )

    for r in ok:
        if not r.atm_ok:
            continue
        d = r.summary.daily
        for col in ("iv30_atm", "iv60_atm"):
            v = d.get(col)
            if v is not None and not (IV_MIN < v < IV_MAX):
                problems.append(f"{r.symbol}: {col}={v:.4f} out of range")

        hist = load_history(r.symbol)
        prior_iv = _floats(hist, "iv30_atm")[-JUMP_LOOKBACK:]
        if len(prior_iv) >= JUMP_LOOKBACK and d.get("iv30_atm") is not None:
            med = statistics.median(prior_iv)
            cur = d["iv30_atm"]
            if med > 0 and (cur > JUMP_FACTOR * med or cur < med / JUMP_FACTOR):
                prev_cboe = _floats(hist[-1:], "cboe_iv30")
                cur_cboe = d.get("cboe_iv30")
                confirmed = False
                if prev_cboe and cur_cboe and prev_cboe[0] > 0:
                    move = cur_cboe / prev_cboe[0]
                    confirmed = move > CBOE_MOVE_FACTOR or move < 1 / CBOE_MOVE_FACTOR
                if not confirmed:
                    problems.append(
                        f"{r.symbol}: iv30_atm {cur:.4f} vs {JUMP_LOOKBACK}-day median {med:.4f} "
                        f"jumped >{JUMP_FACTOR}x without CBOE iv30 confirming (likely parser regression)"
                    )
    return problems


def validate_file(path: Path, columns: list[str], key_cols: list[str]) -> list[str]:
    """Header matches, keys unique, rows sorted by key."""
    problems: list[str] = []
    try:
        rows = read_rows(path, columns)
    except Exception as exc:  # SchemaError or csv error
        return [f"{path}: {exc}"]
    keys = [tuple(r[k] for k in key_cols) for r in rows]
    if len(set(keys)) != len(keys):
        dupes = sorted({k for k in keys if keys.count(k) > 1})[:5]
        problems.append(f"{path}: duplicate keys {dupes}")
    if keys != sorted(keys):
        problems.append(f"{path}: rows not sorted by {key_cols}")
    return problems


# --- surveillance tier -------------------------------------------------------
# These rows carry only CBOE's own iv30, so the checks are simpler: a sane spot and
# an IV inside the same bounds the active tier uses.

MIN_SURVEILLANCE_RATIO = 0.50


@dataclass(slots=True)
class SurveillanceResult:
    symbol: str
    ok: bool
    error: str | None
    row: dict | None
    lag_days: int = 0  # >0 when the row is for an earlier session than the one being collected


def classify_surveillance(symbol: str, row: dict) -> SurveillanceResult:
    spot, iv = row.get("spot"), row.get("cboe_iv30")
    if not spot or spot <= 0:
        return SurveillanceResult(symbol, False, "spot<=0", None)
    if iv is None:
        return SurveillanceResult(symbol, False, "cboe_iv30 missing", None)
    if not (IV_MIN < iv < IV_MAX):
        return SurveillanceResult(symbol, False, f"cboe_iv30 out of range ({iv:.4f})", None)
    return SurveillanceResult(symbol, True, None, row)


def gate_surveillance(results: list[SurveillanceResult], min_ratio: float = MIN_SURVEILLANCE_RATIO) -> list[str]:
    """Surveillance is supplementary, so the bar is low. It only trips when something
    structural has broken, such as the Range/tail parsing, rather than on a normal bad day."""
    if not results:
        return []
    ok = sum(1 for r in results if r.ok)
    ratio = ok / len(results)
    if ratio < min_ratio:
        sample = ", ".join(f"{r.symbol}({r.error})" for r in results if not r.ok)[:300]
        return [f"surveillance success ratio {ok}/{len(results)}={ratio:.2f} < {min_ratio}: {sample}"]
    return []
