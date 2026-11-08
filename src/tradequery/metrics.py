"""Named metrics, computed by code on a set of closed trades and cash flows.

Every number a user sees comes from one of these functions; the language model
will only pick metrics by name. Each metric has a one-line definition in
DEFINITIONS, shown in the "how this was computed" panel.

Rules that keep the numbers honest:
- Cash flows (deposits, withdrawals, credits) never count as trading profit or loss.
- Drawdown is measured on trading P&L only, so a withdrawal never looks like a loss.
- R-multiples and planned risk-reward need the stop loss at entry; trades without
  one are left out and the result says how many.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from statistics import mean, median

from .models import CashFlow, Trade

DEFINITIONS = {
    "trade_count": "Number of closed positions.",
    "win_rate": "Share of closed positions whose net profit is above the break-even tolerance.",
    "win_rate_ci95": "95% Wilson confidence interval for the win rate.",
    "net_profit": "Sum of net profit (profit + commission + swap) of closed positions. Cash flows excluded.",
    "gross_profit": "Sum of net profit of winning positions.",
    "gross_loss": "Sum of net profit of losing positions (negative).",
    "profit_factor": "Gross profit divided by the absolute gross loss. Empty when there are no losses.",
    "expectancy": "Average net profit per closed position.",
    "avg_hold_minutes": "Mean time from open to close, in minutes.",
    "avg_hold_minutes_winners": "Mean hold time of winning positions.",
    "avg_hold_minutes_losers": "Mean hold time of losing positions.",
    "swap_total": "Sum of swap.",
    "swap_share": "Swap total as a share of gross profit.",
    "commission_total": "Sum of commission (including exchange fees).",
    "planned_rr": "Mean of take-profit distance / stop-loss distance at entry, over positions with both.",
    "avg_r": "Mean R-multiple: net profit / initial risk, where risk = stop-loss distance x lots x money per price unit.",
    "total_r": "Sum of R-multiples.",
    "r_excluded": "Positions left out of R and planned RR because they had no initial stop loss.",
    "max_win_streak": "Longest run of consecutive winning positions, in close order.",
    "max_loss_streak": "Longest run of consecutive losing positions, in close order.",
    "current_streak": "The run at the end: +n wins or -n losses.",
    "avg_streak_length": "Mean length of all win and loss runs.",
    "total_deposits": "Sum of deposits.",
    "total_withdrawals": "Sum of withdrawals (negative).",
    "net_deposits": "Deposits plus withdrawals.",
    "credits_and_bonuses": "Sum of credits and bonuses, reported apart from deposits.",
    "return_pct": "Net profit / (opening balance + deposits): a simple return, not time-weighted.",
    "max_drawdown": "Largest peak-to-trough fall of cumulative trading P&L (cash flows removed). Closed trades only.",
    "max_drawdown_pct": "That fall as a share of the account balance at the peak.",
}


# ---------------------------------------------------------------- filtering


def filter_trades(
    trades: Iterable[Trade],
    *,
    symbol: str | None = None,
    magic: int | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[Trade]:
    """Trades matching every given filter; times compare against the close time."""
    out = []
    for t in trades:
        if symbol is not None and t.symbol != symbol:
            continue
        if magic is not None and t.magic != magic:
            continue
        if start is not None and t.close_time_utc < start:
            continue
        if end is not None and t.close_time_utc > end:
            continue
        out.append(t)
    return out


# ---------------------------------------------------------------- results and win rate


def _net(t: Trade) -> float:
    return t.net_profit if t.net_profit is not None else round(t.profit + t.commission + t.swap, 2)


def win_rate(trades: list[Trade]) -> float | None:
    return sum(1 for t in trades if t.outcome == "win") / len(trades) if trades else None


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for a proportion; sound for small samples too."""
    if n == 0:
        return None
    p = successes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def profit_factor(trades: list[Trade]) -> float | None:
    gross_profit = sum(_net(t) for t in trades if t.outcome == "win")
    gross_loss = sum(_net(t) for t in trades if t.outcome == "loss")
    return gross_profit / abs(gross_loss) if gross_loss else None


def _mean_or_none(values: list[float]) -> float | None:
    return mean(values) if values else None


