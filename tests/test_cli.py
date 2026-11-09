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
    monkeypatch.setattr(cli, "_read_from_terminal", lambda **kw: (account, deals, specs, ["EURUSD", "XAUUSD"]))
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


def test_download_infers_the_opening_balance_of_a_partial_history(tmp_path, monkeypatch, capsys):
    deals = generate_account().deals
    later = deals[3:]  # the broker kept only part of the history
    account = dict(ACCOUNT, balance=balance_from_deals(deals))
    monkeypatch.setattr(cli, "_read_from_terminal", lambda **kw: (account, later, [], []))
    assert cli.main(["--data-dir", str(tmp_path), "download"]) == 0
    out = capsys.readouterr().out
    opening = round(balance_from_deals(deals) - balance_from_deals(later), 2)
    assert f"already holding {opening:.2f}" in out and "difference 0.00" in out
    conn = db.connect(db.db_path(1234, tmp_path), read_only=True)
    assert db.load_account(conn)["opening_balance"] == opening
    conn.close()


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


def test_second_download_reads_only_from_a_day_before_the_newest_deal(tmp_path, monkeypatch, capsys):
    deals = generate_account().deals
    first, later = deals[:60], deals[50:]  # the second read overlaps the first by 10 deals
    account = dict(ACCOUNT, balance=balance_from_deals(deals))
    asked = []

    def terminal(batch):
        def read(since_for=None, then=None):
            asked.append(since_for(1234))
            return account, batch, [], []
        return read

    monkeypatch.setattr(cli, "_read_from_terminal", terminal(first))
    cli.main(["--data-dir", str(tmp_path), "download"])
    monkeypatch.setattr(cli, "_read_from_terminal", terminal(later))
    assert cli.main(["--data-dir", str(tmp_path), "download"]) == 0
    out = capsys.readouterr().out

    assert asked[0] is None  # nothing stored yet: read everything
    assert asked[1] == first[-1].time_utc - cli.SYNC_OVERLAP
    assert f"Incremental sync: read {len(later)} deals since" in out
    assert f"{len(deals)} deals ({len(deals) - 60} new)" in out  # the overlap is not duplicated
    assert out.rstrip().count("difference 0.00") == 2


def test_full_flag_rereads_everything(tmp_path, monkeypatch):
    deals = generate_account().deals
    account = dict(ACCOUNT, balance=balance_from_deals(deals))
    asked = []

    def read(since_for=None, then=None):
        asked.append(since_for(1234))
        return account, deals, [], []

    monkeypatch.setattr(cli, "_read_from_terminal", read)
    cli.main(["--data-dir", str(tmp_path), "download"])
    cli.main(["--data-dir", str(tmp_path), "download", "--full"])
    assert asked == [None, None]


def test_latest_deal_time_is_the_newest_stored_deal(tmp_path):
    deals = generate_account().deals
    conn = db.connect(db.db_path(9, tmp_path))
    assert db.latest_deal_time(conn) is None
    with conn:
        db.upsert_deals(conn, deals)
    assert db.latest_deal_time(conn) == max(d.time_utc for d in deals)
    conn.close()


def test_metrics_command_on_a_stored_account(tmp_path, capsys):
    write_account(tmp_path, 1234, balance=balance_from_deals(generate_account().deals))
    assert cli.main(["--data-dir", str(tmp_path), "metrics"]) == 0
    out = capsys.readouterr().out
    assert "Closed trades" in out and "48" in out
    assert "Win rate" in out and "Profit factor" in out and "Max drawdown" in out
    assert "Deposits / withdrawals" in out and "12,000.00 / -500.00" in out
    capsys.readouterr()
    assert cli.main(["--data-dir", str(tmp_path), "metrics", "--symbol", "NOPE"]) == 0
    assert "No closed trades match." in capsys.readouterr().out


def test_tags_command(tmp_path, capsys):
    write_account(tmp_path, 1234, balance=balance_from_deals(generate_account().deals))
    assert cli.main(["--data-dir", str(tmp_path), "tags", "--examples", "1"]) == 0
    out = capsys.readouterr().out
    assert "no_stop_loss:" in out and "revenge_trade:" in out and "oversized:" in out
    assert cli.main(["--data-dir", str(tmp_path), "tags", "--rule", "no_stop_loss"]) == 0
    assert "overtrading" not in capsys.readouterr().out


def test_sequences_command(tmp_path, capsys):
    write_account(tmp_path, 1234, balance=balance_from_deals(generate_account().deals))
    assert cli.main(["--data-dir", str(tmp_path), "sequences"]) == 0
    out = capsys.readouterr().out
    assert "martingale:" in out and "By depth" in out and "depth  5:" in out
    assert cli.main(["--data-dir", str(tmp_path), "sequences", "--type", "grid", "--min-depth", "50"]) == 0
    assert "None." in capsys.readouterr().out


def test_query_command(tmp_path, capsys):
    write_account(tmp_path, 1234, balance=balance_from_deals(generate_account().deals))
    assert cli.main(["--data-dir", str(tmp_path), "query", "--examples", "all"]) == 0
    out = capsys.readouterr().out
    assert out.count("Q: ") == 14 and "Interpretation:" in out
    assert cli.main(["--data-dir", str(tmp_path), "query", "{\"entity\": \"streaks\", \"limit\": 2}", "--sql"]) == 0
    assert "SQL: SELECT" in capsys.readouterr().out
    assert cli.main(["--data-dir", str(tmp_path), "query", "{\"entity\": \"trades\", \"filters\": [{\"field\": \"comment\", \"op\": \"eq\", \"value\": \"x\"}]}"]) == 2
    assert "Spec rejected" in capsys.readouterr().out


def test_ask_command_needs_a_key_and_explains_how(tmp_path, monkeypatch, capsys):
    write_account(tmp_path, 1234, balance=balance_from_deals(generate_account().deals))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert cli.main(["--data-dir", str(tmp_path), "ask", "longest losing streak?"]) == 2
    assert "GEMINI_API_KEY" in capsys.readouterr().err


def test_ask_command_with_a_fake_model(tmp_path, monkeypatch, capsys):
    import json
    from tradequery import llm
    from tradequery.examples import LONGEST_LOSING_STREAK
    write_account(tmp_path, 1234, balance=balance_from_deals(generate_account().deals))
    fake = llm.FakeProvider([json.dumps(LONGEST_LOSING_STREAK), "Your longest losing streak was 6 trades."])
    monkeypatch.setattr(llm, "provider_from_env", lambda: fake)
    assert cli.main(["--data-dir", str(tmp_path), "ask", "--spec", "longest", "losing", "streak?"]) == 0
    out = capsys.readouterr().out
    assert "Your longest losing streak was 6 trades." in out and "Spec: {" in out and "2 requests" in out


def test_sample_command_stores_a_queryable_account(tmp_path, capsys):
    assert cli.main(["--data-dir", str(tmp_path), "sample"]) == 0
    assert "900001" in capsys.readouterr().out
    assert cli.main(["--data-dir", str(tmp_path), "query", "--examples", "11"]) == 0
    assert "loss" in capsys.readouterr().out  # the planted 6-trade losing streak
