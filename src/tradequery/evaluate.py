"""The eval suite: about 80 questions with gold answers, scored automatically.

Each question in evals/questions.jsonl has either a hand-written gold spec (the
gold answer is what it returns on the eval account) or ``"expect": "refusal"``
(the model must answer unsupported or clarify). The eval account is the
synthetic one plus four planted calendar events, rebuilt for every run, so
results are reproducible and safe to publish.

Scores (per category and overall):
  valid spec      the model's reply parsed and passed the whitelist
  result match    the model's spec returned the gold result (clear questions);
                  results are compared, not spec text, since different specs
                  can be right
  refusal         adversarial questions got unsupported or clarify
  numbers checked the written answer passed the number check
  plus requests, tokens and seconds per question.

Matching tolerates harmless differences: column names, extra columns, and row
order unless the question is about an order ("top 3"). Every gold value must
appear in the matching row of the model's result.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from .ask import ask
from .execute import Result, execute
from .llm import Provider, QuotaError
from .news import NewsEvent
from .settings import Settings
from .spec import parse_spec
from .synthetic import generate_account

EVAL_LOGIN = 900001


def build_eval_account(root: Path | str, settings: Settings) -> Path:
    """The synthetic account plus four planted calendar events, stored under ``root``."""
    path = db.db_path(EVAL_LOGIN, root)
    if path.exists():
        path.unlink()
    conn = db.connect(path)
    with conn:
        db.upsert_deals(conn, generate_account().deals)
        db.save_account(conn, {"login": EVAL_LOGIN, "balance": 0.0, "currency": "USD", "server": "Synthetic"},
                        earliest_history=None, synced_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
        trades, _ = db.rebuild_derived(conn, settings)
        by_open = sorted(trades, key=lambda t: (t.open_time_utc, t.position_id))
        martingale = next(t for t in by_open if t.comment.startswith("martingale-1"))
        euro = [t for t in by_open if t.symbol == "EURUSD" and t.hold_minutes >= 20 and not t.comment.startswith("martingale")]
        events = [
            NewsEvent(1, 101, martingale.open_time_utc + timedelta(minutes=10), "USD", "high", "Nonfarm Payrolls"),
            NewsEvent(2, 102, euro[0].open_time_utc + timedelta(minutes=5), "USD", "high", "CPI m/m"),
            NewsEvent(3, 103, euro[1].open_time_utc + timedelta(minutes=5), "EUR", "high", "ECB Press Conference"),
            NewsEvent(4, 104, by_open[-1].open_time_utc, "USD", "low", "Retail Sales m/m"),
        ]
        db.save_news_events(conn, events)
        db.rebuild_derived(conn, settings)
    conn.close()
    return path


def load_questions(path: Path | str) -> list[dict]:
    questions = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        q = json.loads(line)
        if "id" not in q or "question" not in q or ("gold" not in q) == (q.get("expect") != "refusal"):
            raise ValueError(f"line {n}: needs id, question and either gold or expect=refusal")
        questions.append(q)
    return questions


# ---------------------------------------------------------------- matching


def _same(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        a, b = (int(a) if isinstance(a, bool) else a), (int(b) if isinstance(b, bool) else b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=1e-6, abs_tol=0.006)
    return str(a).strip().lower() == str(b).strip().lower()


def _contains(gold_row, row, ignore_text: bool) -> bool:
    """Every gold value (skipping empties, and text when ignore_text) appears somewhere in the row."""
    for g in gold_row:
        if g is None or (ignore_text and isinstance(g, str)):
            continue
        if not any(_same(g, v) for v in row if v is not None):
            return False
    return True


def results_match(gold: Result, got: Result, *, ordered: bool = False, ignore_text: bool = False) -> bool:
    if len(gold.rows) != len(got.rows):
        return False
    if ordered:
        return all(_contains(g, r, ignore_text) for g, r in zip(gold.rows, got.rows))
    unused = list(range(len(got.rows)))
    for g in gold.rows:
        hit = next((i for i in unused if _contains(g, got.rows[i], ignore_text)), None)
        if hit is None:
            return False
        unused.remove(hit)
    return True


# ---------------------------------------------------------------- running


@dataclass
class Outcome:
    id: str
    category: str
    question: str
    expect: str  # result or refusal
    valid_spec: bool = False
    passed: bool = False
    kind: str | None = None
    verified: bool | None = None  # the written answer passed the number check (None: not asked)
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    spec: dict | None = None
    error: str | None = None


@dataclass
class Report:
    model: str
    started: str
    outcomes: list[Outcome] = field(default_factory=list)
    stopped_early: str | None = None

    def summary(self) -> dict:
        def rate(items, test):
            items = list(items)
            return (sum(1 for o in items if test(o)) / len(items)) if items else None

        clear = [o for o in self.outcomes if o.expect == "result"]
        adversarial = [o for o in self.outcomes if o.expect == "refusal"]
        explained = [o for o in self.outcomes if o.verified is not None]
        n = len(self.outcomes) or 1
        return {
            "questions": len(self.outcomes),
            "valid_spec_rate": rate(self.outcomes, lambda o: o.valid_spec),
            "result_match_rate": rate(clear, lambda o: o.passed),
            "refusal_rate": rate(adversarial, lambda o: o.passed),
            "number_check_rate": rate(explained, lambda o: o.verified),
            "requests_per_question": sum(o.requests for o in self.outcomes) / n,
            "tokens_per_question": sum(o.input_tokens + o.output_tokens for o in self.outcomes) / n,
            "seconds_per_question": sum(o.seconds for o in self.outcomes) / n,
        }

    def by_category(self) -> dict[str, tuple[int, int]]:
        out: dict[str, list[int]] = {}
        for o in self.outcomes:
            passed_total = out.setdefault(o.category, [0, 0])
            passed_total[0] += o.passed
            passed_total[1] += 1
        return {k: (v[0], v[1]) for k, v in out.items()}


class Pacer:
    """Keeps model requests under a per-minute limit (free tiers are strict about it)."""

    def __init__(self, rpm: float | None, sleep=time.sleep, clock=time.monotonic):
        self.rpm, self.sleep, self.clock, self.stamps = rpm, sleep, clock, []

    def before(self, requests: int = 2) -> None:
        if not self.rpm:
            return
        while True:
            now = self.clock()
            self.stamps = [s for s in self.stamps if now - s < 60]
            if len(self.stamps) + requests <= self.rpm:
                self.stamps += [now] * requests
                return
            self.sleep(max(0.5, 60 - (now - self.stamps[0]) + 0.1))


def run_one(q: dict, path: Path, settings: Settings, provider: Provider, *, explain: bool) -> Outcome:
    expect = "refusal" if q.get("expect") == "refusal" else "result"
    outcome = Outcome(q["id"], q.get("category", "other"), q["question"], expect)
    started = time.monotonic()
    if explain:
        answer = ask(q["question"], path, settings, provider)
    else:  # spec only: one model call
        answer = _spec_only(q["question"], path, settings, provider)
    outcome.seconds = round(time.monotonic() - started, 2)
    outcome.requests, outcome.input_tokens, outcome.output_tokens = (
        answer.usage.requests, answer.usage.input_tokens, answer.usage.output_tokens)
    outcome.error = answer.error
    if answer.spec is None:
        return outcome
    outcome.valid_spec = True
    outcome.kind = answer.spec.kind
    outcome.spec = answer.spec.model_dump(mode="json", exclude_defaults=True)
    if expect == "refusal":
        outcome.passed = answer.spec.kind in ("clarify", "unsupported")
        return outcome
    if answer.spec.kind != "query" or answer.result is None:
        return outcome
    gold = execute(parse_spec(q["gold"]), path, settings)
    outcome.passed = results_match(gold, answer.result, ordered=q.get("match") == "ordered",
                                   ignore_text=bool(q.get("ignore_text")))
    if explain:
        outcome.verified = answer.verified
    return outcome


def _spec_only(question, path, settings, provider):
    """Call 1 only: question -> spec -> result (no explanation)."""
    from .ask import SPEC_SYSTEM, Answer, _json_object, _spec_prompt, account_context
    from .execute import QueryTimeout
    from .llm import LLMError
    from .spec import SpecError

    answer = Answer(question)
    prompt = _spec_prompt(question, account_context(path, settings), None)
    error = None
    for attempt in (1, 2):
        answer.spec_attempts = attempt
        request = prompt if error is None else (
            f"{prompt}\n\nYour previous reply was rejected: {error}\nReply with a corrected JSON QuerySpec only.")
        try:
            reply, usage = provider.generate(SPEC_SYSTEM, request, json_mode=True)
        except LLMError as exc:
            if isinstance(exc, QuotaError):
                raise
            answer.error = str(exc)
            return answer
        answer.usage.add(usage)
        try:
            answer.spec = parse_spec(_json_object(reply))
            break
        except SpecError as exc:
            error = str(exc)
    if answer.spec is not None and answer.spec.kind == "query":
        try:
            answer.result = execute(answer.spec, path, settings)
        except QueryTimeout as exc:
            answer.error = str(exc)
    return answer


def run_suite(questions: list[dict], path: Path, settings: Settings, provider: Provider, *, explain: bool = True,
              rpm: float | None = None, quota_wait: float = 65.0, sleep=time.sleep, progress=None) -> Report:
    report = Report(f"{provider.name} {provider.model}", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    pacer = Pacer(rpm, sleep=sleep)
    for i, q in enumerate(questions, start=1):
        outcome = None
        for attempt in (1, 2):
            pacer.before(2 if explain else 1)
            try:
                outcome = run_one(q, path, settings, provider, explain=explain)
                quota = outcome.error if outcome.error and "quota" in outcome.error.lower() else None
            except QuotaError as error:
                quota = str(error)
            if not quota:
                break
            if attempt == 2:  # a daily quota, not a per-minute one: stop and keep what was done
                report.stopped_early = f"stopped at {q['id']}: {quota}"
                return report
            sleep(quota_wait)  # a per-minute limit: wait it out once
        report.outcomes.append(outcome)
        if progress:
            progress(i, len(questions), outcome)
    return report


# ---------------------------------------------------------------- reporting


def _pct(v) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def markdown(report: Report) -> str:
    s = report.summary()
    lines = [
        f"### {report.model} ({report.started[:10]})", "",
        "| Measure | Result |", "|---|---|",
        f"| Questions | {s['questions']} |",
        f"| Valid spec | {_pct(s['valid_spec_rate'])} |",
        f"| Result match (clear questions) | {_pct(s['result_match_rate'])} |",
        f"| Refusals (adversarial) | {_pct(s['refusal_rate'])} |",
        f"| Number check passed | {_pct(s['number_check_rate'])} |",
        f"| Requests / tokens per question | {s['requests_per_question']:.1f} / {s['tokens_per_question']:.0f} |",
        f"| Seconds per question | {s['seconds_per_question']:.1f} |", "",
        "| Category | Passed |", "|---|---|",
    ]
    lines += [f"| {cat} | {p}/{t} |" for cat, (p, t) in report.by_category().items()]
    if report.stopped_early:
        lines += ["", f"Stopped early: {report.stopped_early}"]
    return "\n".join(lines) + "\n"


def save(report: Report, out_dir: Path | str) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{report.model.replace(' ', '_')}_{report.started[:19].replace(':', '')}"
    data = {"model": report.model, "started": report.started, "summary": report.summary(),
            "by_category": report.by_category(), "stopped_early": report.stopped_early,
            "outcomes": [asdict(o) for o in report.outcomes]}
    json_path = out / f"{stem}.json"
    md_path = out / f"{stem}.md"
    json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    md_path.write_text(markdown(report), encoding="utf-8")
    return json_path, md_path
