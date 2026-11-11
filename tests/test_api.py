"""The web API, with a scripted fake model: no network, no quota."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tradequery import db
from tradequery.api import MODEL_CALLS_PER_MINUTE, create_app
from tradequery.examples import EXAMPLES, LONGEST_LOSING_STREAK
from tradequery.llm import FakeProvider, LLMError
from tradequery.settings import load_settings
from tradequery.synthetic import generate_account

SETTINGS = load_settings()


def local(app):
    """A client that looks like the TradeQuery page on this computer."""
    return TestClient(app, base_url="http://localhost", headers={"X-TradeQuery": "1"})


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
    return local(app), fake


def test_the_page_and_the_lists(root):
    c, _ = client(root)
    page = c.get("/")
    assert page.status_code == 200 and "<title>TradeQuery</title>" in page.text
    accounts = c.get("/api/accounts").json()
    assert [a["login"] for a in accounts] == [1001, 1002] and accounts[0]["trades"] == 48
    presets = c.get("/api/presets").json()
    assert len(presets) == len(EXAMPLES) and presets[0]["number"] == 1
    assert c.get("/api/status").json() == {"model": "fake fake-model", "ready": True, "login_required": False}


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
    assert all(f["level"] in ("warn", "info") and f["text"] and f["icon"] for f in o["facts"])
    # the calendar: one entry per close day, adding up to the net result
    assert o["daily"] and sum(n for _, _, n in o["daily"]) == 48
    assert round(sum(v for _, v, _ in o["daily"]), 2) == round(o["stats"]["net_profit"], 2)
    assert [m for m, _ in o["monthly"]] == sorted({d[:7] for d, _, _ in o["daily"]})
    assert o["stats"]["baskets"] > 0 and 0 <= o["stats"]["basket_win_rate"] <= 1
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


# ---- upload a report, Sync now, price charts ----

def fresh_client(tmp_path):
    app = create_app(data_root=tmp_path, settings=SETTINGS, provider_factory=lambda: FakeProvider([]))
    return local(app)


def test_uploading_a_report_adds_the_account(tmp_path):
    from test_mt5_report import report_html

    c = fresh_client(tmp_path)
    assert c.get("/api/accounts").json() == []
    body = report_html().encode("utf-16")  # as the terminal writes it
    first = c.post("/api/import", content=body, headers={"Content-Type": "application/octet-stream"}).json()
    assert first["login"] == 11112222 and first["new_deals"] == first["deals"] > 0 and first["trades"] > 0
    again = c.post("/api/import", content=body).json()
    assert again["new_deals"] == 0  # nothing is stored twice
    accounts = c.get("/api/accounts").json()
    assert [a["login"] for a in accounts] == [11112222] and accounts[0]["synced_at"]


def test_a_bad_upload_is_refused_cleanly(tmp_path):
    c = fresh_client(tmp_path)
    bad = c.post("/api/import", content=b"<html><body>not a report</body></html>")
    assert bad.status_code == 400 and "MT5 history report" in bad.json()["detail"]
    assert c.post("/api/import", content=b"").status_code == 400
    assert c.get("/api/accounts").json() == []


def test_sync_now_needs_a_live_worker_and_runs_one_at_a_time(tmp_path):
    import time as _time

    c = fresh_client(tmp_path)
    status = c.get("/api/sync").json()
    assert status["available"] is False and "not running" in status["reason"]
    assert c.post("/api/sync").status_code == 503
    folder = tmp_path / "sync"
    folder.mkdir()
    beat = f"beat={int(_time.time())}\n"
    (folder / "worker").write_text("login=1001\nready=no\nreason=MT5 is not set up yet.\n" + beat)
    status = c.get("/api/sync").json()
    assert status["available"] is False and status["reason"] == "MT5 is not set up yet."
    (folder / "worker").write_text("login=1001\nready=yes\nevery=0\n" + beat)
    asked = c.post("/api/sync")
    assert asked.status_code == 200 and asked.json()["requested"] and (folder / "request").is_file()
    assert c.post("/api/sync").status_code == 409  # already asked
    # the worker picks it up, then fails: the page gets the last line of its log
    (folder / "request").unlink()
    (folder / "job").write_text(f"state=failed\nstarted={int(_time.time()) - 60}\nfinished={int(_time.time())}\n")
    (folder / "job.log").write_text(">> starting\nThe terminal did not log in within 3 minutes.\n")
    status = c.get("/api/sync").json()
    assert status["state"] == "failed" and status["message"] == "The terminal did not log in within 3 minutes."
    # a stale heartbeat means the worker is gone
    (folder / "worker").write_text("login=1001\nready=yes\nbeat=1000\n")
    assert c.get("/api/sync").json()["available"] is False


def test_a_trade_and_a_sequence_on_a_price_chart(root, tmp_path):
    from tradequery.models import Bar

    c, _ = client(root)
    path = db.db_path(1002, root)
    conn = db.connect(path)
    try:
        seq_id, depth = conn.execute("SELECT sequence_id, depth FROM sequences ORDER BY depth DESC LIMIT 1").fetchone()
        pid = conn.execute("SELECT position_id FROM sequence_trades WHERE sequence_id = ? ORDER BY level",
                           (seq_id,)).fetchone()[0]
        t = conn.execute("SELECT symbol, open_time_utc, close_time_utc, open_price FROM trades WHERE position_id = ?",
                         (pid,)).fetchone()
        start, end = db.parse_time(t[1]), db.parse_time(t[2])
        minutes = int((end - start).total_seconds() // 60) + 1
        p = t[3]
        with conn:
            db.save_bars(conn, [Bar(t[0], start + timedelta(minutes=i), p, p + 1, p - 1, p + 0.5, 10)
                                for i in range(minutes)])
    finally:
        conn.close()

    trade = c.get("/api/trade", params={"login": 1002, "position_id": pid}).json()
    assert trade["kind"] == "trade" and trade["trade"]["position_id"] == pid and trade["sequence_id"] == seq_id
    assert trade["candles"] and trade["note"] is None
    assert [m["kind"] for m in trade["markers"]] == ["entry", "exit"]
    assert len(trade["candles"]) <= 240 and all(len(k) == 5 for k in trade["candles"])
    # the account without bars: no candles, a note, the fills still listed
    bare = c.get("/api/trade", params={"login": 1001, "position_id": pid}).json()
    assert bare["candles"] == [] and "No price bars" in bare["note"] and len(bare["markers"]) == 2

    seq = c.get("/api/sequence", params={"login": 1002, "sequence_id": seq_id}).json()
    assert seq["sequence"]["depth"] == depth == len(seq["trades"]) and len(seq["markers"]) == 2 * depth
    assert [m["level"] for m in seq["markers"] if m["kind"] == "entry"] == list(range(1, depth + 1))

    assert c.get("/api/trade", params={"login": 1002, "position_id": 999999999}).status_code == 404
    assert c.get("/api/sequence", params={"login": 1002, "sequence_id": 999999}).status_code == 404
    overview = c.get("/api/overview", params={"login": 1002}).json()
    assert overview["stats"]["deepest_sequence"]["id"] == seq_id


def test_long_spans_are_merged_into_wider_candles(tmp_path):
    from tradequery.models import Bar
    from tradequery.pricechart import MAX_CANDLES, _candles

    conn = db.connect(tmp_path / "x.db")
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    with conn:
        db.save_bars(conn, [Bar("XAUUSD", t0 + timedelta(minutes=i), 100 + i, 101 + i, 99 + i, 100.5 + i, 5)
                            for i in range(1000)])
    candles, size = _candles(conn, "XAUUSD", t0, t0 + timedelta(minutes=999))
    conn.close()
    assert size == 5 and len(candles) == 200 <= MAX_CANDLES
    first = candles[0]
    assert first[1] == 100 and first[2] == 105 and first[3] == 99 and first[4] == 104.5  # open, high, low, close
