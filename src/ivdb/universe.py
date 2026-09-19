"""Ticker universe loaded from tickers.csv.

Two tiers. `active` tickers get the full option chain fetched and every metric
computed. `surveillance` tickers get only the ~900-byte tail of the chain file,
which carries CBOE's own iv30 plus spot and volume. Surveillance costs about
60 bytes of storage a day and exists so that a ticker which becomes interesting
later already has IV history banked, instead of starting a 6-month clock.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

KINDS = {"equity", "etf", "index"}
ACTIVE, SURVEILLANCE = "active", "surveillance"
TIERS = {ACTIVE, SURVEILLANCE}
REQUIRED_COLUMNS = {"symbol", "cboe_symbol", "kind", "active"}


class ConfigError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Ticker:
    symbol: str
    cboe_symbol: str
    kind: str
    active: bool
    tier: str = ACTIVE


def load_tickers(path: Path, only: list[str] | None = None) -> list[Ticker]:
    """Active tickers from the CSV, optionally restricted to `only` (by symbol, case-insensitive).

    The `tier` column is optional; a file without it is read as all-active, so older
    ticker files keep working. Symbols in `only` that are not in the file are fetched
    anyway using the symbol as the CBOE symbol, so ad-hoc `--symbols FOO` works.
    """
    if not path.exists():
        raise ConfigError(f"tickers file not found: {path}")
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fields = set(reader.fieldnames or [])
        if not REQUIRED_COLUMNS.issubset(fields):
            raise ConfigError(f"{path}: header must include {sorted(REQUIRED_COLUMNS)}")
        has_tier = "tier" in fields
        tickers: list[Ticker] = []
        seen: set[str] = set()
        for i, row in enumerate(reader, start=2):
            sym = (row["symbol"] or "").strip().upper()
            if not sym:
                continue
            if sym in seen:
                raise ConfigError(f"{path}:{i}: duplicate symbol {sym}")
            seen.add(sym)
            kind = (row["kind"] or "").strip().lower()
            if kind not in KINDS:
                raise ConfigError(f"{path}:{i}: kind must be one of {sorted(KINDS)}, got {kind!r}")
            tier = ((row.get("tier") or "").strip().lower() or ACTIVE) if has_tier else ACTIVE
            if tier not in TIERS:
                raise ConfigError(f"{path}:{i}: tier must be one of {sorted(TIERS)}, got {tier!r}")
            tickers.append(
                Ticker(
                    symbol=sym,
                    cboe_symbol=(row["cboe_symbol"] or sym).strip(),
                    kind=kind,
                    active=(row["active"] or "1").strip() not in ("0", "false", "no", ""),
                    tier=tier,
                )
            )
    if only:
        wanted = dict.fromkeys(s.strip().upper() for s in only if s.strip())  # de-duplicated, order kept
        by_sym = {t.symbol: t for t in tickers}
        return [by_sym.get(s) or Ticker(s, s, "equity", True, ACTIVE) for s in wanted]
    return [t for t in tickers if t.active]


def split_tiers(tickers: list[Ticker]) -> tuple[list[Ticker], list[Ticker]]:
    return [t for t in tickers if t.tier == ACTIVE], [t for t in tickers if t.tier == SURVEILLANCE]
