"""revenge_trade: a manual trade opened soon after a losing close, with larger lots.

Opened within ``revenge_window_minutes`` of the previous close, that close was a
loss, and the lots are at least ``revenge_lot_multiplier`` times the median lots
of the previous ``revenge_median_lookback`` manual trades. Needs ``min_history``
earlier trades to have a median at all.
"""

from __future__ import annotations

from bisect import bisect_right
from statistics import median

from ..models import Trade
from ..settings import DetectorSettings
from . import Tag, is_automated, net_of

RULE = "revenge_trade"


def detect(trades: list[Trade], config: DetectorSettings) -> list[Tag]:
    manual = sorted((t for t in trades if not is_automated(t, config)), key=lambda t: (t.open_time_utc, t.position_id))
    closes = sorted(manual, key=lambda t: (t.close_time_utc, t.position_id))
    close_times = [t.close_time_utc for t in closes]
    tags = []
    for i, t in enumerate(manual):
        history = manual[max(0, i - config.revenge_median_lookback):i]
        if len(history) < config.min_history:
            continue
        j = bisect_right(close_times, t.open_time_utc)
        if not j:
            continue
        previous = closes[j - 1]  # the last manual position closed before this one opened
        gap_minutes = (t.open_time_utc - previous.close_time_utc).total_seconds() / 60
        if previous.outcome != "loss" or gap_minutes > config.revenge_window_minutes:
            continue
        typical = median(h.lots for h in history)
        ratio = t.lots / typical if typical else 0.0
        if ratio >= config.revenge_lot_multiplier - 1e-9:
            tags.append(Tag(t.position_id, RULE,
                            f"opened {gap_minutes:.0f} min after a {net_of(previous):,.2f} loss "
                            f"at {ratio:.1f}x the median lots ({t.lots:g} vs {typical:g})"))
    return tags
