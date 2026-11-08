from datetime import datetime, timezone

import pytest

from tradequery.trades import build_trades, extract_cash_flows

UTC = timezone.utc


def t(hour, minute=0, day=15):
    return datetime(2026, 7, day, hour, minute, tzinfo=UTC)


def test_one_position_becomes_one_trade(make_deal):
    deals = [
        make_deal(ticket=1, position_id=1, time_utc=t(9), type="buy", entry="in", volume=0.5,
                  price=2650.0, commission=-1.75, magic=12345, sl=2640.0, tp=2670.0, comment="ea"),
        make_deal(ticket=2, position_id=1, time_utc=t(11, 30), type="sell", entry="out", volume=0.5,
                  price=2660.0, commission=-1.75, swap=-0.5, profit=500.0, reason="tp"),
    ]
    [trade] = build_trades(deals)
    assert trade.side == "buy"
    assert trade.lots == 0.5
    assert trade.magic == 12345
    assert trade.comment == "ea"
    assert trade.open_time_utc == t(9)
    assert trade.close_time_utc == t(11, 30)
    assert trade.open_price == 2650.0
    assert trade.close_price == 2660.0
    assert trade.sl == 2640.0
    assert trade.tp == 2670.0
    assert trade.commission == pytest.approx(-3.5)
    assert trade.swap == -0.5
    assert trade.profit == 500.0
    assert trade.close_reason == "tp"


def test_several_exits_are_combined(make_deal):
    deals = [
        make_deal(ticket=1, position_id=1, time_utc=t(9), entry="in", volume=1.0, price=100.0),
        make_deal(ticket=2, position_id=1, time_utc=t(10), type="sell", entry="out", volume=0.4,
                  price=110.0, profit=40.0, reason="client"),
        make_deal(ticket=3, position_id=1, time_utc=t(12), type="sell", entry="out", volume=0.6,
                  price=120.0, profit=120.0, reason="tp"),
    ]
    [trade] = build_trades(deals)
    assert trade.lots == 1.0
    assert trade.close_price == pytest.approx(116.0)  # (0.4*110 + 0.6*120) / 1.0
    assert trade.close_time_utc == t(12)
    assert trade.profit == 160.0
    assert trade.close_reason == "tp"


def test_open_and_partly_closed_positions_are_skipped(make_deal):
    deals = [
        make_deal(ticket=1, position_id=1, entry="in", volume=1.0),  # never closed
        make_deal(ticket=2, position_id=2, entry="in", volume=1.0),
        make_deal(ticket=3, position_id=2, time_utc=t(10), type="sell", entry="out", volume=0.4),
    ]
    assert build_trades(deals) == []


def test_exit_without_entry_is_skipped(make_deal):
    deals = [make_deal(ticket=2, position_id=2, type="sell", entry="out", volume=0.4, profit=5.0)]
    assert build_trades(deals) == []


def test_reversal_deals_are_rejected(make_deal):
    deals = [
        make_deal(ticket=1, position_id=1, entry="in"),
        make_deal(ticket=2, position_id=1, time_utc=t(10), type="sell", entry="inout"),
    ]
    with pytest.raises(ValueError):
        build_trades(deals)


def test_closing_more_than_opened_is_rejected(make_deal):
    deals = [
        make_deal(ticket=1, position_id=1, entry="in", volume=0.5),
        make_deal(ticket=2, position_id=1, time_utc=t(10), type="sell", entry="out", volume=0.6),
    ]
    with pytest.raises(ValueError):
        build_trades(deals)


def test_trades_come_out_oldest_close_first(make_deal):
    deals = [
        make_deal(ticket=1, position_id=1, time_utc=t(8), entry="in"),
        make_deal(ticket=2, position_id=1, time_utc=t(15), type="sell", entry="out"),
        make_deal(ticket=3, position_id=2, time_utc=t(9), entry="in"),
        make_deal(ticket=4, position_id=2, time_utc=t(10), type="sell", entry="out"),
    ]
    assert [trade.position_id for trade in build_trades(deals)] == [2, 1]


def test_cash_flows_are_classified(make_deal):
    deals = [
        make_deal(ticket=10, time_utc=t(8), type="balance", entry="", profit=1000.0, comment="start"),
        make_deal(ticket=11, time_utc=t(9), type="balance", entry="", profit=-200.0),
        make_deal(ticket=12, time_utc=t(10), type="credit", entry="", profit=50.0),
        make_deal(ticket=13, time_utc=t(11), type="correction", entry="", profit=5.0),
        make_deal(ticket=14, time_utc=t(12), type="buy", entry="in"),  # a trade, not a cash flow
    ]
    flows = extract_cash_flows(deals)
    assert [(f.type, f.amount) for f in flows] == [
        ("deposit", 1000.0),
        ("withdrawal", -200.0),
        ("credit", 50.0),
        ("correction", 5.0),
    ]
    assert flows[0].comment == "start"
