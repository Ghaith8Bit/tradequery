"""Named metrics against values computed by hand on a small known account.

Start balance 1000 (a deposit), then seven closed XAUUSD buys of 0.1 lots and a
withdrawal of 500 after the first trade:

  #  net    outcome    balance after  cum P&L  hold  notes
  1  +100   win        1100           100      60    commission -1, sl 1990 / tp 2030 at entry 2000
     -500   withdrawal  600
  2  -30    loss        570            70      10
  3    0    breakeven   570            70       5
  4  -50    loss        520            20      20    sl 1975 at entry 2000
  5  +20    win         540            40      30
  6  +100   win         640           140      90    swap -2
  7  -10    loss        630           130      30

Money per 1.0 price move per lot is 100 (prices are set so profit = move x lots x 100).
"""

from datetime import datetime, timedelta, timezone

import pytest

from tradequery.metrics import (
    DEFINITIONS,
    compute_metrics,
    drawdown_episodes,
    filter_trades,
    planned_rr,
    r_multiple,
    streaks,
    value_per_price_unit,
    wilson_interval,
)
from tradequery.models import CashFlow, Trade

UTC = timezone.utc
T0 = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)

#        net,  outcome,     balance, hold, commission, swap, sl,   tp
ROWS = [
    (100, "win", 1100, 60, -1.0, 0.0, 1990.0, 2030.0),
    (-30, "loss", 570, 10, 0.0, 0.0, 0.0, 0.0),
    (0, "breakeven", 570, 5, 0.0, 0.0, 0.0, 0.0),
    (-50, "loss", 520, 20, 0.0, 0.0, 1975.0, 0.0),
    (20, "win", 540, 30, 0.0, 0.0, 0.0, 0.0),
    (100, "win", 640, 90, 0.0, -2.0, 0.0, 0.0),
    (-10, "loss", 630, 30, 0.0, 0.0, 0.0, 0.0),
]


def make_trades():
    trades = []
    for i, (net, outcome, balance, hold, commission, swap, sl, tp) in enumerate(ROWS, start=1):
        profit = net - commission - swap
        close = T0 + timedelta(days=i)
        trades.append(Trade(
            position_id=i, symbol="XAUUSD", side="buy", lots=0.1, magic=7 if i % 2 else 0, comment="",
            open_time_utc=close - timedelta(minutes=hold), close_time_utc=close,
            open_price=2000.0, close_price=2000.0 + profit / 10, sl=sl, tp=tp, close_reason="",
            commission=commission, swap=swap, profit=profit,
            net_profit=float(net), hold_minutes=float(hold), weekday="Mon", hour=10, session="london",
            outcome=outcome, is_win=outcome == "win", balance_after=float(balance),
        ))
    return trades


FLOWS = [
    CashFlow(1, T0, "deposit", 1000.0),
    CashFlow(2, T0 + timedelta(days=1, hours=1), "withdrawal", -500.0),
    CashFlow(3, T0 + timedelta(days=2), "credit", 50.0),
]


@pytest.fixture
def metrics():
    return compute_metrics(make_trades(), FLOWS)


def test_every_metric_has_a_definition(metrics):
    assert set(metrics) == set(DEFINITIONS)


def test_results(metrics):
    assert metrics["trade_count"] == 7
    assert metrics["win_rate"] == pytest.approx(3 / 7)
    assert metrics["net_profit"] == 130
    assert metrics["gross_profit"] == 220
    assert metrics["gross_loss"] == -90
    assert metrics["profit_factor"] == pytest.approx(220 / 90)
    assert metrics["expectancy"] == pytest.approx(130 / 7)


def test_hold_times(metrics):
    assert metrics["avg_hold_minutes"] == pytest.approx(245 / 7)
    assert metrics["avg_hold_minutes_winners"] == pytest.approx(60)
    assert metrics["avg_hold_minutes_losers"] == pytest.approx(20)


def test_costs(metrics):
    assert metrics["swap_total"] == -2
    assert metrics["swap_share"] == pytest.approx(-2 / 220)
    assert metrics["commission_total"] == -1


def test_r_multiples_use_only_trades_with_a_stop(metrics):
    # trade 1: risk = 10 x 0.1 x 100 = 100, R = 100/100 = 1.0; planned RR = 30/10 = 3
    # trade 4: risk = 25 x 0.1 x 100 = 250, R = -50/250 = -0.2
    assert metrics["avg_r"] == pytest.approx(0.4)
    assert metrics["total_r"] == pytest.approx(0.8)
    assert metrics["planned_rr"] == pytest.approx(3.0)
    assert metrics["r_excluded"] == 5


