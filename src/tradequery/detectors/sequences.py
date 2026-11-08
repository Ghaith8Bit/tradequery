"""Martingale and grid sequences: one row per series, with its depth and lot multiplier.

martingale  On one symbol, after ``martingale_min_losses`` losing positions the
            lots keep growing: each position is at least ``martingale_lot_multiplier``
            times the previous one, and the previous one was losing when it was
            opened. "Losing" covers both styles of martingale: the previous position
            closed at a loss, or it is still open at a worse price than the new entry
            (an EA that keeps adding to a losing basket).
grid        Same-side positions on one symbol overlapping in time, with at least
            ``grid_min_open`` open at once, each entered further against the first.
martingale_grid
            Both: a basket that grows against the price with growing lots.

A sequence ends when the next position does not continue it (a winning close,
or lots back near the first lot), or when the basket has no open position left.
Every sequence reports its depth and max lot multiplier, because winning
sequences are where hidden tail risk sits.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Trade
from ..settings import DetectorSettings
from . import Tag, net_of

MARTINGALE, GRID, BOTH = "martingale", "grid", "martingale_grid"


@dataclass(frozen=True)
class Sequence:
    sequence_id: int
    type: str  # martingale, grid or martingale_grid
    symbol: str
    side: str  # buy, sell or mixed
    position_ids: tuple[int, ...]  # in open order
    first_lot: float
    max_lot: float
    max_open: int  # most of its positions open at the same time
    start_utc: object
    end_utc: object
    net: float

    @property
    def depth(self) -> int:
        return len(self.position_ids)

    @property
    def max_lot_multiplier(self) -> float:
        return self.max_lot / self.first_lot if self.first_lot else 0.0

    @property
    def is_win(self) -> bool:
        return self.net > 0


def _open_order(trades):
    return sorted(trades, key=lambda t: (t.open_time_utc, t.position_id))


def losing_when_next_opens(previous: Trade, nxt: Trade) -> bool:
    """Was ``previous`` losing at the moment ``nxt`` was opened?"""
    if previous.close_time_utc <= nxt.open_time_utc:
        return previous.outcome == "loss"
    if previous.side != nxt.side:
        return False  # an open opposite position is a hedge, not a loss being doubled
    worse = nxt.open_price < previous.open_price if previous.side == "buy" else nxt.open_price > previous.open_price
    return worse


def martingale_chains(trades: list[Trade], config: DetectorSettings) -> list[list[Trade]]:
    """Runs of positions on one symbol where every step grows the lots after a losing position."""
    chains = []
    for symbol in sorted({t.symbol for t in trades}):
        chain: list[Trade] = []
        for t in _open_order(t for t in trades if t.symbol == symbol):
            if chain and losing_when_next_opens(chain[-1], t) and t.lots >= config.martingale_lot_multiplier * chain[-1].lots - 1e-9:
                chain.append(t)
                continue
            if len(chain) > config.martingale_min_losses:
                chains.append(chain)
            chain = [t]
        if len(chain) > config.martingale_min_losses:
            chains.append(chain)
    return chains


def baskets(trades: list[Trade]) -> list[list[Trade]]:
    """Groups of same-symbol, same-side positions that overlap in time (each opens while another is open)."""
    out = []
    for key in sorted({(t.symbol, t.side) for t in trades}):
        basket: list[Trade] = []
        last_close = None
        for t in _open_order(t for t in trades if (t.symbol, t.side) == key):
            if basket and t.open_time_utc < last_close:
                basket.append(t)
                last_close = max(last_close, t.close_time_utc)
            else:
                if basket:
                    out.append(basket)
                basket, last_close = [t], t.close_time_utc
        if basket:
            out.append(basket)
    return out


def _max_open_and_grid(basket: list[Trade], config: DetectorSettings) -> tuple[int, bool]:
    """Most positions open at once, and whether some moment has a grid: enough open,
    each entered further against the first."""
    most, grid = 0, False
    for i, t in enumerate(basket):
        open_now = [p for p in basket[: i + 1] if p.close_time_utc > t.open_time_utc]
        most = max(most, len(open_now))
        if len(open_now) >= config.grid_min_open:
            prices = [p.open_price for p in open_now]
            steps = zip(prices, prices[1:])
            if t.side == "buy":
                grid = grid or all(b < a for a, b in steps)
            else:
                grid = grid or all(b > a for a, b in steps)
    return most, grid


def _sequence(kind: str, positions: list[Trade]) -> dict:
    sides = {p.side for p in positions}
    return dict(
        type=kind,
        symbol=positions[0].symbol,
        side=sides.pop() if len(sides) == 1 else "mixed",
        position_ids=tuple(p.position_id for p in positions),
        first_lot=positions[0].lots,
        max_lot=max(p.lots for p in positions),
        start_utc=positions[0].open_time_utc,
        end_utc=max(p.close_time_utc for p in positions),
        net=round(sum(net_of(p) for p in positions), 2),
    )


def find_sequences(trades: list[Trade], config: DetectorSettings) -> list[Sequence]:
    """Every martingale, grid and martingale-grid sequence, numbered by start time."""
    found = []
    chains = martingale_chains(trades, config)
    claimed: set[int] = set()
    for basket in baskets(trades):
        most, is_grid = _max_open_and_grid(basket, config)
        if not is_grid:
            continue
        members = {p.position_id for p in basket}
        inside = [c for c in chains if {p.position_id for p in c} <= members]
        claimed |= {p.position_id for c in inside for p in c}
        found.append(dict(_sequence(BOTH if inside else GRID, basket), max_open=most))
    for chain in chains:
        if chain[0].position_id in claimed:
            continue
        most, _ = _max_open_and_grid(chain, config)
        found.append(dict(_sequence(MARTINGALE, chain), max_open=most))
    found.sort(key=lambda s: (s["start_utc"], s["position_ids"]))
    return [Sequence(sequence_id=i, **s) for i, s in enumerate(found, start=1)]


def sequence_tags(sequences: list[Sequence], trades: list[Trade]) -> list[Tag]:
    """A martingale and/or grid tag on every position of every sequence."""
    lots = {t.position_id: t.lots for t in trades}
    tags = []
    for s in sequences:
        rules = {MARTINGALE: ("martingale",), GRID: ("grid",), BOTH: ("martingale", "grid")}[s.type]
        outcome = f"won {s.net:,.2f}" if s.net > 0 else f"lost {-s.net:,.2f}" if s.net < 0 else "broke even"
        for level, pid in enumerate(s.position_ids, start=1):
            evidence = (f"level {level} of {s.depth} in {s.type.replace('_', '-')} sequence #{s.sequence_id}: "
                        f"{lots[pid]:g} lots, {lots[pid] / s.first_lot:.1f}x the first; the sequence {outcome}")
            tags += [Tag(pid, rule, evidence) for rule in rules]
    return tags
