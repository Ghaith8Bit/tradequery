"""QuerySpec: the typed question the language model fills in. It never writes SQL.

The spec is validated against a whitelist of entities, fields, operators,
group-bys and named metrics; anything else is rejected with a readable message
(the LLM layer sends that message back once so the model can fix its spec).
compiler.py turns a valid spec into parameterized SQL over the account views.

Trade comments are not in the whitelist: they are free text from the account
and never reach the model.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from . import metrics
from .detectors import RULES

MAX_ROWS = 500

Kind = Literal["num", "int", "text", "bool", "time", "tag"]

OPERATORS: dict[str, tuple[str, ...]] = {
    "num": ("eq", "ne", "gt", "gte", "lt", "lte", "in", "between"),
    "int": ("eq", "ne", "gt", "gte", "lt", "lte", "in", "between"),
    "text": ("eq", "ne", "in"),
    "bool": ("eq", "ne"),
    "time": ("gt", "gte", "lt", "lte", "between"),
    "tag": ("eq", "ne"),  # eq: has the tag; ne: does not
}


class Entity:
    def __init__(self, view: str, time_field: str, fields: dict[str, str], group_by: tuple[str, ...], default: tuple[str, ...]):
        self.view, self.time_field, self.fields, self.group_by, self.default = view, time_field, fields, group_by, default


ENTITIES: dict[str, Entity] = {
    "trades": Entity(
        "v_trades", "close_time_utc",
        dict(position_id="int", symbol="text", canonical_symbol="text", side="text", lots="num", magic="int",
             open_time_utc="time", close_time_utc="time", open_price="num", close_price="num", sl="num", tp="num",
             close_reason="text", commission="num", swap="num", profit="num", net_profit="num", hold_minutes="num",
             weekday="text", hour="int", session="text", outcome="text", is_win="bool", balance_after="num",
             month="text", mae="num", mfe="num", minutes_in_profit="int", minutes_in_loss="int",
             spread_at_open="int", risk_amount="num", planned_rr="num", r_multiple="num", sequence_id="int",
             streak_id="int", drawdown_episode_id="int", equity_drawdown_episode_id="int", news_overlap="bool",
             tags="tag"),
        ("symbol", "canonical_symbol", "side", "magic", "weekday", "hour", "session", "outcome", "close_reason",
         "month", "is_win", "news_overlap", "sequence_id"),
        ("position_id", "canonical_symbol", "side", "lots", "magic", "open_time_utc", "close_time_utc",
         "net_profit", "hold_minutes", "outcome"),
    ),
    "sequences": Entity(
        "v_sequences", "start_utc",
        dict(sequence_id="int", type="text", symbol="text", side="text", depth="int", first_lot="num",
             max_lot="num", max_lot_multiplier="num", max_open="int", start_utc="time", end_utc="time",
             net_pnl="num", is_win="bool", news_overlap="bool", drawdown_abs="num", drawdown_pct="num",
             drawdown_time_utc="time", equity_at_start="num", weekday="text", hour="int", session="text",
             month="text", canonical_symbol="text", magic="int", swap="num", commission="num", profit="num",
             total_lots="num", hold_minutes="num"),
        ("type", "symbol", "side", "depth", "is_win", "news_overlap", "weekday", "hour", "session", "month",
         "canonical_symbol", "magic"),
        ("sequence_id", "type", "symbol", "side", "depth", "first_lot", "max_lot", "max_lot_multiplier",
         "start_utc", "end_utc", "net_pnl", "is_win", "drawdown_abs", "drawdown_pct"),
    ),
    "streaks": Entity(
        "streaks", "start_utc",
        dict(streak_id="int", type="text", length="int", start_utc="time", end_utc="time", net_pnl="num"),
        ("type", "length"),
        ("streak_id", "type", "length", "start_utc", "end_utc", "net_pnl"),
    ),
    "exposure": Entity(
        "exposure", "time_utc",
        dict(time_utc="time", symbol="text", long_lots="num", short_lots="num", net_lots="num", gross_lots="num",
             positions_open="int"),
        ("symbol",),
        ("time_utc", "symbol", "long_lots", "short_lots", "net_lots", "gross_lots", "positions_open"),
    ),
    "cash_flows": Entity(
        "v_cash_flows", "time_utc",
        dict(flow_id="int", time_utc="time", type="text", amount="num", month="text"),
        ("type", "month"),
        ("flow_id", "time_utc", "type", "amount"),
    ),
    "drawdown_episodes": Entity(
        "drawdown_episodes", "trough_time_utc",
        dict(episode_id="int", basis="text", peak_time_utc="time", trough_time_utc="time", recovery_time_utc="time",
             peak_value="num", trough_value="num", depth_abs="num", depth_pct="num", duration_days="num",
             trade_count="int", news_loss_share="num", added_capital="num"),
        ("basis",),
        ("episode_id", "basis", "peak_time_utc", "trough_time_utc", "recovery_time_utc", "depth_abs", "depth_pct",
         "duration_days", "trade_count", "news_loss_share"),
    ),
    # minute by minute from the stored price bars (empty without them): the account at any moment
    "equity_curve": Entity(
        "equity_curve", "time_utc",
        dict(time_utc="time", balance="num", floating_pnl="num", equity="num"),
        (),
        ("time_utc", "balance", "floating_pnl", "equity"),
    ),
    "news_events": Entity(
        "news_events", "time_utc",
        dict(value_id="int", event_id="int", time_utc="time", currency="text", importance="text", name="text",
             actual="num", forecast="num", previous="num"),
        ("currency", "importance", "name"),
        ("time_utc", "currency", "importance", "name", "actual", "forecast", "previous"),
    ),
}

# Averages of the stored per-trade price path, computed by the executor over the filtered trades.
FACT_METRICS = {
    "avg_mae": "Mean adverse excursion (worst open loss) of the trades, in money.",
    "avg_mfe": "Mean favourable excursion (best open profit) of the trades, in money.",
    "worst_mae": "The deepest adverse excursion of any single trade.",
    "avg_minutes_in_profit": "Mean minutes a trade spent with the price on its winning side.",
    "avg_minutes_in_loss": "Mean minutes a trade spent with the price on its losing side.",
}
METRIC_DEFINITIONS = {**metrics.DEFINITIONS, **FACT_METRICS}

Op = Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "between"]
_ALIAS = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


class SpecError(ValueError):
    """The spec breaks a whitelist rule; the message says which and how to fix it."""


class Filter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    op: Op
    value: Any


class Aggregation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    func: Literal["count", "sum", "avg", "min", "max"]
    field: str | None = None  # None only for count
    alias: str


class OrderBy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    direction: Literal["asc", "desc"] = "desc"


class TimeRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: date | datetime | None = None  # date first: "2026-09-01" stays a whole day
    end: date | datetime | None = None  # a date means the whole day
    timezone: Literal["utc", "server"] = "server"


class CompareSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=60)
    filters: list[Filter] = []


class QuerySpec(BaseModel):
    """One question, as data. ``kind`` clarify/unsupported carry a message instead of a query."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["query", "clarify", "unsupported"] = "query"
    message: str | None = None  # the clarifying question, or why it is unsupported
    entity: Literal["trades", "sequences", "streaks", "exposure", "cash_flows", "drawdown_episodes", "news_events",
                    "equity_curve"] | None = None
    fields: list[str] = []  # columns to return (default: the entity's usual columns)
    filters: list[Filter] = []
    time_range: TimeRange | None = None
    as_of: datetime | None = None  # point in time, exposure only
    group_by: list[str] = []
    aggregations: list[Aggregation] = []
    metrics: list[str] = []
    compare: list[CompareSet] = []
    order_by: list[OrderBy] = []
    limit: int | None = Field(default=None, ge=1, le=MAX_ROWS)

    @field_validator("fields", "group_by", "metrics")
    @classmethod
    def _no_duplicates(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("lists must not repeat a name")
        return values

    @model_validator(mode="after")
    def _whitelist(self):
        if self.kind != "query":
            if not self.message:
                raise ValueError(f"a {self.kind} spec needs a message")
            return self
        if self.entity is None:
            raise ValueError("a query needs an entity")
        ent = ENTITIES[self.entity]
        known = ", ".join(sorted(ent.fields))

        def check_field(name: str, where: str) -> str:
            if name not in ent.fields:
                raise ValueError(f"{where}: unknown field {name!r} for {self.entity}; known fields: {known}")
            return ent.fields[name]

        for f in self.fields:
            if check_field(f, "fields") == "tag":
                raise ValueError("fields: tags can be filtered on but not returned")
        for f in [*self.filters, *(f for c in self.compare for f in c.filters)]:
            _check_filter(f, check_field(f.field, "filters"))
        for g in self.group_by:
            check_field(g, "group_by")
            if g not in ent.group_by:
                raise ValueError(f"group_by: cannot group {self.entity} by {g!r}; allowed: {', '.join(ent.group_by)}")
        aliases = set()
        for a in self.aggregations:
            if not _ALIAS.match(a.alias) or a.alias in ent.fields:
                raise ValueError(f"aggregations: alias {a.alias!r} must be a new lower_case name")
            if a.func == "count":
                if a.field is not None:
                    check_field(a.field, "aggregations")
            else:
                if a.field is None:
                    raise ValueError(f"aggregations: {a.func} needs a field")
                kind = check_field(a.field, "aggregations")
                # avg of a true/false field is the share that is true (a win rate per basket)
                if kind not in ("num", "int") and not (kind == "bool" and a.func == "avg"):
                    raise ValueError(f"aggregations: {a.func} needs a number field, {a.field!r} is not one")
            aliases.add(a.alias)
        if self.aggregations and self.fields:
            raise ValueError("fields and aggregations cannot be combined: use group_by for the columns")
        for m in self.metrics:
            if m not in METRIC_DEFINITIONS:
                raise ValueError(f"metrics: unknown metric {m!r}; known: {', '.join(sorted(METRIC_DEFINITIONS))}")
        if self.metrics and self.entity != "trades":
            raise ValueError("metrics are computed on trades: set entity to trades")
        if self.metrics and self.aggregations:
            raise ValueError("use either named metrics or aggregations, not both")
        if self.compare:
            if not self.metrics:
                raise ValueError("compare needs at least one metric to compare")
            if self.group_by:
                raise ValueError("compare and group_by cannot be combined")
            if len(self.compare) < 2:
                raise ValueError("compare needs at least two labelled sets")
        if self.as_of is not None and self.entity != "exposure":
            raise ValueError("as_of (a point in time) is only for the exposure entity")
        for o in self.order_by:
            if o.field not in ent.fields and o.field not in aliases and o.field not in self.metrics:
                raise ValueError(f"order_by: {o.field!r} is not a field, an aggregation alias or a metric of this spec")
            if ent.fields.get(o.field) == "tag":
                raise ValueError("order_by: cannot sort by tags")
        return self


def _check_filter(f: Filter, kind: str) -> None:
    if f.op not in OPERATORS[kind]:
        raise ValueError(f"filters: {f.field!r} is a {kind} field; allowed operators: {', '.join(OPERATORS[kind])}")
    if kind == "tag":
        if f.value not in RULES:
            raise ValueError(f"filters: unknown tag {f.value!r}; tags: {', '.join(RULES)}")
        return
    values = f.value if f.op in ("in", "between") else [f.value]
    if f.op in ("in", "between") and not isinstance(f.value, list):
        raise ValueError(f"filters: {f.op} needs a list value for {f.field!r}")
    if f.op == "between" and len(values) != 2:
        raise ValueError(f"filters: between needs exactly two values for {f.field!r}")
    if f.op == "in" and not 1 <= len(values) <= 50:
        raise ValueError(f"filters: in needs 1 to 50 values for {f.field!r}")
    for v in values:
        if kind in ("num", "int") and (isinstance(v, bool) or not isinstance(v, (int, float))):
            raise ValueError(f"filters: {f.field!r} needs a number, got {v!r}")
        if kind == "text" and not isinstance(v, str):
            raise ValueError(f"filters: {f.field!r} needs text, got {v!r}")
        if kind == "bool" and not isinstance(v, bool):
            raise ValueError(f"filters: {f.field!r} needs true or false, got {v!r}")
        if kind == "time":
            try:
                datetime.fromisoformat(str(v))
            except ValueError:
                raise ValueError(f"filters: {f.field!r} needs an ISO time like 2026-09-25T14:00, got {v!r}") from None


def parse_spec(data: dict | str) -> QuerySpec:
    """Validate a spec (a dict, or JSON text). Raises SpecError with every problem listed."""
    try:
        if isinstance(data, str):
            return QuerySpec.model_validate_json(data)
        return QuerySpec.model_validate(data)
    except ValidationError as error:
        problems = []
        for e in error.errors():
            where = ".".join(str(p) for p in e["loc"])
            message = e["msg"].removeprefix("Value error, ")
            problems.append(f"{where}: {message}" if where else message)
        raise SpecError("; ".join(problems)) from None
