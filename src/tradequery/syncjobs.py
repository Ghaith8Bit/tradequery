"""Hand-off between the web page and the MT5 sync worker, through files in <data>/sync.

The MT5 download needs Wine, so it runs in its own container (the sync worker,
docker/mt5/entrypoint.sh ``worker``). The two sides only share the data folder:

  worker    written by the worker every few seconds: login, ready, reason, beat (epoch seconds)
  request   written here to ask for a sync; the worker deletes it when it starts
  job       the last sync: state (running, done, failed), started, finished (epoch seconds)
  job.log   the last lines of the last sync's output

No credentials pass through these files: the worker reads them from .env itself.
"""

from __future__ import annotations

import time
from pathlib import Path

HEARTBEAT_SECONDS = 30  # a worker that has not written for this long is gone
RUNNING_LIMIT = 20 * 60  # a sync still "running" after this long died without saying so


class SyncUnavailable(RuntimeError):
    pass


class SyncBusy(RuntimeError):
    pass


def _read(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def sync_status(root: Path | str, now: float | None = None) -> dict:
    folder = Path(root) / "sync"
    now = time.time() if now is None else now
    worker, job = _read(folder / "worker"), _read(folder / "job")
    started, finished = _int(job.get("started")), _int(job.get("finished"))
    state = job.get("state") or "idle"
    if state == "running" and started and now - started > RUNNING_LIMIT:
        state = "failed"
    beat = _int(worker.get("beat"))
    alive = bool(beat and now - beat < HEARTBEAT_SECONDS) or state == "running"
    ready = alive and worker.get("ready") == "yes"
    if not alive:
        reason = "The MT5 sync worker is not running (make web starts it once MT5 is set up)."
    else:
        reason = None if ready else (worker.get("reason") or "The MT5 sync worker is not ready.")
    message = None
    if state == "failed":
        try:
            lines = [ln for ln in (folder / "job.log").read_text(encoding="utf-8", errors="replace").splitlines()
                     if ln.strip()]
        except OSError:
            lines = []
        message = lines[-1] if lines else "The sync failed."
    return {
        "available": ready, "login": _int(worker.get("login")), "reason": reason, "state": state,
        "requested": (folder / "request").is_file(), "started": started, "finished": finished,
        "every_minutes": _int(worker.get("every")) or 0, "message": message,
    }


def request_sync(root: Path | str) -> dict:
    status = sync_status(root)
    if not status["available"]:
        raise SyncUnavailable(status["reason"])
    if status["state"] == "running" or status["requested"]:
        raise SyncBusy("A sync is already running.")
    folder = Path(root) / "sync"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "request").write_text(f"at={int(time.time())}\n", encoding="utf-8")
    return sync_status(root)
