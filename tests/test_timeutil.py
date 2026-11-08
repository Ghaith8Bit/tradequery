import calendar
from datetime import datetime, timezone

import pytest

from tradequery.timeutil import ServerClock, TimeResolutionError, to_local

UTC = timezone.utc
CLOCK = ServerClock()  # New York close: server = New York + 7h


def utc(*args):
    return datetime(*args, tzinfo=UTC)


# --- ordinary winter and summer days -----------------------------------------


def test_winter_server_is_utc_plus_2():
    assert CLOCK.to_utc(datetime(2026, 1, 15, 12, 0)) == utc(2026, 1, 15, 10, 0)


def test_summer_server_is_utc_plus_3():
    assert CLOCK.to_utc(datetime(2026, 7, 15, 12, 0)) == utc(2026, 7, 15, 9, 0)


# --- US daylight saving starts Sunday 2026-03-08 -----------------------------


def test_friday_before_spring_switch_is_plus_2():
    assert CLOCK.to_utc(datetime(2026, 3, 6, 12, 0)) == utc(2026, 3, 6, 10, 0)


def test_monday_after_spring_switch_is_plus_3():
    assert CLOCK.to_utc(datetime(2026, 3, 9, 12, 0)) == utc(2026, 3, 9, 9, 0)


def test_spring_switch_boundary():
    # Server clock jumps from 08:59 to 10:00 while UTC advances one minute.
    assert CLOCK.to_utc(datetime(2026, 3, 8, 8, 59)) == utc(2026, 3, 8, 6, 59)
    assert CLOCK.to_utc(datetime(2026, 3, 8, 10, 0)) == utc(2026, 3, 8, 7, 0)


def test_skipped_hour_is_nonexistent():
    skipped = datetime(2026, 3, 8, 9, 30)
    assert CLOCK.state(skipped) == "nonexistent"
    with pytest.raises(TimeResolutionError):
        CLOCK.to_utc(skipped, strict=True)


# --- US daylight saving ends Sunday 2026-11-01 -------------------------------


def test_friday_before_fall_switch_is_plus_3():
    assert CLOCK.to_utc(datetime(2026, 10, 30, 12, 0)) == utc(2026, 10, 30, 9, 0)


def test_monday_after_fall_switch_is_plus_2():
    assert CLOCK.to_utc(datetime(2026, 11, 2, 12, 0)) == utc(2026, 11, 2, 10, 0)


def test_repeated_hour_is_ambiguous_and_fold_picks_one():
    repeated = datetime(2026, 11, 1, 8, 30)
    assert CLOCK.state(repeated) == "ambiguous"
    assert CLOCK.to_utc(repeated, fold=0) == utc(2026, 11, 1, 5, 30)
    assert CLOCK.to_utc(repeated, fold=1) == utc(2026, 11, 1, 6, 30)
    with pytest.raises(TimeResolutionError):
        CLOCK.to_utc(repeated, strict=True)


# --- round trips, epoch values, local time, fixed offset ---------------------


@pytest.mark.parametrize(
    "server_time",
    [
        datetime(2026, 1, 15, 12, 0),
        datetime(2026, 3, 6, 23, 59),
        datetime(2026, 3, 9, 0, 0),
        datetime(2026, 7, 15, 12, 0),
        datetime(2026, 10, 30, 23, 59),
        datetime(2026, 11, 2, 0, 0),
    ],
)
def test_round_trip(server_time):
    assert CLOCK.from_utc(CLOCK.to_utc(server_time, strict=True)) == server_time


def test_mt5_epoch_is_read_as_server_time():
    naive = datetime(2026, 7, 15, 12, 0)
    epoch = calendar.timegm(naive.timetuple())
    assert CLOCK.from_mt5_epoch(epoch) == utc(2026, 7, 15, 9, 0)


def test_local_time_damascus_is_plus_3():
    assert to_local(utc(2026, 7, 15, 9, 0), "Asia/Damascus").hour == 12
    assert to_local(utc(2026, 1, 15, 9, 0), "Asia/Damascus").hour == 12


def test_fixed_offset_clock_ignores_daylight_saving():
    clock = ServerClock(kind="fixed", fixed_offset_hours=3)
    assert clock.to_utc(datetime(2026, 1, 15, 12, 0)) == utc(2026, 1, 15, 9, 0)
    assert clock.to_utc(datetime(2026, 7, 15, 12, 0)) == utc(2026, 7, 15, 9, 0)
    assert clock.state(datetime(2026, 3, 8, 9, 30)) == "normal"


def test_rejects_aware_input_and_unknown_kind():
    with pytest.raises(ValueError):
        CLOCK.to_utc(utc(2026, 1, 15, 12, 0))
    with pytest.raises(ValueError):
        CLOCK.from_utc(datetime(2026, 1, 15, 12, 0))
    with pytest.raises(ValueError):
        ServerClock(kind="weird")
