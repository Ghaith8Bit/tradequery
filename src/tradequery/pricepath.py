"""Price path from one-minute bars: what happened between each trade's open and close.

Per trade, walking the bars from the open minute to the close minute:
  MAE / MFE           worst and best price against the entry, in money
  minutes in profit   minutes whose closing price is on the winning side of the entry
  minutes in loss     ... on the losing side
  spread at open      the spread (points) of the opening minute

Per account: at each minute with an open position, equity = balance + floating
P&L of every open position, and drawdown episodes on that equity (cash flows removed).

Honest limits, stated wherever these numbers appear:
- A bar holds the minute's high and low but not their order, and the first and
  last bars include prices from before the open and after the close, so MAE and
  MFE are approximate (they can only overstate, never understate, the excursion).
- Equity uses bar closes; ``equity_low`` assumes every open position hit its
  worst price in the same minute, a conservative lower bound.
- Bars are bid prices. Buys exit at the bid; sells exit at the ask, so the bar's
  spread is added for them. This matters most on gold.
- The broker keeps a limited depth of bars; trades older than that have no price path.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean

from .metrics import SeriesPoint, deposits_until, series_drawdowns, value_per_price_unit
from .models import NON_BALANCE_FLOW_TYPES, Bar, CashFlow, Trade

MINUTE = timedelta(minutes=1)

PRICE_PATH_DEFINITIONS = {
    "price_path_coverage": "Share of closed positions with one-minute bars for their whole life.",
    "avg_mae": "Mean adverse excursion: the worst open loss a position reached, in money (negative).",
    "avg_mfe": "Mean favourable excursion: the best open profit a position reached, in money.",
    "worst_mae": "The deepest adverse excursion of any single position.",
    "avg_minutes_in_profit": "Mean minutes a position spent with the price on its winning side.",
    "avg_minutes_in_loss": "Mean minutes a position spent with the price on its losing side.",
    "avg_spread_at_open": "Mean spread of the opening minute, in points.",
    "max_equity_drawdown": "Largest peak-to-trough fall of equity (balance + floating P&L, cash flows removed), on bar closes. Approximate.",
    "max_equity_drawdown_pct": "That fall as a share of the money at stake: equity at the peak plus deposits before the trough.",
    "max_equity_drawdown_low": "The same with every open position at its worst price of the minute: a conservative lower bound.",
    "max_equity_drawdown_episode": "The worst equity drawdown: peak, trough, recovery and deposits made during it.",
    "worst_floating_pnl": "The lowest floating P&L of all open positions together at a minute's close.",
}


def floor_minute(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


# ---------------------------------------------------------------- which bars are needed


def bar_windows(trades: list[Trade], merge_gap: timedelta = timedelta(minutes=30)) -> dict[str, list[tuple[datetime, datetime]]]:
    """Per symbol, the time windows (start, end) covering every position's life, merged."""
    by_symbol: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    for t in trades:
        by_symbol[t.symbol].append((floor_minute(t.open_time_utc), floor_minute(t.close_time_utc)))
    out = {}
    for symbol, spans in by_symbol.items():
        spans.sort()
        merged = [list(spans[0])]
        for start, end in spans[1:]:
            if start <= merged[-1][1] + merge_gap:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        out[symbol] = [(a, b) for a, b in merged]
    return out


# ---------------------------------------------------------------- per trade


@dataclass(frozen=True)
class PricePath:
    position_id: int
    mae: float  # money, <= 0
    mfe: float  # money, >= 0
    minutes_in_profit: int
    minutes_in_loss: int
    spread_at_open: int  # points
    bars_used: int


def _bars_between(bars: list[Bar], times: list[datetime], start: datetime, end: datetime) -> list[Bar]:
    return bars[bisect_left(times, start):bisect_right(times, end)]


