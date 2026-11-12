"""Conversation focus: what a chat is narrowed to, shown as chips, kept by follow-ups, dropped on request."""

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from tradequery import db
from tradequery.api import create_app
from tradequery.ask import Session, _spec_prompt, ask
from tradequery.examples import LONGEST_LOSING_STREAK, WORST_DRAWDOWN
from tradequery.execute import Result
from tradequery.followup import drop_focus, focus_items, found_item, period_label
from tradequery.llm import FakeProvider
from tradequery.settings import load_settings
from tradequery.spec import parse_spec
from tradequery.synthetic import generate_account

SETTINGS = load_settings()
DAY = {"entity": "trades", "time_range": {"start": "2026-09-25", "end": "2026-09-25"},
       "filters": [{"field": "canonical_symbol", "op": "eq", "value": "XAUUSD"},
                   {"field": "side", "op": "eq", "value": "buy"}, {"field": "magic", "op": "eq", "value": 0}]}


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


def test_period_labels():
    assert period_label({"start": "2026-09-25", "end": "2026-09-25"}) == "25 Sep 2026"
    assert period_label({"start": "2026-09-01", "end": "2026-09-30"}) == "1 Sep 2026 – 30 Sep 2026"
    assert period_label({"start": "2026-09-01"}) == "From 1 Sep 2026"
    assert period_label({"start": "2026-09-25T01:45:00", "end": "2026-09-25T03:30:00", "timezone": "utc"}) == \
        "25 Sep 2026 01:45 – 25 Sep 2026 03:30 UTC"
    assert period_label(None, "2026-09-09T15:00:00") == "At 9 Sep 2026 15:00"
    assert period_label(None) is None


def test_focus_chips_from_a_spec():
    chips = focus_items(DAY)
    assert [(c["key"], c["label"]) for c in chips] == [
        ("period", "25 Sep 2026"), ("symbol", "XAUUSD"), ("side", "Buys"), ("magic", "Manual trades")]
    assert focus_items({"entity": "exposure", "filters": [{"field": "symbol", "op": "eq", "value": "ALL"}]}) == []
    assert focus_items({"entity": "trades", "filters": [{"field": "sequence_id", "op": "eq", "value": 12}]}) == \
        [{"key": "sequence_id", "label": "Basket 12"}]
    assert focus_items(None, "Streak 8") == [{"key": "item", "label": "Streak 8"}]


def test_a_one_row_answer_focuses_on_what_it_found():
    one = Result("query", columns=["episode_id", "basis", "depth_abs"], rows=[[72, "equity", 10.0]],
                 )
    assert found_item({"entity": "drawdown_episodes"}, one) == "Equity drawdown 72"
    two = Result("query", columns=["sequence_id"], rows=[[1], [2]])
    assert found_item({"entity": "sequences"}, two) is None  # a list is not one thing


def test_dropping_a_chip_removes_it_from_the_conversation():
    assert "time_range" not in drop_focus(DAY, "period")
    no_symbol = drop_focus(DAY, "symbol")
    assert [f["field"] for f in no_symbol["filters"]] == ["side", "magic"]
    assert DAY["filters"][0]["field"] == "canonical_symbol"  # the original is untouched
    session = Session(question="q", spec=DAY, found="sequence_id 3", item="Basket 3")
    session.drop("item")
    assert session.found is None and session.item is None and session.spec == DAY
    session.drop("all")
    assert session.question is session.spec is None and session.focus() == []


def test_the_spec_prompt_names_the_focus(root):
    session = Session()
    spec = parse_spec(WORST_DRAWDOWN)
    from tradequery.execute import execute
    session.remember("What was my worst drawdown?", spec, execute(spec, db.db_path(1001, root), SETTINGS))
    prompt = _spec_prompt("Which of its trades overlapped news?", "ctx", session)
    assert "Current focus: Balance drawdown 1" in prompt and "Previous result: episode_id 1" in prompt
    session.drop("all")
    assert "Current focus" not in _spec_prompt("anything", "ctx", session)


def test_answers_carry_the_focus_and_chips_can_be_dropped(root):
    fake = FakeProvider([json.dumps({**LONGEST_LOSING_STREAK, "time_range": {"start": "2026-09-01",
                                                                            "end": "2026-09-30"}})])
    app = create_app(data_root=root, settings=SETTINGS, provider_factory=lambda: fake, gate=None)
    c = TestClient(app, base_url="http://localhost", headers={"X-TradeQuery": "1"})
    data = c.post("/api/ask", json={"login": 1001, "question": "longest losing streak in September"}).json()
    labels = [f["label"] for f in data["focus"]]
    assert labels[0] == "1 Sep 2026 – 30 Sep 2026" and labels[-1].startswith("Streak ")
    sid = data["session_id"]
    after = c.post("/api/focus", json={"login": 1001, "session_id": sid, "drop": "period"}).json()
    assert [f["key"] for f in after["focus"]] == ["item"]
    assert c.post("/api/focus", json={"login": 1001, "session_id": sid, "drop": "all"}).json() == {"focus": []}
    assert c.post("/api/focus", json={"login": 1001, "session_id": sid, "drop": "nonsense"}).status_code == 400
    # another account's chat is never changed or shown
    assert c.post("/api/focus", json={"login": 2002, "session_id": sid, "drop": "all"}).json() == {"focus": []}


def test_a_template_answer_in_a_chat_also_sets_the_focus(root):
    fake = FakeProvider([json.dumps(LONGEST_LOSING_STREAK)])
    session = Session()
    ask("longest losing streak", db.db_path(1001, root), SETTINGS, fake, session)
    assert session.focus()[0]["label"].startswith("Streak ")



def test_an_equity_drawdown_without_a_basis_column_is_still_named_equity(root):
    from tradequery.execute import execute

    spec = parse_spec({"entity": "drawdown_episodes", "fields": ["episode_id", "depth_abs"],
                       "filters": [{"field": "basis", "op": "eq", "value": "balance"}],
                       "order_by": [{"field": "depth_abs", "direction": "desc"}], "limit": 1})
    result = execute(spec, db.db_path(1001, root), SETTINGS)
    assert "basis" not in result.columns
    session = Session()
    session.remember("worst drawdown", spec, result)
    assert session.item.startswith("Balance drawdown ") and session.found.endswith("(all balance drawdowns)")
    equity = {**spec.model_dump(mode="json", exclude_defaults=True),
              "filters": [{"field": "basis", "op": "eq", "value": "equity"}]}
    assert found_item(equity, result).startswith("Equity drawdown ")


def test_the_focus_line_tells_the_model_which_trade_field_to_use():
    from tradequery.followup import focus_line

    assert focus_line(None, "Equity drawdown 72") == "Equity drawdown 72 (its trades: equity_drawdown_episode_id = 72)"
    assert focus_line(None, "Balance drawdown 3") == "Balance drawdown 3 (its trades: drawdown_episode_id = 3)"
    assert focus_line(None, "Basket 12") == "Basket 12 (its trades: sequence_id = 12)"
    assert focus_line(DAY, None) == "25 Sep 2026; XAUUSD; Buys; Manual trades"
