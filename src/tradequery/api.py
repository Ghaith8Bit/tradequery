"""The web API (FastAPI) and the page it serves.

  GET  /                  the page (web/index.html)
  GET  /api/accounts      stored accounts: login, currency, trades, date range
  GET  /api/status        is a language model configured, and which
  GET  /api/presets       the plan's example questions (answered without the model)
  POST /api/ask           a plain-English question, with a session id for follow-ups
  POST /api/preset        one example question, by number: no model call, instant
  POST /api/query         a hand-written QuerySpec

Every request opens exactly one account database, read-only. Answers carry the
interpretation line, notes, the result table, the spec and the SQL, so the page
can show how each number was found. Questions are capped in length and model
calls are rate limited per process.
"""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict, deque
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import db
from .ask import Answer, Session, ask, is_share_column
from .evaluate import EVAL_LOGIN as SAMPLE_LOGIN
from .examples import EXAMPLES
from .execute import QueryTimeout, Result, execute
from .llm import LLMError, Provider, provider_from_env
from .overview import build_overview
from .settings import Settings, data_dir, load_settings
from .spec import SpecError, parse_spec

PAGE = Path(__file__).with_name("web") / "index.html"
MAX_SESSIONS = 200
MODEL_CALLS_PER_MINUTE = 20


class AskBody(BaseModel):
    login: int
    question: str = Field(min_length=1, max_length=500)
    session_id: str | None = Field(default=None, max_length=64)


class PresetBody(BaseModel):
    login: int
    number: int = Field(ge=1)


class QueryBody(BaseModel):
    login: int
    spec: dict


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


def _answer_json(answer: Answer, provider: Provider | None, session_id: str | None) -> dict:
    return {
        "question": answer.question,
        "kind": answer.spec.kind if answer.spec else None,
        "text": answer.text,
        "verified": answer.verified,
        "unsupported_numbers": answer.unsupported_numbers,
        "error": answer.error,
        "spec": answer.spec.model_dump(mode="json", exclude_defaults=True) if answer.spec else None,
        "result": _result_json(answer.result),
        "usage": {"requests": answer.usage.requests, "input_tokens": answer.usage.input_tokens,
                  "output_tokens": answer.usage.output_tokens},
        "model": f"{provider.name} {provider.model}" if provider else None,
        "session_id": session_id,
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
               provider_factory=provider_from_env) -> FastAPI:
    app = FastAPI(title="TradeQuery", docs_url="/api/docs", openapi_url="/api/openapi.json")
    root = Path(data_root or data_dir())
    settings = settings or load_settings()
    sessions: OrderedDict[str, Session] = OrderedDict()
    limiter = RateLimiter(MODEL_CALLS_PER_MINUTE)
    state: dict = {"provider": None}

    def account_path(login: int) -> Path:
        if login not in db.list_accounts(root):
            raise HTTPException(404, f"no stored account {login}")
        return db.db_path(login, root)

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
                        "trades": span[2], "first": span[0], "last": span[1]})
        return out

    @app.get("/api/overview")
    def overview(login: int):
        return build_overview(account_path(login), settings)

    @app.get("/api/status")
    def status():
        try:
            p = provider()
            return {"model": f"{p.name} {p.model}", "ready": True}
        except HTTPException as error:
            return {"model": None, "ready": False, "message": error.detail}

    @app.get("/api/presets")
    def presets():
        return [{"number": i, "question": q} for i, (q, _) in enumerate(EXAMPLES, start=1)]

    @app.post("/api/ask")
    def ask_question(body: AskBody):
        path = account_path(body.login)
        p = provider()
        if not limiter.allow():
            raise HTTPException(429, f"More than {MODEL_CALLS_PER_MINUTE} questions in a minute; wait a moment.")
        session_id = body.session_id or uuid.uuid4().hex
        session = sessions.pop(session_id, None) or Session()
        if session.login is not None and session.login != body.login:
            session = Session()  # follow-ups never cross accounts
        answer = ask(body.question.strip(), path, settings, p, session)
        session.login = body.login
        sessions[session_id] = session
        while len(sessions) > MAX_SESSIONS:
            sessions.popitem(last=False)
        return _answer_json(answer, p, session_id)

    def run_spec(login: int, spec_data: dict, question: str) -> dict:
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
        return _answer_json(answer, None, None)

    @app.post("/api/preset")
    def preset(body: PresetBody):
        if body.number > len(EXAMPLES):
            raise HTTPException(404, f"there are {len(EXAMPLES)} presets")
        question, spec = EXAMPLES[body.number - 1]
        return run_spec(body.login, spec, question)

    @app.post("/api/query")
    def query(body: QueryBody):
        return run_spec(body.login, body.spec, "(hand-written spec)")

    return app


def serve(host: str = "0.0.0.0", port: int = 8000) -> None:
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port, log_level="info")
