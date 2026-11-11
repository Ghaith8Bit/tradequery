"""An optional password for the page, needed before it is opened beyond this computer.

Off by default: on 127.0.0.1 only you can reach the page. Set TQ_PASSWORD in .env
(at least 10 characters) and the page asks for it once, then keeps you signed in
for 30 days with a cookie that scripts cannot read (HttpOnly) and other sites
cannot send (SameSite=Strict). Changing the password signs every browser out.

The cookie holds an expiry time and a signature (HMAC-SHA256) made with a key
derived from the password and a random secret kept in data/secret_key, so it
cannot be forged or reused after the password changes. Wrong passwords are
limited to 5 per 5 minutes.

Plain http on a home network can be read by others on that network: use it only
on a network you trust, or put the page behind HTTPS.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections import deque
from pathlib import Path

COOKIE = "tq_session"
SESSION_SECONDS = 30 * 24 * 3600
MIN_LENGTH = 10
MAX_FAILURES, FAILURE_WINDOW = 5, 300


class AuthConfigError(RuntimeError):
    """The page would be reachable from other devices without a usable password."""


def _secret(root: Path) -> bytes:
    path = root / "secret_key"
    try:
        return bytes.fromhex(path.read_text().strip())
    except (OSError, ValueError):
        key = secrets.token_bytes(32)
        root.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(key.hex())
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        return key


class PasswordGate:
    def __init__(self, password: str, root: Path | str, *, clock=time.time):
        if len(password) < MIN_LENGTH:
            raise AuthConfigError(f"TQ_PASSWORD must be at least {MIN_LENGTH} characters.")
        self._password = password.encode()
        self._key = hmac.new(_secret(Path(root)), b"session:" + self._password, hashlib.sha256).digest()
        self._clock = clock
        self._failures: deque[float] = deque()

    def _sign(self, expires: int) -> str:
        return hmac.new(self._key, str(expires).encode(), hashlib.sha256).hexdigest()

    def new_cookie(self) -> str:
        expires = int(self._clock()) + SESSION_SECONDS
        return f"{expires}.{self._sign(expires)}"

    def valid(self, cookie: str | None) -> bool:
        if not cookie or "." not in cookie:
            return False
        expires, _, signature = cookie.partition(".")
        if not expires.isdigit() or int(expires) < self._clock():
            return False
        return hmac.compare_digest(signature, self._sign(int(expires)))

    def locked(self) -> bool:
        now = self._clock()
        while self._failures and now - self._failures[0] > FAILURE_WINDOW:
            self._failures.popleft()
        return len(self._failures) >= MAX_FAILURES

    def check(self, password: str) -> bool:
        ok = hmac.compare_digest(password.encode(), self._password)
        if not ok:
            self._failures.append(self._clock())
        return ok


def gate_from_env(root: Path | str, hosts: list[str], local_hosts) -> PasswordGate | None:
    """The gate when TQ_PASSWORD is set; refuse to run open to other devices without one."""
    password = os.environ.get("TQ_PASSWORD", "")
    if password:
        return PasswordGate(password, root)
    if any(h not in local_hosts for h in hosts):
        raise AuthConfigError("TQ_ALLOWED_HOSTS lets other devices reach the page: set TQ_PASSWORD in .env "
                              f"(at least {MIN_LENGTH} characters) first.")
    return None


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>TradeQuery</title>
<style>
  :root { --bg: #f6f7f9; --card: #fff; --text: #16181d; --muted: #667085; --line: #e3e6eb; --accent: #2563eb; --bad: #c0362c; }
  @media (prefers-color-scheme: dark) { :root { --bg: #111317; --card: #1a1d23; --text: #e8eaee; --muted: #9aa3b2; --line: #2b2f38; --accent: #6b9bff; --bad: #ff7a70; } }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--bg); color: var(--text);
         font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
  form { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 28px; width: min(340px, calc(100vw - 32px)); box-sizing: border-box; }
  h1 { font-size: 18px; margin: 0 0 4px; } p { color: var(--muted); margin: 0 0 18px; font-size: 13.5px; }
  input { width: 100%; box-sizing: border-box; padding: 10px 12px; border: 1px solid var(--line); border-radius: 9px; background: transparent; color: inherit; font: inherit; }
  button { margin-top: 12px; width: 100%; padding: 10px; border: 0; border-radius: 9px; background: var(--accent); color: #fff; font: inherit; font-weight: 600; cursor: pointer; }
  .err { color: var(--bad); font-size: 13px; min-height: 1.4em; margin-top: 8px; }
</style></head>
<body>
<form id="f" autocomplete="on">
  <h1>TradeQuery</h1>
  <p>Enter the page password (TQ_PASSWORD in .env).</p>
  <input id="pw" type="password" autocomplete="current-password" aria-label="Password" required autofocus>
  <button type="submit">Sign in</button>
  <div class="err" id="err" role="alert"></div>
</form>
<script>
document.getElementById("f").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = document.getElementById("err");
  err.textContent = "";
  try {
    const res = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json", "X-TradeQuery": "1" },
                                            body: JSON.stringify({ password: document.getElementById("pw").value }) });
    if (res.ok) { location.reload(); return; }
    const data = await res.json().catch(() => ({}));
    err.textContent = data.detail || "Sign-in failed.";
  } catch { err.textContent = "The server could not be reached."; }
});
</script>
</body></html>
"""
