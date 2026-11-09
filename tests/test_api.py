"""The web API, with a scripted fake model: no network, no quota."""

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from tradequery import db
from tradequery.api import MODEL_CALLS_PER_MINUTE, create_app
from tradequery.examples import EXAMPLES, LONGEST_LOSING_STREAK
from tradequery.llm import FakeProvider, LLMError
from tradequery.settings import load_settings
from tradequery.synthetic import generate_account

SETTINGS = load_settings()


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    root = tmp_path_factory.mktemp("data")
    for login in (1001, 1002):
        conn = db.connect(db.db_path(login, root))
        with conn:
            db.upsert_deals(conn, generate_account().deals)
            db.save_account(conn, {"login": login, "balance": 0.0, "currency": "USD"}, earliest_history=None,
                            synced_at=datetime.now(timezone.utc))
            db.rebuild_derived(conn, SETTINGS)
        conn.close()
    return root


def client(root, replies=(), factory=None):
    fake = FakeProvider(list(replies))
    app = create_app(data_root=root, settings=SETTINGS, provider_factory=factory or (lambda: fake))
    return TestClient(app), fake


def test_the_page_and_the_lists(root):
    c, _ = client(root)
    page = c.get("/")
    assert page.status_code == 200 and "<title>TradeQuery</title>" in page.text
    accounts = c.get("/api/accounts").json()
    assert [a["login"] for a in accounts] == [1001, 1002] and accounts[0]["trades"] == 48
    presets = c.get("/api/presets").json()
    assert len(presets) == len(EXAMPLES) and presets[0]["number"] == 1
    assert c.get("/api/status").json() == {"model": "fake fake-model", "ready": True}


def test_status_without_a_key_says_so(root):
    def no_key():
        raise LLMError("No GEMINI_API_KEY.")
    c, _ = client(root, factory=no_key)
    status = c.get("/api/status").json()
    assert status["ready"] is False and "GEMINI_API_KEY" in status["message"]
    assert c.post("/api/ask", json={"login": 1001, "question": "hi"}).status_code == 503
    # presets still work without any model
    assert c.post("/api/preset", json={"login": 1001, "number": 11}).status_code == 200


def test_the_overview_for_the_start_screen(root):
    c, fake = client(root)
    o = c.get("/api/overview", params={"login": 1001}).json()
    assert o["login"] == 1001 and o["trades"] == 48 and o["currency"] == "USD"
    assert o["stats"]["net_profit"] is not None and 0 <= o["stats"]["win_rate"] <= 1
    curve = o["balance_curve"]
    assert 1 < len(curve) <= 48 and all(isinstance(b, float) for _, b in curve)
    days = [d for d, _ in o["by_weekday"]]
    assert days[:5] == ["Mon", "Tue", "Wed", "Thu", "Fri"]
    assert round(sum(v for _, v in o["by_weekday"]), 2) == round(o["stats"]["net_profit"], 2)
    assert all(f["level"] in ("warn", "info") and f["text"] for f in o["facts"])
    assert fake.calls == []  # the overview never calls the model
    assert c.get("/api/overview", params={"login": 4242}).status_code == 404


def test_a_preset_runs_without_the_model(root):
    c, fake = client(root)
    data = c.post("/api/preset", json={"login": 1001, "number": 11}).json()  # longest losing streak
    assert data["result"]["rows"][0][2] == 6 and data["model"] is None and fake.calls == []
    assert data["spec"]["entity"] == "streaks" and "SELECT" in data["result"]["sql"]


def test_ask_returns_a_checked_answer_and_follow_ups_keep_the_session(root):
    first = {"entity": "trades", "group_by": ["weekday"], "metrics": ["win_rate"]}
    c, fake = client(root, [json.dumps(first), "Answer without numbers.",
                            json.dumps({**first, "filters": [{"field": "canonical_symbol", "op": "eq", "value": "XAUUSD"}]}),
                            "Answer without numbers."])
    one = c.post("/api/ask", json={"login": 1001, "question": "win rate by weekday"}).json()
    assert one["verified"] and one["session_id"] and one["result"]["share_columns"] == ["win_rate"]
    two = c.post("/api/ask", json={"login": 1001, "question": "only gold", "session_id": one["session_id"]}).json()
    assert two["session_id"] == one["session_id"]
    assert "Previous question: win rate by weekday" in fake.calls[2][1]


def test_follow_ups_never_cross_accounts(root):
    c, fake = client(root, [json.dumps(LONGEST_LOSING_STREAK), "Answer without numbers.",
                            json.dumps(LONGEST_LOSING_STREAK), "Answer without numbers."])
    one = c.post("/api/ask", json={"login": 1001, "question": "longest losing streak"}).json()
    c.post("/api/ask", json={"login": 1002, "question": "and here?", "session_id": one["session_id"]})
    assert "Previous question" not in fake.calls[2][1]


def test_bad_requests_are_refused(root):
    c, _ = client(root)
    assert c.post("/api/ask", json={"login": 9999, "question": "hi"}).status_code == 404
    assert c.post("/api/ask", json={"login": 1001, "question": "x" * 501}).status_code == 422
    assert c.post("/api/ask", json={"login": 1001, "question": ""}).status_code == 422
    assert c.post("/api/preset", json={"login": 1001, "number": 99}).status_code == 404
    rejected = c.post("/api/query", json={"login": 1001, "spec": {"entity": "trades", "filters": [
        {"field": "comment", "op": "eq", "value": "x"}]}})
    assert rejected.status_code == 400 and "unknown field 'comment'" in rejected.json()["detail"]


def test_a_hand_written_spec(root):
    c, _ = client(root)
    data = c.post("/api/query", json={"login": 1001, "spec": {"entity": "streaks", "limit": 3}}).json()
    assert len(data["result"]["rows"]) == 3 and data["result"]["columns"][0] == "streak_id"


def test_model_calls_are_rate_limited(root):
    replies = []
    for _ in range(MODEL_CALLS_PER_MINUTE):
        replies += [json.dumps(LONGEST_LOSING_STREAK), "Answer without numbers."]
    c, _ = client(root, replies)
    for _ in range(MODEL_CALLS_PER_MINUTE):
        assert c.post("/api/ask", json={"login": 1001, "question": "streak"}).status_code == 200
    assert c.post("/api/ask", json={"login": 1001, "question": "streak"}).status_code == 429
