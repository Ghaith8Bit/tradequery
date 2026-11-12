"""The eval suite's machinery: gold specs, matching, scoring, pacing. A fake model, no quota."""

import json
from pathlib import Path

import pytest

from tradequery import evaluate
from tradequery.evaluate import Pacer, build_eval_account, load_questions, results_match, run_suite
from tradequery.execute import Result, execute
from tradequery.llm import FakeProvider, QuotaError
from tradequery.settings import load_settings
from tradequery.spec import parse_spec

SETTINGS = load_settings()
QUESTIONS = load_questions(Path(__file__).parents[1] / "evals" / "questions.jsonl")


@pytest.fixture(scope="module")
def path(tmp_path_factory):
    return build_eval_account(tmp_path_factory.mktemp("eval"), SETTINGS)


def test_the_suite_has_the_plan_categories_and_size():
    counts = {}
    for q in QUESTIONS:
        counts[q["category"]] = counts.get(q["category"], 0) + 1
    assert len(QUESTIONS) == 81 and len({q["id"] for q in QUESTIONS}) == 81
    assert counts == {"basic filters": 10, "group-by and ranking": 10, "named metrics": 12, "detector tags": 8,
                      "sequences": 7, "news": 6, "drawdown": 6, "exposure": 6, "cash flows": 3, "magic numbers": 3,
                      "adversarial": 10}


def test_every_gold_spec_is_valid_and_runs(path):
    empty = []
    for q in QUESTIONS:
        if "gold" in q:
            result = execute(parse_spec(q["gold"]), path, SETTINGS)
            if not result.rows:
                empty.append(q["id"])
    # questions whose right answer is "nothing": no revenge trades, no overtrading, no grid, no losing martingale
    assert sorted(empty) == ["seq-04", "seq-06", "tag-01", "tag-07"]


def test_the_held_out_set_is_separate_and_every_gold_returns_data(path):
    held = load_questions(Path(__file__).parents[1] / "evals" / "heldout.jsonl")
    assert len(held) == 43 and len({q["id"] for q in held}) == 43
    tuned_ids = {q["id"] for q in QUESTIONS}
    tuned_text = {q["question"].lower() for q in QUESTIONS}
    assert not tuned_ids & {q["id"] for q in held}
    assert not tuned_text & {q["question"].lower() for q in held}
    for q in held:
        if "gold" in q:
            rows = execute(parse_spec(q["gold"]), path, SETTINGS).rows
            assert rows and any(v is not None for r in rows for v in r), q["id"]



def test_follow_ups_have_an_earlier_turn_and_every_gold_returns_data(path):
    follow = load_questions(Path(__file__).parents[1] / "evals" / "followups.jsonl")
    assert len(follow) >= 6 and len({q["id"] for q in follow}) == len(follow)
    for q in follow:
        assert execute(parse_spec(q["after"]["spec"]), path, SETTINGS).rows, q["id"]
        rows = execute(parse_spec(q["gold"]), path, SETTINGS).rows
        assert rows and any(v is not None for r in rows for v in r), q["id"]


def test_a_follow_up_is_asked_with_the_earlier_turn_as_its_focus(path):
    follow = load_questions(Path(__file__).parents[1] / "evals" / "followups.jsonl")
    fake = FakeProvider(perfect_replies(follow, explain=False))
    seen = []
    real = fake.generate

    def spy(system, prompt, **kw):
        seen.append(prompt)
        return real(system, prompt, **kw)

    fake.generate = spy
    report = run_suite(follow, path, SETTINGS, fake, explain=False)
    assert [o.id for o in report.outcomes if not o.passed] == []
    assert "Current focus: 14 Sep 2026" in seen[0] and "Previous question: Show my trades on 2026-09-14." in seen[0]

def test_planted_news_gives_the_news_questions_real_answers(path):
    gold = {q["id"]: q["gold"] for q in QUESTIONS if "gold" in q}
    assert execute(parse_spec(gold["news-01"]), path, SETTINGS).rows
    assert len(execute(parse_spec(gold["news-02"]), path, SETTINGS).rows) == 1  # the planted martingale overlapped NFP
    assert len(execute(parse_spec(gold["news-05"]), path, SETTINGS).rows) == 2  # two high-impact USD events


def result(columns, rows):
    return Result("query", columns, rows)


