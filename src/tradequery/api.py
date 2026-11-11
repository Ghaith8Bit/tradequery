"""The web API (FastAPI) and the page it serves.

  GET  /                  the page (web/index.html)
  GET  /api/accounts      stored accounts: login, currency, trades, date range
  GET  /api/status        is a language model configured, and which
  GET  /api/presets       the plan's example questions (answered without the model)
  POST /api/ask           a plain-English question, with a session id for follow-ups
  POST /api/ask/stream    the same, streamed as it happens (newline-delimited JSON events)
  POST /api/focus         drop one part of a chat's focus (period, symbol, item ...) or all of it
  POST /api/preset        one example question, by number: no model call, instant
  POST /api/query         a hand-written QuerySpec
  GET  /api/overview      the start screen: key numbers, two charts, risk facts
  POST /api/import        upload an MT5 HTML history report (the file is the request body)
  GET  /api/sync          the MT5 sync worker: available, running, last result
  POST /api/sync          ask the worker to sync now
  GET  /api/trade         one trade on a price chart (stored one-minute bars)
  GET  /api/sequence      one martingale/grid sequence on a price chart
  GET  /api/usage         today's model requests and tokens against the daily budget
  POST /api/login         sign in with TQ_PASSWORD (only when it is set); POST /api/logout signs out

Every request opens exactly one account database, read-only. Answers carry the
interpretation line, notes, the result table, the spec and the SQL, so the page
can show how each number was found. Questions are capped in length and model
calls are rate limited per process and capped per day (usage.py).

The page is for this computer only (compose publishes it on 127.0.0.1). Other
websites open in the same browser must not be able to use it, so:
- the Host header must be this computer (stops DNS rebinding, where a website's
  name is pointed at 127.0.0.1 to read the answers);
- every request that changes something (POST) must carry the X-TradeQuery
  header, which a page on another site cannot add without permission this API
  never gives, and must not come from another site's page (Origin, Sec-Fetch-Site);
- the page may not be framed, and scripts may only talk to this server (CSP).
TQ_ALLOWED_HOSTS in .env adds host names (comma separated), e.g. for a LAN name;
that needs TQ_PASSWORD too, and then every request needs a signed-in browser (auth.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import uuid
from collections import OrderedDict, deque
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import db, syncjobs
from .auth import COOKIE, LOGIN_PAGE, SESSION_SECONDS, PasswordGate, gate_from_env
from .followup import ID_LABELS
from .ask import Answer, Session, ask, checked_template, drawdown_default, is_share_column
from .evaluate import EVAL_LOGIN as SAMPLE_LOGIN
from .examples import EXAMPLES, with_drawdown_basis
from .execute import QueryTimeout, Result, execute
from .importers.mt5_report import ReportError
from .ingest import import_report
from .llm import LLMError, Provider, provider_from_env
from .overview import build_overview
from .pricechart import NotFound, sequence_chart, trade_chart
from .scope import GROUPS, account_levels, describe
from .settings import Settings, data_dir, load_settings
from .spec import SpecError, parse_spec
from .usage import UsageBook

PAGE = Path(__file__).with_name("web") / "index.html"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]", "::1")
REQUEST_HEADER = "x-tradequery"  # the page sends it on every POST; other sites' pages cannot
PAGE_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'none'; object-src 'none'")


def allowed_hosts_from_env() -> list[str]:
    extra = [h.strip() for h in os.environ.get("TQ_ALLOWED_HOSTS", "").split(",") if h.strip()]
    return [*LOCAL_HOSTS, *extra]


MAX_SESSIONS = 200
MODEL_CALLS_PER_MINUTE = 20
MAX_REPORT_BYTES = 30 * 1024 * 1024  # a year of busy trading is a few MB


class AskBody(BaseModel):
    login: int
    question: str = Field(min_length=1, max_length=500)
    session_id: str | None = Field(default=None, max_length=64)
    smart: bool = False  # extra checks, a "Why" and questions worth asking next (about one more model call)


FOCUS_KEYS = {"all", "item", "period", "symbol", "magic", "side", *ID_LABELS}

class FocusBody(BaseModel):
    login: int
    session_id: str = Field(min_length=1, max_length=64)
    drop: str = Field(min_length=1, max_length=40)  # a chip's key, or "all"


class PresetBody(BaseModel):
    login: int
    number: int = Field(ge=1)
    session_id: str | None = Field(default=None, max_length=64)


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class QueryBody(BaseModel):
    login: int
    spec: dict
    question: str | None = Field(default=None, max_length=500)  # shown in the chat (switch buttons)
    session_id: str | None = Field(default=None, max_length=64)  # so a follow-up continues from this answer


def _result_json(result: Result | None) -> dict | None:
    if result is None:
        return None
    return {
        "columns": result.columns,
        "rows": result.rows,
        "truncated": result.truncated,
        "interpretation": result.interpretation,
        "notes": result.notes,
        "sql": result.sql,
        "share_columns": [c for c in result.columns if is_share_column(c)],
    }


def _answer_json(answer: Answer, provider: Provider | None, session_id: str | None, levels: dict | None = None,
                 model_ready: bool = True) -> dict:
    levels = levels or {}
    return {
        "scope": describe(answer.spec, answer.result, has_equity=levels.get("has_equity", False),
                          has_baskets=levels.get("has_baskets", False), model_ready=model_ready,
                          question=answer.question),
        "question": answer.question,
        "kind": answer.spec.kind if answer.spec else None,
        "text": answer.text,
        "verified": answer.verified,
        "unsupported_numbers": answer.unsupported_numbers,
        "error": answer.error,
        "error_detail": answer.error_detail,
        "text_source": answer.text_source,
        "spec": answer.spec.model_dump(mode="json", exclude_defaults=True) if answer.spec else None,
        "result": _result_json(answer.result),
        "usage": {"requests": answer.usage.requests, "input_tokens": answer.usage.input_tokens,
                  "output_tokens": answer.usage.output_tokens},
        "model": f"{provider.name} {provider.model}" if provider else None,
        "session_id": session_id,
        "smart": answer.smart,
        "checks": [{"purpose": purpose, "spec": spec.model_dump(mode="json", exclude_defaults=True),
                    "result": _result_json(result)} for purpose, spec, result in answer.checks],
        "ideas": answer.ideas,
    }


class RateLimiter:
    """At most ``limit`` model calls in any 60 seconds (per process)."""

    def __init__(self, limit: int):
        self.limit, self.calls = limit, deque()

    def allow(self) -> bool:
        now = time.monotonic()
        while self.calls and now - self.calls[0] > 60:
            self.calls.popleft()
        if len(self.calls) >= self.limit:
            return False
        self.calls.append(now)
        return True


def create_app(*, data_root: Path | str | None = None, settings: Settings | None = None,
               provider_factory=provider_from_env, allowed_hosts: list[str] | None = None,
               usage_book: UsageBook | None = None, gate: PasswordGate | None | bool = True) -> FastAPI:
    """gate: True reads TQ_PASSWORD from the environment; None turns the password off; or a PasswordGate."""
    # no API docs page: it loads scripts from a CDN, and nothing here needs it
    app = FastAPI(title="TradeQuery", docs_url=None, redoc_url=None, openapi_url=None)
    root = Path(data_root or data_dir())
    settings = settings or load_settings()
    sessions: OrderedDict[str, Session] = OrderedDict()
    limiter = RateLimiter(MODEL_CALLS_PER_MINUTE)
    book = usage_book or UsageBook(root)
    explain_mode = "ai" if os.environ.get("TQ_EXPLAIN", "auto").lower() == "ai" else "auto"
    state: dict = {"provider": None}
    hosts = allowed_hosts or allowed_hosts_from_env()
    if gate is True:
        gate = gate_from_env(root, hosts, LOCAL_HOSTS)
    password_gate: PasswordGate | None = gate or None

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            site = request.headers.get("sec-fetch-site")
            foreign_origin = origin and (urlsplit(origin).hostname or "") not in [h.strip("[]") for h in hosts]
            if request.headers.get(REQUEST_HEADER) != "1" or foreign_origin or site in ("cross-site", "same-site"):
                return JSONResponse({"detail": "This request did not come from the TradeQuery page."}, status_code=403)
        path = request.url.path
        if password_gate and path != "/api/login" and not password_gate.valid(request.cookies.get(COOKIE)):
            if path.startswith("/api/"):
                response = JSONResponse({"detail": "Sign in first."}, status_code=401)
            else:
                response = HTMLResponse(LOGIN_PAGE)
        else:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers.setdefault("Cache-Control", "no-store")  # answers hold account data
        if request.url.path == "/":
            response.headers["Content-Security-Policy"] = PAGE_CSP
        return response

    # added last, so it runs first: a request for another host name is refused before anything else
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    def account_path(login: int) -> Path:
        if login not in db.list_accounts(root):
            raise HTTPException(404, f"no stored account {login}")
        return db.db_path(login, root)

    def levels_of(login: int) -> dict:
        conn = db.connect(account_path(login), read_only=True)
        try:
            return account_levels(conn)
        finally:
            conn.close()

    def model_ready() -> bool:
        try:
            provider()
            return True
        except HTTPException:
            return False

    def remember(session_id: str, session: Session) -> None:
        sessions[session_id] = session
        while len(sessions) > MAX_SESSIONS:
            sessions.popitem(last=False)

    def provider() -> Provider:
        if state["provider"] is None:
            try:
                state["provider"] = provider_factory()
            except LLMError as error:
                raise HTTPException(503, str(error)) from None
        return state["provider"]

    @app.get("/", response_class=HTMLResponse)
    def page():
        return PAGE.read_text(encoding="utf-8")

    @app.post("/api/login")
    def login(body: LoginBody, request: Request):
        if password_gate is None:
            raise HTTPException(404, "No password is set (TQ_PASSWORD).")
        if password_gate.locked():
            raise HTTPException(429, "Too many wrong passwords. Wait 5 minutes.")
        if not password_gate.check(body.password):
            raise HTTPException(401, "Wrong password.")
        response = JSONResponse({"ok": True})
        response.set_cookie(COOKIE, password_gate.new_cookie(), max_age=SESSION_SECONDS, httponly=True,
                            samesite="strict", secure=request.url.scheme == "https", path="/")
        return response

    @app.post("/api/logout")
    def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/api/accounts")
    def accounts():
        out = []
        # real accounts first; the synthetic sample account (make sample) last
        for login in sorted(db.list_accounts(root), key=lambda n: (n == SAMPLE_LOGIN, n)):
            conn = db.connect(db.db_path(login, root), read_only=True)
            try:
                account = db.load_account(conn)
                span = conn.execute("SELECT MIN(open_time_utc), MAX(close_time_utc), COUNT(*) FROM trades").fetchone()
            finally:
                conn.close()
            out.append({"login": login, "currency": account.get("currency"), "server": account.get("server"),
                        "trades": span[2], "first": span[0], "last": span[1],
                        "synced_at": (account.get("synced_at_utc") or "")[:19] or None})
        return out

    @app.get("/api/overview")
    def overview(login: int):
        return build_overview(account_path(login), settings)

    @app.post("/api/import")
    async def import_upload(request: Request):
        body = await request.body()
        if not body:
            raise HTTPException(400, "The file was empty.")
        if len(body) > MAX_REPORT_BYTES:
            raise HTTPException(413, "That file is too large for an MT5 history report.")
        with tempfile.TemporaryDirectory() as tmp:
            report_file = Path(tmp) / "report.html"
            report_file.write_bytes(body)
            try:
                done = import_report(report_file, root, settings)
            except ReportError as error:
                raise HTTPException(400, f"This doesn't look like an MT5 history report: {error}") from None
        report = done.report
        return {"login": report.login, "server": report.server, "currency": report.currency,
                "deals": len(report.deals), "new_deals": done.new_deals, "trades": done.trades,
                "kept_account": done.kept_account,
                "first": db.format_time(report.deals[0].time_utc), "last": db.format_time(report.deals[-1].time_utc)}

    @app.get("/api/sync")
    def sync_status():
        return syncjobs.sync_status(root)

    @app.post("/api/sync")
    def sync_now():
        try:
            return syncjobs.request_sync(root)
        except syncjobs.SyncUnavailable as error:
            raise HTTPException(503, str(error)) from None
        except syncjobs.SyncBusy as error:
            raise HTTPException(409, str(error)) from None

    @app.get("/api/trade")
    def trade(login: int, position_id: int):
        try:
            return trade_chart(account_path(login), position_id)
        except NotFound as error:
            raise HTTPException(404, str(error)) from None

    @app.get("/api/sequence")
    def sequence(login: int, sequence_id: int):
        try:
            return sequence_chart(account_path(login), sequence_id)
        except NotFound as error:
            raise HTTPException(404, str(error)) from None

    @app.get("/api/status")
    def status():
        try:
            p = provider()
            return {"model": f"{p.name} {p.model}", "ready": True, "login_required": password_gate is not None}
        except HTTPException as error:
            return {"model": None, "ready": False, "message": error.detail, "login_required": password_gate is not None}

    @app.get("/api/usage")
    def usage_today():
        return book.today()

    @app.get("/api/presets")
    def presets():
        # "needs": basket drawdown questions only make sense on accounts with price bars
        return [{"number": i, "question": q, "group": GROUPS.get(s.get("entity"), "Trades"),
                 "needs": "basket_drawdown" if "drawdown" in str(s) and s.get("entity") == "sequences" else None}
                for i, (q, s) in enumerate(EXAMPLES, start=1)]

    def start_question(body: AskBody):
        """Checks before any model call (account, budget, rate), and the conversation to continue."""
        path = account_path(body.login)
        p = provider()
        over = book.over_budget()
        if over:
            raise HTTPException(429, f"{over} It resets at midnight UTC. Suggested questions, the calendar and the "
                                     "buttons under answers still work, since they need no AI.")
        if not limiter.allow():
            raise HTTPException(429, f"More than {MODEL_CALLS_PER_MINUTE} questions in a minute; wait a moment.")
        session_id = body.session_id or uuid.uuid4().hex
        session = sessions.pop(session_id, None) or Session()
        if session.login is not None and session.login != body.login:
            session = Session()  # follow-ups never cross accounts
        return path, p, session_id, session

    def finish_question(body: AskBody, answer: Answer, p: Provider, session_id: str, session: Session) -> dict:
        book.record(answer.usage, templated=answer.text_source == "template")
        session.login = body.login
        remember(session_id, session)
        out = _answer_json(answer, p, session_id, levels_of(body.login))
        out["focus"] = session.focus()
        return out

    @app.post("/api/ask")
    def ask_question(body: AskBody):
        path, p, session_id, session = start_question(body)
        answer = ask(body.question.strip(), path, settings, p, session, explain=explain_mode, smart=body.smart)
        return finish_question(body, answer, p, session_id, session)

    @app.post("/api/ask/stream")
    async def ask_stream(body: AskBody):
        """The same answer as /api/ask, as it happens: one JSON object per line (newline-delimited JSON).

        {"event": "stage", "text"} a step starts; {"event": "delta", "text"} a piece of the written answer
        (not yet checked); {"event": "retry", "reason"} the text failed the number check and is rewritten;
        {"event": "done", "answer"} the finished, checked answer (the same JSON as /api/ask);
        {"event": "error", "detail"} something broke. Checks (budget, rate) answer with a normal HTTP error first.
        """
        path, p, session_id, session = await run_in_threadpool(start_question, body)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def emit(kind: str, **data) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, {"event": kind, **data})

        def work() -> None:
            try:
                answer = ask(body.question.strip(), path, settings, p, session, explain=explain_mode, on_event=emit,
                             smart=body.smart)
                emit("done", answer=finish_question(body, answer, p, session_id, session))
            except Exception:  # noqa: BLE001 - the page gets a short message; the log gets the details
                logging.getLogger("tradequery").exception("streamed question failed")
                emit("error", detail="Something went wrong while answering. Try again.")
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        async def lines():
            job = loop.run_in_executor(None, work)
            while (item := await queue.get()) is not None:
                yield json.dumps(item) + "\n"
            await job

        return StreamingResponse(lines(), media_type="application/x-ndjson")

    def run_spec(login: int, spec_data: dict, question: str, session_id: str | None = None) -> dict:
        path = account_path(login)
        try:
            spec = parse_spec(spec_data)
        except SpecError as error:
            raise HTTPException(400, f"Spec rejected: {error}") from None
        answer = Answer(question, spec=spec, verified=False)
        try:
            answer.result = execute(spec, path, settings)
        except QueryTimeout as error:
            answer.error = str(error)
        if spec.kind != "query":
            answer.text = spec.message
        elif answer.result is not None:
            # suggested questions and switch buttons use no model, but still get a checked plain sentence
            text = checked_template(question, spec, answer.result)
            if text:
                answer.text, answer.verified, answer.text_source = text, True, "template"
        if session_id and answer.result is not None:
            # a switch button keeps the conversation going: the next follow-up starts from this answer
            session = sessions.pop(session_id, None) or Session()
            if session.login not in (None, login):
                session = Session()
            session.login = login
            session.remember(question, spec, answer.result)
            remember(session_id, session)
            answer_focus = session.focus()
        else:
            answer_focus = []
        out = _answer_json(answer, None, session_id, levels_of(login), model_ready())
        out["focus"] = answer_focus
        return out

    @app.post("/api/focus")
    def change_focus(body: FocusBody):
        """Drop one part of the chat's focus (a chip's key), or all of it, before the next question."""
        if body.drop not in FOCUS_KEYS:
            raise HTTPException(400, f"drop must be one of: {', '.join(sorted(FOCUS_KEYS))}")
        session = sessions.get(body.session_id)
        if session is None or session.login not in (None, body.login):
            return {"focus": []}  # nothing to narrow: the next question starts fresh anyway
        session.drop(body.drop)
        return {"focus": session.focus()}

    @app.post("/api/preset")
    def preset(body: PresetBody):
        if body.number > len(EXAMPLES):
            raise HTTPException(404, f"there are {len(EXAMPLES)} presets")
        question, spec = EXAMPLES[body.number - 1]
        conn = db.connect(account_path(body.login), read_only=True)
        try:
            basis = drawdown_default(conn)
        finally:
            conn.close()
        return run_spec(body.login, with_drawdown_basis(spec, basis), question, body.session_id or uuid.uuid4().hex)

    @app.post("/api/query")
    def query(body: QueryBody):
        session_id = body.session_id or (uuid.uuid4().hex if body.question else None)
        return run_spec(body.login, body.spec, body.question or "(hand-written spec)", session_id)

    return app


def serve(host: str = "0.0.0.0", port: int = 8000) -> None:
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port, log_level="info")
