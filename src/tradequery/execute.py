"""Run a validated QuerySpec on one account database and return a result table.

- Plain and grouped queries run as compiled SQL on a read-only connection, with
  a row cap (500) and a time limit.
- Named metrics never come from SQL arithmetic: the matching trade ids are
  selected with SQL, and the numbers are computed by metrics.compute_metrics
  (the tested code), per group or per compare set.
- Comparisons return each set's sample size and a 95% range next to the win rate.
- Every result carries an interpretation line: entity, filters, time range,
  timezone and what was left out, so a misread question is visible.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import timezone

from . import db
from . import metrics as metrics_mod
from .compiler import build_select, ids_select, to_sql, to_utc
from .settings import Settings
from .spec import ENTITIES, FACT_METRICS, MAX_ROWS, QuerySpec

QUERY_SECONDS = 5.0


class QueryTimeout(RuntimeError):
    """The query ran longer than the time limit."""


@dataclass
class Result:
    kind: str  # query, clarify or unsupported
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    truncated: bool = False
    interpretation: str = ""
    notes: list[str] = field(default_factory=list)
    sql: str = ""
    message: str | None = None


def _run(conn: sqlite3.Connection, stmt, seconds: float) -> tuple[list[str], list[list], str]:
    sql, params = to_sql(stmt)
    deadline = time.monotonic() + seconds
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        cursor = conn.execute(sql, params)
        columns = [d[0] for d in cursor.description]
        rows = [list(r) for r in cursor.fetchall()]
    except sqlite3.OperationalError as error:
        if "interrupted" in str(error):
            raise QueryTimeout(f"the query took longer than {seconds:g} s") from None
        raise
    finally:
        conn.set_progress_handler(None, 0)
    return columns, rows, sql


def _describe(spec: QuerySpec, login: int, settings: Settings) -> str:
    def f_text(f):
        if f.op == "between":
            return f"{f.field} between {f.value[0]} and {f.value[1]}"
        if spec.entity == "trades" and f.field == "tags":
            return f"tagged {f.value}" if f.op == "eq" else f"not tagged {f.value}"
        symbol = {"eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "in": "in"}[f.op]
        return f"{f.field} {symbol} {f.value}"

    from .scope import level_label  # scope imports this module

    parts = [f"account {login}", f"{level_label(spec).lower()} ({spec.entity})"]
    if spec.filters:
        parts.append("where " + " and ".join(f_text(f) for f in spec.filters))
    tz = spec.time_range.timezone if spec.time_range else "server"
    if spec.time_range:
        parts.append(f"{spec.time_range.start or 'start'} to {spec.time_range.end or 'now'} ({tz} time)")
    if spec.as_of:
        parts.append(f"as of {spec.as_of} ({tz} time)")
    if spec.group_by:
        parts.append("by " + ", ".join(spec.group_by))
    if spec.compare:
        parts.append("comparing " + " vs ".join(c.label for c in spec.compare))
    if spec.entity == "trades" and ({"weekday", "hour", "month"} & ({f.field for f in spec.filters} | set(spec.group_by))
                                    or any(f.field in ("weekday", "hour") for c in spec.compare for f in c.filters)):
        parts.append("weekday, hour and month in broker server time")
    return "; ".join(parts)


def _metric_values(names, chosen, cash_flows, fact_rows, opening, settings, specs):
    computed = metrics_mod.compute_metrics(chosen, cash_flows, opening_balance=opening, symbol_specs=specs,
                                           breakeven_breaks_streak=settings.breakeven_breaks_streak)
    values = {}
    for name in names:
        if name in FACT_METRICS:
            col = {"avg_mae": "mae", "avg_mfe": "mfe", "worst_mae": "mae", "avg_minutes_in_profit": "minutes_in_profit",
                   "avg_minutes_in_loss": "minutes_in_loss"}[name]
            vals = [fact_rows[t.position_id][col] for t in chosen if fact_rows.get(t.position_id, {}).get(col) is not None]
            values[name] = (min(vals) if name == "worst_mae" else sum(vals) / len(vals)) if vals else None
        else:
            values[name] = computed[name]
    return values, computed


def execute(spec: QuerySpec, path, settings: Settings, *, seconds: float = QUERY_SECONDS) -> Result:
    """Run a spec on the account database at ``path`` (opened read-only)."""
    if spec.kind != "query":
        return Result(spec.kind, message=spec.message)
    conn = db.connect(path, read_only=True)
    try:
        account = db.load_account(conn)
        login = account["account_id"]
        interpretation = _describe(spec, login, settings)
        if not spec.metrics:
            columns, rows, sql = _run(conn, build_select(spec, settings.clock), seconds)
            cap = 1 if spec.as_of is not None and not spec.limit else min(spec.limit or 100, MAX_ROWS)
            # Cut short only counts when the spec did not ask for that many rows (a top-1 is not "more").
            truncated = len(rows) > cap and (spec.limit is None or spec.limit == MAX_ROWS) and spec.as_of is None
            return Result("query", columns, rows[:cap], truncated, interpretation, [], sql)
        return _execute_metrics(conn, spec, settings, account, interpretation, seconds)
    finally:
        conn.close()


def _execute_metrics(conn, spec, settings, account, interpretation, seconds) -> Result:
    trades = {t.position_id: t for t in db.load_trades(conn)}
    flows = db.load_cash_flows(conn)
    specs = {s["symbol"]: s for s in db.load_symbols(conn)}
    fact_rows = {r["position_id"]: dict(r) for r in conn.execute("SELECT * FROM trade_facts")}
    opening = account["opening_balance"]
    if spec.time_range:
        tz = spec.time_range.timezone
        start = to_utc(spec.time_range.start, tz, settings.clock) if spec.time_range.start else None
        end = to_utc(spec.time_range.end, tz, settings.clock, end_of_day=True) if spec.time_range.end else None
        flows = [f for f in flows if (not start or f.time_utc >= start) and (not end or f.time_utc <= end)]
        if start:
            opening = 0.0
    notes, sqls = [], []
    sets = ([(c.label, list(c.filters)) for c in spec.compare] if spec.compare else [(None, [])])
    columns = (["set", "trades"] if spec.compare else list(spec.group_by)) + list(spec.metrics)
    if spec.compare and "win_rate" in spec.metrics:
        columns += ["win_rate_low", "win_rate_high"]
    rows = []
    for label, extra in sets:
        cols, id_rows, sql = _run(conn, ids_select(spec, settings.clock, extra, spec.group_by), seconds)
        sqls.append(sql)
        groups: dict[tuple, list] = {}
        for r in id_rows:
            groups.setdefault(tuple(r[1:]), []).append(trades[r[0]])
        if not groups and not spec.group_by:
            groups[()] = []
        for key, chosen in sorted(groups.items(), key=lambda kv: [str(k) for k in kv[0]]):
            values, computed = _metric_values(spec.metrics, chosen, flows, fact_rows, opening, settings, specs)
            row = ([label, len(chosen)] if spec.compare else list(key)) + [values[m] for m in spec.metrics]
            if spec.compare and "win_rate" in spec.metrics:
                ci = computed["win_rate_ci95"]
                row += [ci[0], ci[1]] if ci else [None, None]
            rows.append(row)
            if {"avg_r", "total_r", "planned_rr"} & set(spec.metrics) and chosen and computed["r_excluded"]:
                notes.append(f"{computed['r_excluded']} of {len(chosen)} trades"
                             + (f" ({label})" if label else "") + " had no stop loss at entry and are left out of R")
    for o in reversed(spec.order_by):
        if o.field in columns:
            i = columns.index(o.field)
            rows.sort(key=lambda r: (r[i] is None, r[i] if r[i] is not None else 0), reverse=o.direction == "desc")
    cap = min(spec.limit or 100, MAX_ROWS)
    if any(m in ("max_drawdown", "max_drawdown_pct") for m in spec.metrics):
        notes.append("drawdown on closed trades only, deposits and withdrawals removed")
    truncated = len(rows) > cap and (spec.limit is None or spec.limit == MAX_ROWS)
    return Result("query", columns, rows[:cap], truncated, interpretation, notes, "\n".join(sqls))
