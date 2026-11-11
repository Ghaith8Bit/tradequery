"""The optional page password, and backups of the data folder."""

import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from tradequery import db
from tradequery.api import LOCAL_HOSTS, create_app
from tradequery.auth import AuthConfigError, PasswordGate, gate_from_env
from tradequery.backup import backup
from tradequery.llm import FakeProvider
from tradequery.settings import load_settings
from tradequery.synthetic import generate_account

SETTINGS = load_settings()
PASSWORD = "correct horse battery"


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


def page_client(root, gate):
    app = create_app(data_root=root, settings=SETTINGS, provider_factory=lambda: FakeProvider([]), gate=gate)
    return TestClient(app, base_url="http://localhost", headers={"X-TradeQuery": "1"})


# ---- password ----

def test_without_a_password_the_page_is_open_on_this_computer(root):
    c = page_client(root, None)
    assert c.get("/api/accounts").status_code == 200
    assert c.post("/api/login", json={"password": "x"}).status_code == 404


def test_with_a_password_everything_waits_for_sign_in(root):
    c = page_client(root, PasswordGate(PASSWORD, root))
    assert c.get("/api/accounts").status_code == 401
    page = c.get("/")
    assert page.status_code == 200 and "Sign in" in page.text and "TradeQuery" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]  # the sign-in page is protected too
    assert c.post("/api/login", json={"password": "wrong password"}).status_code == 401
    ok = c.post("/api/login", json={"password": PASSWORD})
    assert ok.status_code == 200
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert c.get("/api/accounts").status_code == 200  # the cookie is kept by the client
    assert c.get("/api/status").json()["login_required"] is True
    c.post("/api/logout")
    assert c.get("/api/accounts").status_code == 401


def test_the_login_still_needs_the_page_header(root):
    app = create_app(data_root=root, settings=SETTINGS, provider_factory=lambda: FakeProvider([]),
                     gate=PasswordGate(PASSWORD, root))
    c = TestClient(app, base_url="http://localhost")
    assert c.post("/api/login", json={"password": PASSWORD}).status_code == 403


def test_wrong_passwords_are_limited(root):
    c = page_client(root, PasswordGate(PASSWORD, root))
    for _ in range(5):
        assert c.post("/api/login", json={"password": "guess guess"}).status_code == 401
    assert c.post("/api/login", json={"password": PASSWORD}).status_code == 429  # even the right one, for now


def test_cookies_cannot_be_forged_and_expire(tmp_path):
    now = [1_000_000.0]
    gate = PasswordGate(PASSWORD, tmp_path, clock=lambda: now[0])
    cookie = gate.new_cookie()
    assert gate.valid(cookie)
    expires, _, signature = cookie.partition(".")
    assert not gate.valid(f"{int(expires) + 999}.{signature}")  # a later expiry breaks the signature
    assert not gate.valid("garbage") and not gate.valid(None)
    assert not PasswordGate("another password", tmp_path, clock=lambda: now[0]).valid(cookie)  # new password: out
    now[0] += 31 * 24 * 3600
    assert not gate.valid(cookie)


def test_short_passwords_and_open_hosts_without_one_are_refused(tmp_path, monkeypatch):
    with pytest.raises(AuthConfigError):
        PasswordGate("short", tmp_path)
    monkeypatch.delenv("TQ_PASSWORD", raising=False)
    assert gate_from_env(tmp_path, list(LOCAL_HOSTS), LOCAL_HOSTS) is None
    with pytest.raises(AuthConfigError):
        gate_from_env(tmp_path, [*LOCAL_HOSTS, "192.168.1.20"], LOCAL_HOSTS)
    monkeypatch.setenv("TQ_PASSWORD", PASSWORD)
    assert gate_from_env(tmp_path, [*LOCAL_HOSTS, "192.168.1.20"], LOCAL_HOSTS) is not None


# ---- backups ----

def test_backup_copies_every_account_and_keeps_the_newest(root):
    (root / "news").mkdir()
    (root / "news" / "calendar.csv").write_text("event")
    first = backup(root, keep=2, now=datetime(2026, 11, 1, tzinfo=timezone.utc))
    copy = sqlite3.connect(first / "accounts" / "1001.db")
    assert copy.execute("select count(*) from deals").fetchone()[0] > 0
    copy.close()
    assert (first / "news" / "calendar.csv").read_text() == "event"
    backup(root, keep=2, now=datetime(2026, 11, 2, tzinfo=timezone.utc))
    backup(root, keep=2, now=datetime(2026, 11, 3, tzinfo=timezone.utc))
    kept = sorted(p.name for p in (root / "backups").iterdir())
    assert kept == ["2026-11-02_000000", "2026-11-03_000000"]
