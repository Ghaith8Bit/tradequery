"""Save and load downloaded account data under data/accounts/<login>/.

The data folder is git-ignored. Files per account:
  deals.csv     every deal, normalized (see models.Deal); times are ISO 8601 UTC
  symbols.json  contract specs of the symbols traded
  account.json  account facts at download time (no name and no credentials)
"""

from __future__ import annotations

import csv
import json
from dataclasses import fields
from datetime import datetime
from pathlib import Path

from .settings import data_dir
from .models import Deal

DEAL_FIELDS = [f.name for f in fields(Deal)]
_INT_FIELDS = {"ticket", "position_id", "magic"}
_FLOAT_FIELDS = {"volume", "price", "commission", "swap", "profit", "sl", "tp"}


def account_dir(login: int | str, root: Path | str | None = None) -> Path:
    return Path(root or data_dir()) / "accounts" / str(login)


def save_deals(path: Path | str, deals: list[Deal]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=DEAL_FIELDS)
        writer.writeheader()
        for deal in deals:
            row = {name: getattr(deal, name) for name in DEAL_FIELDS}
            row["time_utc"] = deal.time_utc.isoformat()
            writer.writerow(row)


def load_deals(path: Path | str) -> list[Deal]:
    deals = []
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            values = {}
            for name in DEAL_FIELDS:
                raw = row[name]
                if name == "time_utc":
                    values[name] = datetime.fromisoformat(raw)
                elif name in _INT_FIELDS:
                    values[name] = int(raw)
                elif name in _FLOAT_FIELDS:
                    values[name] = float(raw)
                else:
                    values[name] = raw
            deals.append(Deal(**values))
    return deals


def save_json(path: Path | str, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_json(path: Path | str):
    return json.loads(Path(path).read_text(encoding="utf-8"))
