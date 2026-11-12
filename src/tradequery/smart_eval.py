"""Normal answers against smart answers, on the same questions, scored automatically.

Each question in evals/smart.jsonl has a gold spec for its main result. It is
asked twice: in normal mode and in smart mode. Both are scored like the main
eval (right result, numbers checked) plus cost (requests, tokens, seconds).
Smart answers are also judged on what smart mode promises:

  checks   at least one extra check was planned and ran
  why      a "Why:" paragraph that names something specific (a number, a
           date, a symbol), not a general remark
  ideas    2 or 3 "worth asking next" questions
  style    no advice or predictions, never "news caused it", "basket" not
           "sequence", no column names, no "lost -20", a streak is not a basket
  model    the smart text itself passed the number check (not the plain
           fallback sentence)

The judge is a set of plain rules, not another model, so runs are cheap and
repeatable. It cannot tell whether a "Why" is insightful, only whether it is
there, specific and safe; read the answers in the report for that.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .ask import Answer, ask
from .evaluate import Pacer, _pct, results_match
from .execute import execute
from .llm import Provider, QuotaError
from .settings import Settings
from .spec import parse_spec

ADVICE = re.compile(r"\byou should\b|\bi recommend\b|\bi suggest\b|\bconsider (?:reducing|increasing|using|"
                    r"avoiding|setting)\b|\bwill (?:likely|probably)\b|\bin the future\b", re.IGNORECASE)
NEWS_CAUSE = re.compile(r"news (?:caused|was the cause|triggered)|(?:because of|due to|caused by) (?:the )?news",
                        re.IGNORECASE)
STYLE = ("advice or a prediction", "says news caused it", 'says "sequence"', "a column name in the text",
         'a double negative ("lost -")', "calls a streak a basket")
SPECIFIC = re.compile(r"\d|\b[A-Z]{6}\b")  # a number, date, hour or a symbol like XAUUSD


@dataclass
class Mode:
    passed: bool = False  # the main result matched the gold answer
    verified: bool = False  # the written answer passed the number check
    text_source: str | None = None
    requests: int = 0
    tokens: int = 0
    seconds: float = 0.0
    error: str | None = None
    text: str | None = None


@dataclass
class SmartOutcome:
    id: str
    question: str
    normal: Mode = field(default_factory=Mode)
    smart: Mode = field(default_factory=Mode)
    checks: list[str] = field(default_factory=list)  # the purposes of the checks that ran
    checks_rejected: int = 0
    ideas: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # what the judge found wrong with the smart answer

    @property
    def good(self) -> bool:
        return self.smart.passed and not self.problems


def why_of(text: str | None) -> str | None:
    m = re.search(r"^\s*Why:\s*(.+)", text or "", re.IGNORECASE | re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else None


def judge(answer: Answer) -> list[str]:
    """What is wrong with a smart answer (empty: nothing the rules can see)."""
    problems = []
    text = answer.text or ""
    if not answer.verified:
        problems.append("numbers not checked")
    elif answer.text_source != "model":
        problems.append("fell back to the plain sentence")
    if not answer.checks:
        problems.append("no extra check ran")
    why = why_of(text)
    if answer.checks and not why:
        problems.append("no Why paragraph")
    elif why and not SPECIFIC.search(why):
        problems.append("the Why names nothing specific")
    if not 2 <= len(answer.ideas) <= 3:
        problems.append(f"{len(answer.ideas)} next questions (want 2 or 3)")
    elif not all(i.rstrip().endswith("?") for i in answer.ideas):
        problems.append("a next question is not a question")
    if ADVICE.search(text):
        problems.append("advice or a prediction")
    if NEWS_CAUSE.search(text):
        problems.append("says news caused it")
    if re.search(r"\bsequences?\b", text, re.IGNORECASE):
        problems.append('says "sequence"')
    if re.search(r"\b[a-z]+_[a-z][a-z_]*\b", text):
        problems.append("a column name in the text")
    if re.search(r"\b(?:lost|losing|loss of) -\d", text):
        problems.append('a double negative ("lost -")')
    if answer.spec is not None and answer.spec.entity == "streaks" and re.search(r"\bbaskets?\b", text, re.I):
        problems.append("calls a streak a basket")
    return problems


def _mode(answer: Answer, gold, seconds: float) -> Mode:
    m = Mode(verified=answer.verified, text_source=answer.text_source, requests=answer.usage.requests,
             tokens=answer.usage.input_tokens + answer.usage.output_tokens, seconds=round(seconds, 2),
             error=answer.error, text=answer.text)
    if answer.spec is not None and answer.spec.kind == "query" and answer.result is not None:
        m.passed = results_match(gold, answer.result, ignore_text=True)
    return m


def run_one(q: dict, path: Path, settings: Settings, provider: Provider) -> SmartOutcome:
    out = SmartOutcome(q["id"], q["question"])
    gold = execute(parse_spec(q["gold"]), path, settings)
    started = time.monotonic()
    normal = ask(q["question"], path, settings, provider)
    out.normal = _mode(normal, gold, time.monotonic() - started)
    started = time.monotonic()
    smart = ask(q["question"], path, settings, provider, smart=True)
    out.smart = _mode(smart, gold, time.monotonic() - started)
    out.checks = [purpose for purpose, _, _ in smart.checks]
    out.checks_rejected = len(smart.plan_problems)
    out.ideas = smart.ideas
    out.problems = judge(smart)
    return out


@dataclass
class SmartReport:
    model: str
    started: str
    outcomes: list[SmartOutcome] = field(default_factory=list)
    stopped_early: str | None = None

    def summary(self) -> dict:
        o = self.outcomes
        n = len(o) or 1

        def rate(test):
            return sum(1 for x in o if test(x)) / len(o) if o else None

        def cost(mode):
            return {"result_match_rate": rate(lambda x: getattr(x, mode).passed),
                    "number_check_rate": rate(lambda x: getattr(x, mode).verified),
                    "requests_per_question": sum(getattr(x, mode).requests for x in o) / n,
                    "tokens_per_question": sum(getattr(x, mode).tokens for x in o) / n,
                    "seconds_per_question": sum(getattr(x, mode).seconds for x in o) / n}

        return {
            "questions": len(o), "normal": cost("normal"), "smart": cost("smart"),
            "checks_rate": rate(lambda x: x.checks),
            "checks_per_question": sum(len(x.checks) for x in o) / n,
            "rejected_per_question": sum(x.checks_rejected for x in o) / n,
            "why_rate": rate(lambda x: "no Why paragraph" not in x.problems
                             and "the Why names nothing specific" not in x.problems and x.checks),
            "ideas_rate": rate(lambda x: 2 <= len(x.ideas) <= 3),
            "style_rate": rate(lambda x: not set(STYLE) & set(x.problems)),
            "good_rate": rate(lambda x: x.good),
        }


def run_compare(questions: list[dict], path: Path, settings: Settings, provider: Provider, *,
                rpm: float | None = None, quota_wait: float = 65.0, sleep=time.sleep, progress=None) -> SmartReport:
    report = SmartReport(f"{provider.name} {provider.model}", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    pacer = Pacer(rpm, sleep=sleep)
    for i, q in enumerate(questions, start=1):
        outcome = None
        for attempt in (1, 2):
            pacer.before(6)  # normal (up to 2) + smart (up to 4)
            try:
                outcome = run_one(q, path, settings, provider)
                errors = [e for e in (outcome.normal.error, outcome.smart.error) if e]
                quota = next((e for e in errors if "quota" in e.lower()), None)
            except QuotaError as error:
                quota = str(error)
            if not quota:
                break
            if attempt == 2:
                report.stopped_early = f"stopped at {q['id']}: {quota}"
                return report
            sleep(quota_wait)
        report.outcomes.append(outcome)
        if progress:
            progress(i, len(questions), outcome)
    return report


def markdown(report: SmartReport) -> str:
    s = report.summary()
    nm, sm = s["normal"], s["smart"]
    lines = [
        f"### Normal vs smart: {report.model} ({report.started[:10]})", "",
        f"{s['questions']} questions, each asked in both modes.", "",
        "| Measure | Normal | Smart |", "|---|---|---|",
        f"| Result match | {_pct(nm['result_match_rate'])} | {_pct(sm['result_match_rate'])} |",
        f"| Number check passed | {_pct(nm['number_check_rate'])} | {_pct(sm['number_check_rate'])} |",
        f"| Requests per question | {nm['requests_per_question']:.1f} | {sm['requests_per_question']:.1f} |",
        f"| Tokens per question | {nm['tokens_per_question']:.0f} | {sm['tokens_per_question']:.0f} |",
        f"| Seconds per question | {nm['seconds_per_question']:.1f} | {sm['seconds_per_question']:.1f} |", "",
        "| Smart answers | Result |", "|---|---|",
        f"| An extra check ran | {_pct(s['checks_rate'])} ({s['checks_per_question']:.1f} per question, "
        f"{s['rejected_per_question']:.1f} rejected) |",
        f"| A specific Why | {_pct(s['why_rate'])} |",
        f"| 2-3 next questions | {_pct(s['ideas_rate'])} |",
        f"| No advice, no \"news caused it\" | {_pct(s['style_rate'])} |",
        f"| Everything above, and the right result | {_pct(s['good_rate'])} |", "",
        "Per question:", "",
    ]
    for o in report.outcomes:
        mark = "good" if o.good else "; ".join(o.problems) or "wrong result"
        lines.append(f"- **{o.id}** {o.question} — {mark}")
        if o.smart.text:
            lines.append("  > " + o.smart.text.replace("\n", "\n  > "))
    if report.stopped_early:
        lines += ["", f"Stopped early: {report.stopped_early}"]
    return "\n".join(lines) + "\n"


def save(report: SmartReport, out_dir: Path | str) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{report.model.replace(' ', '_')}_{report.started[:19].replace(':', '')}"
    data = {"model": report.model, "started": report.started, "summary": report.summary(),
            "stopped_early": report.stopped_early,
            "outcomes": [{**asdict(o), "good": o.good} for o in report.outcomes]}
    json_path, md_path = out / f"{stem}.json", out / f"{stem}.md"
    json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    md_path.write_text(markdown(report), encoding="utf-8")
    return json_path, md_path