def test_matching_ignores_column_names_extra_columns_and_order():
    gold = result(["hour", "net"], [[3, -10.0], [5, 20.5]])
    assert results_match(gold, result(["hour", "total_net", "trades"], [[5, 20.5, 2], [3, -10.0, 1]]))
    assert not results_match(gold, result(["hour", "net"], [[3, -10.0], [5, 20.0]]))  # a wrong value
    assert not results_match(gold, result(["hour", "net"], [[3, -10.0]]))  # a missing row
    assert results_match(gold, result(["hour", "net"], [[3, -10.004], [5, 20.5]]))  # rounding


def test_ordered_matching_and_ignoring_labels():
    gold = result(["id", "net"], [[1, -5.0], [2, -3.0]])
    assert not results_match(gold, result(["id", "net"], [[2, -3.0], [1, -5.0]]), ordered=True)
    assert results_match(gold, result(["id", "net"], [[1, -5.0], [2, -3.0]]), ordered=True)
    compare = result(["set", "trades", "win_rate"], [["Fridays", 2, 0.5]])
    assert not results_match(compare, result(["set", "trades", "win_rate"], [["Friday", 2, 0.5]]))
    assert results_match(compare, result(["set", "trades", "win_rate"], [["Friday", 2, 0.5]]), ignore_text=True)


def perfect_replies(questions, explain):
    replies = []
    for q in questions:
        spec = q["gold"] if "gold" in q else {"kind": "unsupported", "message": "Not something I can answer."}
        replies.append(json.dumps(spec))
        if explain and "gold" in q:
            replies.append("Here is the result.")
    return replies


def test_a_perfect_model_scores_100_percent_on_every_question(path):
    fake = FakeProvider(perfect_replies(QUESTIONS, explain=True))
    report = run_suite(QUESTIONS, path, SETTINGS, fake, explain=True)
    failed = [o.id for o in report.outcomes if not o.passed]
    assert failed == []
    summary = report.summary()
    assert summary["valid_spec_rate"] == summary["result_match_rate"] == summary["refusal_rate"] == 1.0
    assert summary["number_check_rate"] == 1.0


def test_wrong_answers_are_scored_as_wrong(path):
    questions = [q for q in QUESTIONS if q["id"] in ("basic-01", "adv-04", "group-10")]
    fake = FakeProvider([
        json.dumps({"entity": "trades", "fields": ["position_id"], "filters": [{"field": "lots", "op": "gt", "value": 0.3}]}),
        json.dumps({"entity": "trades", "fields": ["position_id"]}),  # answered the password question with data
        json.dumps({"entity": "trades", "fields": ["position_id", "net_profit"],
                    "order_by": [{"field": "net_profit", "direction": "desc"}], "limit": 3}),  # best, not worst
    ])
    report = run_suite(questions, path, SETTINGS, fake, explain=False)
    assert [o.passed for o in report.outcomes] == [False, False, False]
    assert report.summary()["valid_spec_rate"] == 1.0  # valid specs, wrong answers


def test_a_daily_quota_stops_the_run_and_keeps_what_was_done(path):
    questions = [q for q in QUESTIONS if q["id"] in ("metric-01", "metric-02")]
    fake = FakeProvider([json.dumps(questions[0]["gold"]), QuotaError("quota"), QuotaError("quota")])
    naps = []
    report = run_suite(questions, path, SETTINGS, fake, explain=False, sleep=naps.append)
    assert len(report.outcomes) == 1 and report.outcomes[0].passed
    assert report.stopped_early.startswith("stopped at metric-02") and naps == [65.0]


def test_the_pacer_keeps_under_the_per_minute_limit():
    now = [0.0]
    naps = []
    pacer = Pacer(4, sleep=lambda s: (naps.append(s), now.__setitem__(0, now[0] + s)), clock=lambda: now[0])
    pacer.before(2)
    pacer.before(2)
    assert naps == []
    pacer.before(2)  # a fifth and sixth request in the same minute: wait for the window
    assert len(naps) == 1 and naps[0] == pytest.approx(60.1)


def test_reports_are_saved_as_json_and_markdown(path, tmp_path):
    questions = [q for q in QUESTIONS if q["id"] in ("metric-01", "adv-01")]
    report = run_suite(questions, path, SETTINGS, FakeProvider(perfect_replies(questions, explain=False)), explain=False)
    json_path, md_path = evaluate.save(report, tmp_path)
    data = json.loads(json_path.read_text())
    assert data["summary"]["questions"] == 2 and data["by_category"]["adversarial"] == [1, 1]
    assert "| Result match (clear questions) | 100.0% |" in md_path.read_text()
