"""The build plan's example questions as hand-written QuerySpecs.

Phase 8's check is that these return correct answers; the LLM layer reuses
them as worked examples. Questions that need two steps ("...and which trades
were in it?") have a follow-up spec that filters trades by the id found first.
"""

from __future__ import annotations

WORST_DRAWDOWN = {
    "entity": "drawdown_episodes",
    "filters": [{"field": "basis", "op": "eq", "value": "balance"}],
    "fields": ["episode_id", "peak_time_utc", "trough_time_utc", "recovery_time_utc", "depth_abs", "depth_pct",
               "trade_count", "news_loss_share"],
    "order_by": [{"field": "depth_abs", "direction": "desc"}],
    "limit": 1,
}

LONGEST_LOSING_STREAK = {
    "entity": "streaks",
    "filters": [{"field": "type", "op": "eq", "value": "loss"}],
    "order_by": [{"field": "length", "direction": "desc"}],
    "limit": 1,
}


def with_drawdown_basis(spec: dict, basis: str) -> dict:
    """A drawdown spec on another basis ("balance" or "equity"); other specs come back unchanged.

    The presets are written on the balance basis, which every account has; an
    account with price bars also has equity drawdowns, and those are its default.
    """
    if spec.get("entity") != "drawdown_episodes":
        return spec
    filters = [dict(f, value=basis) if f.get("field") == "basis" else f for f in spec.get("filters", [])]
    return dict(spec, filters=filters)


def trades_in(field: str, value: int) -> dict:
    """Follow-up: the trades of the drawdown episode, streak or sequence found by the first spec."""
    return {"entity": "trades", "filters": [{"field": field, "op": "eq", "value": value}],
            "order_by": [{"field": "close_time_utc", "direction": "asc"}], "limit": 500}


EXAMPLES: list[tuple[str, dict]] = [
    ("What was my worst drawdown period and which trades caused it?", WORST_DRAWDOWN),
    ("Compare my win rate on Fridays versus other days.", {
        "entity": "trades",
        "metrics": ["win_rate", "net_profit"],
        "compare": [
            {"label": "Fridays", "filters": [{"field": "weekday", "op": "eq", "value": "Fri"}]},
            {"label": "Other days", "filters": [{"field": "weekday", "op": "ne", "value": "Fri"}]},
        ],
    }),
    ("Which hour of the day loses me the most money on XAUUSD?", {
        "entity": "trades",
        "filters": [{"field": "canonical_symbol", "op": "eq", "value": "XAUUSD"}],
        "group_by": ["hour"],
        "aggregations": [{"func": "sum", "field": "net_profit", "alias": "net"},
                         {"func": "count", "alias": "trades"}],
        "order_by": [{"field": "net", "direction": "asc"}],
        "limit": 1,
    }),
    ("Show trades held longer than 4 hours that lost over $50.", {
        "entity": "trades",
        "filters": [{"field": "hold_minutes", "op": "gt", "value": 240},
                    {"field": "net_profit", "op": "lt", "value": -50}],
        "order_by": [{"field": "net_profit", "direction": "asc"}],
    }),
    ("How many lots did I have open at once at the peak, and when?", {
        "entity": "exposure",
        "filters": [{"field": "symbol", "op": "eq", "value": "ALL"}],
        "order_by": [{"field": "gross_lots", "direction": "desc"}, {"field": "time_utc", "direction": "asc"}],
        "limit": 1,
    }),
    ("How much did I deposit and withdraw each month?", {
        "entity": "cash_flows",
        "filters": [{"field": "type", "op": "in", "value": ["deposit", "withdrawal"]}],
        "group_by": ["month", "type"],
        "aggregations": [{"func": "sum", "field": "amount", "alias": "total"},
                         {"func": "count", "alias": "count"}],
        "order_by": [{"field": "month", "direction": "asc"}],
    }),
    ("Show my results by magic number.", {
        "entity": "trades",
        "group_by": ["magic"],
        "metrics": ["trade_count", "win_rate", "net_profit", "profit_factor"],
    }),
    ("What is my total swap by symbol?", {
        "entity": "trades",
        "group_by": ["canonical_symbol"],
        "aggregations": [{"func": "sum", "field": "swap", "alias": "swap_total"}],
        "order_by": [{"field": "swap_total", "direction": "asc"}],
    }),
    ("Which weekday costs me the most swap?", {
        "entity": "trades",
        "group_by": ["weekday"],
        "aggregations": [{"func": "sum", "field": "swap", "alias": "swap_total"}],
        "order_by": [{"field": "swap_total", "direction": "asc"}],
        "limit": 1,
    }),
    ("What is my average R, and how does planned risk-reward compare with what I got?", {
        "entity": "trades",
        "metrics": ["avg_r", "planned_rr", "r_excluded", "trade_count"],
    }),
    ("What is my longest losing streak, and which trades were in it?", LONGEST_LOSING_STREAK),
    ("Show my revenge trades.", {
        "entity": "trades",
        "filters": [{"field": "tags", "op": "eq", "value": "revenge_trade"}],
        "order_by": [{"field": "open_time_utc", "direction": "asc"}],
    }),
    ("Which winning baskets went more than 20% down before closing in profit?", {
        "entity": "sequences",
        "filters": [{"field": "is_win", "op": "eq", "value": True},
                    {"field": "drawdown_pct", "op": "gte", "value": 0.2}],
        "order_by": [{"field": "drawdown_pct", "direction": "desc"}],
    }),
    ("What is my win rate per basket, and how deep do baskets go on average?", {
        "entity": "sequences",
        "aggregations": [{"func": "count", "alias": "baskets"},
                         {"func": "avg", "field": "is_win", "alias": "win_rate"},
                         {"func": "avg", "field": "drawdown_pct", "alias": "avg_drawdown_pct"},
                         {"func": "max", "field": "drawdown_pct", "alias": "max_drawdown_pct"}],
    }),
    ("Show winning martingale baskets during news.", {
        "entity": "sequences",
        "filters": [{"field": "type", "op": "in", "value": ["martingale", "martingale_grid"]},
                    {"field": "is_win", "op": "eq", "value": True},
                    {"field": "news_overlap", "op": "eq", "value": True}],
        "order_by": [{"field": "depth", "direction": "desc"}],
    }),
    ("Did news cause my worst drawdown?", WORST_DRAWDOWN),
]
