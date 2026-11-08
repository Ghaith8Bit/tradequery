"""overtrading: manual trades opened after the day's count passes its norm.

Days are broker-server days (the forex day). The norm is the mean plus
``overtrading_sd_multiplier`` standard deviations of the daily trade counts over
the trading days in the previous ``overtrading_lookback_days`` calendar days,
and needs ``min_history`` such days. Every trade beyond the norm is tagged.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import timedelta
from statistics import mean, pstdev

from ..models import Trade
from ..settings import DetectorSettings
from ..timeutil import ServerClock
from . import Tag, is_automated

RULE = "overtrading"


def detect(trades: list[Trade], config: DetectorSettings, clock: ServerClock) -> list[Tag]:
    by_day: dict = defaultdict(list)
    for t in trades:
        if not is_automated(t, config):
            by_day[clock.from_utc(t.open_time_utc).date()].append(t)
    tags = []
    for day in sorted(by_day):
        window_start = day - timedelta(days=config.overtrading_lookback_days)
        counts = [len(by_day[d]) for d in by_day if window_start <= d < day]
        if len(counts) < config.min_history:
            continue
        avg, sd = mean(counts), pstdev(counts)
        norm = avg + config.overtrading_sd_multiplier * sd
        allowed = math.floor(norm + 1e-9)
        todays = sorted(by_day[day], key=lambda t: (t.open_time_utc, t.position_id))
        for n, t in enumerate(todays, start=1):
            if n > allowed:
                tags.append(Tag(t.position_id, RULE,
                                f"trade {n} of the day; the norm over the last {config.overtrading_lookback_days} days "
                                f"is {norm:.1f} (mean {avg:.1f} + {config.overtrading_sd_multiplier:g} sd)"))
    return tags
