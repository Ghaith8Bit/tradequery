"""The worst-drop story on the start screen: its parts add up to the drop."""

from datetime import datetime, timezone

from tradequery import db
from tradequery.dropstory import worst_drop
from tradequery.settings import load_settings
from tradequery.synthetic import generate_account

SETTINGS = load_settings()


def test_the_worst_drop_splits_into_closed_losses_and_profits(tmp_path):
    conn = db.connect(db.db_path(1001, tmp_path))
    with conn:
        db.upsert_deals(conn, generate_account().deals)
        db.save_account(conn, {"login": 1001, "balance": 0.0, "currency": "USD"}, earliest_history=None,
                        synced_at=datetime.now(timezone.utc))
        db.rebuild_derived(conn, SETTINGS)
    story = worst_drop(conn, SETTINGS.clock)
    deepest = conn.execute("SELECT depth_abs, trade_count FROM drawdown_episodes WHERE basis = 'balance' "
                           "ORDER BY depth_abs DESC LIMIT 1").fetchone()
    conn.close()

    assert story["basis"] == "balance" and story["depth"] == round(deepest[0], 2)
    assert story["trades"] == deepest[1] == story["losses"]["count"] + story["profits"]["count"]
    # a balance drop is closed trades only: its losses and profits are the whole drop
    assert round(story["losses"]["total"] + story["profits"]["total"], 2) == -story["depth"]
    assert story["open_at_low"] is None and story["low_day"] >= (story["peak_day"] or "")
    worst = story["losses"]["worst_day"]
    assert worst and worst["total"] <= 0 and 1 <= worst["count"] <= story["losses"]["count"]
    assert 0 <= story["no_stop"] <= story["trades"]
