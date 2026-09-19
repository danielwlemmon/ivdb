"""Command line: `ivdb collect`, `ivdb ivr`, `ivdb validate`.

Exit codes for `collect`: 0 ok / market closed / already collected, 1 quality gate failed,
2 dispatched too early (market not final), 3 configuration error.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .cboe import (
    DEFAULT_CONCURRENCY,
    DEFAULT_MIN_INTERVAL_S,
    FetchError,
    MarketStatus,
    Pacer,
    collect,
    collect_surveillance,
    make_client,
    make_fixture_fetcher,
    make_fixture_summary_fetcher,
    make_http_fetcher,
    make_summary_fetcher,
    probe_market,
)
from .ivr import format_report, ivr_report
from .quality import TickerResult, gate_job, gate_surveillance, validate_file
from .store import (
    DAILY_COLUMNS,
    DAILY_KEY,
    SURVEILLANCE_COLUMNS,
    SURVEILLANCE_KEY,
    TERM_COLUMNS,
    TERM_KEY,
    append_run,
    completed_dates,
    daily_path,
    read_rows,
    runs_path,
    surveillance_path,
    term_path,
    upsert_rows,
)
from .timeutil import now_et, parse_cboe_datetime
from .universe import ConfigError, load_tickers, split_tiers

log = logging.getLogger("ivdb")

EXIT_OK, EXIT_QUALITY, EXIT_TOO_EARLY, EXIT_CONFIG = 0, 1, 2, 3


def _github_output(**kv: object) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for k, v in kv.items():
            fh.write(f"{k}={v}\n")


def _print_table(results: list[TickerResult]) -> None:
    print(f"{'symbol':<8}{'status':<7}{'spot':>10}{'iv30_atm':>10}{'cboe_iv30':>10}{'iv60':>8}{'skew30':>8}{'nexp':>5}  note")
    for r in sorted(results, key=lambda r: r.symbol):
        if r.summary is None:
            print(f"{r.symbol:<8}{'FAIL':<7}{'':>10}{'':>10}{'':>10}{'':>8}{'':>8}{'':>5}  {r.error}")
            continue
        d = r.summary.daily

        def f(v, spec=".4f"):
            return format(v, spec) if v is not None else "-"

        note = r.error or (f"atm blank: {r.atm_note}" if r.atm_note else "")
        if d["iv30_atm"] and d["cboe_iv30"]:
            rel = d["iv30_atm"] / d["cboe_iv30"] - 1
            if abs(rel) > 0.30:
                note = (note + " " if note else "") + f"differs from CBOE iv30 by {rel:+.0%}"
        print(
            f"{r.symbol:<8}{('ok' if r.ok else 'FAIL'):<7}{f(d['spot'], '.2f'):>10}{f(d['iv30_atm']):>10}"
            f"{f(d['cboe_iv30']):>10}{f(d['iv60_atm']):>8}{f(d['skew_25d_30']):>8}{d['n_expirations']:>5}  {note}"
        )


MAX_FAILURE_GROUPS = 15


def _print_surveillance_failures(surv: list) -> None:
    """Group surveillance failures by exact reason so a bad ticker list is easy to prune.

    The stale-chain date is deliberately kept in the key. CBOE serves files it has stopped
    updating -- some are one session behind, others are months or years behind -- and the
    date is the only way to tell a symbol that merely lags from one that is dead.
    """
    groups: dict[str, list[str]] = {}
    for r in surv:
        if not r.ok:
            groups.setdefault(r.error or "unknown", []).append(r.symbol)
    ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    for reason, syms in ranked[:MAX_FAILURE_GROUPS]:
        print(f"\nsurveillance FAILED ({len(syms)}) {reason}:")
        print("  " + " ".join(sorted(syms)))
    if len(ranked) > MAX_FAILURE_GROUPS:
        rest = ranked[MAX_FAILURE_GROUPS:]
        print(f"\n... and {sum(len(v) for _, v in rest)} more across {len(rest)} other reasons")


async def _run_collect(args: argparse.Namespace) -> int:
    t0 = time.monotonic()
    data_dir = Path(args.data_dir)
    # The run log can live apart from the data (public code repo vs private data repo):
    # it holds no market data, and committing it daily keeps a public repo's schedule alive.
    runs = Path(args.runs_path) if getattr(args, "runs_path", None) else runs_path(data_dir)
    only = [s for s in (args.symbols or "").split(",") if s.strip()] or None
    try:
        tickers = load_tickers(Path(args.tickers), only)
    except ConfigError as exc:
        log.error("config error: %s", exc)
        return EXIT_CONFIG
    if not tickers:
        log.error("no active tickers")
        return EXIT_CONFIG

    now = parse_cboe_datetime(args.now) if args.now else now_et()
    active, surveil = split_tiers(tickers)
    client = None
    if args.fixture_dir:
        fetch = make_fixture_fetcher(Path(args.fixture_dir))
        fetch_summary = make_fixture_summary_fetcher(Path(args.fixture_dir))
    else:
        client = make_client()
        pacer = Pacer(args.min_interval)
        fetch = make_http_fetcher(client, min_interval_s=args.min_interval, pacer=pacer)
        fetch_summary = make_summary_fetcher(client, min_interval_s=args.min_interval, pacer=pacer)

    try:
        try:
            probe = await probe_market(fetch, args.probe_symbol, now)
        except (FetchError, KeyError, ValueError) as exc:
            log.error("market probe via %s failed: %s", args.probe_symbol, exc)
            _github_output(market_status="fail")
            return EXIT_QUALITY
        log.info(
            "probe %s: last trade %s, now %s -> %s", args.probe_symbol, probe.last_trade, now.strftime("%Y-%m-%d %H:%M"), probe.status.value
        )
        trade_date = probe.trade_date

        if probe.status is MarketStatus.TOO_EARLY and args.intraday:
            log.info("intraday snapshot accepted for %s (last trade %s)", trade_date, probe.last_trade.time())
        elif probe.status is MarketStatus.TOO_EARLY and not args.force:
            log.error("market not final for %s (last trade %s). Re-run after the close or use --force.", trade_date, probe.last_trade.time())
            _github_output(market_status="too_early", trade_date=trade_date)
            return EXIT_TOO_EARLY
        if probe.status is MarketStatus.CLOSED and not args.force:
            log.info("market closed today (last session %s); nothing to do", trade_date)
            if not args.dry_run:
                append_run(runs, _run_row(t0, trade_date, "closed", 0, 0, probe.cboe_ts, "holiday/weekend"))
            _github_output(market_status="closed", trade_date=trade_date, attempted=0, succeeded=0)
            return EXIT_OK
        if not args.force and not args.dry_run and trade_date.isoformat() in completed_dates(runs):
            log.info("%s already collected (see %s); use --force to re-collect", trade_date, runs)
            _github_output(market_status="skipped", trade_date=trade_date, attempted=0, succeeded=0)
            return EXIT_OK

        log.info(
            "collecting %d active + %d surveillance tickers for %s (concurrency %d, min interval %.1fs)",
            len(active), len(surveil), trade_date, args.concurrency, args.min_interval,
        )
        results = await collect(active, trade_date, fetch, concurrency=args.concurrency)
        surv: list = []
        if surveil and not args.no_surveillance:
            surv = await collect_surveillance(surveil, trade_date, fetch_summary, concurrency=args.concurrency)
    finally:
        if client is not None:
            await client.aclose()

    ok = [r for r in results if r.ok]
    surv_ok = [r for r in surv if r.ok]
    _print_table(results)
    surv_lagged = [r for r in surv_ok if r.lag_days]
    if surv:
        log.info("surveillance: %d/%d ok (%d lagged a session or more)", len(surv_ok), len(surv), len(surv_lagged))
        _print_surveillance_failures(surv)

    problems = gate_job(results, lambda sym: read_rows(daily_path(data_dir, sym), DAILY_COLUMNS), min_ratio=args.min_success)
    # Surveillance is supplementary: a bad surveillance night must never cost the active
    # tier its rows, since CBOE only serves today's snapshot and the day cannot be re-fetched.
    surv_problems = gate_surveillance(surv)
    for p in surv_problems:
        log.error("QUALITY (surveillance, non-fatal): %s", p)
    if problems:
        for p in problems:
            log.error("QUALITY: %s", p)
        if not args.dry_run:
            append_run(runs, _run_row(t0, trade_date, "fail", len(results), len(ok), probe.cboe_ts, "; ".join(problems)[:500], len(surv), len(surv_ok)))
        _github_output(market_status="fail", trade_date=trade_date, attempted=len(results), succeeded=len(ok))
        return EXIT_QUALITY

    if args.dry_run:
        n_blank = sum(1 for r in ok if not r.atm_ok)
        log.info(
            "dry run: active %d/%d ok (%d ATM blank), surveillance %d/%d ok, in %.1fs, nothing written",
            len(ok), len(results), n_blank, len(surv_ok), len(surv), time.monotonic() - t0,
        )
        return EXIT_OK

    for r in ok:
        upsert_rows(daily_path(data_dir, r.symbol), DAILY_COLUMNS, DAILY_KEY, [r.summary.daily])
        upsert_rows(term_path(data_dir, r.symbol), TERM_COLUMNS, TERM_KEY, r.summary.term)
    for r in surv_ok:
        upsert_rows(surveillance_path(data_dir, r.symbol), SURVEILLANCE_COLUMNS, SURVEILLANCE_KEY, [r.row])

    post = []
    for r in ok:
        post += validate_file(daily_path(data_dir, r.symbol), DAILY_COLUMNS, DAILY_KEY)
        post += validate_file(term_path(data_dir, r.symbol), TERM_COLUMNS, TERM_KEY)
    if post:
        for p in post:
            log.error("POST-WRITE: %s", p)
        append_run(runs, _run_row(t0, trade_date, "fail", len(results), len(ok), probe.cboe_ts, "post-write validation: " + "; ".join(post)[:400]))
        _github_output(market_status="fail", trade_date=trade_date, attempted=len(results), succeeded=len(ok))
        return EXIT_QUALITY

    failed = [f"{r.symbol}({r.error})" for r in results if not r.ok]
    blank = [r.symbol for r in ok if not r.atm_ok]
    notes = []
    if failed:
        notes.append("failed: " + ", ".join(failed))
    if blank:
        notes.append("atm blank: " + ", ".join(blank))
    surv_failed = [r.symbol for r in surv if not r.ok]
    if surv_problems:
        notes.append("surveillance gate: " + "; ".join(surv_problems)[:200])
    if surv_lagged:
        notes.append(f"surveillance lagged ({len(surv_lagged)})")
    if surv_failed:
        notes.append(f"surveillance failed ({len(surv_failed)}): " + ", ".join(surv_failed[:40]))
    append_run(
        runs,
        _run_row(t0, trade_date, "ok", len(results), len(ok), probe.cboe_ts, "; ".join(notes), len(surv), len(surv_ok)),
    )
    log.info(
        "wrote active %d/%d (%d ATM blank) + surveillance %d/%d for %s in %.1fs",
        len(ok), len(results), len(blank), len(surv_ok), len(surv), trade_date, time.monotonic() - t0,
    )
    _github_output(
        market_status="ok", trade_date=trade_date, attempted=len(results), succeeded=len(ok),
        surv_attempted=len(surv), surv_succeeded=len(surv_ok),
    )
    return EXIT_OK


def _run_row(t0: float, trade_date, status: str, attempted: int, succeeded: int, cboe_ts: str, note: str,
             surv_attempted: int = 0, surv_succeeded: int = 0) -> dict:
    return {
        "run_ts_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "trade_date": trade_date,
        "status": status,
        "attempted": attempted,
        "succeeded": succeeded,
        "surv_attempted": surv_attempted,
        "surv_succeeded": surv_succeeded,
        "duration_s": round(time.monotonic() - t0, 1),
        "cboe_ts": cboe_ts,
        "note": note,
    }


def cmd_collect(args: argparse.Namespace) -> int:
    return asyncio.run(_run_collect(args))


def cmd_ivr(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = sorted({p.stem for p in (data_dir / "daily").glob("*.csv")}
                         | {p.stem for p in (data_dir / "surveillance").glob("*.csv")})
    report = ivr_report(data_dir, symbols, metric=args.metric, window=args.window, min_rows=args.min_rows)
    if args.csv:
        writer = csv.DictWriter(sys.stdout, fieldnames=["symbol", "source", "date", "n", "current", "low", "high", "ivr", "iv_pct", "confidence"], lineterminator="\n")
        writer.writeheader()
        for r in report:
            writer.writerow({k: ("" if r.get(k) is None else (round(r[k], 4) if isinstance(r[k], float) else r[k])) for k in writer.fieldnames})
    else:
        print(format_report(report, min_rows=args.min_rows))
    return EXIT_OK


def cmd_validate(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    problems: list[str] = []
    for p in sorted((data_dir / "daily").glob("*.csv")):
        problems += validate_file(p, DAILY_COLUMNS, DAILY_KEY)
    for p in sorted((data_dir / "term").glob("*.csv")):
        problems += validate_file(p, TERM_COLUMNS, TERM_KEY)
    for p in sorted((data_dir / "surveillance").glob("*.csv")):
        problems += validate_file(p, SURVEILLANCE_COLUMNS, SURVEILLANCE_KEY)
    for p in problems:
        print(p, file=sys.stderr)
    n_daily = len(list((data_dir / "daily").glob("*.csv")))
    n_surv = len(list((data_dir / "surveillance").glob("*.csv")))
    print(f"validated {n_daily} daily + {n_surv} surveillance files: {'OK' if not problems else str(len(problems)) + ' problem(s)'}")
    return EXIT_OK if not problems else EXIT_QUALITY


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ivdb", description="Daily implied-volatility collector (CBOE delayed quotes).")
    p.add_argument("--version", action="version", version=f"ivdb {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="fetch chains, compute metrics, gate, write CSVs")
    c.add_argument("--symbols", help="comma-separated subset (default: all active in tickers.csv)")
    c.add_argument("--tickers", default="tickers.csv")
    c.add_argument("--data-dir", default="data")
    c.add_argument("--runs-path", help="run log location (default: <data-dir>/runs.csv); lets the log live outside the data repo")
    c.add_argument("--dry-run", action="store_true", help="fetch, compute and gate but write nothing")
    c.add_argument("--force", action="store_true", help="collect even if closed/too-early/already collected")
    c.add_argument(
        "--intraday",
        action="store_true",
        default=os.environ.get("IVDB_INTRADAY", "") not in ("", "0", "false"),
        help="accept a same-day snapshot taken while the market is open (for a pre-close schedule)",
    )
    c.add_argument("--concurrency", type=int, default=int(os.environ.get("IVDB_CONCURRENCY", DEFAULT_CONCURRENCY)))
    c.add_argument(
        "--min-interval",
        type=float,
        default=float(os.environ.get("IVDB_MIN_INTERVAL", DEFAULT_MIN_INTERVAL_S)),
        help="minimum seconds between request starts (CBOE rate-limits bursts)",
    )
    c.add_argument("--min-success", type=float, default=0.90, help="job fails below this success ratio")
    c.add_argument("--no-surveillance", action="store_true", help="skip the surveillance tier this run")
    c.add_argument("--probe-symbol", default="_SPX", help="CBOE symbol used to detect the trading session")
    c.add_argument("--now", help="override the ET clock, e.g. 2026-09-04T18:17:00 (testing)")
    c.add_argument("--fixture-dir", help="read {SYMBOL}.json from this directory instead of the network (testing)")
    c.set_defaults(func=cmd_collect)

    r = sub.add_parser("ivr", help="IV Rank / IV Percentile from accumulated history")
    r.add_argument("--symbols")
    r.add_argument("--data-dir", default="data")
    r.add_argument("--metric", default="iv30_atm", choices=["iv30_atm", "iv60_atm", "iv90_atm", "cboe_iv30", "front_atm_iv"])
    r.add_argument("--window", type=int, default=252)
    r.add_argument("--min-rows", type=int, default=120, help="refuse to rank below this many rows (default 120)")
    r.add_argument("--csv", action="store_true")
    r.set_defaults(func=cmd_ivr)

    v = sub.add_parser("validate", help="check every data file for schema, duplicates and ordering")
    v.add_argument("--data-dir", default="data")
    v.set_defaults(func=cmd_validate)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return int(args.func(args))
