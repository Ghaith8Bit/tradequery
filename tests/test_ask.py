"""The ask pipeline with a scripted fake model: spec retry, clarify/unsupported, number check, follow-ups."""

import json
from datetime import datetime, timezone

import pytest

from tradequery import db
from tradequery.ask import EXPLAIN_SYSTEM, SPEC_SYSTEM, Session, _json_object, account_context, ask
from tradequery.examples import LONGEST_LOSING_STREAK
from tradequery.llm import FakeProvider, LLMError, QuotaError, choose_model
from tradequery.settings import load_settings
from tradequery.synthetic import INJECTION_COMMENT, generate_account

SETTINGS = load_settings()


@pytest.fixture(scope="module")
def path(tmp_path_factory):
    p = db.db_path(77, tmp_path_factory.mktemp("acct"))
    conn = db.connect(p)
    with conn:
        db.upsert_deals(conn, generate_account().deals)
        db.save_account(conn, {"login": 77, "balance": 0.0, "currency": "USD"}, earliest_history=None,
                        synced_at=datetime.now(timezone.utc))
        db.rebuild_derived(conn, SETTINGS)
    conn.close()
    return p


def test_a_question_becomes_a_spec_a_table_and_a_checked_answer(path):
    fake = FakeProvider([json.dumps(LONGEST_LOSING_STREAK), "Your longest losing streak was 6 trades."])
    answer = ask("What is my longest losing streak?", path, SETTINGS, fake)
    assert answer.error is None and answer.verified
    assert answer.spec.entity == "streaks" and answer.result.rows[0][2] == 6
    assert answer.text == "Your longest losing streak was 6 trades."
    assert answer.usage.requests == 2
    (spec_system, spec_prompt, json_mode), (explain_system, explain_prompt, _) = fake.calls
    assert spec_system == SPEC_SYSTEM and json_mode and "Question: What is my longest losing streak?" in spec_prompt
    assert explain_system == EXPLAIN_SYSTEM and "Result table:" in explain_prompt


def test_an_invalid_spec_gets_one_retry_with_the_error(path):
    bad = json.dumps({"entity": "trades", "filters": [{"field": "comment", "op": "eq", "value": "x"}]})
    fake = FakeProvider([bad, json.dumps(LONGEST_LOSING_STREAK), "The longest losing streak was 6 trades."])
    answer = ask("longest losing streak", path, SETTINGS, fake)
    assert answer.spec_attempts == 2 and answer.verified
    assert "unknown field 'comment'" in fake.calls[1][1]  # the error went back to the model


def test_two_invalid_specs_fail_politely(path):
    fake = FakeProvider(["not json at all", json.dumps({"entity": "orders"})])
    answer = ask("anything", path, SETTINGS, fake)
    assert answer.spec is None and answer.result is None
    assert answer.error.startswith("The question could not be turned into a valid query")


def test_invented_numbers_get_one_retry_then_the_table_without_prose(path):
    fake = FakeProvider([json.dumps(LONGEST_LOSING_STREAK), "It was 9 trades.", "It was 7 trades, costing 500."])
    answer = ask("longest losing streak", path, SETTINGS, fake)
    assert not answer.verified and answer.text is None
    assert answer.result.rows  # the table is still there
    assert answer.unsupported_numbers == ["7", "500"]
    assert "not in the table: 9" in fake.calls[2][1]


def test_a_corrected_explanation_is_accepted(path):
    fake = FakeProvider([json.dumps(LONGEST_LOSING_STREAK), "It was 9 trades.", "It was 6 trades in a row."])
    assert ask("longest losing streak", path, SETTINGS, fake).verified


def test_clarify_and_unsupported_answers_skip_the_query(path):
    fake = FakeProvider([json.dumps({"kind": "clarify", "message": "How large is a big loss for you?"})])
    answer = ask("show my big losses", path, SETTINGS, fake)
    assert (answer.spec.kind, answer.text, answer.result) == ("clarify", "How large is a big loss for you?", None)
    fake = FakeProvider([json.dumps({"kind": "unsupported", "message": "One account at a time."})])
    assert ask("compare all my accounts", path, SETTINGS, fake).text == "One account at a time."


