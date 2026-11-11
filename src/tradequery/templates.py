"""Answer sentences written by code, so simple answers need no second model call.

A template is used when the result is empty, a single row (one drawdown, one
basket, a summary of metrics, the top group of a ranking) or a plain list. It
copies numbers straight from the table, so it cannot invent one; the number
checker still runs on it. Comparisons and grouped tables with several rows go
to the model, which is better at saying what differs.
"""

from __future__ import annotations

from .execute import Result
from .scope import basis_of
from .spec import QuerySpec

WORDS = {
    "sequence_id": "basket", "depth": "legs", "max_lot_multiplier": "lot multiplier", "max_open": "most open at once",
    "drawdown_abs": "basket drawdown", "drawdown_pct": "basket drawdown (% of equity)", "net_pnl": "net result",
    "net_profit": "net result", "depth_abs": "depth", "depth_pct": "share of the money at stake",
    "episode_id": "drawdown", "position_id": "trade", "gross_lots": "lots open", "trade_count": "trades",
    "win_rate": "win rate", "profit_factor": "profit factor", "total_lots": "total lots", "hold_minutes": "minutes held",
    "avg_r": "average R", "total_r": "total R", "max_drawdown": "drawdown on closed trades",
    "max_drawdown_pct": "drawdown on closed trades (% at stake)",
}
NOUNS = {"trades": "trade", "sequences": "basket", "drawdown_episodes": "drawdown", "streaks": "streak",
         "cash_flows": "deposit or withdrawal", "news_events": "news event", "exposure": "moment"}
SHARE = ("_pct", "_share")
MAX_FIELDS = 5


def words(column: str) -> str:
    return WORDS.get(column, column.replace("_", " "))


def is_share(column: str) -> bool:
    return column.endswith(SHARE) or column.startswith("win_rate") or column == "return_pct"


def value_text(column: str, value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value * 100:.2f}%" if is_share(column) else f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value * 100:.2f}%" if is_share(column) else str(value)
    text = str(value)
    if len(text) >= 16 and text[4] == "-" and text[10] in " T":
        return text[:16].replace("T", " ")  # a timestamp: date and minute
    return text


def _pairs(row: dict, skip=()) -> str:
    parts = []
    for column, value in row.items():
        text = value_text(column, value)
        if text is None or column in skip or column.endswith("_id"):
            continue
        parts.append(f"{words(column)} {text}")
        if len(parts) == MAX_FIELDS:
            break
    return ", ".join(parts)


def _drawdown(row: dict, spec: QuerySpec) -> str | None:
    if "depth_abs" not in row:
        return None
    basis = row.get("basis") or basis_of(spec) or "balance"
    text = f"Your account drawdown ({basis}) went {value_text('depth_abs', row['depth_abs'])} deep"
    if row.get("depth_pct") is not None:
        text += f" ({value_text('depth_pct', row['depth_pct'])} of the money at stake)"
    if row.get("peak_time_utc") and row.get("trough_time_utc"):
        text += f", from its peak on {value_text('t', row['peak_time_utc'])} to its low on {value_text('t', row['trough_time_utc'])}"
    if "recovery_time_utc" in row:
        text += f", recovered on {value_text('t', row['recovery_time_utc'])}" if row["recovery_time_utc"] else ", not recovered yet"
    if row.get("trade_count") is not None:
        text += f"; {row['trade_count']} trades closed in it"
    return text + " (times in UTC)." if "peak_time_utc" in row else text + "."


