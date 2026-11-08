"""Compare two sources of the same account: an MT5 report and the stored (synced) deals.

The build plan's check for the MT5 sync: the same account loaded by report and
by sync must give the same trades. Only fields both sources carry are compared;
a report has no magic numbers and only partial close reasons, so those are skipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Deal, Trade
from .trades import build_trades

DEAL_FIELDS = ("time_utc", "type", "entry", "symbol", "volume", "price", "commission", "swap", "profit", "position_id")
TRADE_FIELDS = (
    "symbol", "side", "lots", "open_time_utc", "close_time_utc", "open_price", "close_price",
    "sl", "tp", "commission", "swap", "profit",
)
_TOLERANCE = 0.005


@dataclass
class Comparison:
    deals_compared: int = 0
    trades_compared: int = 0
    only_in_report: list[int] = field(default_factory=list)  # deal tickets
    only_in_store: list[int] = field(default_factory=list)  # deal tickets, inside the report's time span
    deal_differences: list[str] = field(default_factory=list)
    trade_differences: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.only_in_report or self.only_in_store or self.deal_differences or self.trade_differences)


def _same(a, b) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        return abs(float(a) - float(b)) <= _TOLERANCE
    return a == b


def _diff(kind: str, key: int, a, b, fields_: tuple[str, ...]) -> list[str]:
    return [
        f"{kind} {key}: {name} report={getattr(a, name)!r} sync={getattr(b, name)!r}"
        for name in fields_
        if not _same(getattr(a, name), getattr(b, name))
    ]


def compare(report_deals: list[Deal], stored_deals: list[Deal]) -> Comparison:
    """Deals and closed trades of the report against the stored deals, within the report's span."""
    result = Comparison()
    if not report_deals:
        return result
    start, end = report_deals[0].time_utc, report_deals[-1].time_utc
    in_span = [d for d in stored_deals if start <= d.time_utc <= end]
    report_by_ticket = {d.ticket: d for d in report_deals}
    store_by_ticket = {d.ticket: d for d in in_span}

    result.only_in_report = sorted(report_by_ticket.keys() - store_by_ticket.keys())
    result.only_in_store = sorted(store_by_ticket.keys() - report_by_ticket.keys())
    for ticket in sorted(report_by_ticket.keys() & store_by_ticket.keys()):
        a, b = report_by_ticket[ticket], store_by_ticket[ticket]
        fields_ = DEAL_FIELDS
        if a.entry != "in" and a.position_id == 0:
            # the report cannot name the position of an exit whose entry is before it starts
            fields_ = tuple(f for f in DEAL_FIELDS if f != "position_id")
        result.deal_differences += _diff("deal", ticket, a, b, fields_)
        result.deals_compared += 1

    report_trades: dict[int, Trade] = {t.position_id: t for t in build_trades(report_deals)}
    store_trades: dict[int, Trade] = {t.position_id: t for t in build_trades(in_span)}
    for pid in sorted(report_trades.keys() | store_trades.keys()):
        if pid not in store_trades or pid not in report_trades:
            side = "report" if pid in report_trades else "sync"
            result.trade_differences.append(f"trade {pid}: only in the {side}")
            continue
        result.trade_differences += _diff("trade", pid, report_trades[pid], store_trades[pid], TRADE_FIELDS)
        result.trades_compared += 1
    return result
