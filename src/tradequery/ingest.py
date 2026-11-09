"""Load an MT5 HTML history report into its account database.

Shared by ``tradequery import`` and the web page's upload. Deals already stored
(from a sync or an earlier report) are kept as they are; a synced account row
is kept too, since it is richer than what a report holds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .importers.mt5_report import parse_report
from .settings import Settings


@dataclass
class ImportResult:
    report: object  # the parsed report (importers.mt5_report)
    path: Path
    new_deals: int
    trades: int
    flows: int
    kept_account: bool  # the account was already stored; its row was kept
    account: dict
    stored: list  # every stored deal after the import


def import_report(report_file: Path | str, root: Path | str | None, settings: Settings) -> ImportResult:
    report = parse_report(report_file, settings.clock)
    path = db.db_path(report.login, root)
    conn = db.connect(path)
    try:
        with conn:  # all or nothing
            new = db.upsert_deals(conn, report.deals, overwrite=False)
            try:
                db.load_account(conn)
                kept = True
            except LookupError:
                kept = False
                db.save_account(
                    conn,
                    dict(report.account(), opening_balance=report.opening_balance),
                    earliest_history=report.deals[0].time_utc,
                    synced_at=datetime.now(timezone.utc),
                )
            trades, flows = db.rebuild_derived(conn, settings)
            account = db.load_account(conn)
            stored = db.load_deals(conn)
    finally:
        conn.close()
    return ImportResult(report, path, new, len(trades), len(flows), kept, account, stored)
