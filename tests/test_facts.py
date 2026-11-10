"""Derived layers stored for querying: they must agree with the modules that compute them."""

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
    conn.close()


def test_views_answer_simple_questions(tmp_path):
    conn, trades, _ = stored(tmp_path)
    months = conn.execute("SELECT month, type, SUM(amount) FROM v_cash_flows GROUP BY month, type").fetchall()
    assert sum(r[2] for r in months) == 11500.0  # 10,000 + 2,000 - 500
    no_news = conn.execute("SELECT COUNT(*) FROM v_sequences WHERE news_overlap = 0").fetchone()[0]
    assert no_news == conn.execute("SELECT COUNT(*) FROM sequences").fetchone()[0]  # no calendar stored
    conn.close()
