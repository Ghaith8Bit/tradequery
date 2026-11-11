"""Derived layers stored for querying: they must agree with the modules that compute them."""

from dataclasses import replace
from datetime import datetime, timezone

from tradequery import db, metrics
from tradequery.facts import canonical_symbol
from tradequery.settings import load_settings
from tradequery.synthetic import generate_account


def test_canonical_symbol():
    assert canonical_symbol("XAUUSD-STDc") == canonical_symbol("XAUUSD.m") == canonical_symbol("XAUUSDm") == "XAUUSD"
    assert canonical_symbol("US30.cash") == "US30"
    assert canonical_symbol("GER40") == "GER40"
    assert canonical_symbol("GOLD#", {"GOLD#": "XAUUSD"}) == "XAUUSD"


def stored(tmp_path):
    conn = db.connect(db.db_path(1, tmp_path))
    with conn:
        db.upsert_deals(conn, generate_account().deals)
        trades, flows = db.rebuild_derived(conn, load_settings())
    return conn, trades, flows


def test_facts_agree_with_the_computing_modules(tmp_path):
    conn, trades, flows = stored(tmp_path)
    rows = {r["position_id"]: r for r in conn.execute("SELECT * FROM v_trades")}
    assert len(rows) == len(trades)

    runs = metrics.streaks(trades)
    assert conn.execute("SELECT COUNT(*) FROM streaks").fetchone()[0] == len(runs)
    longest_loss = max((r for r in runs if r.kind == "loss"), key=lambda r: r.length)
    sid = rows[longest_loss.position_ids[0]]["streak_id"]
    members = [pid for pid, r in rows.items() if r["streak_id"] == sid]
    assert sorted(members) == sorted(longest_loss.position_ids)

    worst = max(metrics.drawdown_episodes(trades, flows), key=lambda e: e.depth)
    row = conn.execute("SELECT * FROM drawdown_episodes WHERE basis = 'balance' ORDER BY depth_abs DESC").fetchone()
    assert (row["depth_abs"], row["trade_count"]) == (worst.depth, len(worst.position_ids))

    planted = [t for t in trades if t.comment.startswith("martingale-")]
    seq_ids = {rows[t.position_id]["sequence_id"] for t in planted}
    assert len(seq_ids) == 1 and None not in seq_ids

    peak = conn.execute("SELECT * FROM exposure WHERE symbol = 'ALL' ORDER BY gross_lots DESC LIMIT 1").fetchone()
    assert peak["gross_lots"] == metrics.compute_metrics(trades, flows)["peak_lots_open"]
    assert rows[trades[0].position_id]["month"] == load_settings().clock.from_utc(trades[0].close_time_utc).strftime("%Y-%m")
    conn.close()


def test_trades_link_to_their_equity_drawdown(tmp_path):
    from datetime import timedelta

    from tradequery.models import Bar

    deals = generate_account().deals
    conn = db.connect(db.db_path(2, tmp_path))
    with conn:
        db.upsert_deals(conn, deals)
        trades, _ = db.rebuild_derived(conn, load_settings())
        # price bars along each trade, open price to close price, so there is an equity curve
        bars = {}
        for t in trades:
            minutes = max(1, int((t.close_time_utc - t.open_time_utc).total_seconds() // 60))
            start = t.open_time_utc.replace(second=0, microsecond=0)
            for i in range(minutes + 1):
                p = round(t.open_price + (t.close_price - t.open_price) * i / minutes, 5)
                bars.setdefault((t.symbol, start + timedelta(minutes=i)), Bar(t.symbol, start + timedelta(minutes=i),
                                                                               p, p, p, p, 0))
        db.save_bars(conn, list(bars.values()))
        db.rebuild_derived(conn, load_settings())
    episodes = conn.execute("SELECT episode_id, trade_count FROM drawdown_episodes WHERE basis = 'equity'").fetchall()
    assert episodes
    for episode_id, trade_count in episodes:
        linked = conn.execute("SELECT COUNT(*) FROM v_trades WHERE equity_drawdown_episode_id = ?",
                              (episode_id,)).fetchone()[0]
        assert linked == trade_count

    # every basket now has a drawdown: at least its final loss, since its running result ends at its net
    baskets = conn.execute("SELECT sequence_id, net_pnl, drawdown_abs, drawdown_pct, equity_at_start "
                           "FROM v_sequences").fetchall()
    assert baskets and all(b["drawdown_abs"] is not None for b in baskets)
    for b in baskets:
        assert b["drawdown_abs"] >= max(0.0, -b["net_pnl"]) - 0.01
        assert b["drawdown_pct"] == round(b["drawdown_abs"] / b["equity_at_start"], 4)
    conn.close()


def test_basket_drawdown_counts_closed_and_open_legs():
    from datetime import timedelta

    from tradequery.detectors.sequences import Sequence
    from tradequery.models import Bar
    from tradequery.pricepath import basket_drawdowns

    t0 = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)
    template = next(t for t in stored_trades() if t.symbol == "XAUUSD")
    # two buys of 1 lot at 100 (100 money per 1.0 move); leg a closes at 95 (-500) at 10:03:30, leg b at 104 (+400)
    a = replace(template, position_id=1, side="buy", lots=1.0, open_price=100.0, open_time_utc=t0,
                close_time_utc=t0 + timedelta(minutes=3, seconds=30), close_price=95.0, net_profit=-500.0)
    b = replace(template, position_id=2, side="buy", lots=1.0, open_price=100.0, open_time_utc=t0,
                close_time_utc=t0 + timedelta(minutes=6), close_price=104.0, net_profit=400.0)
    closes = [99, 95, 90, 92, 98, 104, 104]
    bars = {"XAUUSD": [Bar("XAUUSD", t0 + timedelta(minutes=i), c, c, c, c, 0) for i, c in enumerate(closes)]}
    seq = Sequence(7, "grid", "XAUUSD", "buy", (1, 2), 1.0, 1.0, 2, t0, t0 + timedelta(minutes=6), -100.0)
    [dd] = basket_drawdowns([seq], [a, b], bars, symbol_specs={"XAUUSD": {"tick_value": 100.0, "tick_size": 1.0}},
                            covered={1, 2}, opening_balance=4000.0)
    # at 10:03 (bar 10:02 closed at 90): both legs open, (90 - 100) * 100 * 2 = -2,000; at 10:04 leg a is closed
    # at -500 and leg b at 92 is -800, together -1,300. The deepest is -2,000, half of the 4,000 the account had.
    assert (dd.depth_abs, dd.time_utc, dd.equity_at_start, dd.depth_pct) == (2000.0, t0 + timedelta(minutes=3), 4000.0, 0.5)


def stored_trades():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        conn, trades, _ = stored(Path(tmp))
        conn.close()
    return trades


def test_views_answer_simple_questions(tmp_path):
    conn, trades, _ = stored(tmp_path)
    months = conn.execute("SELECT month, type, SUM(amount) FROM v_cash_flows GROUP BY month, type").fetchall()
    assert sum(r[2] for r in months) == 11500.0  # 10,000 + 2,000 - 500
    no_news = conn.execute("SELECT COUNT(*) FROM v_sequences WHERE news_overlap = 0").fetchone()[0]
    assert no_news == conn.execute("SELECT COUNT(*) FROM sequences").fetchone()[0]  # no calendar stored
    conn.close()
