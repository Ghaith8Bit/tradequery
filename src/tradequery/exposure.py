"""Lots open over time, as events: one row each time a position opens or closes.

"Lots open at time T" and "peak lots open" become cheap lookups. Exposure is
built from closed positions; on a hedging account long and short positions are
counted separately (gross) and against each other (net). On a netting account
MT5 already merges positions per symbol, so each position here is the net one.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from .models import Trade

ALL = "ALL"
_EPS = 1e-9


@dataclass(frozen=True)
class ExposureEvent:
    time_utc: datetime
    symbol: str  # a symbol, or ALL for the whole account
    long_lots: float
    short_lots: float
    positions_open: int

    @property
    def net_lots(self) -> float:
        return round(self.long_lots - self.short_lots, 8)

    @property
    def gross_lots(self) -> float:
        return round(self.long_lots + self.short_lots, 8)


def exposure_events(trades: Iterable[Trade]) -> list[ExposureEvent]:
    """Exposure after every open and close, per symbol and for ALL, in time order.

    At the same instant a close is applied before an open, so a position closed
    and another opened in the same second are not counted as open together.
    """
    changes = []
    for t in trades:
        sign = 1 if t.side == "buy" else -1
        changes.append((t.open_time_utc, 1, t.symbol, sign * t.lots, 1))
        changes.append((t.close_time_utc, 0, t.symbol, -sign * t.lots, -1))
    changes.sort(key=lambda c: (c[0], c[1]))

    state: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0])  # long, short, count
    events = []
    for time_utc, _, symbol, signed_lots, count in changes:
        for key in (symbol, ALL):
            s = state[key]
            if (signed_lots > 0) == (count > 0):  # opening a buy or closing a buy
                s[0] = max(0.0, s[0] + abs(signed_lots) * count)
            else:
                s[1] = max(0.0, s[1] + abs(signed_lots) * count)
            s[2] += count
            events.append(ExposureEvent(time_utc, key, round(s[0], 8), round(s[1], 8), s[2]))
    return events


def _for(events: list[ExposureEvent], symbol: str) -> list[ExposureEvent]:
    return [e for e in events if e.symbol == symbol]


def lots_open_at(events: list[ExposureEvent], when: datetime, symbol: str = ALL) -> ExposureEvent | None:
    """Exposure in force at a moment (the last event at or before it), or None before the first."""
    rows = _for(events, symbol)
    i = bisect_right([e.time_utc for e in rows], when)
    return rows[i - 1] if i else None


def peak_lots_open(events: list[ExposureEvent], symbol: str = ALL) -> ExposureEvent | None:
    """The first moment gross lots open reached their maximum."""
    rows = _for(events, symbol)
    return max(rows, key=lambda e: (e.gross_lots, -e.time_utc.timestamp()), default=None)


def avg_lots_open(events: list[ExposureEvent], symbol: str = ALL) -> float | None:
    """Time-weighted average gross lots open, from the first open to the last close."""
    rows = _for(events, symbol)
    if len(rows) < 2:
        return None
    total = sum(a.gross_lots * (b.time_utc - a.time_utc).total_seconds() for a, b in zip(rows, rows[1:]))
    span = (rows[-1].time_utc - rows[0].time_utc).total_seconds()
    return total / span if span > _EPS else None
