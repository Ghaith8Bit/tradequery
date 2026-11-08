"""Martingale and grid sequences: known sequences must be found with the right depth and net result."""

from datetime import datetime, timedelta, timezone

import pytest

from tradequery import db
from tradequery.detectors.sequences import baskets, find_sequences, losing_when_next_opens, sequence_tags
from tradequery.models import Trade
from tradequery.settings import DetectorSettings, load_settings
from tradequery.synthetic import generate_account
from tradequery.trades import build_trades, derive_trades, extract_cash_flows

UTC = timezone.utc
T0 = datetime(2026, 9, 1, 5, 58, tzinfo=UTC)
CONFIG = DetectorSettings()


def pos(pid, open_min, close_min, lots, price, net, side="buy", symbol="XAUUSD"):
    return Trade(
        position_id=pid, symbol=symbol, side=side, lots=lots, magic=122334, comment="",
        open_time_utc=T0 + timedelta(minutes=open_min), close_time_utc=T0 + timedelta(minutes=close_min),
        open_price=price, close_price=price, sl=0.0, tp=0.0, close_reason="expert",
        commission=0.0, swap=0.0, profit=net, net_profit=net, hold_minutes=float(close_min - open_min),
        weekday="Tue", hour=6, session="london", outcome="win" if net > 0 else "loss" if net < 0 else "breakeven",
        is_win=net > 0, balance_after=10000.0,
    )


# The real basket from 1 Sep: buys added against the price with doubling lots, all closed together.
BASKET = [
    pos(1, 0, 267, 0.01, 4444.77, -59.55),
    pos(2, 2, 267, 0.02, 4440.10, -109.76),
    pos(3, 12, 267, 0.04, 4435.80, -202.32),
    pos(4, 30, 267, 0.08, 4431.73, -370.08),
    pos(5, 100, 267, 0.16, 4420.73, -567.04),
    pos(6, 130, 267, 0.32, 4405.47, -645.44),
    pos(7, 152, 267, 0.64, 4383.26, 123.52),
    pos(8, 210, 267, 1.28, 4371.00, 1816.32),
]


def test_the_real_eight_level_basket_is_one_martingale_grid():
    (s,) = find_sequences(BASKET, CONFIG)
    assert (s.type, s.symbol, s.side, s.depth, s.max_open) == ("martingale_grid", "XAUUSD", "buy", 8, 8)
    assert (s.first_lot, s.max_lot, s.max_lot_multiplier) == (0.01, 1.28, 128.0)
    assert s.net == -14.35 and not s.is_win
    assert s.position_ids == tuple(range(1, 9))
    assert (s.start_utc, s.end_utc) == (T0, T0 + timedelta(minutes=267))


def test_sequence_tags_carry_level_and_outcome():
    sequences = find_sequences(BASKET, CONFIG)
    tags = sequence_tags(sequences, BASKET)
    assert len(tags) == 16  # every position gets a martingale and a grid tag
    last = [t for t in tags if t.position_id == 8 and t.rule == "martingale"][0]
    assert last.evidence == "level 8 of 8 in martingale-grid sequence #1: 1.28 lots, 128.0x the first; the sequence lost 14.35"


def test_the_planted_synthetic_martingale_is_found_with_its_depth_and_net():
    deals = generate_account().deals
    trades = derive_trades(build_trades(deals), extract_cash_flows(deals), load_settings())
    planted = [t for t in trades if t.comment.startswith("martingale-")]
    found = [s for s in find_sequences(trades, CONFIG) if set(s.position_ids) & {t.position_id for t in planted}]
    (s,) = found
    assert s.type == "martingale"  # one position at a time: a classic martingale, not a grid
    assert set(s.position_ids) == {t.position_id for t in planted}
    assert (s.depth, s.first_lot, s.max_lot, s.max_lot_multiplier) == (5, 0.1, 1.6, pytest.approx(16.0))
    assert s.net == pytest.approx(sum(t.net_profit for t in planted)) and s.is_win


# ---------------------------------------------------------------- martingale boundaries (one position at a time)


def sequential(lots_and_nets, sides=None):
    out, minute = [], 0
    for i, (lots, net) in enumerate(lots_and_nets, start=1):
        side = sides[i - 1] if sides else "buy"
        out.append(pos(i, minute, minute + 10, lots, 2000.0, net, side=side))
        minute += 15
    return out


