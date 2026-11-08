"""no_stop_loss: a position opened without a stop loss.

The stop loss at entry comes from the opening order (the live sync, or the
report's Orders section). A stop added later does not count: the risk at entry
was unlimited.
"""

from __future__ import annotations

from ..models import Trade
from . import Tag

RULE = "no_stop_loss"


def detect(trades: list[Trade]) -> list[Tag]:
    return [Tag(t.position_id, RULE, "no stop loss at entry") for t in trades if not t.sl]