# ---------------------------------------------------------------- R-multiples


def value_per_price_unit(trades: list[Trade], spec: dict | None = None) -> float | None:
    """Money (account currency) per 1.0 price move per lot for one symbol.

    From the symbol spec (tick value / tick size) when MT5 gave one; MT5 reports a
    tick value of 0 while the market is closed, so otherwise it is measured from
    the closed trades themselves: profit / (price move x lots), median over trades.
    """
    if spec and spec.get("tick_value") and spec.get("tick_size"):
        return spec["tick_value"] / spec["tick_size"]
    samples = []
    for t in trades:
        move = (t.close_price - t.open_price) * (1 if t.side == "buy" else -1)
        if move and t.lots:
            samples.append(t.profit / (move * t.lots))
    return median(samples) if samples else None


def risk_amount(t: Trade, value_per_unit: float | None) -> float | None:
    """Money at risk at entry: stop-loss distance x lots x money per price unit."""
    if not t.sl or not value_per_unit:
        return None
    return abs(t.open_price - t.sl) * t.lots * value_per_unit


def r_multiple(t: Trade, value_per_unit: float | None) -> float | None:
    risk = risk_amount(t, value_per_unit)
    return _net(t) / risk if risk else None


def planned_rr(t: Trade) -> float | None:
    if not t.sl or not t.tp or t.open_price == t.sl:
        return None
    return abs(t.tp - t.open_price) / abs(t.open_price - t.sl)


# ---------------------------------------------------------------- streaks


@dataclass(frozen=True)
class Streak:
    kind: str  # "win" or "loss"
    length: int
    start: datetime
    end: datetime
    net: float
    position_ids: tuple[int, ...]


def streaks(trades: list[Trade], breakeven_breaks: bool = False) -> list[Streak]:
    """Runs of consecutive wins or losses in close order.

    A break-even trade is skipped by default; with ``breakeven_breaks`` it ends the run.
    """
    runs: list[Streak] = []
    current: list[Trade] = []

    def close_run():
        if current:
            runs.append(Streak(current[0].outcome, len(current), current[0].close_time_utc,
                               current[-1].close_time_utc, round(sum(_net(t) for t in current), 2),
                               tuple(t.position_id for t in current)))
            current.clear()

    for t in sorted(trades, key=lambda t: (t.close_time_utc, t.position_id)):
        if t.outcome == "breakeven":
            if breakeven_breaks:
                close_run()
            continue
        if current and current[-1].outcome != t.outcome:
            close_run()
        current.append(t)
    close_run()
    return runs


# ---------------------------------------------------------------- drawdown


@dataclass
class DrawdownEpisode:
    peak_time: datetime | None  # None: the peak is the start of the history
    trough_time: datetime
    recovery_time: datetime | None  # None: not recovered yet
    peak_value: float  # cumulative trading P&L at the peak
    trough_value: float
    balance_at_peak: float
    position_ids: list[int] = field(default_factory=list)  # trades from the peak to the trough

    @property
    def depth(self) -> float:
        return round(self.peak_value - self.trough_value, 2)

    @property
    def depth_pct(self) -> float | None:
        return self.depth / self.balance_at_peak if self.balance_at_peak > 0 else None


def drawdown_episodes(trades: list[Trade]) -> list[DrawdownEpisode]:
    """Peak-to-recovery episodes of cumulative trading P&L (cash flows removed).

    Working on trading P&L rather than the balance means a withdrawal is never
    a drawdown and a deposit never hides one. The percentage uses the real
    balance at the peak (trade.balance_after, which includes cash flows).
    """
    ordered = sorted(trades, key=lambda t: (t.close_time_utc, t.position_id))
    if not ordered:
        return []
    first = ordered[0]
    start_balance = (first.balance_after - _net(first)) if first.balance_after is not None else 0.0

    episodes: list[DrawdownEpisode] = []
    cum = 0.0
    peak, peak_time, peak_balance = 0.0, None, start_balance
    episode: DrawdownEpisode | None = None
    since_peak: list[int] = []
    for t in ordered:
        cum = round(cum + _net(t), 2)
        if cum >= peak:
            if episode is not None:
                episode.recovery_time = t.close_time_utc
                episodes.append(episode)
                episode = None
            peak, peak_time = cum, t.close_time_utc
            peak_balance = t.balance_after if t.balance_after is not None else peak_balance
            since_peak = []
            continue
        since_peak.append(t.position_id)
        if episode is None:
            episode = DrawdownEpisode(peak_time, t.close_time_utc, None, peak, cum, peak_balance)
        if cum < episode.trough_value:
            episode.trough_value, episode.trough_time = cum, t.close_time_utc
            episode.position_ids = list(since_peak)
        elif not episode.position_ids:
            episode.position_ids = list(since_peak)
    if episode is not None:
        episodes.append(episode)
    return episodes


