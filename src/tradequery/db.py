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

from .models import TRADE_TYPES, CashFlow, Deal, Trade
from .settings import Settings, data_dir
from .trades import build_trades, derive_trades, extract_cash_flows

SCHEMA_VERSION = 1

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


def _create_schema(conn: sqlite3.Connection) -> None:
    statements = [
        f"CREATE TABLE accounts ({', '.join(_ACCOUNT_COLUMNS)})",
        f"CREATE TABLE symbols ({', '.join(f'{n} {t}' for n, (t, _) in _SYMBOL_COLUMNS.items())})",
        *(_model_ddl(table) for table in _MODEL_TABLES),
        *_INDEXES,
        f"PRAGMA user_version = {SCHEMA_VERSION}",
    ]
    with conn:
        for statement in statements:
            conn.execute(statement)


def connect(path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    """Open an account database. Writable connections create the file and schema if needed."""
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
    elif version != SCHEMA_VERSION:
        conn.close()
        raise SchemaError(
            f"{path} has schema version {version}, this code expects {SCHEMA_VERSION}. "
            "Delete the file and run the download again."
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
    return trades, flows