def test_r_helpers():
    trades = make_trades()
    assert value_per_price_unit(trades) == pytest.approx(100)  # measured from the trades
    assert value_per_price_unit(trades, {"tick_value": 1.0, "tick_size": 0.01}) == pytest.approx(100)
    assert value_per_price_unit(trades, {"tick_value": 0.0, "tick_size": 0.01}) == pytest.approx(100)  # market closed
    assert r_multiple(trades[0], 100) == pytest.approx(1.0)
    assert r_multiple(trades[1], 100) is None  # no stop loss
    assert planned_rr(trades[3]) is None  # stop but no take profit


def test_streaks_skip_breakeven_by_default(metrics):
    # W, L, (BE), L, W, W, L  ->  runs W1, L2, W2, L1
    assert metrics["max_win_streak"] == 2
    assert metrics["max_loss_streak"] == 2
    assert metrics["current_streak"] == -1
    assert metrics["avg_streak_length"] == pytest.approx(1.5)
    loss_run = [r for r in streaks(make_trades()) if r.kind == "loss"][0]
    assert loss_run.position_ids == (2, 4) and loss_run.net == -80


def test_streaks_when_breakeven_breaks_them():
    # W, L | L, W, W, L  ->  runs W1, L1, L1, W2, L1
    m = compute_metrics(make_trades(), FLOWS, breakeven_breaks_streak=True)
    assert m["max_loss_streak"] == 1
    assert m["avg_streak_length"] == pytest.approx(6 / 5)


def test_cash_flows_and_return(metrics):
    assert metrics["total_deposits"] == 1000
    assert metrics["total_withdrawals"] == -500
    assert metrics["net_deposits"] == 500
    assert metrics["credits_and_bonuses"] == 50
    assert metrics["return_pct"] == pytest.approx(130 / 1000)
    with_opening = compute_metrics(make_trades(), [], opening_balance=2000)
    assert with_opening["return_pct"] == pytest.approx(130 / 2000)


def test_drawdown_across_a_withdrawal_ignores_the_withdrawal(metrics):
    # The balance falls 1100 -> 520 (580), but 500 of that is the withdrawal.
    # Trading P&L: peak 100 after #1, trough 20 after #4: drawdown 80, recovered by #6.
    assert metrics["max_drawdown"] == 80
    assert metrics["max_drawdown_pct"] == pytest.approx(80 / 1100)
    first, last = drawdown_episodes(make_trades())
    assert first.position_ids == [2, 3, 4]
    assert first.peak_time == T0 + timedelta(days=1)
    assert first.trough_time == T0 + timedelta(days=4)
    assert first.recovery_time == T0 + timedelta(days=6)
    # still open at the end: peak 140 after #6, now 130
    assert (last.depth, last.recovery_time) == (10, None)
    assert last.depth_pct == pytest.approx(10 / 640)


def test_drawdown_from_the_very_first_trade():
    trades = make_trades()[1:2]  # a single loss of 30, balance before it 600
    (episode,) = drawdown_episodes(trades)
    assert episode.peak_time is None and episode.depth == 30
    assert episode.depth_pct == pytest.approx(30 / 600)


def test_wilson_interval():
    low, high = wilson_interval(3, 7)
    assert low == pytest.approx(0.1582, abs=1e-4) and high == pytest.approx(0.7495, abs=1e-4)
    assert wilson_interval(0, 0) is None
    assert wilson_interval(10, 10)[1] == 1.0


def test_filters():
    trades = make_trades()
    assert [t.position_id for t in filter_trades(trades, magic=7)] == [1, 3, 5, 7]
    assert filter_trades(trades, symbol="EURUSD") == []
    window = filter_trades(trades, start=T0 + timedelta(days=2), end=T0 + timedelta(days=3))
    assert [t.position_id for t in window] == [2, 3]


def test_no_trades_gives_empty_values_not_errors():
    m = compute_metrics([], [])
    assert m["trade_count"] == 0 and m["win_rate"] is None and m["profit_factor"] is None
    assert m["max_drawdown"] == 0.0 and m["current_streak"] == 0


def test_no_losses_means_no_profit_factor():
    wins = [t for t in make_trades() if t.outcome == "win"]
    assert compute_metrics(wins, [])["profit_factor"] is None