def _basket(row: dict) -> str | None:
    if "sequence_id" not in row:
        return None
    about = [value_text(c, row[c]) for c in ("symbol", "side") if row.get(c)]
    if row.get("type"):
        about.append(str(row["type"]).replace("martingale_grid", "martingale and grid"))
    if row.get("depth") is not None:
        about.append(f"{row['depth']} legs")
    text = f"Basket {row['sequence_id']}" + (f" ({', '.join(about)})" if about else "")
    if row.get("net_pnl") is not None:
        outcome = "in profit" if row["net_pnl"] > 0 else "at a loss" if row["net_pnl"] < 0 else "flat"
        text += f" closed {outcome}, net {value_text('net_pnl', row['net_pnl'])}"
    if row.get("drawdown_abs") is not None:
        text += f", after going {value_text('drawdown_abs', row['drawdown_abs'])} into the red"
        if row.get("drawdown_pct") is not None:
            text += f" ({value_text('drawdown_pct', row['drawdown_pct'])} of equity)"
    rest = _pairs(row, skip={"symbol", "side", "type", "depth", "net_pnl", "is_win", "drawdown_abs", "drawdown_pct",
                             "start_utc", "end_utc", "first_lot", "max_lot", "news_overlap", "drawdown_time_utc",
                             "equity_at_start"})
    return text + (f"; {rest}" if rest else "") + "."


def _streak(row: dict) -> str | None:
    if row.get("length") is None:
        return None
    kind = {"loss": "losing", "win": "winning"}.get(row.get("type"), "")
    text = f"A {kind + ' ' if kind else ''}streak of {row['length']} trades in a row"
    if row.get("start_utc") and row.get("end_utc"):
        text += f", from {value_text('t', row['start_utc'])} to {value_text('t', row['end_utc'])} UTC"
    if row.get("net_pnl") is not None:
        text += f", net {value_text('net_pnl', row['net_pnl'])}"
    return text + "."


def _summary(row: dict, spec: QuerySpec) -> str:
    noun = NOUNS.get(spec.entity or "", "item")
    narrowed = bool(spec.filters or spec.time_range)
    count_col = next((a.alias for a in spec.aggregations if a.func == "count"), None)
    count = row.get(count_col) if count_col else row.get("trade_count") if spec.metrics else None
    if count is not None:
        lead = f"Across {count} {'matching ' if narrowed else ''}{noun}{'' if count == 1 else 's'}"
    else:
        lead = f"Across {'the matching' if narrowed else 'your'} {noun}s"
    return f"{lead}: {_pairs(row, skip={count_col, 'trade_count'})}."


def template_answer(spec: QuerySpec, result: Result) -> str | None:
    """A plain sentence for a simple result, or None when the model should explain it."""
    if spec.compare:
        return None
    rows = result.rows
    noun = NOUNS.get(spec.entity or "", "row")
    if not rows:
        return f"No {noun}s matched." if spec.entity != "cash_flows" else "No deposits or withdrawals matched."
    if spec.group_by:
        if len(rows) != 1 or not spec.order_by:
            return None  # a table of groups: the model says what stands out
        row = dict(zip(result.columns, rows[0]))
        key = ", ".join(f"{words(g)} {value_text(g, row.get(g))}" for g in spec.group_by)
        order = spec.order_by[0]
        top = "highest" if order.direction == "desc" else "lowest"
        value = value_text(order.field, row.get(order.field))
        if value is None:
            return None
        text = f"{key[0].upper() + key[1:]} has the {top} {words(order.field)}: {value}"
        if row.get(order.field) == 0:
            text += " (it is 0, so there may be nothing to compare)"
        rest = _pairs(row, skip=set(spec.group_by) | {order.field})
        return text + (f"; {rest}" if rest else "") + "."
    if spec.metrics or spec.aggregations:
        return _summary(dict(zip(result.columns, rows[0])), spec) if len(rows) == 1 else None
    if len(rows) == 1:
        row = dict(zip(result.columns, rows[0]))
        if spec.entity == "drawdown_episodes":
            return _drawdown(row, spec)
        if spec.entity == "sequences":
            return _basket(row)
        if spec.entity == "streaks":
            return _streak(row) or f"One streak found: {_pairs(row)}."
        return f"One {noun} found: {_pairs(row)}."
    text = f"{len(rows)} {noun}s found"
    if result.truncated:
        text += " (the list was cut at its row limit)"
    if spec.order_by:
        o = spec.order_by[0]
        text += f", {'highest' if o.direction == 'desc' else 'lowest'} {words(o.field)} first"
    return text + "."
