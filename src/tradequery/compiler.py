"""Compile a validated QuerySpec into parameterized SQL (SQLAlchemy Core) over the account views.

Only whitelisted names (spec.ENTITIES) ever become identifiers, and every value
is a bound parameter, so a spec cannot inject SQL. Times in filters and time
ranges are read in the spec's timezone (broker server time by default, like
weekday and hour) and compared in UTC, which is how they are stored.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import and_, column, exists, func, select, table
from sqlalchemy.dialects import sqlite

from .db import format_time
from .spec import ENTITIES, MAX_ROWS, Filter, QuerySpec
from .timeutil import ServerClock

DEFAULT_LIMIT = 100
_TAGS = table("tags", column("position_id"), column("rule"))


def _table(entity: str):
    ent = ENTITIES[entity]
    return table(ent.view, *(column(f) for f, kind in ent.fields.items() if kind != "tag"))


def to_utc(value, tz: str, clock: ServerClock, *, end_of_day: bool = False) -> datetime:
    """A spec time (date or datetime, naive = in the spec's timezone) as aware UTC."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, time()) + (timedelta(days=1) if end_of_day else timedelta())
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    return clock.to_utc(value) if tz == "server" else value.replace(tzinfo=timezone.utc)


def _condition(t, f: Filter, kind: str, tz: str, clock: ServerClock):
    if kind == "tag":
        has = exists(select(_TAGS.c.rule).where(_TAGS.c.position_id == t.c.position_id, _TAGS.c.rule == f.value))
        return has if f.op == "eq" else ~has
    col = t.c[f.field]

    def value(v):
        if kind == "time":
            return format_time(to_utc(v, tz, clock))
        if kind == "bool":
            return int(v)
        return v

    ops = {
        "eq": lambda: col == value(f.value),
        "ne": lambda: col != value(f.value),
        "gt": lambda: col > value(f.value),
        "gte": lambda: col >= value(f.value),
        "lt": lambda: col < value(f.value),
        "lte": lambda: col <= value(f.value),
        "in": lambda: col.in_([value(v) for v in f.value]),
        "between": lambda: col.between(value(f.value[0]), value(f.value[1])),
    }
    return ops[f.op]()


def where_clause(spec: QuerySpec, clock: ServerClock, extra: list[Filter] = ()):
    """All conditions of the spec (filters, time range, as_of) plus extra filters, as one clause."""
    ent = ENTITIES[spec.entity]
    t = _table(spec.entity)
    tz = spec.time_range.timezone if spec.time_range else "server"
    conditions = [_condition(t, f, ent.fields[f.field], tz, clock) for f in [*spec.filters, *extra]]
    if spec.time_range:
        tcol = t.c[ent.time_field]
        if spec.time_range.start is not None:
            conditions.append(tcol >= format_time(to_utc(spec.time_range.start, tz, clock)))
        if spec.time_range.end is not None:
            end = spec.time_range.end
            if isinstance(end, date) and not isinstance(end, datetime):
                conditions.append(tcol < format_time(to_utc(end, tz, clock, end_of_day=True)))
            else:
                conditions.append(tcol <= format_time(to_utc(end, tz, clock)))
    if spec.as_of is not None:
        conditions.append(t.c.time_utc <= format_time(to_utc(spec.as_of, tz, clock)))
        if not any(f.field == "symbol" for f in spec.filters):
            conditions.append(t.c.symbol == "ALL")
    return t, (and_(*conditions) if conditions else None)


def build_select(spec: QuerySpec, clock: ServerClock):
    """The SELECT for a plain or grouped query (named metrics are routed by execute.py)."""
    ent = ENTITIES[spec.entity]
    t, where = where_clause(spec, clock)
    if spec.aggregations or spec.group_by:
        cols = [t.c[g] for g in spec.group_by]
        aggs = []
        for a in spec.aggregations:
            fn = {"count": func.count, "sum": func.sum, "avg": func.avg, "min": func.min, "max": func.max}[a.func]
            aggs.append((fn(t.c[a.field]) if a.field else func.count()).label(a.alias))
        if not aggs:
            aggs = [func.count().label("count")]
        stmt = select(*cols, *aggs).select_from(t)
        if spec.group_by:
            stmt = stmt.group_by(*cols)
        labels = {a.name: a for a in aggs}
        order = [(labels[o.field] if o.field in labels else t.c[o.field]) for o in spec.order_by]
        directions = [o.direction for o in spec.order_by]
    else:
        names = list(spec.fields or ent.default)
        # a list sorted by a column shows that column: "the biggest basket by lots" must show its lots
        names += [o.field for o in spec.order_by if o.field not in names]
        stmt = select(*(t.c[n] for n in names)).select_from(t)
        order = [t.c[o.field] for o in spec.order_by]
        directions = [o.direction for o in spec.order_by]
        if spec.as_of is not None and not spec.order_by:
            order, directions = [t.c.time_utc], ["desc"]
    if where is not None:
        stmt = stmt.where(where)
    for col, direction in zip(order, directions):
        stmt = stmt.order_by(col.desc() if direction == "desc" else col.asc())
    limit = 1 if spec.as_of is not None and not spec.limit else min(spec.limit or DEFAULT_LIMIT, MAX_ROWS)
    return stmt.limit(limit + 1)  # one extra row tells whether the result was cut


def ids_select(spec: QuerySpec, clock: ServerClock, extra: list[Filter] = (), group_by: list[str] = ()):
    """position_id (plus group columns) of the trades matching the spec, for named metrics."""
    t, where = where_clause(spec, clock, list(extra))
    stmt = select(t.c.position_id, *(t.c[g] for g in group_by)).select_from(t)
    return stmt.where(where) if where is not None else stmt


def to_sql(stmt) -> tuple[str, list]:
    """SQL text with ? placeholders, and the parameters in order."""
    # render_postcompile expands IN lists into one ? per value (still bound, never inlined)
    compiled = stmt.compile(dialect=sqlite.dialect(), compile_kwargs={"render_postcompile": True})
    params = [compiled.params[name] for name in compiled.positiontup]
    return str(compiled), params