def test_martingale_at_the_threshold():
    (s,) = find_sequences(sequential([(0.1, -10), (0.15, -15), (0.225, 40)]), CONFIG)  # exactly 1.5x each step
    assert (s.type, s.depth, s.max_lot_multiplier) == ("martingale", 3, pytest.approx(2.25))


@pytest.mark.parametrize("steps", [
    [(0.1, -10), (0.14, -15), (0.3, 40)],       # 1.4x: the chain breaks
    [(0.1, -10), (0.2, 40)],                     # only one losing position before the bigger lot
    [(0.1, 10), (0.2, -20), (0.4, 40)],          # the first one won: the chain starts at 0.2, too short
])
def test_martingale_does_not_trigger(steps):
    assert find_sequences(sequential(steps), CONFIG) == []


def test_martingale_can_switch_sides_after_a_closed_loss():
    (s,) = find_sequences(sequential([(0.1, -10), (0.2, -20), (0.4, 50)], sides=["buy", "sell", "buy"]), CONFIG)
    assert (s.type, s.side) == ("martingale", "mixed")


# ---------------------------------------------------------------- grid boundaries


def test_grid_without_growing_lots():
    trades = [pos(1, 0, 60, 0.1, 2000.0, -5), pos(2, 10, 60, 0.1, 1995.0, 0), pos(3, 20, 60, 0.1, 1990.0, 5)]
    (s,) = find_sequences(trades, CONFIG)
    assert (s.type, s.depth, s.max_open, s.max_lot_multiplier) == ("grid", 3, 3, 1.0)


def test_sell_grid_adds_at_higher_prices():
    trades = [pos(i, 10 * i, 60, 0.1, 2000.0 + 5 * i, 1, side="sell") for i in range(3)]
    assert find_sequences(trades, CONFIG)[0].type == "grid"


@pytest.mark.parametrize("trades", [
    # only two open at once: the first closes before the third opens
    [pos(1, 0, 15, 0.1, 2000.0, -5), pos(2, 10, 60, 0.1, 1995.0, 0), pos(3, 20, 60, 0.1, 1990.0, 5)],
    # the third entry is not further against the first than the second
    [pos(1, 0, 60, 0.1, 2000.0, -5), pos(2, 10, 60, 0.1, 1995.0, 0), pos(3, 20, 60, 0.1, 1998.0, 5)],
    # different symbols never form one grid
    [pos(1, 0, 60, 0.1, 2000.0, -5), pos(2, 10, 60, 0.1, 1995.0, 0, symbol="EURUSD"), pos(3, 20, 60, 0.1, 1990.0, 5)],
])
def test_grid_does_not_trigger(trades):
    assert find_sequences(trades, CONFIG) == []


def test_losing_when_next_opens():
    closed_loss, closed_win = pos(1, 0, 5, 0.1, 2000.0, -5), pos(1, 0, 5, 0.1, 2000.0, 5)
    later = pos(2, 10, 20, 0.2, 2010.0, 0)
    assert losing_when_next_opens(closed_loss, later) and not losing_when_next_opens(closed_win, later)
    still_open = pos(1, 0, 60, 0.1, 2000.0, 0)
    assert losing_when_next_opens(still_open, pos(2, 10, 60, 0.2, 1990.0, 0))  # buy, price now lower
    assert not losing_when_next_opens(still_open, pos(2, 10, 60, 0.2, 2010.0, 0))  # price higher: winning
    assert not losing_when_next_opens(still_open, pos(2, 10, 60, 0.2, 1990.0, 0, side="sell"))  # a hedge


def test_baskets_group_overlapping_same_side_positions():
    trades = BASKET + [pos(20, 300, 310, 0.01, 4390.0, 3)]
    assert [len(b) for b in baskets(trades)] == [8, 1]


def test_sequences_are_stored_on_every_rebuild(tmp_path):
    conn = db.connect(db.db_path(1, tmp_path))
    with conn:
        db.upsert_deals(conn, generate_account().deals)
        trades, _ = db.rebuild_derived(conn, load_settings())
    stored = db.load_sequences(conn)
    assert stored == find_sequences(trades, load_settings().detectors)
    assert {t.rule for t in db.load_tags(conn)} >= {"martingale"}
    conn.close()
