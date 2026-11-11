"""What an answer is about, said plainly: the level (account, baskets, single trades), and one-click switches.

Every answer carries a short line of labels ("Baskets · 2026-09 · 12 found"),
so it is always clear whether trades or baskets were counted and which
drawdown was used. Switches re-ask the same question at another level: flipping
a drawdown between equity and balance, or opening the trades of one basket or
one drawdown, is a hand-made spec (no model call); "the same per basket" goes
back to the model as a follow-up.
"""

from __future__ import annotations

from .examples import trades_in, with_drawdown_basis
from .execute import Result
from .spec import QuerySpec

LEVELS = {
    "trades": "Single trades",
    "sequences": "Baskets",
    "streaks": "Streaks of trades",
    "drawdown_episodes": "Account drawdown",
    "exposure": "Lots open",
    "cash_flows": "Deposits and withdrawals",
    "news_events": "News events",
}

# Groups for the suggested questions on the start screen.
GROUPS = {"drawdown_episodes": "Account", "cash_flows": "Account", "exposure": "Account",
          "sequences": "Baskets", "trades": "Trades", "streaks": "Trades", "news_events": "Account"}

DRILL_DOWN_FIELDS = ("sequence_id", "drawdown_episode_id", "equity_drawdown_episode_id", "streak_id", "position_id")


def basis_of(spec: QuerySpec) -> str | None:
    return next((f.value for f in spec.filters if f.field == "basis" and f.op == "eq"), None)


def level_label(spec: QuerySpec) -> str:
    """"Baskets", "Single trades", "Account drawdown (equity)" ..."""
    label = LEVELS.get(spec.entity or "", spec.entity or "")
    if spec.entity == "drawdown_episodes":
        basis = basis_of(spec)
        label += f" ({basis})" if basis in ("equity", "balance") else " (balance and equity)"
    return label


def level_meaning(spec: QuerySpec) -> str:
    """The level with what it means, for the explanation call."""
    label = level_label(spec)
    return {
        "Baskets": "Baskets (martingale or grid series of positions, each row one basket)",
        "Single trades": "Single trades (each row or count is one closed position)",
        "Account drawdown (equity)": "Account drawdown (equity: open positions counted)",
        "Account drawdown (balance)": "Account drawdown (balance: closed trades only)",
    }.get(label, label)


def _period(spec: QuerySpec) -> str | None:
    tr = spec.time_range
    if not tr or not (tr.start or tr.end):
        return None
    start, end = (str(v)[:16] if v else None for v in (tr.start, tr.end))
    return f"{start or 'start'} to {end or 'now'}" if start != end else start


def describe(spec: QuerySpec | None, result: Result | None, *, has_equity: bool, has_baskets: bool,
             model_ready: bool = True, question: str = "") -> dict | None:
    """Labels and switches for one answer (None for clarify/unsupported answers)."""
    if spec is None or spec.kind != "query" or result is None:
        return None
    labels = [level_label(spec)]
    period = _period(spec)
    if period:
        labels.append(period)
    if spec.group_by:
        labels.append("by " + ", ".join(g.replace("_", " ") for g in spec.group_by))
    noun = {"sequences": "basket", "trades": "trade", "drawdown_episodes": "drawdown"}.get(spec.entity or "")
    if noun and not spec.group_by and not spec.metrics and not spec.aggregations:
        n = len(result.rows)
        labels.append(f"{n} {noun}{'' if n == 1 else 's'} found")

    switches = []
    filters = {f.field for f in spec.filters}
    one = result.rows[0] if len(result.rows) == 1 else None
    col = {c: i for i, c in enumerate(result.columns)}
    data = spec.model_dump(mode="json", exclude_defaults=True)

    if spec.entity == "drawdown_episodes":
        basis = basis_of(spec)
        if one is not None and "episode_id" in col:
            field = "equity_drawdown_episode_id" if basis == "equity" else "drawdown_episode_id"
            switches.append({"label": "Show the trades in this drawdown", "kind": "spec",
                             "question": "Which trades were in this drawdown?",
                             "spec": trades_in(field, one[col["episode_id"]])})
        if has_equity and basis in ("equity", "balance"):
            other = "balance" if basis == "equity" else "equity"
            switches.append({"label": f"Show the {other} drawdown instead", "kind": "spec",
                             "question": f"The same, as {other} drawdown",
                             "spec": with_drawdown_basis(data, other)})
    elif spec.entity == "sequences":
        if one is not None and "sequence_id" in col:
            switches.append({"label": "Show the trades in this basket", "kind": "spec",
                             "question": "Which trades were in this basket?",
                             "spec": trades_in("sequence_id", one[col["sequence_id"]])})
        if model_ready:
            switches.append({"label": "Ask the same for single trades", "kind": "ask",
                             "question": _again(question, "for single trades instead of baskets")})
    elif spec.entity == "trades" and has_baskets and model_ready and not filters & set(DRILL_DOWN_FIELDS):
        switches.append({"label": "Ask the same per basket", "kind": "ask",
                         "question": _again(question, "per basket instead of single trades")})
    return {"level": level_label(spec), "labels": labels, "switches": switches}


def _again(question: str, how: str) -> str:
    """The question asked again at another level; it names the question, so it works without the chat history."""
    question = question.strip().rstrip("?.!")
    if not question or question.startswith("("):
        return f"The same question, but {how}."
    return f'"{question}", but {how}.'


def account_levels(conn) -> dict:
    """What this account can answer: equity drawdowns (price bars) and baskets."""
    has_equity = conn.execute("SELECT 1 FROM drawdown_episodes WHERE basis = 'equity' LIMIT 1").fetchone() is not None
    has_baskets = conn.execute("SELECT 1 FROM sequences LIMIT 1").fetchone() is not None
    basket_dd = conn.execute("SELECT 1 FROM sequence_facts LIMIT 1").fetchone() is not None
    return {"has_equity": has_equity, "has_baskets": has_baskets, "has_basket_drawdown": basket_dd}
