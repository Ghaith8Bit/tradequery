"""Every answer says what level it is about (account, baskets, single trades) and offers switches."""

from tradequery.examples import WORST_DRAWDOWN, with_drawdown_basis
from tradequery.execute import Result
from tradequery.scope import describe, level_label
from tradequery.spec import parse_spec


def result(columns, rows):
    return Result("query", columns=columns, rows=rows)


def test_levels_are_named_plainly():
    assert level_label(parse_spec({"entity": "sequences"})) == "Baskets"
    assert level_label(parse_spec({"entity": "trades"})) == "Single trades"
    assert level_label(parse_spec(with_drawdown_basis(WORST_DRAWDOWN, "equity"))) == "Account drawdown (equity)"
    assert level_label(parse_spec({"entity": "drawdown_episodes"})) == "Account drawdown (balance and equity)"


def test_a_drawdown_answer_offers_its_trades_and_the_other_basis():
    spec = parse_spec(with_drawdown_basis(WORST_DRAWDOWN, "equity"))
    scope = describe(spec, result(["episode_id", "depth_abs"], [[72, 12980.45]]), has_equity=True, has_baskets=True)
    assert scope["labels"] == ["Account drawdown (equity)", "1 drawdown found"]
    trades, flip = scope["switches"]
    assert trades["spec"]["filters"] == [{"field": "equity_drawdown_episode_id", "op": "eq", "value": 72}]
    assert flip["label"] == "Show the balance drawdown instead"
    assert flip["spec"]["filters"] == [{"field": "basis", "op": "eq", "value": "balance"}]
    # an account without price bars has no equity drawdowns to switch to
    no_bars = describe(parse_spec(WORST_DRAWDOWN), result(["episode_id"], [[3]]), has_equity=False, has_baskets=True)
    assert [s["label"] for s in no_bars["switches"]] == ["Show the trades in this drawdown"]
    assert no_bars["switches"][0]["spec"]["filters"][0]["field"] == "drawdown_episode_id"


def test_baskets_and_trades_switch_to_each_other():
    basket = describe(parse_spec({"entity": "sequences", "limit": 1}), result(["sequence_id"], [[212]]),
                      has_equity=True, has_baskets=True)
    assert basket["labels"] == ["Baskets", "1 basket found"]
    assert basket["switches"][0]["spec"]["filters"] == [{"field": "sequence_id", "op": "eq", "value": 212}]
    assert basket["switches"][1]["kind"] == "ask"

    trades = describe(parse_spec({"entity": "trades", "metrics": ["win_rate"]}), result(["win_rate"], [[0.6]]),
                      has_equity=True, has_baskets=True)
    assert trades["labels"] == ["Single trades"]
    assert trades["switches"][0]["label"] == "Ask the same per basket"
    # no basket switch when the account has no baskets, no model, or the trades are one basket's legs
    assert describe(parse_spec({"entity": "trades"}), result([], []), has_equity=False, has_baskets=False)["switches"] == []
    assert describe(parse_spec({"entity": "trades"}), result([], []), has_equity=False, has_baskets=True,
                    model_ready=False)["switches"] == []
    legs = parse_spec({"entity": "trades", "filters": [{"field": "sequence_id", "op": "eq", "value": 212}]})
    assert describe(legs, result([], []), has_equity=True, has_baskets=True)["switches"] == []


def test_clarify_answers_have_no_scope():
    assert describe(parse_spec({"kind": "clarify", "message": "How big?"}), None, has_equity=True,
                    has_baskets=True) is None