def test_follow_ups_send_the_previous_spec(path):
    session = Session()
    first = {"entity": "trades", "group_by": ["weekday"], "metrics": ["win_rate"]}
    fake = FakeProvider([json.dumps(first), "Answer without numbers.",
                         json.dumps({**first, "filters": [{"field": "canonical_symbol", "op": "eq", "value": "XAUUSD"}]}),
                         "Answer without numbers."])
    ask("win rate by weekday", path, SETTINGS, fake, session)
    assert session.spec == first
    ask("only gold", path, SETTINGS, fake, session)
    follow_up_prompt = fake.calls[2][1]
    assert "Previous question: win rate by weekday" in follow_up_prompt and '"group_by": ["weekday"]' in follow_up_prompt
    assert session.spec["filters"][0]["value"] == "XAUUSD"


def test_provider_errors_are_reported_not_raised(path):
    answer = ask("anything", path, SETTINGS, FakeProvider([QuotaError("daily quota reached")]))
    assert answer.error == "daily quota reached"
    fake = FakeProvider([json.dumps(LONGEST_LOSING_STREAK), LLMError("network down")])
    answer = ask("longest losing streak", path, SETTINGS, fake)
    assert answer.result.rows and "without an explanation" in answer.error


def test_the_model_never_sees_trade_comments(path):
    fake = FakeProvider([json.dumps({"entity": "trades", "limit": 500}), "Answer without numbers."])
    ask("show all my trades", path, SETTINGS, fake)
    everything_sent = " ".join(system + prompt for system, prompt, _ in fake.calls)
    assert INJECTION_COMMENT not in everything_sent  # the planted instruction in a trade comment
    assert "comment" not in json.loads(json.dumps(fake.calls[1][1]))  # no comment column in the table


def test_account_context_lists_symbols_and_magic_numbers(path):
    context = account_context(path, SETTINGS)
    assert "XAUUSD" in context and "12345" in context and "currency USD" in context
    assert "Drawdown default: balance" in context  # the synthetic account has no price bars


def test_json_is_found_inside_code_fences_and_chatter():
    assert json.loads(_json_object('Sure! ```json\n{"entity": "streaks"}\n``` done')) == {"entity": "streaks"}
    assert json.loads(_json_object('here: {"entity": "streaks"} ok')) == {"entity": "streaks"}


def test_choose_model_prefers_the_newest_stable_flash_lite():
    names = ["models/gemini-3.0-flash-lite", "models/gemini-3.5-flash-lite", "models/gemini-3.5-flash-lite-preview",
             "models/gemini-3.8-flash", "models/gemini-3.5-pro", "models/text-embedding-004"]
    assert choose_model(names) == "gemini-3.5-flash-lite"
    assert choose_model(["models/gemini-3.8-flash", "models/gemini-3.8-flash-tts"]) == "gemini-3.8-flash"
    assert choose_model(["models/gemini-3.5-pro"]) is None


def test_shares_reach_the_model_as_percentages(path):
    from tradequery.examples import WORST_DRAWDOWN
    fake = FakeProvider([json.dumps(WORST_DRAWDOWN), "Answer without numbers."])
    ask("worst drawdown", path, SETTINGS, fake)
    table = fake.calls[1][1].split("Result table:")[1]
    assert "%" in table and "0.0" not in table.split(chr(10))[1].split(" | ")[5]  # depth_pct column as a percentage


def test_account_context_lists_the_session_and_close_reason_values(path):
    context = account_context(path, SETTINGS)
    assert "session values: london, london_new_york, new_york" in context
    assert "close_reason values: client, expert, sl, tp" in context


