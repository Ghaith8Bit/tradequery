"""News layer: scheduled economic events joined to trades. Overlap, never cause.

Source: mql5/ExportCalendar.mq5 writes the MT5 economic calendar to a
tab-separated file (``make calendar``). Calendar scheduled events only: surprise
headlines are not covered.

Time alignment: the calendar's clock is not always the trade server's clock
(on one live broker the calendar ran at UTC+2 while the server ran at UTC+3), so
the importer calibrates it on US Nonfarm Payrolls, which is always released at
08:30 New York time: of the candidate clocks it keeps the one that puts every
NFP in the file at exactly 08:30 New York. Without an NFP in the file it falls
back to the configured clock and says so.

Overlap rule (settings.toml [news]): only events of at least ``min_importance``
count, for the currencies of the trade's symbol. A trade overlaps an event when
its open-to-close span meets the window from ``minutes_before`` before to
``minutes_after`` after the event. Phase: before (closed before the event),
during (open across it) or after (opened after it).
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .detectors import Tag, net_of
from .models import Trade
from .settings import NewsSettings
from .timeutil import ServerClock

NEW_YORK = ZoneInfo("America/New_York")
IMPORTANCE = {"none": 0, "low": 1, "moderate": 2, "high": 3}
NFP = re.compile(r"^nonfarm payrolls$", re.IGNORECASE)
RULE = "news_overlap"


class CalendarError(ValueError):
    """The calendar file is not in the format ExportCalendar.mq5 writes."""


@dataclass(frozen=True)
class NewsEvent:
    value_id: int  # one release of an event (the calendar's value id)
    event_id: int
    time_utc: datetime
    currency: str
    importance: str  # none, low, moderate, high
    name: str
    actual: float | None = None
    forecast: float | None = None
    previous: float | None = None


@dataclass(frozen=True)
class TradeNews:
    position_id: int
    value_id: int
    minutes_to_event: float  # open time minus event time; negative = opened before the event
    phase: str  # before, during, after


# ---------------------------------------------------------------- reading and calibrating


def _num(text: str) -> float | None:
    return float(text) if text.strip() else None


def read_calendar(path: Path | str) -> list[dict]:
    """Rows of the export with naive calendar-clock times (not yet UTC)."""
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        expected = {"time_server", "currency", "importance", "event_id", "value_id", "name"}
        if not reader.fieldnames or not expected <= set(reader.fieldnames):
            raise CalendarError(f"{path} is not a calendar export (columns: {reader.fieldnames})")
        rows = []
        for r in reader:
            try:
                naive = datetime.strptime(r["time_server"], "%Y.%m.%d %H:%M:%S")
            except ValueError:
                raise CalendarError(f"bad time {r['time_server']!r} in {path}") from None
            rows.append(dict(
                naive=naive, currency=r["currency"].strip().upper(), importance=r["importance"].strip().lower(),
                event_id=int(r["event_id"]), value_id=int(r["value_id"]), name=r["name"].strip(),
                actual=_num(r.get("actual", "")), forecast=_num(r.get("forecast", "")),
                previous=_num(r.get("previous", "")),
            ))
    return rows


def _nfp_utc(day) -> datetime:
    return datetime.combine(day, time(8, 30), NEW_YORK).astimezone(timezone.utc)


def calibrate_clock(rows: list[dict], fallback: ServerClock) -> tuple[ServerClock, str]:
    """The calendar's clock, found from the US Nonfarm Payrolls releases in the file."""
    anchors = [r["naive"] for r in rows if r["currency"] == "USD" and NFP.match(r["name"])]
    if not anchors:
        return fallback, "no US Nonfarm Payrolls in the file to calibrate on; used the configured clock"
    candidates = [ServerClock("ny_close")] + [ServerClock("fixed", h / 2) for h in range(-24, 29)]
    for clock in candidates:
        if all(clock.to_utc(a) == _nfp_utc(a.date()) for a in anchors):
            label = "New York close (UTC+2/+3)" if clock.kind == "ny_close" else f"UTC{clock.fixed_offset_hours:+g}"
            return clock, f"calibrated on {len(anchors)} Nonfarm Payrolls release(s): calendar clock is {label}"
    return fallback, (f"the {len(anchors)} Nonfarm Payrolls time(s) fit no single clock; used the configured clock. "
                      "Check the export.")


def parse_calendar(path: Path | str, settings: NewsSettings, server_clock: ServerClock) -> tuple[list[NewsEvent], str]:
    """Calendar events in UTC, with a sentence on how the clock was chosen."""
    rows = read_calendar(path)
    setting = settings.calendar_clock.strip().lower()
    if setting == "auto":
        clock, note = calibrate_clock(rows, server_clock)
    elif setting == "server":
        clock, note = server_clock, "calendar clock set to the trade server clock"
    else:
        clock, note = ServerClock("fixed", float(setting)), f"calendar clock set to UTC{float(setting):+g}"
    events = [
        NewsEvent(r["value_id"], r["event_id"], clock.to_utc(r["naive"]), r["currency"], r["importance"], r["name"],
                  r["actual"], r["forecast"], r["previous"])
        for r in rows
    ]
    events.sort(key=lambda e: (e.time_utc, e.value_id))
    return events, note


