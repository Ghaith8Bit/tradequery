"""One SQLite database per account: data/accounts/<login>.db (git-ignored).

Tables
  accounts    one row: broker, server, currency, leverage, margin mode, balance
              at the last sync, earliest deal. Never the account holder's name.
  symbols     contract specs of the symbols traded
  deals       every deal, normalized (models.Deal); the source of truth, upserted
              by ticket so a re-sync never duplicates
  cash_flows  balance operations (models.CashFlow), rebuilt from deals
  trades      closed positions with derived fields (models.Trade), rebuilt from deals

The deals, cash_flows and trades columns are generated from the dataclasses in
models.py, so the schema follows the models. Times are UTC text,
"YYYY-MM-DD HH:MM:SS", which sorts correctly and works with SQLite's date
functions.

Write functions do not commit: wrap a whole sync in ``with conn:`` so it lands
all at once or not at all. Analysis and the API open the file with
``connect(path, read_only=True)``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path

from . import detectors
from . import news
from .detectors import sequences
from .models import TRADE_TYPES, Bar, CashFlow, Deal, Trade
from .settings import Settings, data_dir
from .trades import build_trades, derive_trades, extract_cash_flows

SCHEMA_VERSION = 5

_SQL_TYPES = {"int": "INTEGER", "float": "REAL", "str": "TEXT", "datetime": "TEXT", "bool": "INTEGER"}

_ACCOUNT_COLUMNS = (
    "account_id INTEGER PRIMARY KEY",
    "label TEXT",
    "broker TEXT",
    "server TEXT",
    "currency TEXT",
    "leverage INTEGER",
    "margin_mode TEXT",
    "trade_mode TEXT",
    "trade_allowed INTEGER",
    "balance REAL",
    "equity REAL",
    "opening_balance REAL NOT NULL DEFAULT 0",  # balance before the first stored deal
    "earliest_history_utc TEXT",
    "synced_at_utc TEXT",
)

# symbols column <- key in the MT5 spec dict (mt5.SYMBOL_SPEC_FIELDS)
_SYMBOL_COLUMNS = {
    "symbol": ("TEXT PRIMARY KEY", "name"),
    "canonical_name": ("TEXT NOT NULL", "name"),  # broker-suffix aliases come later
    "description": ("TEXT", "description"),
    "base_currency": ("TEXT", "currency_base"),
    "profit_currency": ("TEXT", "currency_profit"),
    "margin_currency": ("TEXT", "currency_margin"),
    "digits": ("INTEGER", "digits"),
    "point": ("REAL", "point"),
    "contract_size": ("REAL", "trade_contract_size"),
    "tick_size": ("REAL", "trade_tick_size"),
    "tick_value": ("REAL", "trade_tick_value"),
    "swap_long": ("REAL", "swap_long"),
    "swap_short": ("REAL", "swap_short"),
    "swap_mode": ("INTEGER", "swap_mode"),
    "swap_rollover3days": ("INTEGER", "swap_rollover3days"),
    "volume_min": ("REAL", "volume_min"),
    "volume_max": ("REAL", "volume_max"),
    "volume_step": ("REAL", "volume_step"),
}

# table -> (dataclass, primary key)
_MODEL_TABLES = {
    "deals": (Deal, "ticket"),
    "cash_flows": (CashFlow, "flow_id"),
    "trades": (Trade, "position_id"),
}

_INDEXES = (
    "CREATE INDEX deals_time ON deals (time_utc)",
    "CREATE INDEX deals_position ON deals (position_id)",
    "CREATE INDEX trades_close ON trades (close_time_utc)",
    "CREATE INDEX trades_symbol ON trades (symbol)",
    "CREATE INDEX cash_flows_time ON cash_flows (time_utc)",
)


class SchemaError(RuntimeError):
    """The database file was made by a different version of TradeQuery."""


# ---------------------------------------------------------------- paths


def db_path(login: int | str, root: Path | str | None = None) -> Path:
    return Path(root or data_dir()) / "accounts" / f"{login}.db"


def list_accounts(root: Path | str | None = None) -> list[int]:
    """Logins that have a database, sorted."""
    folder = Path(root or data_dir()) / "accounts"
    if not folder.is_dir():
        return []
    return sorted(int(p.stem) for p in folder.glob("*.db") if p.stem.isdigit())


# ---------------------------------------------------------------- times


def format_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError(f"naive datetime {value!r}: times must be aware UTC")
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%d %H:%M:%S.%f" if value.microsecond else "%Y-%m-%d %H:%M:%S")


def parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- schema


def _kind(field_type) -> str:
    """'float | None' -> 'float'. models.py uses postponed annotations, so these are strings."""
    name = field_type if isinstance(field_type, str) else field_type.__name__
    return name.split("|")[0].strip()


def _model_ddl(table: str) -> str:
    model, key = _MODEL_TABLES[table]
    columns = []
    for f in fields(model):
        nullable = isinstance(f.type, str) and "None" in f.type
        column = f"{f.name} {_SQL_TYPES[_kind(f.type)]}"
        if f.name == key:
            column += " PRIMARY KEY"
        elif not nullable:
            column += " NOT NULL"
        columns.append(column)
    return f"CREATE TABLE {table} ({', '.join(columns)})"


# Upgrades from one schema version to the next. They only add things, so an
# existing account database upgrades in place the next time it is opened.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: (
        # One-minute bars (bid prices; spread in points), cached for the windows
        # in which positions were open. Input to the price path; never shown to the LLM.
        "CREATE TABLE bars (symbol TEXT NOT NULL, time_utc TEXT NOT NULL, open REAL NOT NULL, "
        "high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, spread INTEGER NOT NULL, "
        "PRIMARY KEY (symbol, time_utc)) WITHOUT ROWID",
    ),
    3: (
        # Detector tags, one row per position and rule, with a short evidence sentence.
        # Rebuilt from the trades on every sync or import.
        "CREATE TABLE tags (position_id INTEGER NOT NULL, rule TEXT NOT NULL, evidence TEXT NOT NULL, "
        "PRIMARY KEY (position_id, rule))",
        "CREATE INDEX tags_rule ON tags (rule)",
    ),
    4: (
        # Martingale and grid series, and which positions (in order) belong to each.
        "CREATE TABLE sequences (sequence_id INTEGER PRIMARY KEY, type TEXT NOT NULL, symbol TEXT NOT NULL, "
        "side TEXT NOT NULL, depth INTEGER NOT NULL, first_lot REAL NOT NULL, max_lot REAL NOT NULL, "
        "max_lot_multiplier REAL NOT NULL, max_open INTEGER NOT NULL, start_utc TEXT NOT NULL, "
        "end_utc TEXT NOT NULL, net_pnl REAL NOT NULL, is_win INTEGER NOT NULL)",
        "CREATE TABLE sequence_trades (sequence_id INTEGER NOT NULL, position_id INTEGER NOT NULL, "
        "level INTEGER NOT NULL, PRIMARY KEY (sequence_id, position_id))",
        "CREATE INDEX sequence_trades_position ON sequence_trades (position_id)",
    ),
    5: (
        # Calendar events (UTC) and which trades overlapped which events.
        "CREATE TABLE news_events (value_id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, "
        "time_utc TEXT NOT NULL, currency TEXT NOT NULL, importance TEXT NOT NULL, name TEXT NOT NULL, "
        "actual REAL, forecast REAL, previous REAL)",
        "CREATE INDEX news_events_time ON news_events (time_utc)",
        "CREATE TABLE trade_news (position_id INTEGER NOT NULL, value_id INTEGER NOT NULL, "
        "minutes_to_event REAL NOT NULL, phase TEXT NOT NULL, PRIMARY KEY (position_id, value_id))",
    ),
}


def _create_schema(conn: sqlite3.Connection) -> None:
    statements = [
        f"CREATE TABLE accounts ({', '.join(_ACCOUNT_COLUMNS)})",
        f"CREATE TABLE symbols ({', '.join(f'{n} {t}' for n, (t, _) in _SYMBOL_COLUMNS.items())})",
        *(_model_ddl(table) for table in _MODEL_TABLES),
        *_INDEXES,
        "PRAGMA user_version = 1",
    ]
    with conn:
        for statement in statements:
            conn.execute(statement)
    _migrate(conn, 1)


def _migrate(conn: sqlite3.Connection, version: int) -> None:
    with conn:
        for target in range(version + 1, SCHEMA_VERSION + 1):
            for statement in MIGRATIONS[target]:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {target}")


def connect(path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    """Open an account database. Writable connections create the file and schema if needed.

    A database from an older version of TradeQuery is upgraded in place first,
    even when it is then opened read-only.
    """
    path = Path(path)
    if read_only:
        if not path.is_file():
            raise FileNotFoundError(f"no account database at {path}")
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0 and not read_only:
        _create_schema(conn)
    elif 0 < version < SCHEMA_VERSION:
        if read_only:
            conn.close()
            connect(path).close()  # upgrade with a writable connection, then reopen
            return connect(path, read_only=True)
        _migrate(conn, version)
    elif version != SCHEMA_VERSION:
        conn.close()
        raise SchemaError(
            f"{path} has schema version {version}, this code expects {SCHEMA_VERSION}. "
            "It was made by a newer TradeQuery, or is not a TradeQuery database."
        )
    return conn


# ---------------------------------------------------------------- dataclass rows


def _to_db(value, kind: str):
    if value is None:
        return None
    if kind == "datetime":
        return format_time(value)
    if kind == "bool":
        return int(value)
    return value


def _from_db(value, kind: str):
    if value is None:
        return None
    if kind == "datetime":
        return parse_time(value)
    if kind == "bool":
        return bool(value)
    if kind == "float":
        return float(value)
    return value


def _write_rows(conn: sqlite3.Connection, table: str, items, *, replace_all: bool) -> None:
    model, _ = _MODEL_TABLES[table]
    specs = [(f.name, _kind(f.type)) for f in fields(model)]
    names = ", ".join(name for name, _ in specs)
    marks = ", ".join("?" for _ in specs)
    if replace_all:
        conn.execute(f"DELETE FROM {table}")
    conn.executemany(
        f"INSERT OR REPLACE INTO {table} ({names}) VALUES ({marks})",
        ([_to_db(getattr(item, name), kind) for name, kind in specs] for item in items),
    )


def _read_rows(conn: sqlite3.Connection, table: str, order_by: str) -> list:
    model, _ = _MODEL_TABLES[table]
    specs = [(f.name, _kind(f.type)) for f in fields(model)]
    rows = conn.execute(f"SELECT {', '.join(n for n, _ in specs)} FROM {table} ORDER BY {order_by}")
    return [model(**{name: _from_db(row[name], kind) for name, kind in specs}) for row in rows]


# ---------------------------------------------------------------- accounts and symbols


def save_account(conn: sqlite3.Connection, account: dict, *, earliest_history: datetime | None,
                 synced_at: datetime) -> None:
    """Upsert the account row from mt5.fetch_account output. The holder's name is dropped.

    ``opening_balance`` is 0 for a full history (the live sync); a report that
    starts later passes the balance before its first deal.
    """
    trade_allowed = account.get("trade_allowed")
    row = {
        "account_id": int(account["login"]),
        "broker": account.get("company"),
        "server": account.get("server"),
        "currency": account.get("currency"),
        "leverage": account.get("leverage"),
        "margin_mode": account.get("margin_mode"),
        "trade_mode": account.get("trade_mode"),
        "trade_allowed": None if trade_allowed is None else int(bool(trade_allowed)),
        "balance": account.get("balance"),
        "equity": account.get("equity"),
        "opening_balance": float(account.get("opening_balance", 0.0)),
        "earliest_history_utc": format_time(earliest_history) if earliest_history else None,
        "synced_at_utc": format_time(synced_at),
    }
    updates = ", ".join(f"{k} = excluded.{k}" for k in row if k != "account_id")
    conn.execute(
        f"INSERT INTO accounts ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)}) "
        f"ON CONFLICT (account_id) DO UPDATE SET {updates}",  # keeps a label set by hand
        list(row.values()),
    )


def load_account(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT * FROM accounts").fetchone()
    if row is None:
        raise LookupError("this database has no account row; run the download again")
    return dict(row)


def save_symbols(conn: sqlite3.Connection, specs: list[dict]) -> None:
    """Upsert symbol specs from mt5.fetch_symbol_specs. Entries with an "error" are skipped."""
    names = list(_SYMBOL_COLUMNS)
    conn.executemany(
        f"INSERT OR REPLACE INTO symbols ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
        ([spec.get(key) for _, key in _SYMBOL_COLUMNS.values()] for spec in specs if "error" not in spec),
    )


def load_symbols(conn: sqlite3.Connection) -> list[dict]:
    return [dict(row) for row in conn.execute("SELECT * FROM symbols ORDER BY symbol")]


# ---------------------------------------------------------------- deals and derived tables


def upsert_deals(conn: sqlite3.Connection, deals: list[Deal], *, overwrite: bool = True) -> int:
    """Insert deals by ticket. Returns how many tickets were new.

    With ``overwrite`` (the live sync) a stored deal is replaced. Without it
    (report import) stored deals are kept, so a report, which lacks magic
    numbers and deal reasons, never replaces richer synced rows.
    """
    before = conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
    if not overwrite:
        stored = {row[0] for row in conn.execute("SELECT ticket FROM deals")}
        deals = [d for d in deals if d.ticket not in stored]
    _write_rows(conn, "deals", deals, replace_all=False)
    return conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0] - before


def save_bars(conn: sqlite3.Connection, bars: list[Bar]) -> int:
    """Insert or replace one-minute bars. Returns how many rows were written."""
    conn.executemany(
        "INSERT OR REPLACE INTO bars (symbol, time_utc, open, high, low, close, spread) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(b.symbol, format_time(b.time_utc), b.open, b.high, b.low, b.close, int(b.spread)) for b in bars],
    )
    return len(bars)


def load_bars(conn: sqlite3.Connection, symbol: str | None = None) -> dict[str, list[Bar]]:
    """Stored bars by symbol, oldest first."""
    query = "SELECT symbol, time_utc, open, high, low, close, spread FROM bars"
    params: tuple = ()
    if symbol:
        query += " WHERE symbol = ?"
        params = (symbol,)
    out: dict[str, list[Bar]] = {}
    for row in conn.execute(query + " ORDER BY symbol, time_utc", params):
        out.setdefault(row[0], []).append(Bar(row[0], parse_time(row[1]), row[2], row[3], row[4], row[5], row[6]))
    return out


def bar_count(conn: sqlite3.Connection, symbol: str, start: datetime, end: datetime) -> int:
    """How many bars of a symbol are stored with start <= time <= end."""
    return conn.execute(
        "SELECT COUNT(*) FROM bars WHERE symbol = ? AND time_utc BETWEEN ? AND ?",
        (symbol, format_time(start), format_time(end)),
    ).fetchone()[0]


def latest_deal_time(conn: sqlite3.Connection) -> datetime | None:
    """Time of the newest stored deal (where the next incremental sync picks up), or None."""
    row = conn.execute("SELECT MAX(time_utc) FROM deals").fetchone()
    return parse_time(row[0]) if row and row[0] else None


def load_deals(conn: sqlite3.Connection) -> list[Deal]:
    return _read_rows(conn, "deals", "time_utc, ticket")


def load_cash_flows(conn: sqlite3.Connection) -> list[CashFlow]:
    return _read_rows(conn, "cash_flows", "time_utc, flow_id")


def load_trades(conn: sqlite3.Connection) -> list[Trade]:
    return _read_rows(conn, "trades", "close_time_utc, position_id")


def rebuild_derived(conn: sqlite3.Connection, settings: Settings) -> tuple[list[Trade], list[CashFlow]]:
    """Recompute cash_flows and trades from every stored deal, replacing the old rows."""
    deals = load_deals(conn)
    row = conn.execute("SELECT opening_balance FROM accounts").fetchone()
    flows = extract_cash_flows(deals)
    closed = build_trades(deals)
    # Exits of positions that are not closed trades (opened before the history
    # starts, or only partly closed) still move the balance, so the running
    # balance includes them. They are not cash flows and are not stored as such.
    closed_ids = {t.position_id for t in closed}
    stray_exits = [
        CashFlow(d.ticket, d.time_utc, "position_exit", round(d.profit + d.commission + d.swap, 2))
        for d in deals
        if d.type in TRADE_TYPES and d.entry != "in" and d.position_id not in closed_ids
    ]
    trades = derive_trades(closed, flows + stray_exits, settings, initial_balance=row[0] if row else 0.0)
    _write_rows(conn, "cash_flows", flows, replace_all=True)
    _write_rows(conn, "trades", trades, replace_all=True)
    found = sequences.find_sequences(trades, settings.detectors)
    save_sequences(conn, found)
    events = load_news_events(conn)
    specs = {s["symbol"]: s for s in load_symbols(conn)}
    links = news.join(trades, events, settings.news, specs) if events else []
    save_trade_news(conn, links)
    save_tags(conn, detectors.run_all(trades, settings, flows) + sequences.sequence_tags(found, trades)
              + news.news_tags(links, events))
    return trades, flows


def save_news_events(conn: sqlite3.Connection, events) -> int:
    """Insert or replace calendar events (news.NewsEvent). Returns how many were new."""
    before = conn.execute("SELECT COUNT(*) FROM news_events").fetchone()[0]
    conn.executemany(
        "INSERT OR REPLACE INTO news_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(e.value_id, e.event_id, format_time(e.time_utc), e.currency, e.importance, e.name,
          e.actual, e.forecast, e.previous) for e in events],
    )
    return conn.execute("SELECT COUNT(*) FROM news_events").fetchone()[0] - before


def load_news_events(conn: sqlite3.Connection) -> list:
    return [
        news.NewsEvent(r[0], r[1], parse_time(r[2]), r[3], r[4], r[5], r[6], r[7], r[8])
        for r in conn.execute("SELECT * FROM news_events ORDER BY time_utc, value_id")
    ]


def save_trade_news(conn: sqlite3.Connection, links) -> None:
    conn.execute("DELETE FROM trade_news")
    conn.executemany("INSERT OR REPLACE INTO trade_news VALUES (?, ?, ?, ?)",
                     [(l.position_id, l.value_id, l.minutes_to_event, l.phase) for l in links])


def load_trade_news(conn: sqlite3.Connection) -> list:
    return [news.TradeNews(*r) for r in conn.execute("SELECT * FROM trade_news ORDER BY position_id, value_id")]


def save_sequences(conn: sqlite3.Connection, found) -> None:
    """Replace every stored sequence (detectors.sequences.Sequence) and its members."""
    conn.execute("DELETE FROM sequence_trades")
    conn.execute("DELETE FROM sequences")
    conn.executemany(
        "INSERT INTO sequences VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(s.sequence_id, s.type, s.symbol, s.side, s.depth, s.first_lot, s.max_lot, s.max_lot_multiplier,
          s.max_open, format_time(s.start_utc), format_time(s.end_utc), s.net, int(s.is_win)) for s in found],
    )
    conn.executemany(
        "INSERT INTO sequence_trades VALUES (?, ?, ?)",
        [(s.sequence_id, pid, level) for s in found for level, pid in enumerate(s.position_ids, start=1)],
    )


def load_sequences(conn: sqlite3.Connection) -> list:
    """Stored sequences (detectors.sequences.Sequence), by start time."""
    members: dict[int, list[int]] = {}
    for sid, pid in conn.execute("SELECT sequence_id, position_id FROM sequence_trades ORDER BY sequence_id, level"):
        members.setdefault(sid, []).append(pid)
    return [
        sequences.Sequence(row["sequence_id"], row["type"], row["symbol"], row["side"],
                           tuple(members.get(row["sequence_id"], ())), row["first_lot"], row["max_lot"],
                           row["max_open"], parse_time(row["start_utc"]), parse_time(row["end_utc"]), row["net_pnl"])
        for row in conn.execute("SELECT * FROM sequences ORDER BY sequence_id")
    ]


def save_tags(conn: sqlite3.Connection, tags) -> None:
    """Replace every stored tag with these (detectors.Tag rows)."""
    conn.execute("DELETE FROM tags")
    conn.executemany("INSERT INTO tags (position_id, rule, evidence) VALUES (?, ?, ?)",
                     [(t.position_id, t.rule, t.evidence) for t in tags])


def load_tags(conn: sqlite3.Connection, rule: str | None = None) -> list:
    """Stored tags (detectors.Tag), optionally of one rule, by position."""
    query, params = "SELECT position_id, rule, evidence FROM tags", ()
    if rule:
        query, params = query + " WHERE rule = ?", (rule,)
    return [detectors.Tag(*row) for row in conn.execute(query + " ORDER BY position_id, rule", params)]
