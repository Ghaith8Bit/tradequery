"""Detectors: small, tested rules that tag trades, each tag with a short evidence sentence.

One file per rule. Thresholds live in settings.toml [detectors] so they can be
tuned without code changes. Behavioural rules (revenge, overtrading) run only on
manual trades; risk rules (no stop loss, oversized) run on every trade.

A trade is automated (an EA) when its magic number is not 0, unless the magic
number is listed in ``manual_magics``.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from ..models import NON_BALANCE_FLOW_TYPES, CashFlow, Trade
from ..settings import DetectorSettings, Settings


@dataclass(frozen=True)
class Tag:
    position_id: int
    rule: str
    evidence: str


def is_automated(trade: Trade, config: DetectorSettings) -> bool:
    return trade.magic != 0 and trade.magic not in config.manual_magics


def net_of(trade: Trade) -> float:
    return trade.net_profit if trade.net_profit is not None else round(trade.profit + trade.commission + trade.swap, 2)


class BalanceTimeline:
    """The account balance at any moment, from trade closes (balance_after) and cash flows."""

    def __init__(self, trades: Iterable[Trade], cash_flows: Iterable[CashFlow] = ()):
        events = [(t.close_time_utc, 1, t.balance_after, None) for t in trades if t.balance_after is not None]
        events += [(f.time_utc, 0, None, f.amount) for f in cash_flows if f.type not in NON_BALANCE_FLOW_TYPES]
        events.sort(key=lambda e: (e[0], e[1]))
        ordered = sorted((t for t in trades if t.balance_after is not None), key=lambda t: t.close_time_utc)
        start = ordered[0].balance_after - net_of(ordered[0]) if ordered else 0.0
        flows_before_first = sum(e[3] for e in events if e[3] is not None and ordered and e[0] < ordered[0].close_time_utc)
        balance = start - flows_before_first
        self._times, self._values = [], []
        for when, _, anchor, amount in events:
            balance = anchor if anchor is not None else balance + amount
            self._times.append(when)
            self._values.append(balance)
        self._start = start - flows_before_first

    def at(self, when: datetime) -> float:
        """Balance in force at a moment (after everything up to and including it)."""
        i = bisect_right(self._times, when)
        return self._values[i - 1] if i else self._start


def run_all(trades: list[Trade], settings: Settings, cash_flows: Iterable[CashFlow] = ()) -> list[Tag]:
    """Every rule on these trades, tags sorted by position and rule."""
    from . import no_stop_loss, overtrading, oversized, revenge

    config = settings.detectors
    timeline = BalanceTimeline(trades, cash_flows)
    tags = []
    tags += revenge.detect(trades, config)
    tags += overtrading.detect(trades, config, settings.clock)
    tags += no_stop_loss.detect(trades)
    tags += oversized.detect(trades, config, timeline)
    return sorted(tags, key=lambda t: (t.position_id, t.rule))


RULES = ("revenge_trade", "overtrading", "no_stop_loss", "oversized")
