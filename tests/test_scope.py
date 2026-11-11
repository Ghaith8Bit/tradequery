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


def test_a_single_row_answer_has_a_headline_number():
    from tradequery.scope import headline

    basket = parse_spec({"entity": "sequences", "limit": 1})
    row = result(["sequence_id", "depth", "net_pnl", "is_win", "drawdown_abs", "drawdown_pct"],
                 [[222, 7, 73.78, 1, 2544.76, 0.2486]])
    assert headline(basket, row) == {"column": "drawdown_pct", "value": 0.2486}
    metrics = parse_spec({"entity": "trades", "metrics": ["profit_factor", "win_rate"]})
    assert headline(metrics, result(["profit_factor", "win_rate"], [[0.97, 0.62]]))["column"] == "profit_factor"
    assert headline(basket, result(["sequence_id"], [[1], [2]])) is None  # a list has no single number
    assert headline(basket, result(["sequence_id", "symbol"], [[1, "XAUUSD"]])) is None


def test_the_level_switch_is_offered_only_when_the_other_level_has_the_fields():
    from tradequery.scope import fits_level

    swap_by_day = parse_spec({"entity": "trades", "group_by": ["weekday"],
                              "aggregations": [{"func": "sum", "field": "swap", "alias": "swap_total"}],
                              "order_by": [{"field": "swap_total", "direction": "asc"}], "limit": 1})
    assert fits_level(swap_by_day, "sequences")  # baskets now have weekday and swap
    by_r = parse_spec({"entity": "trades", "metrics": ["avg_r"]})
    assert not fits_level(by_r, "sequences")  # R exists per trade only
    tagged = parse_spec({"entity": "trades", "filters": [{"field": "tags", "op": "eq", "value": "revenge_trade"}]})
    assert not fits_level(tagged, "sequences")
    lots = parse_spec({"entity": "trades", "filters": [{"field": "net_profit", "op": "lt", "value": 0}],
                       "order_by": [{"field": "lots", "direction": "desc"}]})
    assert fits_level(lots, "sequences")  # net_profit and lots have basket names
    deep = parse_spec({"entity": "sequences", "filters": [{"field": "drawdown_pct", "op": "gte", "value": 0.2}]})
    assert not fits_level(deep, "trades")  # trades have no basket drawdown
    scope = describe(deep, result(["sequence_id"], [[1], [2]]), has_equity=True, has_baskets=True)
    assert [s["label"] for s in scope["switches"]] == []
    scope = describe(by_r, result(["avg_r"], [[0.4]]), has_equity=True, has_baskets=True)
    assert scope["switches"] == []


def test_baskets_carry_trade_fields(tmp_path):
    from tradequery import db
    from tradequery.settings import load_settings
    from tradequery.synthetic import generate_account

    conn = db.connect(db.db_path(3, tmp_path))
    with conn:
        db.upsert_deals(conn, generate_account().deals)
        db.rebuild_derived(conn, load_settings())
    for row in conn.execute("SELECT sequence_id, weekday, swap, total_lots, drawdown_pct FROM v_sequences"):
        legs = conn.execute("SELECT weekday, swap, lots FROM v_trades WHERE sequence_id = ? ORDER BY open_time_utc",
                            (row["sequence_id"],)).fetchall()
        assert row["weekday"] == legs[0]["weekday"]
        assert row["swap"] == round(sum(t["swap"] for t in legs), 2)
        assert row["total_lots"] == round(sum(t["lots"] for t in legs), 2)
        assert row["drawdown_pct"] is None  # no price bars in the sample
    conn.close()


def test_clarify_answers_have_no_scope():
    assert describe(parse_spec({"kind": "clarify", "message": "How big?"}), None, has_equity=True,
                    has_baskets=True) is None
