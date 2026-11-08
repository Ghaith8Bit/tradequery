from dataclasses import replace
from datetime import datetime, timezone

import pytest

from tradequery.settings import load_settings
from tradequery.trades import balance_curve, derive_trades, session_label
from tradequery.models import CashFlow

UTC = timezone.utc
SETTINGS = load_settings()


def t(hour, minute=0, day=15):
    return datetime(2026, 7, day, hour, minute, tzinfo=UTC)


def test_net_profit_and_hold_minutes(make_trade):
    trade = make_trade(profit=100.0, commission=-3.5, swap=-2.0, open_time_utc=t(9), close_time_utc=t(11, 30))
    [derived] = derive_trades([trade], [], SETTINGS)
    assert derived.net_profit == 94.5
    assert derived.hold_minutes == 150.0


def test_weekday_and_hour_use_broker_server_time(make_trade):
    summer = make_trade(position_id=1, open_time_utc=datetime(2026, 7, 15, 9, 0, tzinfo=UTC),
                        close_time_utc=datetime(2026, 7, 15, 10, 0, tzinfo=UTC))
    late = make_trade(position_id=2, open_time_utc=datetime(2026, 7, 15, 22, 0, tzinfo=UTC),
                      close_time_utc=datetime(2026, 7, 15, 23, 0, tzinfo=UTC))
    winter = make_trade(position_id=3, open_time_utc=datetime(2026, 1, 15, 10, 0, tzinfo=UTC),
                        close_time_utc=datetime(2026, 1, 15, 11, 0, tzinfo=UTC))
    by_id = {d.position_id: d for d in derive_trades([summer, late, winter], [], SETTINGS)}
    assert (by_id[1].weekday, by_id[1].hour) == ("Wed", 12)  # 09:00 UTC = 12:00 server (UTC+3)
    assert (by_id[2].weekday, by_id[2].hour) == ("Thu", 1)  # 22:00 UTC Wed = 01:00 server Thu
    assert (by_id[3].weekday, by_id[3].hour) == ("Thu", 12)  # 10:00 UTC = 12:00 server (UTC+2)


@pytest.mark.parametrize(
    "hour,expected",
    [(3, "asia"), (7, "asia_london"), (9, "london"), (12, "london_new_york"), (18, "new_york"), (22, "off_hours")],
)
def test_session_label(hour, expected):
    assert session_label(hour, SETTINGS.sessions) == expected


def test_session_is_derived_from_utc_open_hour(make_trade):
    trade = make_trade(open_time_utc=t(9), close_time_utc=t(10))
    [derived] = derive_trades([trade], [], SETTINGS)
    assert derived.session == "london"


@pytest.mark.parametrize(
    "tolerance,net,expected",
    [
        (0.0, 0.0, "breakeven"),
        (0.0, 0.01, "win"),
        (0.0, -0.01, "loss"),
        (0.5, 0.4, "breakeven"),
        (0.5, -0.4, "breakeven"),
        (0.5, 0.5, "breakeven"),
        (0.5, 0.6, "win"),
        (0.5, -0.6, "loss"),
    ],
)
def test_outcome_uses_break_even_tolerance(make_trade, tolerance, net, expected):
    settings = replace(SETTINGS, break_even_tolerance=tolerance)
    [derived] = derive_trades([make_trade(profit=net)], [], settings)
    assert derived.outcome == expected
    assert derived.is_win == (expected == "win")


def test_balance_curve_with_deposit_withdrawal_and_credit(make_trade):
    trade_a = make_trade(position_id=1, profit=50.0, commission=-2.0, swap=-1.0, close_time_utc=t(10))
    trade_b = make_trade(position_id=2, profit=-30.0, commission=-1.0, close_time_utc=t(12))
    flows = [
        CashFlow(100, t(8), "deposit", 1000.0),
        CashFlow(101, t(11), "withdrawal", -200.0),
        CashFlow(102, t(13), "credit", 500.0),  # credit does not change the balance
    ]
    points = balance_curve([trade_a, trade_b], flows)
    assert [(p.kind, p.balance) for p in points] == [
        ("deposit", 1000.0),
        ("trade", 1047.0),
        ("withdrawal", 847.0),
        ("trade", 816.0),
    ]
    derived = derive_trades([trade_a, trade_b], flows, SETTINGS)
    assert [d.balance_after for d in derived] == [1047.0, 816.0]


def test_cash_flow_is_applied_before_a_trade_at_the_same_instant(make_trade):
    trade = make_trade(profit=10.0, close_time_utc=t(10))
    flows = [CashFlow(1, t(10), "deposit", 100.0)]
    [derived] = derive_trades([trade], flows, SETTINGS)
    assert derived.balance_after == 110.0


def test_initial_balance(make_trade):
    [derived] = derive_trades([make_trade(profit=10.0)], [], SETTINGS, initial_balance=500.0)
    assert derived.balance_after == 510.0


def test_output_is_sorted_and_inputs_are_not_changed(make_trade):
    late = make_trade(position_id=1, close_time_utc=t(15))
    early = make_trade(position_id=2, close_time_utc=t(10))
    derived = derive_trades([late, early], [], SETTINGS)
    assert [d.position_id for d in derived] == [2, 1]
    assert late.balance_after is None and early.net_profit is None
