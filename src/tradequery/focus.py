"""What a question is about, so the answer leads with it.

"Did news cause my worst drawdown?" and "What was my worst drawdown?" can run the
same query, but the first is about the news column and the second about the
depth. The answer card's big number and the written sentence follow the
question's words, not just the shape of the table. No model call is needed.
"""

from __future__ import annotations

import re

# question words -> the columns that answer them, best first
FOCUS = (
    (r"\bnews\b|\bnfp\b|\bcpi\b|calendar", ("news_loss_share", "news_overlap")),
    (r"how long|duration|\blasted\b|\blast\W*$", ("duration_days", "hold_minutes")),  # not "my last trade"
    (r"recover", ("recovery_time",)),
    (r"how many trades|which trades|trades (?:were )?in", ("trade_count",)),
    (r"\bswaps?\b", ("swap_total", "swap")),
    (r"commission", ("commission_total", "commission")),
    (r"win\W?rate|winning (?:rate|percent|%)|how often (?:do|did) i win", ("win_rate",)),
    (r"profit factor", ("profit_factor",)),
)


def focus_columns(question: str | None) -> tuple[str, ...]:
    """Every column the question's words point at, best first (empty when nothing specific is asked)."""
    text = (question or "").lower()
    found: list[str] = []
    for pattern, columns in FOCUS:
        if re.search(pattern, text):
            found += [c for c in columns if c not in found]
    return tuple(found)


def focus_column(question: str | None, columns) -> str | None:
    """The first column of the result that the question asks about, or None."""
    present = set(columns)
    return next((c for c in focus_columns(question) if c in present), None)


def news_verdict(share: float | None) -> str:
    """One plain sentence on how much of a loss overlapped news. Overlap, never cause."""
    if share is None:
        return "News can't be checked here: no economic calendar is loaded."
    pct = f"{share * 100:.2f}%"
    if share < 0.2:
        return f"Mostly not: only {pct} of the losses in this drawdown came from trades that overlapped news."
    if share < 0.5:
        return f"Partly: {pct} of the losses in this drawdown came from trades that overlapped news."
    return (f"Much of it overlapped news: {pct} of the losses in this drawdown came from trades during news "
            "(an overlap, not proof that news caused it).")
