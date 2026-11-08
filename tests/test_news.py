"""News layer: calendar clock calibration (around the US daylight-saving switch), overlap join, attribution."""

from datetime import datetime, timedelta, timezone

import pytest

from tradequery import cli, db, news
from tradequery.models import Trade
from tradequery.news import NewsEvent, calibrate_clock, inside_outside, join, news_loss_share, parse_calendar
from tradequery.settings import NewsSettings, load_settings
from tradequery.timeutil import ServerClock

UTC = timezone.utc
HEADER = "time_server\tcurrency\timportance\tevent_id\tvalue_id\tname\tactual\tforecast\tprevious\n"
SERVER = ServerClock("ny_close")


def calendar_file(tmp_path, rows):
    path = tmp_path / "calendar.csv"
    path.write_text(HEADER + "".join("\t".join(map(str, r)) + "\n" for r in rows), encoding="utf-8")
    return path


def nfp(naive_text, value_id):
    return (naive_text, "USD", "high", 840030016, value_id, "Nonfarm Payrolls", "", "", "")


# NFP is always 08:30 New York: 13:30 UTC before the US switch to summer time, 12:30 UTC after.
# 6 Mar 2026 is before the switch (8 Mar), 3 Apr 2026 after it.


def test_calibration_finds_a_new_york_close_calendar_across_the_dst_switch(tmp_path):
    # UTC+2 in winter, UTC+3 in summer: both releases print as 15:30
    rows = [nfp("2026.03.06 15:30:00", 1), nfp("2026.04.03 15:30:00", 2)]
    events, note = parse_calendar(calendar_file(tmp_path, rows), NewsSettings(), ServerClock("fixed", 0))
    assert [e.time_utc for e in events] == [datetime(2026, 3, 6, 13, 30, tzinfo=UTC), datetime(2026, 4, 3, 12, 30, tzinfo=UTC)]
    assert "New York close" in note


def test_calibration_finds_a_fixed_utc_plus_2_calendar(tmp_path):
    # what the live broker's calendar did: 14:30 for a 12:30 UTC release, in summer
    rows = [nfp("2026.03.06 15:30:00", 1), nfp("2026.04.03 14:30:00", 2)]
    events, note = parse_calendar(calendar_file(tmp_path, rows), NewsSettings(), SERVER)
    assert events[1].time_utc == datetime(2026, 4, 3, 12, 30, tzinfo=UTC)
    assert "UTC+2" in note


def test_without_nfp_the_configured_clock_is_used_and_said(tmp_path):
    rows = [("2026.09.10 15:30:00", "USD", "high", 1, 7, "CPI m/m", "", "", "")]
    events, note = parse_calendar(calendar_file(tmp_path, rows), NewsSettings(), SERVER)
    assert events[0].time_utc == datetime(2026, 9, 10, 12, 30, tzinfo=UTC)  # server clock, UTC+3 in September
    assert "no US Nonfarm Payrolls" in note


def test_fixed_and_server_clock_settings(tmp_path):
    path = calendar_file(tmp_path, [nfp("2026.09.04 14:30:00", 1)])
    assert parse_calendar(path, NewsSettings(calendar_clock="server"), SERVER)[0][0].time_utc.hour == 11
    assert parse_calendar(path, NewsSettings(calendar_clock="+2"), SERVER)[0][0].time_utc.hour == 12


def test_nfp_times_that_fit_no_clock_are_flagged():
    rows = [dict(naive=datetime(2026, 9, 4, 14, 30), currency="USD", name="Nonfarm Payrolls"),
            dict(naive=datetime(2026, 10, 2, 16, 47), currency="USD", name="Nonfarm Payrolls")]
    clock, note = calibrate_clock(rows, SERVER)
    assert clock is SERVER and "fit no single clock" in note


def test_a_file_that_is_not_an_export_is_refused(tmp_path):
    path = tmp_path / "x.csv"
    path.write_text("a,b\n1,2\n")
    with pytest.raises(news.CalendarError):
        parse_calendar(path, NewsSettings(), SERVER)


# ---------------------------------------------------------------- the join

EVENT_AT = datetime(2026, 9, 4, 12, 30, tzinfo=UTC)
EVENTS = [
    NewsEvent(1, 10, EVENT_AT, "USD", "high", "Nonfarm Payrolls"),
    NewsEvent(2, 11, EVENT_AT, "EUR", "high", "ECB Speech"),
    NewsEvent(3, 12, EVENT_AT + timedelta(hours=3), "USD", "low", "Some low event"),
]
GOLD = {"XAUUSD-STDc": {"base_currency": "XAU", "profit_currency": "USD"}}


