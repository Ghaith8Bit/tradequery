import pytest

from tradequery.trades import balance_from_deals
from tradequery.synthetic import generate_account
from tradequery.store import account_dir, load_deals, load_json, save_deals, save_json
from tradequery import cli


def write_account(root, login, balance):
    deals = generate_account().deals
    folder = account_dir(login, root)
    save_deals(folder / "deals.csv", deals)
    save_json(folder / "account.json", {"login": login, "balance": balance})
    return deals


def test_show_reports_ok_when_deals_add_up_to_the_balance(tmp_path, capsys):
    deals = write_account(tmp_path, 1234, balance=balance_from_deals(generate_account().deals))
    assert cli.main(["--data-dir", str(tmp_path), "show", "--rows", "3"]) == 0
    out = capsys.readouterr().out
    assert "48 closed trades" in out
    assert "OK: the deals add up" in out
    assert "deposit: 2 x, total 12000.00" in out
    assert "withdrawal: 1 x, total -500.00" in out
    assert len(deals) == 99


def test_show_flags_a_balance_mismatch(tmp_path, capsys):
    write_account(tmp_path, 1234, balance=balance_from_deals(generate_account().deals) + 5)
    assert cli.main(["--data-dir", str(tmp_path), "show"]) == 1
    assert "Mismatch" in capsys.readouterr().out


def test_show_needs_a_downloaded_account(tmp_path):
    with pytest.raises(SystemExit, match="download"):
        cli.main(["--data-dir", str(tmp_path), "show"])


def test_show_asks_for_a_login_when_several_accounts_exist(tmp_path):
    write_account(tmp_path, 1, 0.0)
    write_account(tmp_path, 2, 0.0)
    with pytest.raises(SystemExit, match="--login"):
        cli.main(["--data-dir", str(tmp_path), "show"])


ACCOUNT = {
    "login": 1234, "name": "Test Name", "server": "Srv", "company": "Broker", "currency": "USD",
    "leverage": 500, "balance": 0.0, "equity": 0.0, "margin_mode": "retail_hedging",
    "trade_mode": "real", "trade_allowed": False,
}


def fake_terminal(monkeypatch, account):
    deals = generate_account().deals
    account = dict(account, balance=balance_from_deals(deals))
    specs = [{"name": "XAUUSD", "trade_contract_size": 100.0, "trade_tick_size": 0.01,
              "trade_tick_value": 1.0, "swap_long": -7.0, "swap_short": 2.0, "digits": 2}]
    monkeypatch.setattr(cli, "_read_from_terminal", lambda: (account, deals, specs, ["EURUSD", "XAUUSD"]))
    return deals


def test_download_saves_files_without_the_name_or_credentials(tmp_path, monkeypatch, capsys):
    deals = fake_terminal(monkeypatch, ACCOUNT)
    assert cli.main(["--data-dir", str(tmp_path), "download"]) == 0
    folder = account_dir(1234, tmp_path)
    assert load_deals(folder / "deals.csv") == deals
    saved = load_json(folder / "account.json")
    assert "name" not in saved and "password" not in str(saved).lower()
    assert saved["login"] == 1234 and saved["first_deal_utc"]
    assert load_json(folder / "symbols.json")[0]["name"] == "XAUUSD"
    assert "OK: the deals add up" in capsys.readouterr().out


def test_inspect_warns_when_the_login_can_trade(monkeypatch, capsys):
    fake_terminal(monkeypatch, dict(ACCOUNT, trade_allowed=True))
    cli.main(["inspect"])
    assert "MASTER password" in capsys.readouterr().out


def test_inspect_confirms_a_read_only_login_and_lists_symbols(monkeypatch, capsys):
    fake_terminal(monkeypatch, ACCOUNT)
    cli.main(["inspect", "--all-symbols"])
    out = capsys.readouterr().out
    assert "read-only (investor)" in out
    assert "XAUUSD" in out and "EURUSD, XAUUSD" in out
    assert "Symbols traded:" in out and "Magic numbers:" in out
