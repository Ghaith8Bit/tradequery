"""The normal-vs-smart eval: its question set, its judge, and a full run with a scripted model."""

import json
import re
from pathlib import Path

import pytest

from tradequery import smart_eval
from tradequery.ask import Answer
from tradequery.evaluate import build_eval_account, load_questions
from tradequery.execute import execute
from tradequery.llm import FakeProvider, Usage
from tradequery.settings import load_settings
from tradequery.spec import parse_spec

SETTINGS = load_settings()
SMART = load_questions(Path(__file__).parents[1] / "evals" / "smart.jsonl")
GOOD = ("The answer is in the table.\nWhy: most of it came from XAUUSD trades.\n"
        "Worth asking next:\n- Which hour lost most?\n- Which symbol lost most?")


@pytest.fixture(scope="module")
def path(tmp_path_factory):
    return build_eval_account(tmp_path_factory.mktemp("smart"), SETTINGS)


def test_the_smart_set_has_unique_ids_and_every_gold_returns_data(path):
    assert len(SMART) >= 15 and len({q["id"] for q in SMART}) == len(SMART)
    for q in SMART:
        rows = execute(parse_spec(q["gold"]), path, SETTINGS).rows
        assert rows and any(v is not None for r in rows for v in r), q["id"]


def answer(text=GOOD, checks=1, ideas=None, verified=True, source="model"):
    a = Answer("q", text=text, verified=verified, text_source=source)
    a.checks = [("Recent trades", None, None)] * checks
    a.ideas = ["Which hour lost most?", "Which symbol lost most?"] if ideas is None else ideas
    return a


def test_the_judge():
    assert smart_eval.judge(answer()) == []
    assert "no extra check ran" in smart_eval.judge(answer(checks=0))
    assert "no Why paragraph" in smart_eval.judge(answer(text="Just an answer."))
    assert "the Why names nothing specific" in smart_eval.judge(answer(text="Answer.\nWhy: it was a bad time."))
    assert "1 next questions (want 2 or 3)" in smart_eval.judge(answer(ideas=["Only one?"]))
    assert "a next question is not a question" in smart_eval.judge(answer(ideas=["Look at hours", "And symbols?"]))
    assert "advice or a prediction" in smart_eval.judge(answer(text=GOOD + " You should trade less."))
    assert "says news caused it" in smart_eval.judge(answer(text=GOOD.replace("most of it", "the loss was due to news, ")))
    assert 'says "sequence"' in smart_eval.judge(answer(text=GOOD + " The sequence lost."))
    assert "fell back to the plain sentence" in smart_eval.judge(answer(source="template"))
    assert "numbers not checked" in smart_eval.judge(answer(verified=False))
    assert "a column name in the text" in smart_eval.judge(answer(text=GOOD + " See position_id 1095."))
    assert 'a double negative ("lost -")' in smart_eval.judge(answer(text=GOOD + " It lost -21.4."))
    streak = answer(text=GOOD + " The basket lost.")
    streak.spec = parse_spec({"entity": "streaks", "filters": [{"field": "type", "op": "eq", "value": "loss"}]})
    assert "calls a streak a basket" in smart_eval.judge(streak)


def test_plain_words_fix_column_names_sessions_seconds_and_double_negatives():
    from tradequery.ask import plain_words

    text = ("position_id 1095 and position ID 7 in sequence_id 1 during london_new_york and new_york, "
            "asia and new york at 13:20:00; it lost -21.4 and a loss of -5")
    assert plain_words(text) == ("trade 1095 and trade 7 in basket 1 during London/New York overlap and New York, "
                                 "Asia and New York at 13:20; it lost 21.4 and a loss of 5")


class Scripted(FakeProvider):
    """Answers every call from the question in the prompt: the gold spec, a plan, a reply."""

    def __init__(self, questions):
        super().__init__([])
        self.gold = {q["question"]: q["gold"] for q in questions}

    def generate(self, system, prompt, *, json_mode=False):
        question = re.search(r"^Question: (.+)$", prompt, re.MULTILINE).group(1)
        if "PLANNING MODE" in system:
            reply = json.dumps({"checks": [{"purpose": "Recent trades", "spec": {"entity": "trades", "limit": 3}}]})
        elif json_mode:
            reply = json.dumps(self.gold[question])
        else:
            reply = GOOD if "Why:" in system else "The answer is in the table."
        return reply, Usage(1, 100, 10)


def test_a_full_run_compares_the_modes(path, tmp_path):
    questions = SMART[:3]
    report = smart_eval.run_compare(questions, path, SETTINGS, Scripted(questions))
    assert [o.good for o in report.outcomes] == [True, True, True]
    s = report.summary()
    assert s["normal"]["result_match_rate"] == s["smart"]["result_match_rate"] == 1.0
    assert s["smart"]["requests_per_question"] == 3 and s["good_rate"] == s["why_rate"] == 1.0
    assert s["normal"]["requests_per_question"] < s["smart"]["requests_per_question"]
    json_path, md_path = smart_eval.save(report, tmp_path)
    text = md_path.read_text()
    assert "| Requests per question |" in text and "Why: most of it came from XAUUSD" in text
    assert json.loads(json_path.read_text())["outcomes"][0]["good"] is True


def test_a_daily_quota_stops_the_smart_run(path):
    from tradequery.llm import QuotaError

    class Out(FakeProvider):
        def generate(self, system, prompt, *, json_mode=False):
            raise QuotaError("daily quota reached")

    report = smart_eval.run_compare(SMART[:2], path, SETTINGS, Out([]), sleep=lambda s: None)
    assert report.outcomes == [] and "quota" in report.stopped_early
