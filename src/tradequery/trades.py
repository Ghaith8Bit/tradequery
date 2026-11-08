"""Deals -> closed trades -> derived fields, plus the balance cross-check.

Weekday and hour are taken from the trade's OPEN time in broker server time,
because the forex week is defined by the server clock. The session label comes
from the UTC hour of the open time and the ranges in settings.toml.

The balance curve applies each trade's whole net profit (including the
commission charged when it opened) at its close time. The final balance is
exact; balances in between differ slightly from MT5 while positions are open.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime

from .models import NON_BALANCE_FLOW_TYPES, TRADE_TYPES, CashFlow, Deal, Trade
from .settings import Settings

_EPS = 1e-9
_OUT_ENTRIES = ("out", "out_by")


def extract_cash_flows(deals: Iterable[Deal]) -> list[CashFlow]:
    """Balance operations as cash flows, oldest first.

    A "balance" deal is a deposit when its amount is positive and a withdrawal
    when negative. Credit, bonus, correction and charge keep their own type.
    """
    flows = []
    for deal in deals:
        if deal.type in TRADE_TYPES:
            continue
        if deal.type == "balance":
            kind = "deposit" if deal.profit >= 0 else "withdrawal"
        elif deal.type in ("credit", "bonus", "correction", "charge"):
            kind = deal.type
        else:
            kind = "other"
        flows.append(CashFlow(deal.ticket, deal.time_utc, kind, deal.profit, deal.comment))
    flows.sort(key=lambda f: (f.time_utc, f.flow_id))
    return flows


def build_trades(deals: Iterable[Deal]) -> list[Trade]:
    """One Trade per fully closed position, oldest close first.

    Positions still open, partly closed, or whose opening deal is missing from
    the history are skipped. Several entry or exit deals for one position are
    combined: lots add up, prices are volume-weighted, commission, swap and
    profit are summed, and the close time is the last exit.
    """
    by_position: dict[int, list[Deal]] = defaultdict(list)
    for deal in deals:
        if deal.type in TRADE_TYPES:
            by_position[deal.position_id].append(deal)

    trades = []
    for position_id, group in by_position.items():
        group.sort(key=lambda d: (d.time_utc, d.ticket))
        for deal in group:
            if deal.entry == "inout":
                raise ValueError(
                    f"position {position_id}: reversal deals (entry='inout') are not supported yet"
                )
            if deal.entry != "in" and deal.entry not in _OUT_ENTRIES:
                raise ValueError(f"position {position_id}: unknown entry {deal.entry!r}")

        entries = [d for d in group if d.entry == "in"]
        exits = [d for d in group if d.entry in _OUT_ENTRIES]
        if not entries:
            continue  # the history starts in the middle of this position

        lots = sum(d.volume for d in entries)
        closed_lots = sum(d.volume for d in exits)
        if closed_lots > lots + _EPS:
            raise ValueError(f"position {position_id}: closed {closed_lots} lots but opened {lots}")
        if not exits or closed_lots < lots - _EPS:
            continue  # still open, or only partly closed

        first = entries[0]
        last = exits[-1]
        trades.append(
            Trade(
                position_id=position_id,
                symbol=first.symbol,
                side=first.type,
                lots=lots,
                magic=first.magic,
                comment=first.comment,
                open_time_utc=first.time_utc,
                close_time_utc=last.time_utc,
                open_price=sum(d.volume * d.price for d in entries) / lots,
                close_price=sum(d.volume * d.price for d in exits) / closed_lots,
                sl=first.sl,
                tp=first.tp,
                close_reason=last.reason,
                commission=sum(d.commission for d in group),
                swap=sum(d.swap for d in group),
                profit=sum(d.profit for d in group),
            )
        )

    trades.sort(key=lambda t: (t.close_time_utc, t.position_id))
    return trades


WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class BalancePoint:
    time_utc: datetime
    balance: float
    kind: str  # "trade" or the cash-flow type
    ref: int  # position_id for a trade, flow_id for a cash flow


def net_profit_of(trade: Trade) -> float:
    return round(trade.profit + trade.commission + trade.swap, 2)


def outcome_of(net_profit: float, tolerance: float) -> str:
    if net_profit > tolerance:
        return "win"
    if net_profit < -tolerance:
        return "loss"
    return "breakeven"


def session_label(utc_hour: int, sessions: dict[str, tuple[int, int]]) -> str:
    """Names of every session whose [start, end) UTC range holds the hour, joined by "_"."""
    active = [name for name, (start, end) in sessions.items() if start <= utc_hour < end]
    return "_".join(active) if active else "off_hours"


def balance_curve(
    trades: Iterable[Trade], cash_flows: Iterable[CashFlow], initial_balance: float = 0.0
) -> list[BalancePoint]:
    """Running balance after every trade and every balance-changing cash flow.

    At the same instant a cash flow is applied before a trade. Credits and
    bonuses are left out because they do not change the balance.
    """
    events = []
    for flow in cash_flows:
        if flow.type not in NON_BALANCE_FLOW_TYPES:
            events.append((flow.time_utc, 0, flow.flow_id, flow.type, flow.amount))
    for trade in trades:
        events.append((trade.close_time_utc, 1, trade.position_id, "trade", net_profit_of(trade)))
    events.sort(key=lambda e: e[:3])

    balance = initial_balance
    points = []
    for time_utc, _, ref, kind, amount in events:
        balance = round(balance + amount, 2)
        points.append(BalancePoint(time_utc, balance, kind, ref))
    return points


def derive_trades(
    trades: Iterable[Trade],
    cash_flows: Iterable[CashFlow],
    settings: Settings,
    initial_balance: float = 0.0,
) -> list[Trade]:
    """Return new trades, oldest close first, with every derived field filled in."""
    ordered = sorted(trades, key=lambda t: (t.close_time_utc, t.position_id))
    balance_after = {
        p.ref: p.balance for p in balance_curve(ordered, cash_flows, initial_balance) if p.kind == "trade"
    }

    derived = []
    for trade in ordered:
        net = net_profit_of(trade)
        outcome = outcome_of(net, settings.break_even_tolerance)
        server_open = settings.clock.from_utc(trade.open_time_utc)
        derived.append(
            replace(
                trade,
                net_profit=net,
                hold_minutes=(trade.close_time_utc - trade.open_time_utc).total_seconds() / 60,
                weekday=WEEKDAYS[server_open.weekday()],
                hour=server_open.hour,
                session=session_label(trade.open_time_utc.hour, settings.sessions),
                outcome=outcome,
                is_win=outcome == "win",
                balance_after=balance_after[trade.position_id],
            )
        )
    return derived


def balance_from_deals(deals: Iterable[Deal]) -> float:
    """Account balance implied by the deals: profit + commission + swap of every deal.

    This includes the entry commission of positions that are still open (MT5
    charges it at once) and leaves out credits and bonuses, so it should equal
    the balance MT5 shows when the history is complete.
    """
    return round(
        sum(d.profit + d.commission + d.swap for d in deals if d.type not in NON_BALANCE_FLOW_TYPES), 2
    )
