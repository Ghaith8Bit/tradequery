"""Settings and MT5 credentials.

Settings (timezones, thresholds) come from the packaged ``settings.toml``; set
``TQ_CONFIG`` to point at your own copy. Credentials come from environment
variables, with a ``.env`` file in the working directory as a fallback.
Use the INVESTOR (read-only) password, never the master password. Credentials
are never printed, saved to data files, or passed to the language model.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .timeutil import ServerClock

DEFAULT_CONFIG = Path(__file__).with_name("settings.toml")


def data_dir() -> Path:
    """Where account data lives: $TQ_DATA_DIR, else ./data (git-ignored)."""
    return Path(os.environ.get("TQ_DATA_DIR") or "data")


@dataclass(frozen=True)
class Settings:
    clock: ServerClock
    local_timezone: str
    break_even_tolerance: float
    # Trading sessions as {name: (start_hour, end_hour)} in UTC, end exclusive.
    sessions: dict[str, tuple[int, int]] = field(default_factory=dict)


def load_settings(path: Path | str | None = None) -> Settings:
    with open(path or os.environ.get("TQ_CONFIG") or DEFAULT_CONFIG, "rb") as handle:
        raw = tomllib.load(handle)
    time = raw["time"]
    return Settings(
        clock=ServerClock(
            kind=time.get("server_clock", "ny_close"),
            fixed_offset_hours=float(time.get("fixed_offset_hours", 2)),
        ),
        local_timezone=time.get("local_timezone", "UTC"),
        break_even_tolerance=float(raw.get("metrics", {}).get("break_even_tolerance", 0.0)),
        sessions={n: (int(b[0]), int(b[1])) for n, b in raw.get("sessions", {}).items()},
    )


class CredentialsError(ValueError):
    """A required credential is missing or malformed."""


@dataclass(frozen=True)
class Mt5Credentials:
    login: int
    password: str = field(repr=False)  # never shown in logs or tracebacks
    server: str = ""
    path: str | None = None  # path to terminal64.exe (set inside the Docker image)


def read_env_file(path: Path | str) -> dict[str, str]:
    """Parse KEY=VALUE lines. Blank lines and # comments are ignored; quotes are stripped."""
    path = Path(path)
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_credentials(env_path: Path | str = ".env") -> Mt5Credentials:
    """Credentials from the .env file, overridden by real environment variables."""
    values = read_env_file(env_path)
    for key in ("MT5_LOGIN", "MT5_INVESTOR_PASSWORD", "MT5_SERVER", "MT5_PATH"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    missing = [k for k in ("MT5_LOGIN", "MT5_INVESTOR_PASSWORD", "MT5_SERVER") if not values.get(k)]
    if missing:
        raise CredentialsError(
            f"missing {', '.join(missing)}. Copy .env.example to .env and fill it in "
            "(use the investor password)."
        )
    try:
        login = int(values["MT5_LOGIN"])
    except ValueError:
        raise CredentialsError("MT5_LOGIN must be the account number (digits only)") from None
    return Mt5Credentials(
        login=login,
        password=values["MT5_INVESTOR_PASSWORD"],
        server=values["MT5_SERVER"],
        path=values.get("MT5_PATH") or None,
    )
