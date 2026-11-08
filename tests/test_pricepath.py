"""Price path, exposure and equity on a hand-built bar series (the plan's Phase 4 check).

Six XAUUSD minutes (bid prices, point 0.01, money per 1.0 move per lot = 100):

  minute  open     high     low      close    spread (points)
  10:00   2000.0   2000.5   1999.8   2000.2   10
  10:01   2000.2   2000.4   1998.5   1999.0   12
  10:02   1999.0   2002.0   1999.0   2001.5   10
  10:03   2001.5   2001.6   2000.9   2001.0   10
  10:04   2001.0   2001.2   2000.0   2000.5   20
  10:05   2000.5   2000.6   1999.5   2000.0   10

A: buy  0.1 lots at 2000.00, 10:00:30 -> 10:03:20, closed at 2001.00 (+10)
B: sell 0.2 lots at 2000.00, 10:01:10 -> 10:04:40, closed at 2000.60 ask (-12)
Opening balance 1000, no cash flows.
"""

from datetime import datetime, timedelta, timezone

import pytest

from tradequery import exposure, pricepath
from tradequery.models import Bar, Trade
from tradequery.pricepath import bar_windows, compute_price_path_metrics, equity_curve, equity_drawdowns, price_paths

UTC = timezone.utc


def at(h, m, s=0):
    return datetime(2026, 9, 1, h, m, s, tzinfo=UTC)


ROWS = [
    (2000.0, 2000.5, 1999.8, 2000.2, 10),
    (2000.2, 2000.4, 1998.5, 1999.0, 12),
    (1999.0, 2002.0, 1999.0, 2001.5, 10),
    (2001.5, 2001.6, 2000.9, 2001.0, 10),
    (2001.0, 2001.2, 2000.0, 2000.5, 20),
    (2000.5, 2000.6, 1999.5, 2000.0, 10),
]
BARS = {"XAUUSD": [Bar("XAUUSD", at(10, i), *row) for i, row in enumerate(ROWS)]}
SPECS = {"XAUUSD": {"point": 0.01, "digits": 2, "tick_size": 0.01, "tick_value": 1.0}}


def trade(pid, side, lots, opened, closed, close_price, net, balance):
    return Trade(
        position_id=pid, symbol="XAUUSD", side=side, lots=lots, magic=0, comment="",
        open_time_utc=opened, close_time_utc=closed, open_price=2000.0, close_price=close_price,
        sl=0.0, tp=0.0, close_reason="", commission=0.0, swap=0.0, profit=net,
        net_profit=net, hold_minutes=(closed - opened).total_seconds() / 60, weekday="Tue", hour=13,
        session="london", outcome="win" if net > 0 else "loss", is_win=net > 0, balance_after=balance,
    )


A = trade(1, "buy", 0.1, at(10, 0, 30), at(10, 3, 20), 2001.0, 10.0, 1010.0)
B = trade(2, "sell", 0.2, at(10, 1, 10), at(10, 4, 40), 2000.6, -12.0, 998.0)


def test_buy_excursions_and_minutes():
    # window 10:00-10:03: lowest bid 1998.5 -> 1.5 against x 0.1 x 100 = -15
    # highest bid 2002.0 -> 2.0 in favour = +20; closes 2000.2 / 1999.0 / 2001.5 / 2001.0 vs 2000
    path = price_paths([A], BARS, SPECS)[1]
    assert (path.mae, path.mfe) == (-15.0, 20.0)
    assert (path.minutes_in_profit, path.minutes_in_loss) == (3, 1)
    assert (path.spread_at_open, path.bars_used) == (10, 4)


def test_sell_excursions_use_the_ask():
    # window 10:01-10:04; ask = bid + spread x 0.01
    # highest ask 2002.0 + 0.10 = 2002.10 -> 2.10 against x 0.2 x 100 = -42
    # lowest ask 1998.5 + 0.12 = 1998.62 -> 1.38 in favour = +27.6
    # ask closes 1999.12 (profit) / 2001.60 / 2001.10 / 2000.70 (loss)
    path = price_paths([B], BARS, SPECS)[2]
    assert (path.mae, path.mfe) == (-42.0, 27.6)
    assert (path.minutes_in_profit, path.minutes_in_loss) == (1, 3)
    assert path.spread_at_open == 12


def test_no_bars_means_no_price_path_not_zeros():
    late = trade(3, "buy", 0.1, at(12, 0), at(12, 5), 2000.0, 0.0, 998.0)
    assert price_paths([late], BARS, SPECS) == {}
    m = compute_price_path_metrics([A, late], [], BARS, opening_balance=1000, symbol_specs=SPECS)
    assert m["price_path_coverage"] == 0.5
    assert compute_price_path_metrics([late], [], {}, symbol_specs=SPECS)["avg_mae"] is None


