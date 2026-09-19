"""OCC option symbol parsing, e.g. 'SPY261130C00689000' -> SPY, 2026-11-30, C, 689.0."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

_OCC_RE = re.compile(r"^(?P<root>[A-Z0-9 .]+?)(?P<ymd>\d{6})(?P<right>[CP])(?P<strike>\d{8})$")


@dataclass(frozen=True, slots=True)
class OccContract:
    root: str
    expiry: date
    right: str  # "C" or "P"
    strike: float


def parse_occ(symbol: str) -> OccContract | None:
    """Return the parsed contract, or None if the string is not a valid OCC symbol."""
    m = _OCC_RE.match(symbol.strip())
    if not m:
        return None
    ymd = m.group("ymd")
    try:
        expiry = date(2000 + int(ymd[0:2]), int(ymd[2:4]), int(ymd[4:6]))
    except ValueError:
        return None
    return OccContract(
        root=m.group("root").strip(),
        expiry=expiry,
        right=m.group("right"),
        strike=int(m.group("strike")) / 1000.0,
    )
