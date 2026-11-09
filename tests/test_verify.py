"""Number checker: supported numbers pass in every usual written form; invented ones are caught."""

from tradequery.verify import check_numbers

ROWS = [
    [22, "2026-09-25 14:49:38", "2026-09-28 03:32:56", None, 10765.07, 0.9998, 48, 0.0204],
]


def test_numbers_from_the_table_pass_in_their_usual_written_forms():
    text = ("Your worst drawdown was 10,765.07 from 2026-09-25 14:49 to 2026-09-28 03:32, "
            "99.98% of the money at stake, over 48 trades; 2% of the loss overlapped news.")
    check = check_numbers(text, ROWS)
    assert check.ok, check.unsupported
    assert check.checked >= 6


def test_rounding_and_signs():
    rows = [["Fridays", 645, 0.6357, 787.16], ["Other days", 2314, 0.6223, -1617.06]]
    assert check_numbers("Fridays: 63.6% over 645 trades, net 787; other days lost 1,617.06.", rows).ok
    assert check_numbers("Other days netted -1,617.1.", rows).ok


def test_invented_numbers_are_caught():
    rows = [["Fridays", 645, 0.6357, 787.16], ["Other days", 2314, 0.6223, -1617.06]]
    check = check_numbers("Fridays won 70% of 645 trades, 1.4 points better than other days.", rows)
    assert not check.ok and check.unsupported == ["70%", "1.4"]
    # a computed difference is not in the table either
    assert not check_numbers("Fridays beat other days by 2,404.22.", rows).ok


def test_percent_needs_the_right_share_not_any_close_number():
    rows = [["x", 0.6252]]
    assert check_numbers("62.52%", rows).ok and check_numbers("63%", rows).ok
    assert not check_numbers("65%", rows).ok


def test_dates_and_times_must_be_in_the_table():
    assert check_numbers("It bottomed on Sep 28 at 03:32.", ROWS).ok
    assert check_numbers("It bottomed on 28 September.", ROWS).ok
    check = check_numbers("It bottomed on 2026-09-29 at 04:10.", ROWS)
    assert check.unsupported == ["2026-09-29", "04:10"]


def test_numbers_from_the_question_and_spec_are_allowed():
    rows = [[1, "XAUUSD", -1893.44, 252.55]]
    text = "Of the trades held longer than 4 hours that lost over 50, the worst lost 1,893.44 after 252.55 minutes."
    assert check_numbers(text, rows, extra_numbers=[240, 50, 4]).ok
    assert not check_numbers(text, rows).ok  # without the question, 4 and 50 are unsupported


def test_years_in_result_dates_are_not_counted_and_row_count_is_allowed():
    assert check_numbers("In 2026 there was 1 such episode.", ROWS).ok
    assert check_numbers("Nothing matched.", []).ok


def test_a_percentage_never_matches_a_count():
    # seen live: "overlapped news by 100%" for a 0.0204 share, while the table had 1 row.
    # (With a 0.9998 depth share in the row, 100% would be a fair rounding of it; numbers alone
    # cannot tell which column was meant, so the explanation prompt shows shares as percentages.)
    rows = [[22, "2026-09-25 14:49:38", 10765.07, 48, 0.0204]]
    check = check_numbers("Your worst drawdown of 10,765.07 overlapped news by 100%.", rows)
    assert not check.ok and check.unsupported == ["100%"]
    assert check_numbers("Your worst drawdown of 10,765.07 overlapped news by 2%.", rows).ok
    assert not check_numbers("48% of trades", rows).ok  # 48 is a count, not a share
