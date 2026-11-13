"""The account's worst drop as a short story the start screen can tell step by step.

It reads only stored, tested figures (the deepest drawdown episode, its trades,
the cash flows and the equity curve) and splits the depth into its parts:
closed losses, closed profits, the loss still open at the lowest moment, and
the deposits that do not count as recovering. Nothing here is advice.
"""

from __future__ import annotations

import sqlite3

from . import db
from .timeutil import ServerClock


def _day(clock: ServerClock, text: str | None) -> str | None:
    """A stored UTC time as the broker's server date (the calendar's days)."""
    return clock.from_utc(db.parse_time(text)).strftime("%Y-%m-%d") if text else None


def worst_drop(conn: sqlite3.Connection, clock: ServerClock) -> dict | None:
    """The deepest drawdown (equity when price bars allow it, else balance), or None when there is none."""
    basis = "equity" if conn.execute(
        "SELECT 1 FROM drawdown_episodes WHERE basis = 'equity' LIMIT 1").fetchone() else "balance"
    ep = conn.execute("SELECT * FROM drawdown_episodes WHERE basis = ? AND depth_abs > 0 "
                      "ORDER BY depth_abs DESC LIMIT 1", (basis,)).fetchone()
    if ep is None:
        return None
    field = "equity_drawdown_episode_id" if basis == "equity" else "drawdown_episode_id"
    trades = conn.execute(
        f"SELECT t.position_id, t.close_time_utc, t.net_profit, t.balance_after FROM trades t "
        f"JOIN trade_facts f ON f.position_id = t.position_id WHERE f.{field} = ? "
        f"ORDER BY t.close_time_utc, t.position_id", (ep["episode_id"],)).fetchall()
    peak, trough = ep["peak_time_utc"], ep["trough_time_utc"]
    flows = conn.execute(
        "SELECT time_utc, type, amount FROM cash_flows WHERE (? IS NULL OR time_utc > ?) AND time_utc <= ? "
        "AND type IN ('deposit', 'withdrawal') ORDER BY time_utc", (peak, peak, trough)).fetchall()
    added = ep["added_capital"] or 0.0
    # depth_pct = depth / (account at the peak + deposits before the low), so the account at the peak is:
    at_peak = round(ep["depth_abs"] / ep["depth_pct"] - added, 2) if ep["depth_pct"] else None

    losses = [t for t in trades if (t["net_profit"] or 0) < 0]
    wins = [t for t in trades if (t["net_profit"] or 0) > 0]
    by_day: dict[str, list] = {}
    for t in losses:
        entry = by_day.setdefault(_day(clock, t["close_time_utc"]), [0.0, 0])
        entry[0] += t["net_profit"]
        entry[1] += 1
    worst_day = min(by_day.items(), key=lambda kv: kv[1][0]) if by_day else None

    open_at_low = None
    if basis == "equity":
        row = conn.execute("SELECT floating_pnl FROM equity_curve WHERE time_utc <= ? ORDER BY time_utc DESC LIMIT 1",
                           (trough,)).fetchone()
        open_at_low = round(row[0], 2) if row else None

    # how far it fell with only the money that was there at the peak (before the first deposit)
    before = None
    first_deposit = next((f["time_utc"] for f in flows if f["type"] == "deposit"), None)
    if first_deposit and at_peak:
        if basis == "equity":
            low = conn.execute("SELECT time_utc, equity FROM equity_curve WHERE (? IS NULL OR time_utc > ?) "
                               "AND time_utc < ? ORDER BY equity ASC LIMIT 1", (peak, peak, first_deposit)).fetchone()
        else:
            low = conn.execute("SELECT close_time_utc, balance_after FROM trades WHERE (? IS NULL OR close_time_utc > ?) "
                               "AND close_time_utc < ? AND balance_after IS NOT NULL ORDER BY balance_after ASC LIMIT 1",
                               (peak, peak, first_deposit)).fetchone()
        if low is not None:
            before = {"day": _day(clock, low[0]), "value": round(low[1], 2),
                      "pct": max(0.0, (at_peak - low[1]) / at_peak)}

    no_stop = 0
    if trades:
        ids = [t["position_id"] for t in trades]
        marks = ",".join("?" * len(ids))
        no_stop = conn.execute(f"SELECT COUNT(DISTINCT position_id) FROM tags WHERE rule = 'no_stop_loss' "
                               f"AND position_id IN ({marks})", ids).fetchone()[0]
    return {
        "basis": basis, "episode_id": ep["episode_id"],
        "peak_day": _day(clock, peak), "low_day": _day(clock, trough), "recovered_day": _day(clock, ep["recovery_time_utc"]),
        "depth": round(ep["depth_abs"], 2), "depth_pct": ep["depth_pct"], "at_peak": at_peak, "added": round(added, 2),
        "before_deposit": before,
        "losses": {"count": len(losses), "total": round(sum(t["net_profit"] for t in losses), 2),
                   "worst_day": ({"day": worst_day[0], "count": worst_day[1][1], "total": round(worst_day[1][0], 2)}
                                 if worst_day else None)},
        "profits": {"count": len(wins), "total": round(sum(t["net_profit"] for t in wins), 2)},
        "cash": [{"day": _day(clock, f["time_utc"]), "type": f["type"], "amount": round(f["amount"], 2)} for f in flows],
        "open_at_low": open_at_low,
        "trades": len(trades), "no_stop": no_stop,
    }
