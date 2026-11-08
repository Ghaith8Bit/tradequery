"""Detectors: for every rule, a case that must trigger, one that must not, and one at the threshold.

Default thresholds (settings.toml): revenge within 15 min at 1.5x the median of the
last 20 trades; overtrading above mean + 2 sd of the last 30 days; oversized at 2x
the account median lots per 1,000; at least 5 trades / trading days of history.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from tradequery import db, detectors
from tradequery.detectors import BalanceTimeline, no_stop_loss, overtrading, oversized, revenge
from tradequery.models import CashFlow, Trade
from tradequery.settings import DetectorSettings, load_settings
from tradequery.timeutil import ServerClock

UTC = timezone.utc
DAY1 = datetime(2026, 9, 7, 8, 0, tzinfo=UTC)  # a Monday
CONFIG = DetectorSettings()
CLOCK = ServerClock("fixed", fixed_offset_hours=0)


def trade(pid, opened, minutes=30, lots=0.1, net=10.0, magic=0, sl=0.0, balance=1000.0):
    return Trade(
        position_id=pid, symbol="XAUUSD", side="buy", lots=lots, magic=magic, comment="",
        open_time_utc=opened, close_time_utc=opened + timedelta(minutes=minutes), open_price=2000.0,
        close_price=2000.0, sl=sl, tp=0.0, close_reason="", commission=0.0, swap=0.0, profit=net,
        net_profit=net, hold_minutes=float(minutes), weekday="Mon", hour=opened.hour, session="london",
        outcome="win" if net > 0 else "loss" if net < 0 else "breakeven", is_win=net > 0, balance_after=balance,
    )


# ---------------------------------------------------------------- revenge_trade


def revenge_setup(gap=10, lots=0.2, previous_net=-50.0, magic=0, history=5):
    """`history` winning 0.1-lot trades, then a 0.1-lot trade closing at 12:00 with `previous_net`,
    then the candidate opened `gap` minutes later."""
    trades = [trade(i, DAY1 + timedelta(hours=i)) for i in range(1, history + 1)]
    trades.append(trade(50, DAY1 + timedelta(hours=3, minutes=30) + timedelta(hours=history), net=previous_net))
    loss_close = trades[-1].close_time_utc
    trades.append(trade(99, loss_close + timedelta(minutes=gap), lots=lots, magic=magic))
    return trades


def test_revenge_triggers_soon_after_a_loss_with_bigger_lots():
    (tag,) = revenge.detect(revenge_setup(), CONFIG)
    assert tag.position_id == 99 and tag.rule == "revenge_trade"
    assert tag.evidence == "opened 10 min after a -50.00 loss at 2.0x the median lots (0.2 vs 0.1)"


@pytest.mark.parametrize("case", [
    dict(gap=20),                 # too late
    dict(previous_net=30.0),      # the previous close was a win
    dict(lots=0.12),              # only 1.2x the median
    dict(magic=12345),            # an EA trade: behavioural rules skip it
    dict(history=3),              # not enough history for a median
])
def test_revenge_does_not_trigger(case):
    assert revenge.detect(revenge_setup(**case), CONFIG) == []


def test_revenge_at_the_threshold():
    (tag,) = revenge.detect(revenge_setup(gap=15, lots=0.15), CONFIG)  # exactly 15 min, exactly 1.5x
    assert "15 min" in tag.evidence and "1.5x" in tag.evidence


def test_listed_magic_numbers_count_as_manual():
    trades = [replace(t, magic=7) for t in revenge_setup()]
    assert revenge.detect(trades, CONFIG) == []
    assert len(revenge.detect(trades, replace(CONFIG, manual_magics=(7,)))) == 1


# ---------------------------------------------------------------- overtrading


def day_of_trades(day_index, count, start_pid, magic=0):
    day = DAY1 + timedelta(days=day_index)
    return [trade(start_pid + n, day + timedelta(minutes=40 * n), magic=magic) for n in range(count)]


def history_days(counts):
    trades, pid = [], 1
    for i, count in enumerate(counts):
        trades += day_of_trades(i, count, pid)
        pid += 100
    return trades


def test_overtrading_tags_trades_beyond_the_norm():
    # five days of 2 trades: mean 2, sd 0, norm 2 -> trades 3 and 4 of the sixth day
    trades = history_days([2, 2, 2, 2, 2]) + day_of_trades(5, 4, 900)
    tags = overtrading.detect(trades, CONFIG, CLOCK)
    assert [t.position_id for t in tags] == [902, 903]
    assert tags[0].evidence == "trade 3 of the day; the norm over the last 30 days is 2.0 (mean 2.0 + 2 sd)"


def test_overtrading_at_the_threshold_and_with_spread():
    assert overtrading.detect(history_days([2, 2, 2, 2, 2]) + day_of_trades(5, 2, 900), CONFIG, CLOCK) == []
    # counts 1,2,3,2,2: mean 2, sd 0.632, norm 3.26 -> only the 4th trade
    tags = overtrading.detect(history_days([1, 2, 3, 2, 2]) + day_of_trades(5, 4, 900), CONFIG, CLOCK)
    assert [t.position_id for t in tags] == [903]


def test_overtrading_needs_recent_history_and_skips_eas():
    old = history_days([2, 2, 2, 2, 2])
    late_day = day_of_trades(45, 6, 900)  # 40+ days after the history: outside the 30-day window
    assert overtrading.detect(old + late_day, CONFIG, CLOCK) == []
    ea_day = day_of_trades(5, 6, 900, magic=12345)
    assert overtrading.detect(old + ea_day, CONFIG, CLOCK) == []


# ---------------------------------------------------------------- no_stop_loss


def test_no_stop_loss():
    trades = [trade(1, DAY1, sl=0.0), trade(2, DAY1, sl=1990.0), trade(3, DAY1, sl=0.0, magic=12345)]
    tags = no_stop_loss.detect(trades)
    assert [(t.position_id, t.evidence) for t in tags] == [(1, "no stop loss at entry"), (3, "no stop loss at entry")]


# ---------------------------------------------------------------- oversized


def sized(lots_list, magic=0):
    # one after another, every trade break-even, so the balance stays 1,000
    return [trade(i, DAY1 + timedelta(hours=i), lots=lots, net=0.0, magic=magic) for i, lots in enumerate(lots_list, 1)]


def test_oversized_trigger_threshold_and_no_trigger():
    trades = sized([0.1, 0.1, 0.1, 0.1, 0.2, 0.19, 0.5])  # median 0.1 lots per 1,000
    tags = oversized.detect(trades, CONFIG, BalanceTimeline(trades))
    assert [t.position_id for t in tags] == [5, 7]  # 2.0x (the threshold) and 5.0x; 1.9x is not
    assert tags[1].evidence == "0.5 lots on a 1,000.00 balance: 0.5000 lots per 1,000, 5.0x the account median 0.1000"


def test_oversized_applies_to_ea_trades_and_uses_the_balance_at_entry():
    trades = sized([0.1, 0.1, 0.1, 0.1, 0.2], magic=12345)
    deposit = [CashFlow(1, trades[-1].open_time_utc - timedelta(minutes=1), "deposit", 1000.0)]
    assert [t.position_id for t in oversized.detect(trades, CONFIG, BalanceTimeline(trades))] == [5]
    # with 2,000 in the account the same 0.2 lots is only the median size again
    assert oversized.detect(trades, CONFIG, BalanceTimeline(trades, deposit)) == []


def test_balance_timeline():
    trades = [trade(1, DAY1, net=100.0, balance=1100.0), trade(2, DAY1 + timedelta(hours=2), net=-50.0, balance=550.0)]
    withdrawal = [CashFlow(1, DAY1 + timedelta(hours=1), "withdrawal", -500.0)]
    line = BalanceTimeline(trades, withdrawal)
    assert line.at(DAY1 - timedelta(hours=1)) == 1000.0
    assert line.at(DAY1 + timedelta(minutes=45)) == 1100.0
    assert line.at(DAY1 + timedelta(hours=1, minutes=30)) == 600.0
    assert line.at(DAY1 + timedelta(hours=3)) == 550.0


# ---------------------------------------------------------------- all together, stored


def test_tags_are_stored_on_every_rebuild(tmp_path):
    from tradequery.synthetic import generate_account
    conn = db.connect(db.db_path(1, tmp_path))
    with conn:
        db.upsert_deals(conn, generate_account().deals)
        trades, _ = db.rebuild_derived(conn, load_settings())
    tags = db.load_tags(conn)
    assert tags and {t.rule for t in tags} <= set(detectors.RULES)
    no_sl = {t.position_id for t in db.load_tags(conn, "no_stop_loss")}
    assert no_sl == {t.position_id for t in trades if not t.sl}
    with conn:
        db.rebuild_derived(conn, load_settings())  # rebuilding replaces, never duplicates
    assert len(db.load_tags(conn)) == len(tags)
    conn.close()


def test_detector_settings_load_from_the_file(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('[time]\nserver_clock = "fixed"\n[detectors]\nrevenge_window_minutes = 30\nmanual_magics = [5, 6]\n')
    config = load_settings(path).detectors
    assert config.revenge_window_minutes == 30.0 and config.manual_magics == (5, 6)
    assert config.oversized_multiplier == 2.0  # untouched values keep their defaults