def price_path(trade: Trade, bars: list[Bar], times: list[datetime], value_per_unit: float, point: float) -> PricePath | None:
    """Excursions and time in profit or loss of one position, or None without complete bars.

    ``times`` is the list of bar times (for fast lookup); ``point`` turns the
    bar spread (points) into price, so sells are valued at the ask.
    """
    first, last = floor_minute(trade.open_time_utc), floor_minute(trade.close_time_utc)
    window = _bars_between(bars, times, first, last)
    if not window or window[0].time_utc != first or window[-1].time_utc != last:
        return None  # the broker's bars do not reach back this far, or there is a gap at an end
    entry = trade.open_price
    if trade.side == "buy":
        worst = min(b.low for b in window)
        best = max(b.high for b in window)
        adverse, favourable = entry - worst, best - entry
        in_profit = sum(1 for b in window if b.close > entry)
        in_loss = sum(1 for b in window if b.close < entry)
    else:
        ask = lambda b, price: price + b.spread * point  # noqa: E731
        worst = max(ask(b, b.high) for b in window)
        best = min(ask(b, b.low) for b in window)
        adverse, favourable = worst - entry, entry - best
        in_profit = sum(1 for b in window if ask(b, b.close) < entry)
        in_loss = sum(1 for b in window if ask(b, b.close) > entry)
    money = trade.lots * value_per_unit
    return PricePath(
        position_id=trade.position_id,
        mae=round(-max(0.0, adverse) * money, 2),
        mfe=round(max(0.0, favourable) * money, 2),
        minutes_in_profit=in_profit,
        minutes_in_loss=in_loss,
        spread_at_open=window[0].spread,
        bars_used=len(window),
    )


def symbol_point(spec: dict | None, bars: list[Bar]) -> float:
    """Price of one point: from the symbol spec, else from the digits of the bar prices."""
    if spec and spec.get("point"):
        return float(spec["point"])
    if spec and spec.get("digits") is not None:
        return 10.0 ** -int(spec["digits"])
    return 0.0  # unknown: spread is ignored


def price_paths(trades: list[Trade], bars_by_symbol: dict[str, list[Bar]], symbol_specs: dict[str, dict] | None = None) -> dict[int, PricePath]:
    """Price path of every position that has complete bars, by position id."""
    specs = symbol_specs or {}
    out = {}
    for symbol in {t.symbol for t in trades}:
        bars = bars_by_symbol.get(symbol, [])
        if not bars:
            continue
        mine = [t for t in trades if t.symbol == symbol]
        per_unit = value_per_price_unit(mine, specs.get(symbol))
        if not per_unit:
            continue
        times = [b.time_utc for b in bars]
        point = symbol_point(specs.get(symbol), bars)
        for t in mine:
            path = price_path(t, bars, times, per_unit, point)
            if path:
                out[t.position_id] = path
    return out


# ---------------------------------------------------------------- equity curve


@dataclass(frozen=True)
class EquityPoint:
    time_utc: datetime  # end of the minute
    balance: float
    floating: float  # bar closes
    floating_low: float  # every open position at its worst price of the minute
    net_flows: float  # deposits and withdrawals so far (taken out for drawdown)

    @property
    def equity(self) -> float:
        return round(self.balance + self.floating, 2)

    @property
    def equity_low(self) -> float:
        return round(self.balance + self.floating_low, 2)


def equity_curve(
    trades: list[Trade],
    cash_flows: list[CashFlow],
    bars_by_symbol: dict[str, list[Bar]],
    *,
    opening_balance: float = 0.0,
    symbol_specs: dict[str, dict] | None = None,
) -> list[EquityPoint]:
    """Equity at the end of every minute with an open position, plus every close.

    The balance follows trade.balance_after (which already includes every deal),
    with cash flows applied in between. A position without a bar in some minute
    is valued at its last known bar (or at its entry before the first bar).
    """
    specs = symbol_specs or {}
    per_unit = {s: value_per_price_unit([t for t in trades if t.symbol == s], specs.get(s)) or 0.0
                for s in {t.symbol for t in trades}}
    points_per = {s: symbol_point(specs.get(s), bars_by_symbol.get(s, [])) for s in per_unit}
    bar_at = {s: {b.time_utc: b for b in bars_by_symbol.get(s, [])} for s in per_unit}

    # balance-changing events in time order: cash flows, then closes at the same instant
    events = [(f.time_utc, 0, f) for f in cash_flows if f.type not in NON_BALANCE_FLOW_TYPES]
    events += [(t.close_time_utc, 1, t) for t in trades]
    events.sort(key=lambda e: (e[0], e[1]))

    # minutes in which at least one position is open at the minute's end
    minutes = set()
    for t in trades:
        m = floor_minute(t.open_time_utc)
        while m + MINUTE <= t.close_time_utc:
            minutes.add(m)
            m += MINUTE
    stamps = sorted([(m + MINUTE, "minute") for m in minutes] + [(t.close_time_utc, "close") for t in trades])

    ordered_trades = sorted(trades, key=lambda t: t.open_time_utc)
    balance, flows, ei, oi = opening_balance, 0.0, 0, 0
    active: dict[int, Trade] = {}  # positions open at the current stamp
    last_bar: dict[int, Bar] = {}
    out: list[EquityPoint] = []
    for when, kind in stamps:
        while ei < len(events) and events[ei][0] <= when:
            _, order, item = events[ei]
            if order == 0:
                balance, flows = round(balance + item.amount, 2), round(flows + item.amount, 2)
            elif item.balance_after is not None:
                balance = item.balance_after
            ei += 1
        while oi < len(ordered_trades) and ordered_trades[oi].open_time_utc <= when:
            active[ordered_trades[oi].position_id] = ordered_trades[oi]
            oi += 1
        for pid in [pid for pid, t in active.items() if t.close_time_utc <= when]:
            del active[pid]
            last_bar.pop(pid, None)
        floating = floating_low = 0.0
        for t in active.values():
            bar = bar_at[t.symbol].get(floor_minute(when) - (MINUTE if kind == "minute" else timedelta(0)))
            if bar is not None:
                last_bar[t.position_id] = bar
            bar = last_bar.get(t.position_id)
            if bar is None:
                continue  # no price yet: valued at entry (0 floating)
            money = t.lots * per_unit[t.symbol]
            if t.side == "buy":
                floating += (bar.close - t.open_price) * money
                floating_low += (bar.low - t.open_price) * money
            else:
                spread = bar.spread * points_per[t.symbol]
                floating += (t.open_price - (bar.close + spread)) * money
                floating_low += (t.open_price - (bar.high + spread)) * money
        out.append(EquityPoint(when, balance, round(floating, 2), round(floating_low, 2), flows))
    return out


