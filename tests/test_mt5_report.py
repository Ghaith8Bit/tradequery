"""MT5 HTML report importer, on a small made-up report in the terminal's exact layout."""

from datetime import datetime, timezone

import pytest

from tradequery import cli, db
from tradequery.importers.mt5_report import (
    ReportError,
    parse_number,
    parse_report,
    reason_from_comment,
)
from tradequery.models import Deal
from tradequery.settings import load_settings
from tradequery.timeutil import ServerClock
from tradequery.trades import build_trades, extract_cash_flows

UTC = timezone.utc
CLOCK = ServerClock("fixed", fixed_offset_hours=3)

HEADER = """<tr align="center"><td colspan="14"><div><b>Trade History Report</b></div></td></tr>
<tr align="left"><th colspan="4">Name:</th><th colspan="10"><b>Someone Private</b></th></tr>
<tr align="left"><th colspan="4">Account:</th><th colspan="10"><b>11112222&nbsp;(USC,&nbsp;Demo-Live 1,&nbsp;real,&nbsp;Hedge)</b></th></tr>
<tr align="left"><th colspan="4">Company:</th><th colspan="10"><b>Demo Broker Ltd</b></th></tr>
<tr align="left"><th colspan="4">Date:</th><th colspan="10"><b>2026.11.08 12:54</b></th></tr>"""


def title(name):
    return f'<tr align="left"><th colspan="14"><div><b>{name}</b></div></th></tr>'


def row(*cells):
    return "<tr>" + "".join(cells) + "</tr>"


def td(text="", hidden=False, span=1):
    attrs = (' class="hidden"' if hidden else "") + (f' colspan="{span}"' if span > 1 else "")
    return f"<td{attrs}>{text}</td>"


POSITIONS = [
    title("Positions"),
    row(*map(td, ["Time", "Position", "Symbol", "Type", "Volume", "Price", "S / L", "T / P", "Time",
                  "Price", "Commission", "Swap"]), td("Profit", span=2)),
    row(td("2026.09.01 02:00:00"), td("902"), td("XAUUSD-STDc"), td("buy"), td("TMX", hidden=True, span=8),
        td("0.02"), td("2001.00"), td("1995.00"), td("2010.00"), td("2026.09.01 03:00:00"), td("2003.50"),
        td("0.00"), td("0.00"), td("5.00", span=2)),
    row(td("2026.09.03 04:00:00"), td("904"), td("XAUUSD-STDc"), td("buy"), td("", hidden=True, span=8),
        td("0.01"), td("2010.00"), td(), td(), td("2026.09.03 05:00:00"), td("2005.00"),
        td("-0.10"), td("-0.20"), td("-5.00", span=2)),
]

ORDERS = [
    title("Orders"),
    row(*map(td, ["Open Time", "Order", "Symbol", "Type", "Volume", "Price", "S / L", "T / P", "Time"]),
        td("State", span=2), td("Comment", span=3)),
    *[
        row(td(t), td(o), td("XAUUSD-STDc"), td(side), td("0.01 / 0.01"), td("market"), td(sl), td(tp), td(t),
            td("filled", span=2), td("", span=3))
        for t, o, side, sl, tp in [
            ("2026.09.01 01:00:00", "901", "sell", "", ""),
            ("2026.09.01 02:00:00", "902", "buy", "1995.00", "2010.00"),
            ("2026.09.01 03:00:00", "903", "sell", "", ""),
            ("2026.09.03 04:00:00", "904", "buy", "", ""),
            ("2026.09.03 05:00:00", "905", "sell", "", ""),
        ]
    ],
]

DEAL_COLUMNS = ["Time", "Deal", "Symbol", "Type", "Direction", "Volume", "Price", "Order", "Cost",
                "Commission", "Fee", "Swap", "Profit", "Balance", "Comment"]


def deal(time, ticket, symbol, kind, direction, volume, price, order, comm, swap, profit, balance, comment=""):
    return row(td(time), td(ticket), td(symbol), td(kind), td(direction), td(volume), td(price), td(order),
               td("", hidden=True), td(comm), td("0.00"), td(swap), td(profit), td(balance), td(comment))


