"""Smart mode: planned extra checks, a "Why", questions worth asking next, all numbers still checked."""

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from tradequery import db
from tradequery.api import create_app
from tradequery.ask import ask, split_ideas
from tradequery.examples import LONGEST_LOSING_STREAK
from tradequery.llm import FakeProvider
from tradequery.settings import load_settings
from tradequery.synthetic import generate_account

SETTINGS = load_settings()
CHECK = {"entity": "trades", "limit": 5}
TEXT = ("Your longest losing streak is shown in the table.\nWhy: the extra check lists recent trades next to it.\n"
        "Worth asking next:\n- Which symbol lost the most?\n- Which hour loses me the most?")


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


def plan(*checks):
    return json.dumps({"checks": list(checks)})


def test_split_ideas():
    text, ideas = split_ideas(TEXT)
    assert text.endswith("next to it.") and ideas == ["Which symbol lost the most?", "Which hour loses me the most?"]
    assert split_ideas("Just an answer.") == ("Just an answer.", [])


def test_smart_runs_valid_checks_only_and_splits_the_ideas(root):
    fake = FakeProvider([json.dumps(LONGEST_LOSING_STREAK), plan(
        {"purpose": "Recent trades", "spec": CHECK},
        {"purpose": "Same as the main one", "spec": LONGEST_LOSING_STREAK},  # a repeat: dropped
        {"purpose": "Not a spec", "spec": {"entity": "passwords"}},  # invalid: dropped
    ), TEXT])
    answer = ask("longest losing streak", db.db_path(1001, root), SETTINGS, fake, smart=True)
    assert answer.verified and answer.text_source == "model" and "Worth asking" not in answer.text
    assert [p for p, _, _ in answer.checks] == ["Recent trades"] and len(answer.checks[0][2].rows) == 5
    assert answer.ideas == ["Which symbol lost the most?", "Which hour loses me the most?"]
    assert answer.usage.requests == 3


def test_smart_numbers_are_still_checked(root):
    fake = FakeProvider([json.dumps(LONGEST_LOSING_STREAK), plan({"purpose": "Recent trades", "spec": CHECK}),
                         "It lost 987654.25 in total.", TEXT])
    answer = ask("longest losing streak", db.db_path(1001, root), SETTINGS, fake, smart=True)
    assert answer.verified and answer.usage.requests == 4  # the made-up number forced one rewrite


def test_a_broken_plan_still_answers(root):
    fake = FakeProvider([json.dumps(LONGEST_LOSING_STREAK), "no json here", TEXT])
    answer = ask("longest losing streak", db.db_path(1001, root), SETTINGS, fake, smart=True)
    assert answer.verified and answer.checks == [] and len(answer.ideas) == 2


def test_the_planner_never_sees_the_account_number(tmp_path):
    login = 7_654_321  # unlike 1001, no trade id or price looks like it
    conn = db.connect(db.db_path(login, tmp_path))
    with conn:
        db.upsert_deals(conn, generate_account().deals)
        db.save_account(conn, {"login": login, "balance": 0.0, "currency": "USD"}, earliest_history=None,
                        synced_at=datetime.now(timezone.utc))
        db.rebuild_derived(conn, SETTINGS)
    conn.close()
    seen = []

    class Spy(FakeProvider):
        def generate(self, system, prompt, *, json_mode=False):
            seen.append(prompt)
            return super().generate(system, prompt, json_mode=json_mode)

    fake = Spy([json.dumps(LONGEST_LOSING_STREAK), plan({"purpose": "Recent trades", "spec": CHECK}), TEXT])
    ask("longest losing streak", db.db_path(login, tmp_path), SETTINGS, fake, smart=True)
    assert len(seen) == 3 and not any(str(login) in p for p in seen)


def test_smart_over_the_api_and_the_stream(root):
    replies = [json.dumps(LONGEST_LOSING_STREAK), plan({"purpose": "Recent trades", "spec": CHECK}), TEXT] * 2
    fake = FakeProvider(replies)
    app = create_app(data_root=root, settings=SETTINGS, provider_factory=lambda: fake, gate=None)
    c = TestClient(app, base_url="http://localhost", headers={"X-TradeQuery": "1"})
    body = {"login": 1001, "question": "longest losing streak", "smart": True}
    data = c.post("/api/ask", json=body).json()
    assert data["smart"] and data["checks"][0]["purpose"] == "Recent trades" and len(data["ideas"]) == 2
    assert data["checks"][0]["result"]["rows"]

    events = [json.loads(line) for line in c.post("/api/ask/stream", json=body).text.splitlines() if line.strip()]
    stages = [e["text"] for e in events if e["event"] == "stage"]
    assert "Planning extra checks" in stages and "Check 1 of 1: Recent trades" in stages
    assert events[-1]["event"] == "done" and events[-1]["answer"]["ideas"] == data["ideas"]


def test_normal_mode_is_unchanged(root):
    fake = FakeProvider([json.dumps(LONGEST_LOSING_STREAK)])  # template answer: one call, no plan
    answer = ask("longest losing streak", db.db_path(1001, root), SETTINGS, fake)
    assert answer.text_source == "template" and answer.checks == [] and answer.ideas == []
