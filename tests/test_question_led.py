"""Answers lead with what was asked; deposits inside a drawdown are explained; equity over time is queryable."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tradequery import db
from tradequery.api import create_app
from tradequery.ask import checked_template
from tradequery.examples import EXAMPLES, WORST_DRAWDOWN
from tradequery.execute import Result, execute
from tradequery.focus import focus_column, focus_columns, news_verdict
from tradequery.llm import FakeProvider
from tradequery.scope import headline
from tradequery.settings import load_settings
from tradequery.spec import parse_spec
from tradequery.synthetic import generate_account

SETTINGS = load_settings()
FMT = "%Y-%m-%d %H:%M:%S"


@pytest.fixture
def root(tmp_path):
    conn = db.connect(db.db_path(1001, tmp_path))
    with conn:
        db.upsert_deals(conn, generate_account().deals)
        db.save_account(conn, {"login": 1001, "balance": 0.0, "currency": "USD"}, earliest_history=None,
                        synced_at=datetime.now(timezone.utc))
        db.rebuild_derived(conn, SETTINGS)
    conn.close()
    return tmp_path


# ---- what the question is about ----

def test_question_words_pick_the_column():
    assert focus_columns("Did news cause my worst drawdown?")[0] == "news_loss_share"
    assert focus_columns("How long did my worst drawdown last?")[0] == "duration_days"
    assert focus_columns("Has it recovered?") == ("recovery_time_utc",)
    assert focus_columns("Show my last trade") == ()  # "last" here is not a duration
    assert focus_columns("What was my worst drawdown?") == ()
    assert focus_column("news?", ["depth_abs", "news_loss_share"]) == "news_loss_share"
    assert focus_column("news?", ["depth_abs"]) is None


def test_the_news_verdict_says_overlap_never_cause():
    assert news_verdict(0.024).startswith("Mostly not: only 2.40%")
    assert news_verdict(0.3).startswith("Partly: 30.00%")
    high = news_verdict(0.7)
    assert "70.00%" in high and "not proof" in high
    assert "no economic calendar" in news_verdict(None)


def _drawdown_result(share=0.0239):
    return Result("query", ["episode_id", "depth_abs", "depth_pct", "trade_count", "news_loss_share"],
                  [[72, 12980.45, 0.6277, 76, share]], interpretation="account drawdown")


def test_the_card_and_sentence_lead_with_the_news_when_asked():
    spec = parse_spec(WORST_DRAWDOWN)
    result = _drawdown_result()
    assert headline(spec, result, "Did news cause my worst drawdown?")["column"] == "news_loss_share"
    assert headline(spec, result, "What was my worst drawdown?")["column"] == "depth_pct"
    text = checked_template("Did news cause my worst drawdown?", spec, result)
    assert text.startswith("Mostly not: only 2.39% of the losses")
    plain = checked_template("What was my worst drawdown?", spec, result)
    assert plain.startswith("Your account drawdown (balance) went 12,980.45 deep")


# ---- suggested questions get a sentence too ----

def test_a_suggested_question_gets_a_checked_sentence(root):
    app = create_app(data_root=root, settings=SETTINGS, provider_factory=lambda: FakeProvider([]), gate=None)
    c = TestClient(app, base_url="http://localhost", headers={"X-TradeQuery": "1"})
    number = next(i + 1 for i, (q, _) in enumerate(EXAMPLES) if "news cause" in q)
    data = c.post("/api/preset", json={"login": 1001, "number": number}).json()
    assert data["text_source"] == "template" and data["verified"]
    assert data["text"].split(":")[0] in ("Mostly not", "Partly", "Much of it overlapped news")
    assert data["scope"]["headline"]["column"] == "news_loss_share"


# ---- deposits inside a drawdown ----

def test_a_deposit_inside_a_drawdown_is_explained(root):
    path = db.db_path(1001, root)
    conn = db.connect(path)
    with conn:
        deposit_time, amount = conn.execute("SELECT time_utc, amount FROM cash_flows WHERE type = 'deposit' "
                                            "ORDER BY time_utc DESC LIMIT 1").fetchone()
        t = datetime.strptime(deposit_time[:19], FMT)
        peak, low_at, trough = t - timedelta(days=2), t - timedelta(days=1), t + timedelta(days=1)
        conn.execute("INSERT INTO drawdown_episodes VALUES (999, 'equity', ?, ?, NULL, 1000, -500, 1500, 0.4, 3, 5, "
                     "NULL, ?)", (peak.strftime(FMT), trough.strftime(FMT), amount))
        for when, equity in ((peak, 1000.0), (low_at, 100.0), (trough, 400.0)):
            conn.execute("INSERT INTO equity_curve VALUES (?, ?, 0, ?, ?)", (when.strftime(FMT), equity, equity, equity))
    conn.close()
    spec = parse_spec({"entity": "drawdown_episodes", "fields": ["episode_id", "depth_abs"],
                       "filters": [{"field": "basis", "op": "eq", "value": "equity"},
                                   {"field": "episode_id", "op": "eq", "value": 999}]})
    (note,) = execute(spec, path, SETTINGS).notes
    assert f"{amount:,.2f}" in note and "looks smaller" in note
    assert "equity fell from 1,000.00 to 100.00" in note and "90.00% below" in note


def test_no_note_without_a_deposit(root):
    spec = parse_spec({"entity": "drawdown_episodes", "filters": [{"field": "basis", "op": "eq", "value": "balance"}],
                       "order_by": [{"field": "depth_abs", "direction": "desc"}], "limit": 1})
    assert all("deposit" not in n for n in execute(spec, db.db_path(1001, root), SETTINGS).notes)


# ---- equity over time ----

def test_equity_over_time_can_be_asked(root):
    path = db.db_path(1001, root)
    conn = db.connect(path)
    with conn:
        for minute, equity in ((0, 900.0), (1, 350.0), (2, 600.0)):
            conn.execute("INSERT INTO equity_curve VALUES (?, 1000, ?, ?, ?)",
                         (f"2026-09-28 02:0{minute}:00", equity - 1000, equity, equity))
    conn.close()
    lowest = parse_spec({"entity": "equity_curve", "time_range": {"start": "2026-09-28", "end": "2026-09-28",
                                                                   "timezone": "utc"},
                         "aggregations": [{"func": "min", "field": "equity", "alias": "lowest_equity"}]})
    assert execute(lowest, path, SETTINGS).rows == [[350.0]]
    from tradequery.templates import template_answer
    assert template_answer(lowest, execute(lowest, path, SETTINGS)) == "In that period: lowest equity 350.00."
    latest = parse_spec({"entity": "equity_curve", "order_by": [{"field": "time_utc", "direction": "desc"}], "limit": 1})
    assert execute(latest, path, SETTINGS).rows[0][-1] == 600.0


def test_a_zero_length_range_on_exposure_means_one_moment():
    from tradequery.spec import parse_spec

    spec = parse_spec({"entity": "exposure", "fields": ["gross_lots"],
                       "time_range": {"start": "2026-09-09T15:00:00", "end": "2026-09-09T15:00:00"}})
    assert spec.time_range is None and spec.as_of.isoformat() == "2026-09-09T15:00:00"
    whole_day = parse_spec({"entity": "exposure", "fields": ["gross_lots"],
                            "time_range": {"start": "2026-09-09", "end": "2026-09-09"}})
    assert whole_day.as_of is None and whole_day.time_range is not None  # a whole day stays a range
