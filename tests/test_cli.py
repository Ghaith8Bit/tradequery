from datetime import datetime, timezone

import pytest

from tradequery.trades import balance_from_deals
from tradequery.synthetic import generate_account
from tradequery import db
from tradequery.settings import load_settings
from tradequery import cli


def write_account(root, login, balance):
    deals = generate_account().deals
    conn = db.connect(db.db_path(login, root))
    with conn:
        db.upsert_deals(conn, deals)
        db.save_account(conn, {"login": login, "balance": balance}, earliest_history=None,
                        synced_at=datetime.now(timezone.utc))
        db.rebuild_derived(conn, load_settings())
    conn.close()
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


def test_download_saves_the_account_without_the_name_or_credentials(tmp_path, monkeypatch, capsys):
    deals = fake_terminal(monkeypatch, ACCOUNT)
    assert cli.main(["--data-dir", str(tmp_path), "download"]) == 0
    conn = db.connect(db.db_path(1234, tmp_path), read_only=True)
    assert db.load_deals(conn) == deals
    saved = db.load_account(conn)
    assert "name" not in saved and "Test Name" not in str(saved) and "password" not in str(saved).lower()
    assert saved["account_id"] == 1234 and saved["broker"] == "Broker"
    assert saved["earliest_history_utc"] == db.format_time(deals[0].time_utc)
    assert db.load_symbols(conn)[0]["contract_size"] == 100.0
    assert len(db.load_trades(conn)) == 48
    conn.close()
    out = capsys.readouterr().out
    assert f"{len(deals)} deals ({len(deals)} new)" in out
    assert "OK: the deals add up" in out


def test_a_second_download_adds_nothing_twice(tmp_path, monkeypatch, capsys):
    deals = fake_terminal(monkeypatch, ACCOUNT)
    cli.main(["--data-dir", str(tmp_path), "download"])
    assert cli.main(["--data-dir", str(tmp_path), "download"]) == 0
    assert f"{len(deals)} deals (0 new)" in capsys.readouterr().out
    assert cli.main(["--data-dir", str(tmp_path), "show"]) == 0


def test_cli_runs_as_a_module():
    # The Wine container calls `python -m tradequery.cli`; without a __main__ guard it prints nothing.
    import subprocess
    import sys

    result = subprocess.run([sys.executable, "-m", "tradequery.cli", "--help"], capture_output=True, text=True)
    assert result.returncode == 0 and "inspect" in result.stdout


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