# ---------------------------------------------------------------- joining


def symbol_currencies(symbol: str, spec: dict | None, settings: NewsSettings) -> set[str]:
    """Currencies whose news moves a symbol: overrides, else base and profit currency, else the name."""
    overrides = dict(settings.symbol_currencies)
    for name in (symbol, re.sub(r"[^A-Z]", "", symbol.upper())[:6]):
        if name in overrides:
            return set(overrides[name])
    found = set()
    if spec:
        found |= {spec.get("base_currency") or "", spec.get("profit_currency") or ""}
    letters = re.sub(r"[^A-Z]", "", symbol.upper())
    if not found - {""} and len(letters) >= 6:
        found |= {letters[:3], letters[3:6]}
    return {c for c in found if c}


def join(trades: Iterable[Trade], events: list[NewsEvent], settings: NewsSettings,
         symbol_specs: dict[str, dict] | None = None) -> list[TradeNews]:
    """Every (trade, event) overlap under the configured rule."""
    floor = IMPORTANCE.get(settings.min_importance, 3)
    chosen = [e for e in events if IMPORTANCE.get(e.importance, 0) >= floor]
    before, after = timedelta(minutes=settings.minutes_before), timedelta(minutes=settings.minutes_after)
    specs = symbol_specs or {}
    currencies: dict[str, set[str]] = {}
    out = []
    for t in trades:
        if t.symbol not in currencies:
            currencies[t.symbol] = symbol_currencies(t.symbol, specs.get(t.symbol), settings)
        for e in chosen:
            if e.currency not in currencies[t.symbol]:
                continue
            if t.open_time_utc > e.time_utc + after or t.close_time_utc < e.time_utc - before:
                continue
            phase = "before" if t.close_time_utc < e.time_utc else "after" if t.open_time_utc > e.time_utc else "during"
            minutes = (t.open_time_utc - e.time_utc).total_seconds() / 60
            out.append(TradeNews(t.position_id, e.value_id, round(minutes, 1), phase))
    return out


def news_tags(links: list[TradeNews], events: list[NewsEvent]) -> list[Tag]:
    """One news_overlap tag per trade, naming the events it overlapped."""
    by_id = {e.value_id: e for e in events}
    per_trade: dict[int, list[TradeNews]] = {}
    for link in links:
        per_trade.setdefault(link.position_id, []).append(link)
    tags = []
    for pid, mine in per_trade.items():
        parts = []
        for link in sorted(mine, key=lambda l: by_id[l.value_id].time_utc):
            e = by_id[link.value_id]
            parts.append(f"{link.phase} {e.name} ({e.currency}, {e.importance}) at {e.time_utc:%Y-%m-%d %H:%M} UTC")
        tags.append(Tag(pid, RULE, "overlapped " + "; ".join(parts)))
    return tags


# ---------------------------------------------------------------- analysis


@dataclass(frozen=True)
class NewsSplit:
    trades: int
    wins: int
    net: float

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.trades if self.trades else None


def inside_outside(trades: list[Trade], links: list[TradeNews]) -> tuple[NewsSplit, NewsSplit]:
    """Results of trades that overlapped news versus those that did not."""
    hit = {l.position_id for l in links}
    inside = [t for t in trades if t.position_id in hit]
    outside = [t for t in trades if t.position_id not in hit]

    def split(rows):
        return NewsSplit(len(rows), sum(1 for t in rows if t.outcome == "win"), round(sum(net_of(t) for t in rows), 2))

    return split(inside), split(outside)


def news_loss_share(position_ids: Iterable[int], trades: dict[int, Trade], links: list[TradeNews]) -> tuple[float | None, set[int]]:
    """Loss from news-overlapping trades / total loss, over the trades of a drawdown episode.

    Also returns the value ids of the events they overlapped. Worded as
    "overlapped with", never "was caused by".
    """
    hit: dict[int, set[int]] = {}
    for l in links:
        hit.setdefault(l.position_id, set()).add(l.value_id)
    losses = [(pid, net_of(trades[pid])) for pid in position_ids if pid in trades and net_of(trades[pid]) < 0]
    total = sum(n for _, n in losses)
    if not total:
        return None, set()
    news = sum(n for pid, n in losses if pid in hit)
    events = set().union(*(hit[pid] for pid, _ in losses if pid in hit)) if any(pid in hit for pid, _ in losses) else set()
    return news / total + 0.0, events  # + 0.0 turns -0.0 into 0.0
