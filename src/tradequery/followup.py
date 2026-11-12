"""Conversation focus: what a chat is narrowed to, so follow-ups stay on it.

After "Show my trades on 25 September", the chat's focus is that day. "What about
the news?" then means the news on that day, not over the whole account. The
focus comes from the last answered spec (its period, symbol, magic number, side
and item ids) and from a one-row result (the basket, drawdown or streak found).
The page shows it as chips; dropping a chip removes that part from the
conversation, so the next question no longer carries it.
"""

from __future__ import annotations

from datetime import date, datetime

from .execute import Result

SYMBOL_FIELDS = ("canonical_symbol", "symbol")
ID_LABELS = {
    "sequence_id": "Basket",
    "drawdown_episode_id": "Balance drawdown",
    "equity_drawdown_episode_id": "Equity drawdown",
    "streak_id": "Streak",
    "position_id": "Trade",
}
# a one-row result on these entities is "the thing we are talking about"
ITEM_ENTITIES = {"sequences": ("sequence_id", "Basket"), "streaks": ("streak_id", "Streak"),
                 "drawdown_episodes": ("episode_id", "drawdown")}


def _when(value) -> tuple[date, bool]:
    """A spec time as (date or datetime, has a time of day)."""
    if isinstance(value, (date, datetime)):
        parsed = value
    else:
        text = str(value)
        parsed = datetime.fromisoformat(text) if "T" in text or " " in text else date.fromisoformat(text)
    timed = isinstance(parsed, datetime) and (parsed.hour, parsed.minute, parsed.second) != (0, 0, 0)
    return parsed, timed


def _day(value) -> str:
    when, timed = _when(value)
    text = f"{when.day} {when:%b %Y}"
    return text + (f" {when:%H:%M}" if timed else "")


def period_label(time_range: dict | None, as_of=None) -> str | None:
    if as_of:
        return f"At {_day(as_of)}"
    if not time_range:
        return None
    start, end = time_range.get("start"), time_range.get("end")
    utc = " UTC" if time_range.get("timezone") == "utc" else ""
    if start and end:
        a, b = _day(start), _day(end)
        return (a if a == b else f"{a} – {b}") + utc
    if start:
        return f"From {_day(start)}{utc}"
    if end:
        return f"Until {_day(end)}{utc}"
    return None


def found_item(spec: dict, result: Result | None) -> str | None:
    """The basket, drawdown or streak a one-row answer found ("Equity drawdown 72")."""
    entity = spec.get("entity")
    if entity not in ITEM_ENTITIES or result is None or len(result.rows) != 1:
        return None
    column, noun = ITEM_ENTITIES[entity]
    if column not in result.columns:
        return None
    value = result.rows[0][result.columns.index(column)]
    if value is None:
        return None
    if entity == "drawdown_episodes":
        basis = result.rows[0][result.columns.index("basis")] if "basis" in result.columns else None
        return f"{'Equity' if basis == 'equity' else 'Balance'} drawdown {value}"
    return f"{noun} {value}"


def focus_items(spec: dict | None, item: str | None = None) -> list[dict]:
    """The chat's focus as chips: [{"key", "label"}], most general first."""
    if not spec:
        return [{"key": "item", "label": item}] if item else []
    chips: list[dict] = []
    period = period_label(spec.get("time_range"), spec.get("as_of"))
    if period:
        chips.append({"key": "period", "label": period})
    seen: set[str] = set()
    for f in spec.get("filters") or []:
        field, op, value = f.get("field"), f.get("op"), f.get("value")
        if op not in ("eq", "in") or field in seen:
            continue
        values = value if isinstance(value, list) else [value]
        if field in SYMBOL_FIELDS and "ALL" not in values:
            chips.append({"key": "symbol", "label": ", ".join(map(str, values))})
        elif field == "magic" and op == "eq":
            chips.append({"key": "magic", "label": "Manual trades" if value == 0 else f"Magic {value}"})
        elif field == "side" and op == "eq":
            chips.append({"key": "side", "label": "Buys" if value == "buy" else "Sells"})
        elif field in ID_LABELS and op == "eq":
            chips.append({"key": field, "label": f"{ID_LABELS[field]} {value}"})
        else:
            continue
        seen.add(field)
    if item and item not in [c["label"] for c in chips]:
        chips.append({"key": "item", "label": item})
    return chips


def drop_focus(spec: dict | None, key: str) -> dict | None:
    """The spec without one part of its focus (a copy); the next question then no longer carries it."""
    if not spec:
        return spec
    out = dict(spec)
    if key == "period":
        out.pop("time_range", None)
        out.pop("as_of", None)
        return out
    fields = SYMBOL_FIELDS if key == "symbol" else (key,)
    out["filters"] = [f for f in out.get("filters") or [] if f.get("field") not in fields]
    if not out["filters"]:
        out.pop("filters")
    return out


def focus_line(spec: dict | None, item: str | None) -> str | None:
    """The focus as the model sees it in the next spec prompt."""
    chips = focus_items(spec, item)
    return "; ".join(c["label"] for c in chips) if chips else None
