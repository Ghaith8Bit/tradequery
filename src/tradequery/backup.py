"""Backups of the data folder: every account database and the news calendar files.

  make backup              -> data/backups/<UTC date and time>/
  make backup KEEP=20      keeps the newest 20 (default 10)

Databases are copied with SQLite's backup API, so a backup taken while the page
or a sync is running is still consistent. Chats are not included: they live in
the browser. Copy data/backups to another drive now and then; a backup on the
same disk does not survive the disk.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_KEEP = 10


def backup(root: Path | str, *, keep: int = DEFAULT_KEEP, now: datetime | None = None) -> Path:
    root = Path(root)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d_%H%M%S")
    target = root / "backups" / stamp
    (target / "accounts").mkdir(parents=True, exist_ok=False)
    for db in sorted((root / "accounts").glob("*.db")):
        source = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
        copy = sqlite3.connect(target / "accounts" / db.name)
        try:
            with copy:
                source.backup(copy)
        finally:
            source.close()
            copy.close()
    if (root / "news").is_dir():
        shutil.copytree(root / "news", target / "news")
    if (root / "usage.json").is_file():
        shutil.copy2(root / "usage.json", target / "usage.json")
    prune(root / "backups", keep)
    return target


def prune(folder: Path, keep: int) -> list[Path]:
    """Delete all but the newest `keep` backups (folder names sort by time)."""
    backups = sorted(p for p in folder.iterdir() if p.is_dir())
    old = backups[:-keep] if keep > 0 else []
    for path in old:
        shutil.rmtree(path)
    return old
