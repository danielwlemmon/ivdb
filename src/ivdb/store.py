"""CSV storage: one append-only file per symbol, idempotent per trade date, atomic writes."""

from __future__ import annotations

import csv
import os
from datetime import date, datetime
from pathlib import Path

DAILY_COLUMNS = [
    "date", "spot", "last", "close", "prev_close", "volume",
    "cboe_iv30", "iv30_atm", "iv60_atm", "iv90_atm",
    "put25_iv_30", "call25_iv_30", "skew_25d_30",
    "front_expiry", "front_dte", "front_atm_iv",
    "n_expirations", "n_contracts", "total_oi", "source_ts",
]
DAILY_KEY = ["date"]

TERM_COLUMNS = [
    "date", "expiry", "dte", "atm_strike", "atm_iv", "atm_n",
    "put25_iv", "put25_strike", "call25_iv", "call25_strike", "oi", "n_usable",
]
TERM_KEY = ["date", "expiry"]

SURVEILLANCE_COLUMNS = ["date", "spot", "close", "prev_close", "volume", "cboe_iv30"]
SURVEILLANCE_KEY = ["date"]

RUNS_COLUMNS = [
    "run_ts_utc", "trade_date", "status", "attempted", "succeeded",
    "surv_attempted", "surv_succeeded", "duration_s", "cboe_ts", "note",
]


class SchemaError(Exception):
    """An existing file's header does not match the expected columns."""


def fmt(value) -> str:
    """Canonical cell text: blanks for missing, up to 6 decimals, no trailing zeros."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        s = f"{value:.6f}".rstrip("0").rstrip(".")
        return s if s not in ("", "-0") else "0"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def format_row(row: dict, columns: list[str]) -> dict[str, str]:
    return {c: fmt(row.get(c)) for c in columns}


def daily_path(data_dir: Path, symbol: str) -> Path:
    return data_dir / "daily" / f"{symbol}.csv"


def term_path(data_dir: Path, symbol: str) -> Path:
    return data_dir / "term" / f"{symbol}.csv"


def surveillance_path(data_dir: Path, symbol: str) -> Path:
    return data_dir / "surveillance" / f"{symbol}.csv"


def runs_path(data_dir: Path) -> Path:
    return data_dir / "runs.csv"


def read_rows(path: Path, columns: list[str] | None = None) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        if columns is not None and list(header) != list(columns):
            raise SchemaError(f"{path}: header {header} != expected {columns}")
        return [dict(r) for r in reader]


def write_rows(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    """Atomic replace: write to a sibling temp file, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def upsert_rows(path: Path, columns: list[str], key_cols: list[str], rows: list[dict]) -> None:
    """Replace any existing rows with the same key, append the new ones, keep the file sorted."""
    incoming = [format_row(r, columns) for r in rows]
    keys = {tuple(r[k] for k in key_cols) for r in incoming}
    existing = read_rows(path, columns)
    kept = [r for r in existing if tuple(r[k] for k in key_cols) not in keys]
    merged = kept + incoming
    merged.sort(key=lambda r: tuple(r[k] for k in key_cols))
    write_rows(path, columns, merged)


def append_run(path: Path, row: dict) -> None:
    exists = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=RUNS_COLUMNS, lineterminator="\n")
        if not exists:
            writer.writeheader()
        writer.writerow(format_row(row, RUNS_COLUMNS))


def completed_dates(path: Path) -> set[str]:
    """Trade dates that already have a successful run recorded."""
    return {r["trade_date"] for r in read_rows(path) if r.get("status") == "ok"}
