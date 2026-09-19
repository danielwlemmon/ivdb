"""Time helpers. CBOE timestamps are naive, US/Eastern wall-clock strings."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def now_et() -> datetime:
    """Current wall-clock time in New York as a naive datetime (matches CBOE's format)."""
    return datetime.now(tz=ET).replace(tzinfo=None)


def parse_cboe_datetime(value: str) -> datetime:
    """Parse '2026-09-04T15:59:59' or '2026-09-05 06:00:28' (both ET-local, naive)."""
    return datetime.fromisoformat(value.strip().replace(" ", "T"))


def is_third_friday(d: date) -> bool:
    """Standard monthly option expiration day."""
    return d.weekday() == 4 and 15 <= d.day <= 21