DEALS = [
    title("Deals"),
    row(*[td(c, hidden=c == "Cost") for c in DEAL_COLUMNS]),
    # an exit of a position opened before the report starts
    deal("2026.09.01 01:00:00", "501", "XAUUSD-STDc", "sell", "out", "0.01", "2000.00", "901", "0.00", "0.00", "-1.00", "999.00"),
    deal("2026.09.01 02:00:00", "502", "XAUUSD-STDc", "buy", "in", "0.02", "2001.00", "902", "0.00", "0.00", "0.00", "999.00", "TMX"),
    deal("2026.09.01 03:00:00", "503", "XAUUSD-STDc", "sell", "out", "0.02", "2003.50", "903", "0.00", "0.00", "5.00", "1 004.00"),
    deal("2026.09.02 10:00:00", "504", "", "balance", "", "", "", "", "0.00", "0.00", "-200.00", "804.00", "Transfer Out to 999"),
    deal("2026.09.03 04:00:00", "505", "XAUUSD-STDc", "buy", "in", "0.01", "2010.00", "904", "-0.10", "0.00", "0.00", "803.90"),
    deal("2026.09.03 05:00:00", "506", "XAUUSD-STDc", "sell", "out", "0.01", "2005.00", "905", "0.00", "-0.20", "-5.00", "798.70",
         "[so 18.98%/411.09/2166.15]"),
    deal("2026.10.01 12:00:00", "507", "", "balance", "", "", "", "", "0.00", "0.00", "1 000.00", "1 798.70", "Deposit"),
    row(td(span=8), td("-0.10"), td("0.00"), td("-0.20"), td("-1.00"), td("1 798.70"), td()),  # totals line
]

RESULTS = [
    title("Results"),
    row(td("Total Net Profit:", span=3), td("-1.30"), td("Gross Profit:", span=3), td("5.00")),
]


def report_html(deals=DEALS):
    body = "\n".join([HEADER, row(td(span=14)), *POSITIONS, *ORDERS, *deals, *RESULTS])
    return f"<html><head><title>11112222</title></head><body><table>{body}</table></body></html>"


@pytest.fixture
def report_file(tmp_path):
    path = tmp_path / "ReportHistory-11112222.html"
    path.write_text(report_html(), encoding="utf-16")  # the terminal writes UTF-16 with a BOM
    return path


def test_number_and_reason_helpers():
    assert parse_number("10 062.67") == 10062.67
    assert parse_number("-1 000.00") == -1000.0
    assert parse_number("") == 0.0
    with pytest.raises(ReportError):
        parse_number("abc")
    assert reason_from_comment("[so 18.98%/411.09/2166.15]") == "so"
    assert reason_from_comment("[sl 4450.00]") == "sl"
    assert reason_from_comment("[tp 4460.00]") == "tp"
    assert reason_from_comment("TMX") == ""


def test_account_line_and_balances_are_read(report_file):
    report = parse_report(report_file, CLOCK)
    assert (report.login, report.currency, report.server) == (11112222, "USC", "Demo-Live 1")
    assert (report.trade_mode, report.margin_mode) == ("real", "retail_hedging")
    assert report.company == "Demo Broker Ltd"
    assert (report.opening_balance, report.closing_balance) == (1000.0, 1798.70)
    assert "Someone Private" not in str(report.account())
    assert report.summary["Total Net Profit"] == "-1.30"


def test_deals_are_normalized_to_utc_with_positions_and_stops(report_file):
    deals = {d.ticket: d for d in parse_report(report_file, CLOCK).deals}
    assert len(deals) == 7  # the totals line is not a deal
    entry = deals[502]
    assert entry == Deal(
        ticket=502, position_id=902, time_utc=datetime(2026, 8, 31, 23, 0, tzinfo=UTC), type="buy",
        entry="in", symbol="XAUUSD-STDc", volume=0.02, price=2001.0, commission=0.0, swap=0.0,
        profit=0.0, comment="TMX", sl=1995.0, tp=2010.0,
    )
    assert deals[503].position_id == 902  # exit matched through the Positions section
    assert deals[506].position_id == 904 and deals[506].reason == "so"
    assert deals[505].commission == -0.10
    assert deals[501].position_id == 0  # its position opened before the report


