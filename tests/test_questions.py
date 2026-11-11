"""Phase 8 check: the plan's example questions, as hand-written specs, return correct answers.

Each answer from the spec pipeline is compared with the same answer computed
directly from the trades in plain Python, on the synthetic account (which has
a planted martingale and a planted 6-trade losing streak).
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pytest

from tradequery import db, metrics
from tradequery.examples import EXAMPLES, LONGEST_LOSING_STREAK, WORST_DRAWDOWN, trades_in
from tradequery.execute import execute
from tradequery.facts import canonical_symbol
from tradequery.news import NewsEvent
from tradequery.settings import load_settings
from tradequery.spec import parse_spec
from tradequery.synthetic import generate_account

SETTINGS = load_settings()
SPECS = dict(EXAMPLES)


@pytest.fixture(scope="module")
def account(tmp_path_factory):
    path = db.db_path(4242, tmp_path_factory.mktemp("acct"))
    conn = db.connect(path)
    with conn:
        db.upsert_deals(conn, generate_account().deals)
        db.save_account(conn, {"login": 4242, "balance": 0.0, "currency": "USD"}, earliest_history=None,
                        synced_at=datetime.now(timezone.utc))
        trades, flows = db.rebuild_derived(conn, SETTINGS)
    conn.close()
    return path, trades, flows


def ask(path, spec):
    return execute(parse_spec(spec), path, SETTINGS)


def rows_as_dicts(result):
    return [dict(zip(result.columns, r)) for r in result.rows]


def test_every_example_is_a_valid_spec():
    for question, spec in EXAMPLES:
        parse_spec(spec)


def test_worst_drawdown_and_its_trades(account):
    path, trades, flows = account
    (row,) = rows_as_dicts(ask(path, WORST_DRAWDOWN))
    worst = max(metrics.drawdown_episodes(trades, flows), key=lambda e: e.depth)
    assert row["depth_abs"] == worst.depth and row["trade_count"] == len(worst.position_ids)
    members = ask(path, trades_in("drawdown_episode_id", row["episode_id"]))
    assert {r[0] for r in members.rows} == set(worst.position_ids)


def test_fridays_versus_other_days(account):
    path, trades, _ = account
    result = ask(path, SPECS["Compare my win rate on Fridays versus other days."])
    got = {r["set"]: r for r in rows_as_dicts(result)}
    for label, chosen in (("Fridays", [t for t in trades if t.weekday == "Fri"]),
                          ("Other days", [t for t in trades if t.weekday != "Fri"])):
        assert got[label]["trades"] == len(chosen)
        assert got[label]["win_rate"] == pytest.approx(sum(t.outcome == "win" for t in chosen) / len(chosen))
        assert got[label]["win_rate_low"] <= got[label]["win_rate"] <= got[label]["win_rate_high"]
    assert "server time" in result.interpretation


def test_worst_hour_on_xauusd(account):
    path, trades, _ = account
    (row,) = rows_as_dicts(ask(path, SPECS["Which hour of the day loses me the most money on XAUUSD?"]))
    by_hour = defaultdict(float)
    for t in trades:
        if canonical_symbol(t.symbol) == "XAUUSD":
            by_hour[t.hour] += t.net_profit
    worst_hour = min(by_hour, key=by_hour.get)
    assert (row["hour"], row["net"]) == (worst_hour, pytest.approx(by_hour[worst_hour]))


def test_long_losing_trades(account):
    path, trades, _ = account
    result = ask(path, SPECS["Show trades held longer than 4 hours that lost over $50."])
    expected = {t.position_id for t in trades if t.hold_minutes > 240 and t.net_profit < -50}
    assert {r["position_id"] for r in rows_as_dicts(result)} == expected


def test_peak_lots_open(account):
    path, trades, _ = account
    (row,) = rows_as_dicts(ask(path, SPECS["How many lots did I have open at once at the peak, and when?"]))
    # brute force: lots open just after each open
    best = max(((sum(o.lots for o in trades if o.open_time_utc <= t.open_time_utc < o.close_time_utc), t.open_time_utc)
                for t in trades), key=lambda x: (x[0], -x[1].timestamp()))
    assert row["gross_lots"] == pytest.approx(best[0])
    assert row["time_utc"] == db.format_time(best[1])


def test_deposits_and_withdrawals_per_month(account):
    path, _, flows = account
    result = ask(path, SPECS["How much did I deposit and withdraw each month?"])
    expected = defaultdict(float)
    for f in flows:
        if f.type in ("deposit", "withdrawal"):
            expected[(f.time_utc.strftime("%Y-%m"), f.type)] += f.amount
    assert {(r["month"], r["type"]): r["total"] for r in rows_as_dicts(result)} == expected


def test_results_by_magic_number(account):
    path, trades, _ = account
    rows = {r["magic"]: r for r in rows_as_dicts(ask(path, SPECS["Show my results by magic number."]))}
    for magic in {t.magic for t in trades}:
        mine = [t for t in trades if t.magic == magic]
        assert rows[magic]["trade_count"] == len(mine)
        assert rows[magic]["net_profit"] == pytest.approx(sum(t.net_profit for t in mine))
        assert rows[magic]["win_rate"] == pytest.approx(sum(t.outcome == "win" for t in mine) / len(mine))


def test_swap_by_symbol_and_costliest_weekday(account):
    path, trades, _ = account
    by_symbol = {r["canonical_symbol"]: r["swap_total"] for r in rows_as_dicts(ask(path, SPECS["What is my total swap by symbol?"]))}
    expected = defaultdict(float)
    for t in trades:
        expected[canonical_symbol(t.symbol)] += t.swap
    assert by_symbol == pytest.approx(dict(expected))
    (row,) = rows_as_dicts(ask(path, SPECS["Which weekday costs me the most swap?"]))
    days = defaultdict(float)
    for t in trades:
        days[t.weekday] += t.swap
    assert row["weekday"] == min(days, key=days.get)


def test_average_r_and_planned_rr(account):
    path, trades, flows = account
    result = ask(path, SPECS["What is my average R, and how does planned risk-reward compare with what I got?"])
    (row,) = rows_as_dicts(result)
    expected = metrics.compute_metrics(trades, flows)
    assert row["avg_r"] == pytest.approx(expected["avg_r"]) and row["planned_rr"] == pytest.approx(expected["planned_rr"])
    assert row["r_excluded"] == sum(1 for t in trades if not t.sl)
    assert any("no stop loss" in n for n in result.notes)


def test_longest_losing_streak_is_the_planted_one(account):
    path, trades, _ = account
    (row,) = rows_as_dicts(ask(path, LONGEST_LOSING_STREAK))
    assert row["length"] == 6  # planted in the synthetic account
    members = ask(path, trades_in("streak_id", row["streak_id"]))
    assert len(members.rows) == 6 and all(r[-1] == "loss" for r in members.rows)


def test_revenge_trades_match_the_detector_tags(account):
    path, _, _ = account
    conn = db.connect(path, read_only=True)
    tagged = {t.position_id for t in db.load_tags(conn, "revenge_trade")}
    conn.close()
    result = ask(path, SPECS["Show my revenge trades."])
    assert {r["position_id"] for r in rows_as_dicts(result)} == tagged


def test_winning_martingales_during_news(tmp_path):
    path = db.db_path(7, tmp_path)
    conn = db.connect(path)
    with conn:
        db.upsert_deals(conn, generate_account().deals)
        db.save_account(conn, {"login": 7, "balance": 0.0}, earliest_history=None, synced_at=datetime.now(timezone.utc))
        trades, _ = db.rebuild_derived(conn, SETTINGS)
    conn.close()
    spec = SPECS["Show winning martingale baskets during news."]
    assert ask(path, spec).rows == []  # no calendar: nothing overlapped news
    planted = min((t for t in trades if t.comment.startswith("martingale-")), key=lambda t: t.open_time_utc)
    conn = db.connect(path)
    with conn:
        db.save_news_events(conn, [NewsEvent(1, 1, planted.open_time_utc + timedelta(minutes=5), "USD", "high", "Test NFP")])
        db.rebuild_derived(conn, SETTINGS)
    conn.close()
    (row,) = rows_as_dicts(ask(path, spec))
    assert (row["type"], row["depth"], row["is_win"]) == ("martingale", 5, 1)


def test_did_news_cause_the_worst_drawdown(account):
    path, _, _ = account
    (row,) = rows_as_dicts(ask(path, SPECS["Did news cause my worst drawdown?"]))
    assert row["news_loss_share"] in (None, 0.0)  # no calendar in this account: no overlap at all


def test_a_list_sorted_by_a_column_shows_that_column(account):
    spec = {"entity": "sequences", "order_by": [{"field": "total_lots", "direction": "desc"}], "limit": 1}
    result = ask(account[0], spec)
    assert "total_lots" in result.columns  # not a default column, but the question is about it
