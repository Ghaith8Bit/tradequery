"""Price charts for one trade or one martingale/grid sequence.

Uses the one-minute bars a sync stored around each open position. Many bars
are merged into wider candles, about MAX_CANDLES per chart.
Entries and exits come from the stored trades, so every marker is a real fill.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path

from . import db

MAX_CANDLES = 240


class NotFound(LookupError):
    pass


def _padding(start: datetime, end: datetime) -> timedelta:
    """Context either side: a tenth of the span, between 15 minutes and 4 hours."""
    span = (end - start).total_seconds() / 60
    return timedelta(minutes=min(240, max(15, span / 10)))


def _candles(conn, symbol: str, start: datetime, end: datetime) -> tuple[list, int]:
    rows = conn.execute(
        "SELECT time_utc, open, high, low, close FROM bars WHERE symbol = ? AND time_utc >= ? AND time_utc <= ? "
        "ORDER BY time_utc",
        (symbol, db.format_time(start), db.format_time(end)),
    ).fetchall()
    if not rows:
        return [], 1
    # minutes per candle, from the bars there are: hours without prices (weekends) cost nothing
    size = max(1, math.ceil(len(rows) / MAX_CANDLES))
    out: list[list] = []
    bucket = None
    for t, o, h, low, c in rows:
        minutes = int((db.parse_time(t) - start).total_seconds() // 60)
        key = minutes // size
        if key != bucket:
            bucket = key
            out.append([db.format_time(start + timedelta(minutes=key * size)), o, h, low, c])
        else:
            last = out[-1]
            last[2], last[3], last[4] = max(last[2], h), min(last[3], low), c
    return out, size


def _trade_row(conn, position_id: int) -> dict:
    row = conn.execute(
        "SELECT position_id, symbol, side, lots, open_time_utc, close_time_utc, open_price, close_price, sl, tp, "
        "close_reason, net_profit, hold_minutes, outcome FROM trades WHERE position_id = ?",
        (position_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"no closed trade with position id {position_id}")
    return dict(row)


def _markers(trades: list[dict]) -> list[dict]:
    out = []
    for i, t in enumerate(trades, start=1):
        out.append({"time": t["open_time_utc"], "price": t["open_price"], "kind": "entry", "side": t["side"],
                    "lots": t["lots"], "position_id": t["position_id"], "level": i})
        out.append({"time": t["close_time_utc"], "price": t["close_price"], "kind": "exit", "side": t["side"],
                    "lots": t["lots"], "position_id": t["position_id"], "level": i, "net": t["net_profit"]})
    return out


def _chart(conn, symbol: str, trades: list[dict]) -> dict:
    start = min(db.parse_time(t["open_time_utc"]) for t in trades)
    end = max(db.parse_time(t["close_time_utc"]) for t in trades)
    pad = _padding(start, end)
    candles, size = _candles(conn, symbol, start - pad, end + pad)
    return {
        "symbol": symbol, "candles": candles, "minutes_per_candle": size, "markers": _markers(trades),
        "note": None if candles else
        "No price bars are stored for this time, so only the fills are listed. A sync (Sync now) downloads them.",
    }


def trade_chart(path: Path, position_id: int) -> dict:
    conn = db.connect(path, read_only=True)
    try:
        trade = _trade_row(conn, position_id)
        seq = conn.execute("SELECT sequence_id FROM sequence_trades WHERE position_id = ?", (position_id,)).fetchone()
        out = _chart(conn, trade["symbol"], [trade])
    finally:
        conn.close()
    out.update(kind="trade", trade=trade, sequence_id=seq[0] if seq else None,
               lines=[{"label": name.upper(), "price": trade[name]} for name in ("sl", "tp") if trade[name]])
    return out


def sequence_chart(path: Path, sequence_id: int) -> dict:
    conn = db.connect(path, read_only=True)
    try:
        seq = conn.execute("SELECT * FROM v_sequences WHERE sequence_id = ?", (sequence_id,)).fetchone()
        if seq is None:
            raise NotFound(f"no basket {sequence_id}")
        ids = [r[0] for r in conn.execute(
            "SELECT position_id FROM sequence_trades WHERE sequence_id = ? ORDER BY level", (sequence_id,))]
        trades = [_trade_row(conn, p) for p in ids]
        out = _chart(conn, seq["symbol"], trades)
    finally:
        conn.close()
    out.update(kind="sequence", sequence=dict(seq), trades=trades, lines=[])
    return out