def test_trades_and_cash_flows_match_the_report(report_file):
    report = parse_report(report_file, CLOCK)
    assert report.unmatched_exits == 1
    trades = build_trades(report.deals)
    assert [(t.position_id, t.profit, t.close_reason) for t in trades] == [(902, 5.0, ""), (904, -5.0, "so")]
    flows = extract_cash_flows(report.deals)
    assert [(f.type, f.amount) for f in flows] == [("withdrawal", -200.0), ("deposit", 1000.0)]


def test_a_misread_number_is_caught_by_the_running_balance(tmp_path):
    broken = [d.replace("1 004.00", "1 005.00") for d in DEALS]
    path = tmp_path / "r.html"
    path.write_text(report_html(broken), encoding="utf-16")
    with pytest.raises(ReportError, match="running balance mismatch at deal 503"):
        parse_report(path, CLOCK)


def test_a_file_without_the_deals_section_is_refused(tmp_path):
    path = tmp_path / "r.html"
    path.write_text(report_html(deals=[]), encoding="utf-8")
    with pytest.raises(ReportError, match="Deals"):
        parse_report(path, CLOCK)


def test_import_command_fills_the_database(report_file, tmp_path, capsys):
    assert cli.main(["--data-dir", str(tmp_path), "import", str(report_file)]) == 0
    out = capsys.readouterr().out
    assert "7 deals in the report (7 new), 2 closed trades, 2 cash flows" in out
    assert "1 exit deals close positions opened before the report" in out
    assert "difference 0.00" in out
    conn = db.connect(db.db_path(11112222, tmp_path), read_only=True)
    account = db.load_account(conn)
    assert account["opening_balance"] == 1000.0 and account["balance"] == 1798.70
    assert account["trade_allowed"] is None  # a report cannot tell
    trades = db.load_trades(conn)
    assert [t.balance_after for t in trades] == [1004.0, 798.7]  # starts from the opening balance
    conn.close()
    capsys.readouterr()
    assert cli.main(["--data-dir", str(tmp_path), "show"]) == 0
    assert "Balance after the last closed trade: 798.70" in capsys.readouterr().out


def test_import_never_overwrites_synced_deals(report_file, tmp_path, capsys):
    synced = Deal(ticket=502, position_id=902, time_utc=datetime(2026, 8, 31, 23, 0, tzinfo=UTC), type="buy",
                  entry="in", symbol="XAUUSD-STDc", volume=0.02, price=2001.0, commission=0.0, swap=0.0,
                  profit=0.0, magic=12345, reason="expert", comment="TMX", sl=1995.0, tp=2010.0)
    conn = db.connect(db.db_path(11112222, tmp_path))
    with conn:
        db.upsert_deals(conn, [synced])
        db.save_account(conn, {"login": 11112222, "balance": 1798.70, "trade_allowed": False},
                        earliest_history=None, synced_at=datetime.now(UTC))
    conn.close()
    cli.main(["--data-dir", str(tmp_path), "import", str(report_file)])
    assert "(6 new)" in capsys.readouterr().out
    conn = db.connect(db.db_path(11112222, tmp_path), read_only=True)
    stored = {d.ticket: d for d in db.load_deals(conn)}
    assert stored[502].magic == 12345 and stored[502].reason == "expert"
    assert db.load_account(conn)["trade_allowed"] == 0
    conn.close()


def test_a_bad_report_is_a_clean_error(tmp_path, capsys):
    path = tmp_path / "r.html"
    path.write_text("<html><table><tr><td>hello</td></tr></table></html>", encoding="utf-8")
    assert cli.main(["--data-dir", str(tmp_path), "import", str(path)]) == 2
    assert "Orders" in capsys.readouterr().err


def test_settings_clock_is_used_for_report_times(report_file):
    report = parse_report(report_file, load_settings().clock)  # ny_close: UTC+3 in September
    assert report.deals[0].time_utc == datetime(2026, 8, 31, 22, 0, tzinfo=UTC)
