from datetime import datetime, timezone

import pytest

from tradequery.settings import load_settings
from tradequery.trades import balance_curve, derive_trades
from tradequery.synthetic import EA_MAGIC, generate_account, swap_for
from tradequery.trades import build_trades, extract_cash_flows
from tradequery.timeutil import ServerClock

UTC = timezone.utc
SETTINGS = load_settings()


@pytest.fixture(scope="module")
def account():
    return generate_account()


@pytest.fixture(scope="module")
def flows(account):
    return extract_cash_flows(account.deals)


@pytest.fixture(scope="module")
def trades(account, flows):
    return derive_trades(build_trades(account.deals), flows, SETTINGS)


def longest_run(outcomes, which):
    best = run = 0
    for outcome in outcomes:
        run = run + 1 if outcome == which else 0
        best = max(best, run)
    return best


def test_same_seed_gives_same_account_and_other_seed_differs():
    assert generate_account(seed=3) == generate_account(seed=3)
    assert generate_account(seed=3) != generate_account(seed=4)


def test_every_position_closes_into_a_trade(account, trades):
    assert len(trades) == account.planted["trade_count"]


def test_cash_flows_match_the_plan(account, flows):
    deposits = [f.amount for f in flows if f.type == "deposit"]
    withdrawals = [-f.amount for f in flows if f.type == "withdrawal"]
    assert deposits == account.planted["deposits"]
    assert withdrawals == account.planted["withdrawals"]


def test_final_balance_equals_an_independent_sum(account, trades, flows):
    expected = sum(d.profit + d.commission + d.swap for d in account.deals)
    assert balance_curve(trades, flows)[-1].balance == pytest.approx(expected, abs=0.01)
    assert trades[-1].balance_after == pytest.approx(expected, abs=0.01)


def test_longest_streaks_match_the_planted_runs(account, trades):
    outcomes = [trade.outcome for trade in trades]
    assert set(outcomes) <= {"win", "loss"}
    assert longest_run(outcomes, "loss") == account.planted["max_loss_streak"]
    assert longest_run(outcomes, "win") == account.planted["max_win_streak"]


def test_martingale_lots_double_and_the_last_trade_wins(account, trades):
    sequence = sorted(
        (trade for trade in trades if trade.comment.startswith("martingale-")),
        key=lambda trade: trade.comment,
    )
    assert [trade.lots for trade in sequence] == account.planted["martingale_lots"]
    assert all(trade.magic == EA_MAGIC and trade.sl == 0.0 for trade in sequence)
    assert [trade.outcome for trade in sequence] == ["loss"] * 4 + ["win"]


def test_manual_and_ea_trades_both_exist(trades):
    assert any(trade.magic == 0 for trade in trades)
    assert any(trade.magic == EA_MAGIC for trade in trades)


def test_one_comment_tries_to_give_instructions(account, trades):
    hits = [trade for trade in trades if trade.comment == account.planted["injection_comment"]]
    assert len(hits) == 1


def test_no_trade_touches_the_weekend(trades):
    assert all(trade.open_time_utc.weekday() < 5 and trade.close_time_utc.weekday() < 5 for trade in trades)


def test_some_trades_pay_swap_and_some_have_no_stop_loss(trades):
    assert any(trade.swap != 0 for trade in trades)
    assert any(trade.sl == 0 for trade in trades)
    assert any(trade.sl != 0 for trade in trades)


# Swap: charged per server midnight; the night ending Wednesday counts triple.
# September is US summer time, so server midnight is 21:00 UTC.
CLOCK = ServerClock()


def test_swap_same_day_is_zero():
    open_t = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
    close_t = datetime(2026, 9, 8, 15, 0, tzinfo=UTC)
    assert swap_for("XAUUSD", "buy", 0.5, open_t, close_t, CLOCK) == 0.0


def test_swap_one_ordinary_night():
    open_t = datetime(2026, 9, 8, 15, 0, tzinfo=UTC)  # Tuesday
    close_t = datetime(2026, 9, 9, 5, 0, tzinfo=UTC)  # Wednesday
    assert swap_for("XAUUSD", "buy", 0.5, open_t, close_t, CLOCK) == -3.5  # -7.0 * 0.5 * 1


def test_swap_wednesday_night_is_triple():
    open_t = datetime(2026, 9, 9, 15, 0, tzinfo=UTC)  # Wednesday
    close_t = datetime(2026, 9, 10, 5, 0, tzinfo=UTC)  # Thursday
    assert swap_for("XAUUSD", "buy", 0.5, open_t, close_t, CLOCK) == -10.5  # -7.0 * 0.5 * 3
