"""The account overview shown when the page opens: key numbers, two charts, plain risk facts.

Every number comes from the existing tested code (metrics, stored sequences and
drawdown episodes); nothing here calculates a new kind of figure. Risk facts are
stated as facts ("8 positions were closed by a stop-out"), never as advice.
"""

from __future__ import annotations

from pathlib import Path

from . import db, metrics
from .settings import Settings

CURVE_POINTS = 400  # the balance chart never needs more than this
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _downsample(points: list, limit: int) -> list:
    if len(points) <= limit:
        return points
    step = len(points) / (limit - 1)
    picked = [points[int(i * step)] for i in range(limit - 1)]
    return picked + [points[-1]]


def build_overview(path: Path, settings: Settings) -> dict:
    conn = db.connect(path, read_only=True)
    try:
        account = db.load_account(conn)
        trades = db.load_trades(conn)
        flows = db.load_cash_flows(conn)
        specs = {s["symbol"]: s for s in db.load_symbols(conn)}
        deepest = conn.execute(
            "SELECT depth, max_lot_multiplier, net_pnl, start_utc, sequence_id FROM sequences "
            "ORDER BY depth DESC, net_pnl ASC LIMIT 1"
        ).fetchone()
        deep_count = conn.execute("SELECT COUNT(*), SUM(is_win) FROM sequences WHERE depth >= 6").fetchone()
        no_stop = conn.execute("SELECT COUNT(*) FROM tags WHERE rule = 'no_stop_loss'").fetchone()[0]
    finally:
        conn.close()

    out = {
        "login": account["account_id"], "currency": account.get("currency") or "", "server": account.get("server"),
        "balance": account.get("balance"), "trades": len(trades),
        "first": db.format_time(trades[0].open_time_utc) if trades else None,
        "last": db.format_time(trades[-1].close_time_utc) if trades else None,
        "stats": None, "balance_curve": [], "by_weekday": [], "facts": [],
    }
    if not trades:
        return out

    m = metrics.compute_metrics(trades, flows, opening_balance=account.get("opening_balance") or 0.0,
                                symbol_specs=specs, breakeven_breaks_streak=settings.breakeven_breaks_streak)
    out["stats"] = {
        "net_profit": m["net_profit"], "win_rate": m["win_rate"], "profit_factor": m["profit_factor"],
        "max_drawdown": m["max_drawdown"], "max_drawdown_pct": m["max_drawdown_pct"],
        "deposits": m["total_deposits"], "withdrawals": m["total_withdrawals"],
        "deepest_sequence": ({"depth": deepest[0], "multiplier": deepest[1], "net": deepest[2], "start": deepest[3],
                                  "id": deepest[4]}
                             if deepest else None),
    }
    curve = [(db.format_time(t.close_time_utc), t.balance_after) for t in trades if t.balance_after is not None]
    out["balance_curve"] = _downsample(curve, CURVE_POINTS)
    by_day = {d: 0.0 for d in WEEKDAYS}
    for t in trades:
        by_day[t.weekday] = by_day.get(t.weekday, 0.0) + (t.net_profit or 0.0)
    out["by_weekday"] = [(d, round(v, 2)) for d, v in by_day.items() if v or d not in ("Sat", "Sun")]

    facts = []
    if no_stop:
        facts.append({"level": "warn" if no_stop * 2 >= len(trades) else "info",
                      "text": f"{no_stop} of {len(trades)} trades had no stop loss at entry."})
    stop_outs = sum(1 for t in trades if t.close_reason == "so")
    if stop_outs:
        facts.append({"level": "warn", "text": f"{stop_outs} positions were closed by a stop-out."})
    if deep_count and deep_count[0]:
        won = deep_count[1] or 0
        facts.append({"level": "warn", "text": f"{deep_count[0]} martingale or grid sequences went 6 or more levels "
                                               f"deep; {won} of them closed in profit."})
    if m["max_drawdown_pct"] is not None and m["max_drawdown_pct"] >= 0.5:
        facts.append({"level": "warn", "text": f"The deepest drawdown took {m['max_drawdown_pct'] * 100:.1f}% of the "
                                               "money at stake."})
    out["facts"] = facts
    return out