def equity_drawdowns(curve: list[EquityPoint], cash_flows: list[CashFlow] = (), *, conservative: bool = False):
    """Drawdown episodes of equity with cash flows removed (see metrics.series_drawdowns).

    Percentages are of the money at stake: equity at the peak plus deposits before the trough.
    """
    if not curve:
        return []
    deposited = deposits_until(cash_flows)
    base = curve[0].equity - curve[0].net_flows
    points = [
        SeriesPoint(p.time_utc, round(p.equity - p.net_flows - base, 2),
                    round((p.equity_low if conservative else p.equity) - p.net_flows - base, 2), p.equity,
                    deposits=deposited(p.time_utc))
        for p in curve
    ]
    return series_drawdowns(points, curve[0].equity)


# ---------------------------------------------------------------- metrics


def compute_price_path_metrics(
    trades: list[Trade],
    cash_flows: list[CashFlow],
    bars_by_symbol: dict[str, list[Bar]],
    *,
    opening_balance: float = 0.0,
    symbol_specs: dict[str, dict] | None = None,
) -> dict[str, object]:
    """Every price-path metric. Keys match PRICE_PATH_DEFINITIONS; empty values when bars are missing."""
    paths = price_paths(trades, bars_by_symbol, symbol_specs)
    values = list(paths.values())
    covered = bool(values)
    curve = equity_curve(trades, cash_flows, bars_by_symbol, opening_balance=opening_balance,
                         symbol_specs=symbol_specs) if covered else []
    close_dd = max(equity_drawdowns(curve, cash_flows), key=lambda e: e.depth, default=None)
    low_dd = max(equity_drawdowns(curve, cash_flows, conservative=True), key=lambda e: e.depth, default=None)
    worst_floating = min(curve, key=lambda p: p.floating, default=None)
    return {
        "price_path_coverage": len(values) / len(trades) if trades else None,
        "avg_mae": mean(p.mae for p in values) if covered else None,
        "avg_mfe": mean(p.mfe for p in values) if covered else None,
        "worst_mae": min(p.mae for p in values) if covered else None,
        "avg_minutes_in_profit": mean(p.minutes_in_profit for p in values) if covered else None,
        "avg_minutes_in_loss": mean(p.minutes_in_loss for p in values) if covered else None,
        "avg_spread_at_open": mean(p.spread_at_open for p in values) if covered else None,
        "max_equity_drawdown": close_dd.depth if close_dd else None,
        "max_equity_drawdown_pct": close_dd.depth_pct if close_dd else None,
        "max_equity_drawdown_low": low_dd.depth if low_dd else None,
        "max_equity_drawdown_episode": close_dd,
        "worst_floating_pnl": (worst_floating.floating, worst_floating.time_utc) if worst_floating and worst_floating.floating < 0 else None,
    }