def test_exposure():
    # +0.1 long at 10:00:30, +0.2 short at 10:01:10, A closes 10:03:20, B closes 10:04:40
    events = exposure.exposure_events([A, B])
    peak = exposure.peak_lots_open(events)
    assert (peak.gross_lots, peak.net_lots, peak.positions_open, peak.time_utc) == (0.3, -0.1, 2, at(10, 1, 10))
    # time-weighted over 250 s: 0.1 x 40 + 0.3 x 130 + 0.2 x 80 = 59 -> 0.236
    assert exposure.avg_lots_open(events) == pytest.approx(0.236)
    assert exposure.lots_open_at(events, at(10, 2)).gross_lots == 0.3
    assert exposure.lots_open_at(events, at(10, 3, 20)).short_lots == 0.2
    assert exposure.lots_open_at(events, at(10, 4, 40)).gross_lots == 0
    assert exposure.lots_open_at(events, at(9, 0)) is None
    assert exposure.peak_lots_open(events, "XAUUSD").gross_lots == 0.3


def test_equity_curve():
    # end of 10:00: A +2.0                          -> 1002.0  (low: A -2.0 -> 998.0)
    # end of 10:01: A -10, B (2000-1999.12)x20=+17.6 -> 1007.6  (low: -15 - 10.4 -> 974.6)
    # end of 10:02: A +15, B -32                     -> 983.0   (low: -10 - 42 -> 948.0)
    # 10:03:20 A closes (balance 1010), B -22        -> 988.0
    # end of 10:03: B -22                            -> 988.0
    # 10:04:40 B closes (balance 998)                -> 998.0
    curve = equity_curve([A, B], [], BARS, opening_balance=1000, symbol_specs=SPECS)
    assert [p.time_utc for p in curve] == [at(10, 1), at(10, 2), at(10, 3), at(10, 3, 20), at(10, 4), at(10, 4, 40)]
    assert [p.equity for p in curve] == [1002.0, 1007.6, 983.0, 988.0, 988.0, 998.0]
    assert [p.equity_low for p in curve[:3]] == [998.0, 974.6, 948.0]


def test_equity_drawdown_and_its_conservative_bound():
    curve = equity_curve([A, B], [], BARS, opening_balance=1000, symbol_specs=SPECS)
    # closes: peak 1007.6 at 10:02, trough 983.0 at 10:03 -> 24.6, never recovered
    (worst,) = [e for e in equity_drawdowns(curve) if e.depth > 0]
    assert (worst.depth, worst.peak_time, worst.trough_time, worst.recovery_time) == (24.6, at(10, 2), at(10, 3), None)
    assert worst.depth_pct == pytest.approx(24.6 / 1007.6)
    # lows: from the 1007.6 peak down to 948.0 within the 10:02 minute -> 59.6
    assert max(e.depth for e in equity_drawdowns(curve, conservative=True)) == 59.6


def test_price_path_metrics():
    m = compute_price_path_metrics([A, B], [], BARS, opening_balance=1000, symbol_specs=SPECS)
    assert set(m) == set(pricepath.PRICE_PATH_DEFINITIONS)
    assert m["price_path_coverage"] == 1.0
    assert m["avg_mae"] == pytest.approx(-28.5) and m["avg_mfe"] == pytest.approx(23.8)
    assert m["worst_mae"] == -42.0
    assert (m["avg_minutes_in_profit"], m["avg_minutes_in_loss"], m["avg_spread_at_open"]) == (2, 2, 11)
    assert m["max_equity_drawdown"] == 24.6 and m["max_equity_drawdown_low"] == 59.6
    assert m["worst_floating_pnl"] == (-22.0, at(10, 3, 20))


def test_withdrawal_during_open_positions_is_not_an_equity_drawdown():
    from tradequery.models import CashFlow
    flow = CashFlow(9, at(10, 2, 30), "withdrawal", -500.0)
    curve = equity_curve([A, B], [flow], BARS, opening_balance=1000, symbol_specs=SPECS)
    assert curve[2].equity == 483.0  # the balance really dropped by 500
    assert max(e.depth for e in equity_drawdowns(curve)) == 24.6  # but the drawdown did not


def test_bar_windows_merge_overlapping_positions():
    later = trade(3, "buy", 0.1, at(13, 0), at(13, 10), 2000.0, 0.0, 998.0)
    assert bar_windows([A, B, later]) == {"XAUUSD": [(at(10, 0), at(10, 4)), (at(13, 0), at(13, 10))]}
    assert bar_windows([A, B, later], merge_gap=timedelta(hours=3)) == {"XAUUSD": [(at(10, 0), at(13, 10))]}
