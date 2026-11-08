"""QuerySpec safety: a rejected spec for every whitelist rule, injection attempts, row cap, time limit."""

import sqlite3
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text

from tradequery import db
from tradequery.compiler import build_select, to_sql, to_utc
from tradequery.execute import QueryTimeout, _run, execute
from tradequery.settings import load_settings
from tradequery.spec import SpecError, parse_spec
from tradequery.synthetic import generate_account

SETTINGS = load_settings()


@pytest.fixture(scope="module")
def path(tmp_path_factory):
    p = db.db_path(9, tmp_path_factory.mktemp("acct"))
    conn = db.connect(p)
    with conn:
        db.upsert_deals(conn, generate_account().deals)
        db.save_account(conn, {"login": 9, "balance": 0.0}, earliest_history=None, synced_at=datetime.now(timezone.utc))
        db.rebuild_derived(conn, SETTINGS)
    conn.close()
    return p


@pytest.mark.parametrize("spec, message", [
    ({"entity": "trades", "filters": [{"field": "comment", "op": "eq", "value": "x"}]}, "unknown field 'comment'"),
    ({"entity": "orders"}, "entity"),
    ({"entity": "trades", "filters": [{"field": "symbol", "op": "gt", "value": "A"}]}, "allowed operators"),
    ({"entity": "trades", "filters": [{"field": "symbol", "op": "like", "value": "%"}]}, "op"),
    ({"entity": "trades", "filters": [{"field": "net_profit", "op": "lt", "value": "-50"}]}, "needs a number"),
    ({"entity": "trades", "filters": [{"field": "is_win", "op": "eq", "value": 1}]}, "true or false"),
    ({"entity": "trades", "filters": [{"field": "lots", "op": "between", "value": [1]}]}, "exactly two"),
    ({"entity": "trades", "filters": [{"field": "tags", "op": "eq", "value": "lucky"}]}, "unknown tag"),
    ({"entity": "trades", "filters": [{"field": "open_time_utc", "op": "gt", "value": "yesterday"}]}, "ISO time"),
    ({"entity": "trades", "group_by": ["position_id"]}, "cannot group"),
    ({"entity": "trades", "aggregations": [{"func": "sum", "field": "symbol", "alias": "s"}]}, "number field"),
    ({"entity": "trades", "aggregations": [{"func": "sum", "field": "lots", "alias": "Bad Alias"}]}, "alias"),
    ({"entity": "trades", "metrics": ["sharpe"]}, "unknown metric"),
    ({"entity": "sequences", "metrics": ["win_rate"]}, "computed on trades"),
    ({"entity": "trades", "metrics": ["win_rate"], "compare": [{"label": "only one", "filters": []}]}, "two labelled"),
    ({"entity": "trades", "compare": [{"label": "a"}, {"label": "b"}]}, "needs at least one metric"),
    ({"entity": "trades", "as_of": "2026-09-01T10:00"}, "only for the exposure"),
    ({"entity": "trades", "fields": ["lots"], "aggregations": [{"func": "count", "alias": "n"}]}, "cannot be combined"),
    ({"entity": "trades", "order_by": [{"field": "tags"}]}, "order_by"),
    ({"entity": "trades", "limit": 501}, "limit"),
    ({"entity": "trades", "sql": "DROP TABLE trades"}, "sql"),
    ({"kind": "clarify"}, "needs a message"),
    ({}, "needs an entity"),
])
def test_specs_that_break_a_rule_are_rejected_with_a_reason(spec, message):
    with pytest.raises(SpecError, match=message):
        parse_spec(spec)


def test_json_text_is_accepted_and_errors_are_listed():
    assert parse_spec('{"entity": "streaks"}').entity == "streaks"
    with pytest.raises(SpecError) as error:
        parse_spec('{"entity": "trades", "limit": 0, "extra": 1}')
    assert "limit" in str(error.value) and "extra" in str(error.value)


def test_injection_text_is_only_ever_a_value(path):
    spec = parse_spec({"entity": "trades", "filters": [
        {"field": "symbol", "op": "eq", "value": "XAUUSD'; DROP TABLE trades; --"}]})
    sql, params = to_sql(build_select(spec, SETTINGS.clock))
    assert "DROP" not in sql and params[0].startswith("XAUUSD'")
    assert execute(spec, path, SETTINGS).rows == []
    conn = db.connect(path, read_only=True)
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] > 0  # still there
    conn.close()


def test_clarify_and_unsupported_pass_their_message_through(path):
    result = execute(parse_spec({"kind": "clarify", "message": "Which account?"}), path, SETTINGS)
    assert (result.kind, result.message, result.rows) == ("clarify", "Which account?", [])
    assert execute(parse_spec({"kind": "unsupported", "message": "Several accounts at once"}), path, SETTINGS).kind == "unsupported"


def test_row_cap_and_truncation_flag(path):
    top = execute(parse_spec({"entity": "trades", "limit": 5}), path, SETTINGS)
    assert len(top.rows) == 5 and not top.truncated  # asked for five: nothing is missing
    capped = execute(parse_spec({"entity": "streaks"}), path, SETTINGS)  # default cap 100
    conn = db.connect(path, read_only=True)
    total = conn.execute("SELECT COUNT(*) FROM streaks").fetchone()[0]
    conn.close()
    assert capped.truncated == (total > 100)
    everything = execute(parse_spec({"entity": "streaks", "limit": 500}), path, SETTINGS)
    assert not everything.truncated


def test_time_limit_interrupts_a_slow_query(path):
    conn = db.connect(path, read_only=True)
    slow = text("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 50000000) SELECT COUNT(*) FROM n")
    with pytest.raises(QueryTimeout):
        _run(conn, slow, 0.05)
    conn.close()


def test_the_connection_is_read_only(path):
    conn = db.connect(path, read_only=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM trades")
    conn.close()


def test_time_ranges_default_to_server_time():
    clock = SETTINGS.clock  # New York close: UTC+3 in September
    assert to_utc(datetime(2026, 9, 1, 3, 0), "server", clock) == datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    assert to_utc(datetime(2026, 9, 1, 3, 0), "utc", clock) == datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)
    assert to_utc(date(2026, 9, 1), "server", clock, end_of_day=True) == datetime(2026, 9, 1, 21, 0, tzinfo=timezone.utc)


def test_a_time_range_filters_by_the_entity_time(path):
    conn = db.connect(path, read_only=True)
    first_day = SETTINGS.clock.from_utc(db.load_trades(conn)[0].close_time_utc).date()
    conn.close()
    spec = parse_spec({"entity": "trades", "time_range": {"start": str(first_day), "end": str(first_day)},
                       "aggregations": [{"func": "count", "alias": "n"}]})
    (row,) = execute(spec, path, SETTINGS).rows
    assert row[0] >= 1


def test_as_of_gives_the_exposure_in_force_at_a_moment(path):
    conn = db.connect(path, read_only=True)
    t = db.load_trades(conn)[10]
    conn.close()
    moment = SETTINGS.clock.from_utc(t.open_time_utc).isoformat()
    result = execute(parse_spec({"entity": "exposure", "as_of": moment}), path, SETTINGS)
    (row,) = result.rows
    assert dict(zip(result.columns, row))["gross_lots"] >= t.lots
