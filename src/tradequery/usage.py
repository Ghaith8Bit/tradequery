"""Today's model usage from the web page, and a daily budget so cost can't run away.

Counted per UTC day in data/usage.json: model requests, input and output tokens,
questions, and how many answers were written by a template (no second call).
Limits come from the environment (.env):

  TQ_DAILY_REQUESTS  model requests per day (default 300; 0 = no limit)
  TQ_DAILY_TOKENS    input + output tokens per day (default 1,000,000; 0 = no limit)

Suggested questions, calendar days and switch buttons use no model and are never limited.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

DAYS_KEPT = 31
DEFAULT_REQUESTS = 300
DEFAULT_TOKENS = 1_000_000


def _limit(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


class UsageBook:
    def __init__(self, root: Path | str, *, requests: int | None = None, tokens: int | None = None, clock=None):
        self.path = Path(root) / "usage.json"
        self.request_limit = _limit("TQ_DAILY_REQUESTS", DEFAULT_REQUESTS) if requests is None else requests
        self.token_limit = _limit("TQ_DAILY_TOKENS", DEFAULT_TOKENS) if tokens is None else tokens
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()

    def _today(self) -> str:
        return self._clock().strftime("%Y-%m-%d")

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def today(self) -> dict:
        day = self._load().get(self._today(), {})
        used = {k: int(day.get(k, 0)) for k in ("requests", "input_tokens", "output_tokens", "questions", "templated")}
        return {"date": self._today(), **used, "request_limit": self.request_limit, "token_limit": self.token_limit}

    def over_budget(self) -> str | None:
        """Why no more model calls are allowed today, or None."""
        t = self.today()
        tokens = t["input_tokens"] + t["output_tokens"]
        if self.request_limit and t["requests"] >= self.request_limit:
            return f"Today's AI budget is used up ({t['requests']} of {self.request_limit} requests)."
        if self.token_limit and tokens >= self.token_limit:
            return f"Today's AI budget is used up ({tokens:,} of {self.token_limit:,} tokens)."
        return None

    def record(self, usage, *, templated: bool = False) -> None:
        with self._lock:
            data = self._load()
            day = data.setdefault(self._today(), {})
            for key, add in (("requests", usage.requests), ("input_tokens", usage.input_tokens),
                             ("output_tokens", usage.output_tokens), ("questions", 1), ("templated", int(templated))):
                day[key] = int(day.get(key, 0)) + int(add)
            for old in sorted(data)[:-DAYS_KEPT]:
                del data[old]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            tmp.replace(self.path)  # never a half-written file
