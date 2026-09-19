"""IV Rank and IV Percentile computed on read from data/daily/{SYMBOL}.csv."""

from __future__ import annotations

from pathlib import Path

from .store import DAILY_COLUMNS, SURVEILLANCE_COLUMNS, daily_path, read_rows, surveillance_path

DEFAULT_WINDOW = 252
# Accuracy of a short window against the mature 252-row answer, measured over ~16,800
# observations of real IV history (16 Cboe volatility indices, 2026-09-08):
#
#   rows   mean abs err   agrees on IVR>50
#     60          14.3             81.8%
#    120           8.8             87.7%
#    180           4.7             92.1%
#
# 60 rows is a rough gauge at best: one reading in five lands on the wrong side of the
# call the number exists to make. 120 is the floor for a directional read; below 180 the
# answer is still labelled provisional.
MIN_ROWS = 120
FIRM_ROWS = 180


def rank_and_percentile(values: list[float]) -> tuple[float | None, float | None]:
    """values: chronological, last element is current.

    IVR  = (current - min) / (max - min) * 100 over the whole window (None if flat).
    IV%  = share of PRIOR observations strictly below current, * 100.
    """
    if len(values) < 2:
        return None, None
    cur = values[-1]
    lo, hi = min(values), max(values)
    ivr = (cur - lo) / (hi - lo) * 100.0 if hi > lo else None
    prior = values[:-1]
    pct = sum(1 for v in prior if v < cur) / len(prior) * 100.0
    return ivr, pct


SURVEILLANCE_METRIC = "cboe_iv30"


def symbol_series(data_dir: Path, symbol: str, metric: str) -> tuple[list[tuple[str, float]], str]:
    """Values for `metric`, newest last, plus which tier they came from.

    Active tickers use data/daily. A surveillance-only ticker has just CBOE's own iv30,
    so any request falls back to that column; it tracks the computed ATM IV closely
    enough (median 0.7% on 2026-09-08) to rank against.
    """
    daily = read_rows(daily_path(data_dir, symbol), DAILY_COLUMNS)
    if daily and metric in DAILY_COLUMNS:
        out = _extract(daily, metric)
        if out:
            return out, "daily"
    surv = read_rows(surveillance_path(data_dir, symbol), SURVEILLANCE_COLUMNS)
    if surv:
        col = metric if metric in SURVEILLANCE_COLUMNS else SURVEILLANCE_METRIC
        return _extract(surv, col), f"surveillance:{col}"
    return [], "daily"


def _extract(rows: list[dict[str, str]], metric: str) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for r in rows:
        try:
            out.append((r["date"], float(r[metric])))
        except (KeyError, ValueError):
            continue
    return out


def ivr_report(
    data_dir: Path,
    symbols: list[str],
    metric: str = "iv30_atm",
    window: int = DEFAULT_WINDOW,
    min_rows: int = MIN_ROWS,
) -> list[dict]:
    report: list[dict] = []
    for sym in symbols:
        full, source = symbol_series(data_dir, sym, metric)
        series = full[-window:]
        n = len(series)
        row = {"symbol": sym, "source": source, "n": n,
               "date": series[-1][0] if series else None, "current": series[-1][1] if series else None}
        if n >= min_rows:
            ivr, pct = rank_and_percentile([v for _, v in series])
            row.update(ivr=ivr, iv_pct=pct, low=min(v for _, v in series), high=max(v for _, v in series),
                       confidence="firm" if n >= FIRM_ROWS else "provisional")
        else:
            row.update(ivr=None, iv_pct=None, low=None, high=None,
                       confidence="insufficient")
        report.append(row)
    return report


def format_report(report: list[dict], min_rows: int = MIN_ROWS) -> str:
    def f(v, spec):
        return format(v, spec) if v is not None else "-"

    lines = [f"{'symbol':<8}{'date':<12}{'n':>5}{'current':>9}{'low':>8}{'high':>8}{'IVR':>7}{'IV%':>7}  {'confidence':<13}source"]
    for r in report:
        if r["n"] < min_rows:
            tail = f"{'n/a (n=' + str(r['n']) + ')':>30}"
            lines.append(f"{r['symbol']:<8}{str(r['date'] or '-'):<12}{r['n']:>5}{f(r['current'], '.4f'):>9}{tail}"
                         f"  {'insufficient':<13}{r.get('source','')}")
            continue
        lines.append(
            f"{r['symbol']:<8}{r['date']:<12}{r['n']:>5}{f(r['current'], '.4f'):>9}"
            f"{f(r['low'], '.4f'):>8}{f(r['high'], '.4f'):>8}{f(r['ivr'], '.1f'):>7}{f(r['iv_pct'], '.1f'):>7}"
            f"  {r.get('confidence',''):<13}{r.get('source','')}"
        )
    if any(r.get("confidence") == "provisional" for r in report):
        lines.append(f"\nprovisional = fewer than {FIRM_ROWS} rows; typical error is still several IVR points.")
    return "\n".join(lines)
