from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest
from fake_mt5 import FakeMt5, epoch, raw_account, raw_deal, raw_symbol

from tradequery.trades import balance_from_deals
from tradequery.settings import load_settings
from tradequery.trades import derive_trades
from tradequery.settings import Mt5Credentials
from tradequery.trades import build_trades, extract_cash_flows
from tradequery import mt5 as mt5_client
from tradequery.mt5 import Mt5Error

UTC = timezone.utc
CLOCK = load_settings().clock


def test_connect_passes_the_credentials_and_hides_the_password():
    fake = FakeMt5()
    creds = Mt5Credentials(login=12345, password="secret", server="Srv-Live", path="C:/mt5/terminal64.exe")
    assert mt5_client.connect(creds, mt5=fake) is fake
    assert fake.init_kwargs == dict(
        login=12345, password="secret", server="Srv-Live", timeout=60000, path="C:/mt5/terminal64.exe"
    )
    assert "secret" not in repr(creds)


def test_connect_failure_raises_a_clear_error():
    creds = Mt5Credentials(login=1, password="x", server="s")
    with pytest.raises(Mt5Error, match="login failed"):
        mt5_client.connect(creds, mt5=FakeMt5(login_ok=False))


def test_fetch_account_translates_modes():
    account = mt5_client.fetch_account(FakeMt5(account=raw_account()))
    assert account["login"] == 12345
    assert account["margin_mode"] == "retail_hedging"
    assert account["trade_mode"] == "real"
    assert account["trade_allowed"] is False
    assert account["balance"] == 1000.5


def test_fetch_account_without_account_is_an_error():
    with pytest.raises(Mt5Error):
        mt5_client.fetch_account(FakeMt5(account=None))


ENTRY = raw_deal(ticket=1, order=900, time=epoch(2026, 7, 15, 12), type=0, entry=0, magic=12345,
                 position_id=1, reason=3, volume=0.5, price=2650.0, commission=-1.75, fee=-0.25,
                 symbol="XAUUSD", comment="ea")
EXIT = raw_deal(ticket=2, order=901, time=epoch(2026, 7, 15, 14, 30), type=1, entry=1, magic=12345,
                position_id=1, reason=5, volume=0.5, price=2660.0, commission=-1.75, swap=-0.5,
                profit=500.0, symbol="XAUUSD")
ORDERS = [NS(ticket=900, sl=2640.0, tp=2670.0), NS(ticket=901, sl=0.0, tp=0.0)]


def test_entry_and_exit_deals_are_converted():
    deals = mt5_client.fetch_deals(FakeMt5(deals=[EXIT, ENTRY], orders=ORDERS), CLOCK)
    entry, exit_ = deals
    assert entry.time_utc == datetime(2026, 7, 15, 9, 0, tzinfo=UTC)  # 12:00 server = 09:00 UTC in summer
    assert (entry.type, entry.entry, entry.reason) == ("buy", "in", "expert")
    assert (entry.sl, entry.tp) == (2640.0, 2670.0)  # from the opening order
    assert entry.commission == -2.0  # commission -1.75 plus fee -0.25
    assert entry.magic == 12345 and entry.comment == "ea" and entry.position_id == 1
    assert exit_.time_utc == datetime(2026, 7, 15, 11, 30, tzinfo=UTC)
    assert (exit_.type, exit_.entry, exit_.reason) == ("sell", "out", "tp")
    assert (exit_.sl, exit_.tp) == (0.0, 0.0)
    assert exit_.profit == 500.0 and exit_.swap == -0.5


def test_balance_operations_keep_their_amount_in_profit():
    deals = mt5_client.fetch_deals(
        FakeMt5(deals=[
            raw_deal(ticket=10, time=epoch(2026, 7, 1, 8), type=2, entry=0, profit=1000.0, comment="Deposit"),
            raw_deal(ticket=11, time=epoch(2026, 7, 2, 8), type=3, entry=0, profit=50.0),
            raw_deal(ticket=12, time=epoch(2026, 7, 3, 8), type=7, entry=0, commission=-3.0),
        ]),
        CLOCK,
    )
    assert [(d.type, d.entry, d.position_id, d.profit) for d in deals] == [
        ("balance", "", 0, 1000.0),
        ("credit", "", 0, 50.0),
        ("commission", "", 0, -3.0),
    ]
    flows = extract_cash_flows(deals)
    assert [(f.type, f.amount) for f in flows] == [("deposit", 1000.0), ("credit", 50.0), ("other", -3.0)]


def test_history_error_is_reported():
    with pytest.raises(Mt5Error, match="deal history"):
        mt5_client.fetch_deals(FakeMt5(deals=None), CLOCK)


def test_symbol_specs_select_hidden_symbols_and_flag_unknown_ones():
    fake = FakeMt5(symbols=[raw_symbol("XAUUSD")], hidden=[raw_symbol("EURUSD")])
    specs = mt5_client.fetch_symbol_specs(fake, ["XAUUSD", "EURUSD", "FOO"])
    assert [s["name"] for s in specs] == ["EURUSD", "FOO", "XAUUSD"]
    assert "error" in specs[1] and "error" not in specs[0] and "error" not in specs[2]
    assert specs[2]["trade_contract_size"] == 1


def test_list_symbol_names_is_sorted():
    fake = FakeMt5(symbols=[raw_symbol("XAUUSD"), raw_symbol("EURUSD")])
    assert mt5_client.list_symbol_names(fake) == ["EURUSD", "XAUUSD"]


def test_end_to_end_from_fake_terminal_to_checked_balance():
    open_entry = raw_deal(ticket=3, order=902, time=epoch(2026, 7, 16, 9), type=1, entry=0, position_id=3,
                          volume=0.1, price=1.1, commission=-1.0, symbol="EURUSD")  # still open
    deposit = raw_deal(ticket=10, time=epoch(2026, 7, 1, 8), type=2, entry=0, profit=1000.0)
    deals = mt5_client.fetch_deals(FakeMt5(deals=[deposit, ENTRY, EXIT, open_entry], orders=ORDERS), CLOCK)

    trades = derive_trades(build_trades(deals), extract_cash_flows(deals), load_settings())
    assert len(trades) == 1  # the open position is not a closed trade yet
    assert trades[0].net_profit == 495.75  # 500 - 2.0 - 1.75 - 0.5
    assert trades[0].balance_after == 1495.75
    # MT5's balance already includes the open position's entry commission.
    assert balance_from_deals(deals) == 1494.75