def test_gemini_retries_once_on_a_temporary_server_error():
    from types import SimpleNamespace
    from google.genai import errors, types
    from tradequery.llm import GeminiProvider

    class FakeModels:
        def __init__(self, failures):
            self.failures, self.calls = failures, 0

        def generate_content(self, **kwargs):
            self.calls += 1
            if self.failures:
                code = self.failures.pop(0)
                raise errors.ServerError(code, {"error": {"message": "high demand", "status": "UNAVAILABLE"}})
            meta = SimpleNamespace(prompt_token_count=10, candidates_token_count=5, thoughts_token_count=0)
            return SimpleNamespace(text="ok", usage_metadata=meta, prompt_feedback=None)

    def provider(failures):
        p = object.__new__(GeminiProvider)
        p._types, p._errors, p.model, p._sleep = types, errors, "m", lambda s: None
        p._client = SimpleNamespace(models=FakeModels(failures))
        return p

    once = provider([503])
    assert once.generate("s", "p")[0] == "ok" and once._client.models.calls == 2
    with pytest.raises(LLMError, match="503"):
        provider([503, 503]).generate("s", "p")


def test_drawdown_default_follows_the_account():
    import sqlite3

    from tradequery.ask import SPEC_SYSTEM, _example_question, drawdown_default
    from tradequery.examples import WORST_DRAWDOWN, with_drawdown_basis

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE drawdown_episodes (basis TEXT)")
    conn.execute("INSERT INTO drawdown_episodes VALUES ('balance')")
    assert drawdown_default(conn) == "balance"  # no price bars, no equity drawdowns
    conn.execute("INSERT INTO drawdown_episodes VALUES ('equity')")
    assert drawdown_default(conn) == "equity"

    equity = with_drawdown_basis(WORST_DRAWDOWN, "equity")
    assert equity["filters"] == [{"field": "basis", "op": "eq", "value": "equity"}]
    assert WORST_DRAWDOWN["filters"][0]["value"] == "balance"  # the preset itself is unchanged
    assert with_drawdown_basis({"entity": "trades"}, "equity") == {"entity": "trades"}

    # worked examples name their basis, so the model follows the account's default instead
    assert _example_question("What was my worst drawdown period?", WORST_DRAWDOWN) == \
        "What was my worst balance drawdown period?"
    assert "Q: What was my worst balance drawdown period" in SPEC_SYSTEM
    assert "Drawdown default" in SPEC_SYSTEM and "equity_drawdown_episode_id" in SPEC_SYSTEM


def test_answers_say_basket_not_sequence():
    from tradequery.ask import plain_words
    assert plain_words("Across 1 baskets, sequence 222 closed in profit.") == "Across 1 basket, basket 222 closed in profit."
    assert plain_words("Sequences of 12 trades") == "Baskets of 12 trades"
    assert plain_words("11 baskets and 21 trades") == "11 baskets and 21 trades"


def test_follow_ups_see_the_ids_the_last_answer_found():
    from tradequery.ask import _found_ids, _spec_prompt
    from tradequery.execute import Result

    result = Result("query", columns=["episode_id", "basis", "depth_abs"], rows=[[72, "equity", 12980.45]],
                    truncated=False, interpretation="", notes=[], sql="")
    found = _found_ids(result)
    assert found == "episode_id 72; basis equity"  # ids only, never amounts or trade details
    session = Session(question="What was my worst drawdown?", spec={"entity": "drawdown_episodes"}, found=found)
    prompt = _spec_prompt("Which trades were in it?", "context", session)
    assert "Previous result: episode_id 72; basis equity" in prompt
    many = Result("query", columns=["sequence_id"], rows=[[i] for i in range(8)], truncated=False, interpretation="",
                  notes=[], sql="")
    assert _found_ids(many).endswith("(and 3 more rows)")
    assert _found_ids(Result("query", columns=["net"], rows=[[1.0]], truncated=False, interpretation="", notes=[],
                             sql="")) is None
