"""Wall-clock access.

Every timing decision in pagekeeper goes through a clock so that the
escalation engine can be driven deterministically from tests.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional

from .errors import UsageError

Clock = Callable[[], datetime]

#: Formats used for the status page and CLI output.
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_epoch(moment: datetime) -> int:
    """Seconds since the epoch. Naive input is read as UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp())


def from_epoch(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, timezone.utc)


def current(clock: Optional[Clock] = None) -> int:
    return to_epoch(clock() if clock is not None else utc_now())


def format_time(seconds: Optional[int]) -> str:
    if seconds is None:
        return "-"
    return f"{from_epoch(seconds).strftime(TIME_FORMAT)} UTC"


def parse_time(value: str) -> int:
    """Parse an ISO-8601 date or timestamp; naive values are UTC."""
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise UsageError(f"not a valid ISO-8601 time: {value!r}") from exc
    return to_epoch(moment)
