"""Streamed answers: progress events, the answer's words as they are written, and the /api/ask/stream endpoint."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tradequery import db
from tradequery.api import create_app
from tradequery.ask import ask
from tradequery.examples import LONGEST_LOSING_STREAK
from tradequery.llm import FakeProvider, LLMError, Usage
from tradequery.settings import load_settings
from tradequery.synthetic import generate_account
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


def recorder():
    events = []
    return events, lambda kind, **data: events.append((kind, data))


def test_fake_stream_gives_words_then_usage():
    pieces = list(FakeProvider(["Your streak was 6 trades."]).stream("s", "p"))
    assert "".join(pieces[:-1]) == "Your streak was 6 trades." and len(pieces) == 6
    assert isinstance(pieces[-1], Usage)


def test_ask_reports_stages_and_streams_the_text(root):
    fake = FakeProvider([json.dumps(LONGEST_LOSING_STREAK), "Your longest losing streak was 6 trades."])
    events, emit = recorder()
    answer = ask("longest losing streak", db.db_path(1001, root), SETTINGS, fake, explain="ai", on_event=emit)
    stages = [d["text"] for k, d in events if k == "stage"]
    assert stages == ["Reading your question", "Looking it up in your data", "Writing the answer"]
    assert "".join(d["text"] for k, d in events if k == "delta") == answer.text
    assert answer.verified and answer.usage.requests == 2


def test_a_failed_number_check_is_announced_and_rewritten(root):
    fake = FakeProvider([json.dumps(LONGEST_LOSING_STREAK), "It was 9 trades.", "It was 6 trades in a row."])
    events, emit = recorder()
    answer = ask("longest losing streak", db.db_path(1001, root), SETTINGS, fake, explain="ai", on_event=emit)
    kinds = [k for k, _ in events]
    assert kinds.count("retry") == 1 and kinds.index("retry") > kinds.index("delta")
    after = kinds.index("retry")
    assert "".join(d["text"] for k, d in events[after:] if k == "delta") == answer.text == "It was 6 trades in a row."


def test_template_answers_stream_no_text(root):
    fake = FakeProvider([json.dumps(LONGEST_LOSING_STREAK)])  # no explanation scripted: none may be asked for
    events, emit = recorder()
    answer = ask("longest losing streak", db.db_path(1001, root), SETTINGS, fake, on_event=emit)
    assert answer.text_source == "template" and not [k for k, _ in events if k == "delta"]


def lines_of(response):
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def client(root, replies, **kw):
    fake = FakeProvider(list(replies))
    app = create_app(data_root=root, settings=SETTINGS, provider_factory=lambda: fake, gate=None, **kw)
    return TestClient(app, base_url="http://localhost", headers={"X-TradeQuery": "1"})


def test_the_stream_endpoint_ends_with_the_same_answer_as_ask(root, monkeypatch):
    monkeypatch.setenv("TQ_EXPLAIN", "ai")
    c = client(root, [json.dumps(LONGEST_LOSING_STREAK), "Your longest losing streak was 6 trades."])
    r = c.post("/api/ask/stream", json={"login": 1001, "question": "longest losing streak"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    events = lines_of(r)
    assert events[0] == {"event": "stage", "text": "Reading your question"}
    done = events[-1]
    assert done["event"] == "done" and done["answer"]["text"] == "Your longest losing streak was 6 trades."
    assert "".join(e["text"] for e in events if e["event"] == "delta") == done["answer"]["text"]
    assert done["answer"]["session_id"] and c.get("/api/usage").json()["requests"] == 2


def test_the_stream_endpoint_keeps_the_checks(root):
    c = client(root, [json.dumps(LONGEST_LOSING_STREAK)], usage_book=UsageBook(root, requests=1, tokens=0))
    no_header = TestClient(c.app, base_url="http://localhost")
    assert no_header.post("/api/ask/stream", json={"login": 1001, "question": "x"}).status_code == 403
    assert c.post("/api/ask/stream", json={"login": 1001, "question": "longest losing streak"}).status_code == 200
    over = c.post("/api/ask/stream", json={"login": 1001, "question": "longest losing streak"})
    assert over.status_code == 429 and "midnight UTC" in over.json()["detail"]
    assert c.post("/api/ask/stream", json={"login": 4242, "question": "x"}).status_code == 404


def test_a_broken_answer_ends_the_stream_with_an_error(root, monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError("bug")

    monkeypatch.setattr("tradequery.api.ask", broken)
    events = lines_of(client(root, []).post("/api/ask/stream", json={"login": 1001, "question": "x"}))
    assert events == [{"event": "error", "detail": "Something went wrong while answering. Try again."}]


def test_gemini_stream_retries_only_before_the_first_piece():
    import httpx
    from google.genai import errors, types

    from tradequery.llm import GeminiProvider

    meta = SimpleNamespace(prompt_token_count=10, candidates_token_count=5, thoughts_token_count=0)

    class Models:
        def __init__(self, script):
            self.script, self.calls = script, 0

        def generate_content_stream(self, **kwargs):
            self.calls += 1
            for item in self.script.pop(0):
                if isinstance(item, Exception):
                    raise item
                yield SimpleNamespace(text=item, usage_metadata=meta)

    def provider(script):
        p = object.__new__(GeminiProvider)
        p._types, p._errors, p.model, p._sleep = types, errors, "m", lambda s: None
        p.retry_pauses = (1.0, 3.0)
        p._client = SimpleNamespace(models=Models(script))
        return p

    unavailable = errors.ServerError(503, {"error": {"message": "high demand", "status": "UNAVAILABLE"}})
    p = provider([[unavailable], [httpx.ReadTimeout("slow")], ["Hello ", "there."]])
    out = list(p.stream("s", "p"))
    assert out[:-1] == ["Hello ", "there."] and out[-1].output_tokens == 5 and p._client.models.calls == 3

    midway = provider([["Hello ", httpx.ReadTimeout("slow")], ["never"]])
    with pytest.raises(LLMError, match="Could not reach Gemini"):
        list(midway.stream("s", "p"))
    assert midway._client.models.calls == 1  # half an answer was shown: no silent second answer

    with pytest.raises(LLMError, match="no text"):
        list(provider([[""]]).stream("s", "p"))
