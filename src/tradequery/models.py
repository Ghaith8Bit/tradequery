"""Plain data structures shared by every importer and the rest of TradeQuery.

A *deal* is one line of MT5 history: a trade entry, a trade exit, or a balance
operation. Every source (MT5 report, live sync, synthetic generator) produces
deals in this shape, and ``positions.build_trades`` turns them into one
``Trade`` per closed position. ``derive.derive_trades`` then fills in the
derived fields (net profit, hold time, weekday, hour, session, outcome and
running balance).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Deal types that open or close a position. Anything else is a balance operation.
TRADE_TYPES = ("buy", "sell")

# Cash-flow types that do not change the account balance (they change credit).
NON_BALANCE_FLOW_TYPES = frozenset({"credit", "bonus"})


@dataclass(frozen=True)
class Deal:
    """One MT5 deal, normalized. Times are aware UTC.

    type:   "buy" or "sell" for trades; "balance", "credit", "bonus",
            "correction", "charge" or anything else for balance operations.
    entry:  "in", "out", "inout" or "out_by" for trades; "" for balance operations.
    profit: for balance operations this is the signed amount (deposit > 0).
    sl, tp: the stop loss and take profit of the opening order (0 = none).
            MT5 deals do not carry them, so importers copy them from the order.
    reason: how a closing deal happened: "sl", "tp", "client", "expert", "so", ...
    """

    ticket: int
    position_id: int
    time_utc: datetime
    type: str
    entry: str
    symbol: str
    volume: float
    price: float
    commission: float
    swap: float
    profit: float
    magic: int = 0
    reason: str = ""
    comment: str = ""
    sl: float = 0.0
    tp: float = 0.0


@dataclass(frozen=True)
class CashFlow:
    """A balance operation: deposit, withdrawal, credit, bonus, correction, charge."""

    flow_id: int
    time_utc: datetime
    type: str
    amount: float  # signed: deposits positive, withdrawals negative
    comment: str = ""


@dataclass(frozen=True)
class Trade:
    """One closed position.

    The first block is built from deals. The second block is filled in by
    ``derive.derive_trades`` and is ``None`` until then.
    """

    position_id: int
    symbol: str
    side: str  # "buy" or "sell"
    lots: float
    magic: int
    comment: str
    open_time_utc: datetime
    close_time_utc: datetime
    open_price: float
    close_price: float
    sl: float
    tp: float
    close_reason: str
    commission: float
    swap: float
    profit: float

    # Derived
    net_profit: float | None = None  # profit + commission + swap
    hold_minutes: float | None = None
    weekday: str | None = None  # of the OPEN time, in broker server time
    hour: int | None = None  # of the OPEN time, in broker server time
    session: str | None = None  # from the UTC hour of the open time
    outcome: str | None = None  # "win", "loss" or "breakeven"
    is_win: bool | None = None
    balance_after: float | None = None