def trade(pid, opened_min, closed_min, net=-10.0, symbol="XAUUSD-STDc"):
    return Trade(
        position_id=pid, symbol=symbol, side="buy", lots=0.1, magic=0, comment="",
        open_time_utc=EVENT_AT + timedelta(minutes=opened_min), close_time_utc=EVENT_AT + timedelta(minutes=closed_min),
        open_price=1.0, close_price=1.0, sl=0.0, tp=0.0, close_reason="", commission=0.0, swap=0.0, profit=net,
        net_profit=net, hold_minutes=float(closed_min - opened_min), weekday="Fri", hour=12, session="new_york",
        outcome="win" if net > 0 else "loss", is_win=net > 0, balance_after=1000.0,
    )


def test_overlap_window_boundaries_and_phases():
    trades = [
        trade(1, -40, -15),   # closes exactly 15 min before: overlaps, phase before
        trade(2, -40, -16),   # closes 16 min before: no
        trade(3, -5, 5),      # open across the event: during
        trade(4, 60, 90),     # opens exactly 60 min after: overlaps, phase after
        trade(5, 61, 90),     # opens 61 min after: no
    ]
    links = {l.position_id: l for l in join(trades, EVENTS, NewsSettings(), GOLD)}
    assert set(links) == {1, 3, 4}
    assert (links[1].phase, links[3].phase, links[4].phase) == ("before", "during", "after")
    assert (links[1].minutes_to_event, links[4].minutes_to_event) == (-40.0, 60.0)


def test_only_the_symbols_currencies_and_enough_importance_count():
    gold = join([trade(1, -5, 200)], EVENTS, NewsSettings(), GOLD)
    assert [l.value_id for l in gold] == [1]  # not the EUR event, not the low-importance one
    everything = join([trade(1, -5, 200)], EVENTS, NewsSettings(min_importance="low"), GOLD)
    assert [l.value_id for l in everything] == [1, 3]
    euro = join([trade(1, -5, 5, symbol="EURUSD")], EVENTS, NewsSettings(), {})
    assert {l.value_id for l in euro} == {1, 2}  # currencies from the name when there is no spec
    override = NewsSettings(symbol_currencies=(("XAUUSD-STDc", ("EUR",)),))
    assert [l.value_id for l in join([trade(1, -5, 5)], EVENTS, override, GOLD)] == [2]


def test_news_tags_name_the_events():
    links = join([trade(3, -5, 5)], EVENTS, NewsSettings(), GOLD)
    (tag,) = news.news_tags(links, EVENTS)
    assert tag.rule == "news_overlap"
    assert tag.evidence == "overlapped during Nonfarm Payrolls (USD, high) at 2026-09-04 12:30 UTC"


def test_inside_outside_and_drawdown_news_share():
    trades = [trade(1, -5, 5, net=-100.0), trade(2, 200, 300, net=-300.0), trade(3, 400, 500, net=50.0)]
    links = join(trades, EVENTS, NewsSettings(), GOLD)
    inside, outside = inside_outside(trades, links)
    assert (inside.trades, inside.net, outside.trades, outside.net) == (1, -100.0, 2, -250.0)
    share, events = news_loss_share([1, 2, 3], {t.position_id: t for t in trades}, links)
    assert share == pytest.approx(0.25) and events == {1}  # 100 of the 400 lost overlapped news
    assert news_loss_share([2], {t.position_id: t for t in trades}, links) == (0.0, set())
    assert news_loss_share([3], {t.position_id: t for t in trades}, links) == (None, set())  # no losses at all


def test_calendar_import_joins_and_tags_through_the_database(tmp_path, capsys):
    from tradequery.synthetic import generate_account
    deals = generate_account().deals
    conn = db.connect(db.db_path(1234, tmp_path))
    with conn:
        db.upsert_deals(conn, deals)
        db.save_account(conn, {"login": 1234, "balance": 0.0}, earliest_history=None, synced_at=datetime.now(UTC))
        trades, _ = db.rebuild_derived(conn, load_settings())
    conn.close()
    first = min(trades, key=lambda t: t.open_time_utc)
    server = load_settings().clock.from_utc(first.open_time_utc + timedelta(minutes=1))
    path = calendar_file(tmp_path, [(server.strftime("%Y.%m.%d %H:%M:%S"), "USD", "high", 5, 55, "CPI m/m", "", "", "")])
    assert cli.main(["--data-dir", str(tmp_path), "import-calendar", str(path)]) == 0
    out = capsys.readouterr().out
    assert "1 events (1 new)" in out and "no US Nonfarm Payrolls" in out
    conn = db.connect(db.db_path(1234, tmp_path), read_only=True)
    assert first.position_id in {l.position_id for l in db.load_trade_news(conn)}
    assert db.load_tags(conn, "news_overlap")
    conn.close()
    assert cli.main(["--data-dir", str(tmp_path), "news"]) == 0
    assert "Inside news windows" in capsys.readouterr().out