# ---------------------------------------------------------------- everything at once


def compute_metrics(
    trades: list[Trade],
    cash_flows: list[CashFlow],
    *,
    opening_balance: float = 0.0,
    symbol_specs: dict[str, dict] | None = None,
    breakeven_breaks_streak: bool = False,
) -> dict[str, object]:
    """Every named metric for these trades and cash flows. Keys match DEFINITIONS."""
    wins = [t for t in trades if t.outcome == "win"]
    losses = [t for t in trades if t.outcome == "loss"]
    gross_profit = round(sum(_net(t) for t in wins), 2)
    gross_loss = round(sum(_net(t) for t in losses), 2)
    net = round(sum(_net(t) for t in trades), 2)

    # R needs a money value per price unit for each symbol
    specs = symbol_specs or {}
    per_unit = {s: value_per_price_unit([t for t in trades if t.symbol == s], specs.get(s))
                for s in {t.symbol for t in trades}}
    rs = [r for t in trades if (r := r_multiple(t, per_unit[t.symbol])) is not None]
    rrs = [rr for t in trades if (rr := planned_rr(t)) is not None]

    runs = streaks(trades, breakeven_breaks_streak)
    win_runs = [r.length for r in runs if r.kind == "win"]
    loss_runs = [r.length for r in runs if r.kind == "loss"]
    current = (runs[-1].length if runs[-1].kind == "win" else -runs[-1].length) if runs else 0

    deposits = round(sum(f.amount for f in cash_flows if f.type == "deposit"), 2)
    withdrawals = round(sum(f.amount for f in cash_flows if f.type == "withdrawal"), 2)
    credits = round(sum(f.amount for f in cash_flows if f.type in ("credit", "bonus")), 2)
    capital = opening_balance + deposits

    episodes = drawdown_episodes(trades)
    worst = max(episodes, key=lambda e: e.depth, default=None)

    ci = wilson_interval(len(wins), len(trades))
    return {
        "trade_count": len(trades),
        "win_rate": win_rate(trades),
        "win_rate_ci95": ci,
        "net_profit": net,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": gross_profit / abs(gross_loss) if gross_loss else None,
        "expectancy": net / len(trades) if trades else None,
        "avg_hold_minutes": _mean_or_none([t.hold_minutes for t in trades]),
        "avg_hold_minutes_winners": _mean_or_none([t.hold_minutes for t in wins]),
        "avg_hold_minutes_losers": _mean_or_none([t.hold_minutes for t in losses]),
        "swap_total": round(sum(t.swap for t in trades), 2),
        "swap_share": sum(t.swap for t in trades) / gross_profit if gross_profit else None,
        "commission_total": round(sum(t.commission for t in trades), 2),
        "planned_rr": _mean_or_none(rrs),
        "avg_r": _mean_or_none(rs),
        "total_r": round(sum(rs), 4) if rs else None,
        "r_excluded": len(trades) - len(rs),
        "max_win_streak": max(win_runs, default=0),
        "max_loss_streak": max(loss_runs, default=0),
        "current_streak": current,
        "avg_streak_length": _mean_or_none([r.length for r in runs]),
        "total_deposits": deposits,
        "total_withdrawals": withdrawals,
        "net_deposits": round(deposits + withdrawals, 2),
        "credits_and_bonuses": credits,
        "return_pct": net / capital if capital > 0 else None,
        "max_drawdown": worst.depth if worst else 0.0,
        "max_drawdown_pct": worst.depth_pct if worst else None,
    }
