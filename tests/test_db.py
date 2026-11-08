import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from tradequery import db
from tradequery.settings import load_settings
from tradequery.synthetic import generate_account
from tradequery.trades import build_trades, derive_trades, extract_cash_flows

UTC = timezone.utc
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(db.db_path(777, tmp_path))
    yield connection
    connection.close()


def test_one_database_file_per_account(tmp_path):
    assert db.db_path(777, tmp_path) == tmp_path / "accounts" / "777.db"
    assert db.list_accounts(tmp_path) == []
    for login in (20, 3):
        db.connect(db.db_path(login, tmp_path)).close()
    (tmp_path / "accounts" / "notes.db").touch()  # not an account number
    assert db.list_accounts(tmp_path) == [3, 20]


def test_schema_has_every_table_and_a_version(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == {"accounts", "symbols", "deals", "cash_flows", "trades", "bars", "tags", "sequences", "sequence_trades", "news_events", "trade_news", "trade_facts", "streaks", "drawdown_episodes", "exposure", "equity_curve"}
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_a_database_from_another_version_is_refused(tmp_path):
    path = db.db_path(1, tmp_path)
    db.connect(path).close()
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA user_version = 99")
    raw.close()
    with pytest.raises(db.SchemaError, match="version 99"):
        db.connect(path)


def test_times_are_utc_text_that_sorts_and_round_trips():
    t = datetime(2026, 3, 8, 6, 59, 59, tzinfo=UTC)
    assert db.format_time(t) == "2026-03-08 06:59:59"
    assert db.parse_time(db.format_time(t)) == t
    fine = t.replace(microsecond=500000)
    assert db.parse_time(db.format_time(fine)) == fine
    assert db.format_time(t) < db.format_time(fine)
    with pytest.raises(ValueError, match="naive"):
        db.format_time(datetime(2026, 1, 1))


def test_deals_survive_a_save_and_load(conn):
    deals = generate_account().deals
    with conn:
        assert db.upsert_deals(conn, deals) == len(deals)
    assert db.load_deals(conn) == deals


def test_upsert_updates_by_ticket_and_never_duplicates(conn):
    deals = generate_account().deals
    with conn:
        db.upsert_deals(conn, deals)
        changed = replace(deals[5], comment="edited")
        assert db.upsert_deals(conn, [changed, *deals[:3]]) == 0
    stored = db.load_deals(conn)
    assert len(stored) == len(deals)
    assert stored[5].comment == "edited"


def test_derived_tables_match_the_in_memory_pipeline(conn):
    deals = generate_account().deals
    settings = load_settings()
    flows = extract_cash_flows(deals)
    expected = derive_trades(build_trades(deals), flows, settings)
    with conn:
        db.upsert_deals(conn, deals)
        db.rebuild_derived(conn, settings)
        db.rebuild_derived(conn, settings)  # rebuilding replaces, never appends
    assert db.load_trades(conn) == expected
    assert db.load_cash_flows(conn) == flows
    assert isinstance(db.load_trades(conn)[0].is_win, bool)


def test_account_row_drops_the_name_and_keeps_a_hand_set_label(conn):
    account = {"login": 777, "name": "Real Person", "company": "Broker Ltd", "server": "Srv",
               "currency": "USD", "leverage": 500, "balance": 100.0, "equity": 90.0,
               "margin_mode": "retail_hedging", "trade_mode": "real", "trade_allowed": False}
    with conn:
        db.save_account(conn, account, earliest_history=NOW, synced_at=NOW)
        conn.execute("UPDATE accounts SET label = 'TMX main'")
        db.save_account(conn, dict(account, balance=150.0), earliest_history=NOW, synced_at=NOW)
    row = db.load_account(conn)
    assert "Real Person" not in str(row)
    assert row["label"] == "TMX main" and row["balance"] == 150.0
    assert row["broker"] == "Broker Ltd" and row["trade_allowed"] == 0
    assert row["earliest_history_utc"] == "2026-11-08 12:00:00"


def test_symbol_specs_are_mapped_and_errors_skipped(conn):
    specs = [
        {"name": "XAUUSD", "trade_contract_size": 100.0, "trade_tick_size": 0.01,
         "trade_tick_value": 1.0, "currency_profit": "USD", "digits": 2},
        {"name": "BOGUS", "error": "symbol not found on this server"},
    ]
    with conn:
        db.save_symbols(conn, specs)
    (row,) = db.load_symbols(conn)
    assert row["symbol"] == row["canonical_name"] == "XAUUSD"
    assert (row["contract_size"], row["tick_size"], row["tick_value"]) == (100.0, 0.01, 1.0)
    assert row["profit_currency"] == "USD" and row["swap_long"] is None


def test_read_only_connection_cannot_write(tmp_path):
    path = db.db_path(5, tmp_path)
    db.connect(path).close()
    ro = db.connect(path, read_only=True)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        ro.execute("DELETE FROM deals")
    ro.close()
    with pytest.raises(FileNotFoundError):
        db.connect(db.db_path(6, tmp_path), read_only=True)


def test_a_failed_sync_leaves_the_database_unchanged(conn):
    deals = generate_account().deals
    with conn:
        db.upsert_deals(conn, deals[:10])
    with pytest.raises(RuntimeError):
        with conn:
            db.upsert_deals(conn, deals)
            raise RuntimeError("terminal disconnected")
    assert len(db.load_deals(conn)) == 10


def test_a_version_1_database_upgrades_in_place_even_when_opened_read_only(tmp_path):
    path = db.db_path(1, tmp_path)
    conn = db.connect(path)
    deals = generate_account().deals
    with conn:
        db.upsert_deals(conn, deals)
        conn.execute("DROP TABLE bars")
        conn.execute("DROP TABLE tags")
        conn.execute("DROP TABLE sequence_trades")
        conn.execute("DROP TABLE sequences")
        conn.execute("DROP TABLE news_events")
        conn.execute("DROP TABLE trade_news")
        for name in ("v_trades", "v_sequences", "v_cash_flows"):
            conn.execute(f"DROP VIEW {name}")
        for name in ("trade_facts", "streaks", "drawdown_episodes", "exposure", "equity_curve"):
            conn.execute(f"DROP TABLE {name}")
        conn.execute("PRAGMA user_version = 1")  # what an older TradeQuery left behind
    conn.close()
    ro = db.connect(path, read_only=True)
    assert ro.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert db.load_deals(ro) == deals and db.load_bars(ro) == {}
    ro.close()


def test_bars_survive_a_save_and_load(conn):
    from tradequery.models import Bar
    t = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    bars = [Bar("XAUUSD", t, 2000.0, 2001.5, 1999.0, 2001.0, 12), Bar("XAUUSD", t.replace(minute=1), 2001.0, 2002.0, 2000.5, 2001.5, 10)]
    with conn:
        db.save_bars(conn, bars)
        db.save_bars(conn, bars[:1])  # re-saving replaces, never duplicates
    assert db.load_bars(conn) == {"XAUUSD": bars}
    assert db.bar_count(conn, "XAUUSD", t, t.replace(minute=5)) == 2
