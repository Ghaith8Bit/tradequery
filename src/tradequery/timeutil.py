"""Broker server time, UTC and local time.

MT5 reports and the MT5 Python library give timestamps in broker server time.
Most forex brokers run that clock as New York time + 7 hours ("New York close"):
UTC+2 in winter and UTC+3 in summer, switching on the US daylight-saving dates.
Everything else in TradeQuery works in UTC, so every timestamp goes through
this module first.

The New York close rule is an assumption. Verify it for each broker with one
known event, such as a past NFP release (see the build plan, News layer).
Brokers with a fixed offset are supported with ``kind="fixed"``.

A naive server time can be ambiguous (the repeated hour when US clocks go
back) or nonexistent (the skipped hour when they go forward). Both fall on a
Sunday while the forex market is closed, so trades rarely land in them, but the
functions here never guess silently: ``strict=True`` raises, otherwise ``fold``
picks the first (0) or second (1) occurrence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")
NY_CLOSE_OFFSET = timedelta(hours=7)
_EPOCH = datetime(1970, 1, 1)


class TimeResolutionError(ValueError):
    """A server time that is ambiguous or does not exist."""


def _ny_state(ny_naive: datetime) -> str:
    """Return 'normal', 'ambiguous' or 'nonexistent' for a naive New York time."""
    first = ny_naive.replace(tzinfo=NEW_YORK, fold=0).utcoffset()
    second = ny_naive.replace(tzinfo=NEW_YORK, fold=1).utcoffset()
    if first == second:
        return "normal"
    return "ambiguous" if first > second else "nonexistent"


@dataclass(frozen=True)
class ServerClock:
    """How one broker's server clock relates to UTC."""

    kind: str = "ny_close"  # "ny_close" (New York + 7h) or "fixed"
    fixed_offset_hours: float = 2.0  # used only when kind == "fixed"

    def __post_init__(self) -> None:
        if self.kind not in ("ny_close", "fixed"):
            raise ValueError(f"unknown server clock kind: {self.kind!r}")

    def state(self, server_time: datetime) -> str:
        """'normal', 'ambiguous' or 'nonexistent' for a naive server time."""
        if self.kind == "fixed":
            return "normal"
        return _ny_state(server_time - NY_CLOSE_OFFSET)

    def to_utc(self, server_time: datetime, *, strict: bool = False, fold: int = 0) -> datetime:
        """Convert a naive server time to an aware UTC datetime."""
        if server_time.tzinfo is not None:
            raise ValueError("expected a naive server time")
        if self.kind == "fixed":
            return (server_time - timedelta(hours=self.fixed_offset_hours)).replace(tzinfo=UTC)
        ny_naive = server_time - NY_CLOSE_OFFSET
        state = _ny_state(ny_naive)
        if state != "normal" and strict:
            raise TimeResolutionError(f"server time {server_time.isoformat()} is {state}")
        return ny_naive.replace(tzinfo=NEW_YORK, fold=fold).astimezone(UTC)

    def from_utc(self, utc_time: datetime) -> datetime:
        """Convert an aware UTC (or any aware) datetime to a naive server time."""
        if utc_time.tzinfo is None:
            raise ValueError("expected an aware datetime")
        if self.kind == "fixed":
            return utc_time.astimezone(UTC).replace(tzinfo=None) + timedelta(hours=self.fixed_offset_hours)
        return utc_time.astimezone(NEW_YORK).replace(tzinfo=None) + NY_CLOSE_OFFSET

    def from_mt5_epoch(self, epoch_seconds: float, *, strict: bool = False, fold: int = 0) -> datetime:
        """Convert a timestamp from the MT5 Python library to aware UTC.

        The library returns server time encoded as if it were UTC epoch seconds,
        so the epoch value is first read as a naive server time.
        """
        naive = _EPOCH + timedelta(seconds=epoch_seconds)
        return self.to_utc(naive, strict=strict, fold=fold)


def to_local(utc_time: datetime, tz_name: str) -> datetime:
    """Convert an aware datetime to an aware local time in the named zone."""
    if utc_time.tzinfo is None:
        raise ValueError("expected an aware datetime")
    return utc_time.astimezone(ZoneInfo(tz_name))
