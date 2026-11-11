"""The daily AI budget, template answers, and the checks that keep other websites out."""

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from tradequery import db
from tradequery.api import create_app
from tradequery.examples import LONGEST_LOSING_STREAK
from tradequery.execute import Result
from tradequery.llm import FakeProvider, Usage
from tradequery.settings import load_settings
from tradequery.spec import parse_spec
from tradequery.synthetic import generate_account
from tradequery.templates import template_answer
from tradequery.usage import UsageBook

SETTINGS = load_settings()


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


def make(root, replies=(), **kw):
    fake = FakeProvider(list(replies))
    return create_app(data_root=root, settings=SETTINGS, provider_factory=lambda: fake, **kw), fake


def page_client(app):
    return TestClient(app, base_url="http://localhost", headers={"X-TradeQuery": "1"})


# ---- daily budget ----

def test_usage_is_counted_per_day_and_the_budget_stops_model_calls(tmp_path):
    day = [datetime(2026, 11, 11, 10, tzinfo=timezone.utc)]
    book = UsageBook(tmp_path, requests=3, tokens=0, clock=lambda: day[0])
    book.record(Usage(requests=2, input_tokens=100, output_tokens=10), templated=False)
    assert book.over_budget() is None
    book.record(Usage(requests=1, input_tokens=50, output_tokens=5), templated=True)
    today = book.today()
    assert (today["requests"], today["input_tokens"], today["questions"], today["templated"]) == (3, 150, 2, 1)
    assert "3 of 3 requests" in book.over_budget()
    day[0] = datetime(2026, 11, 12, 0, 1, tzinfo=timezone.utc)  # a new UTC day starts from zero
    assert book.over_budget() is None and book.today()["requests"] == 0


def test_the_token_budget_and_unreadable_files(tmp_path):
    (tmp_path / "usage.json").write_text("not json")
    book = UsageBook(tmp_path, requests=0, tokens=1000)
    assert book.today()["requests"] == 0  # a broken file counts as an empty day
    book.record(Usage(requests=1, input_tokens=990, output_tokens=20))
    assert "1,010 of 1,000 tokens" in book.over_budget()


def test_the_api_refuses_questions_over_budget_but_not_the_free_parts(root):
    book = UsageBook(root, requests=1, tokens=0)
    app, _ = make(root, [json.dumps(LONGEST_LOSING_STREAK)], usage_book=book)
    c = page_client(app)
    first = c.post("/api/ask", json={"login": 1001, "question": "longest losing streak"})
    assert first.status_code == 200 and first.json()["text_source"] == "template"
    assert c.get("/api/usage").json()["requests"] == 1
    over = c.post("/api/ask", json={"login": 1001, "question": "longest losing streak"})
    assert over.status_code == 429 and "midnight UTC" in over.json()["detail"]
    assert c.get("/api/presets").status_code == 200  # suggested questions need no AI


# ---- templates ----

def test_templates_copy_numbers_from_the_table():
    spec = parse_spec({"entity": "trades", "group_by": ["weekday"], "metrics": ["net_profit"],
                       "order_by": [{"field": "net_profit", "direction": "asc"}], "limit": 1})
    result = Result(kind="query", columns=["weekday", "net_profit", "trade_count"], rows=[["Friday", -120.5, 9]],
                    sql="", interpretation="", notes=[], truncated=False)
    text = template_answer(spec, result)
    assert text.startswith("Weekday Friday has the lowest net result: -120.50") and "trades 9" in text
    empty = Result(kind="query", columns=["weekday"], rows=[], sql="", interpretation="", notes=[], truncated=False)
    assert template_answer(spec, empty) == "No trades matched."
    many = Result(kind="query", columns=["weekday", "net_profit"], rows=[["Mon", 1.0], ["Tue", 2.0]], sql="", interpretation="",
                  notes=[], truncated=False)
    assert template_answer(parse_spec({"entity": "trades", "group_by": ["weekday"], "metrics": ["net_profit"]}),
                           many) is None  # a table of groups goes to the model


# ---- other websites ----

def test_posts_without_the_page_header_are_refused(root):
    app, _ = make(root)
    c = TestClient(app, base_url="http://localhost")
    r = c.post("/api/ask", json={"login": 1001, "question": "streak"})
    assert r.status_code == 403
    assert c.post("/api/sync", json={"login": 1001}).status_code == 403


def test_posts_from_another_site_are_refused_even_with_the_header(root):
    app, _ = make(root)
    c = page_client(app)
    assert c.post("/api/ask", json={"login": 1001, "question": "x"},
                  headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/ask", json={"login": 1001, "question": "x"},
                  headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_other_host_names_are_refused(root):
    app, _ = make(root)
    assert TestClient(app, base_url="http://attacker.example").get("/api/accounts").status_code == 400
    assert TestClient(app, base_url="http://127.0.0.1:8000").get("/api/accounts").status_code == 200
    extra, _ = make(root, allowed_hosts=["localhost", "tq.lan"], gate=None)
    assert TestClient(extra, base_url="http://tq.lan").get("/api/accounts").status_code == 200


def test_security_headers(root):
    app, _ = make(root)
    c = page_client(app)
    page = c.get("/")
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY" and page.headers["x-content-type-options"] == "nosniff"
    assert c.get("/api/accounts").headers["cache-control"] == "no-store"
    assert c.get("/api/docs").status_code == 404  # no docs page that loads outside scripts
