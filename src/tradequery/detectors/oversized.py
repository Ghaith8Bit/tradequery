"""oversized: lots per 1,000 of balance far above the account's norm.

Size = lots / (balance at entry / 1,000). The norm is the median size over all
of the account's trades; a trade at ``oversized_multiplier`` times the norm or
more is tagged. Uses the account balance, so it applies across symbols.
"""

from __future__ import annotations

from statistics import median

from ..models import Trade
from ..settings import DetectorSettings
from . import BalanceTimeline, Tag

RULE = "oversized"


def detect(trades: list[Trade], config: DetectorSettings, timeline: BalanceTimeline) -> list[Tag]:
    sizes = {}
    for t in trades:
        balance = timeline.at(t.open_time_utc)
        if balance > 0:
            sizes[t.position_id] = (t, balance, t.lots / (balance / 1000))
    if not sizes:
        return []
    norm = median(s for _, _, s in sizes.values())
    tags = []
    for t, balance, size in sizes.values():
        ratio = size / norm if norm else 0.0
        if ratio >= config.oversized_multiplier - 1e-9:
            tags.append(Tag(t.position_id, RULE,
                            f"{t.lots:g} lots on a {balance:,.2f} balance: {size:.4f} lots per 1,000, "
                            f"{ratio:.1f}x the account median {norm:.4f}"))
    return tags
