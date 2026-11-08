"""Command line.

  tradequery demo        print a synthetic account (no MT5, no credentials)
  tradequery inspect     log in to MT5; show account, symbols, history summary
  tradequery download    save account, deals and symbol specs to data/accounts/<login>/
  tradequery show        analyse a downloaded account (no MT5 needed)

inspect and download need Windows Python + the MT5 terminal: run them with
``make inspect`` / ``make sync``, which use the Wine container.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import mt5 as mt5_client
from .mt5 import Mt5Error
from .settings import CredentialsError, data_dir, load_credentials, load_settings
from .store import account_dir, load_deals, load_json, save_deals, save_json
from .synthetic import generate_account
from .trades import balance_curve, balance_from_deals, build_trades, derive_trades, extract_cash_flows


def _fmt_time(value: datetime) -> str:
    return f"{value:%Y-%m-%d %H:%M}"


def _print_account(account: dict) -> None:
    print(f"Account {account['login']} ({account['name']}) on {account['server']}")
    print(
        f"Broker: {account['company']} | Currency: {account['currency']} | Leverage: 1:{account['leverage']} "
        f"| Type: {account['trade_mode']} | Margin mode: {account['margin_mode']}"
    )
    print(f"Balance: {account['balance']:.2f}   Equity: {account['equity']:.2f}")
    if account["trade_allowed"]:
        print(
            "WARNING: this login is allowed to trade, so it may be the MASTER password. "
            "Use the investor (read-only) password in .env instead."
        )
    else:
        print("Login type: read-only (investor). Good.")


def _print_history_summary(deals) -> None:
    if not deals:
        print("\nNo deals found in the history.")
        return
    print(f"\nHistory: {len(deals)} deals from {_fmt_time(deals[0].time_utc)} to {_fmt_time(deals[-1].time_utc)} UTC")
    print("Deal types: " + ", ".join(f"{k} {v}" for k, v in Counter(d.type for d in deals).most_common()))
    symbols = Counter(d.symbol for d in deals if d.symbol)
    print("Symbols traded: " + ", ".join(f"{s} ({n} deals)" for s, n in symbols.most_common()))
    magics = Counter(d.magic for d in deals if d.type in ("buy", "sell"))
    print("Magic numbers: " + ", ".join(f"{m} ({n} deals)" for m, n in magics.most_common()))


def _print_specs(specs: list[dict]) -> None:
    print("\nSymbol specs:")
    print(f"  {'symbol':<12} {'contract':>9} {'tick size':>10} {'tick value':>10} {'swap long':>10} {'swap short':>10} {'digits':>6}")
    for s in specs:
        if "error" in s:
            print(f"  {s['name']:<12} {s['error']}")
            continue
        print(
            f"  {s['name']:<12} {s['trade_contract_size']:>9} {s['trade_tick_size']:>10} "
            f"{s['trade_tick_value']:>10} {s['swap_long']:>10} {s['swap_short']:>10} {s['digits']:>6}"
        )


def _read_from_terminal():
    """Log in, read everything, log out. Returns (account, deals, specs, all_symbol_names)."""
    settings = load_settings()
    mt5 = mt5_client.connect(load_credentials())
    try:
        account = mt5_client.fetch_account(mt5)
        deals = mt5_client.fetch_deals(mt5, settings.clock)
        specs = mt5_client.fetch_symbol_specs(mt5, {d.symbol for d in deals if d.symbol})
        names = mt5_client.list_symbol_names(mt5)
    finally:
        mt5_client.disconnect(mt5)
    return account, deals, specs, names


def _print_balance_check(deals, mt5_balance: float) -> bool:
    computed = balance_from_deals(deals)
    diff = round(computed - mt5_balance, 2)
    print(f"\nBalance check: computed from deals {computed:.2f} | MT5 balance at download {mt5_balance:.2f} | difference {diff:.2f}")
    if abs(diff) < 0.01:
        print("OK: the deals add up to the balance MT5 reports.")
        return True
    print(
        "Mismatch. Likely causes: the broker keeps only part of the history (older deals are missing), "
        "or the balance changed between the download and this check."
    )
    return False


def cmd_inspect(args) -> int:
    account, deals, specs, names = _read_from_terminal()
    _print_account(account)
    _print_history_summary(deals)
    _print_specs(specs)
    print(f"\nThe broker server lists {len(names)} symbols.")
    if args.all_symbols:
        print(", ".join(names))
    else:
        print("(add --all-symbols to list them)")
    _print_balance_check(deals, account["balance"])
    return 0


def cmd_download(args) -> int:
    account, deals, specs, _ = _read_from_terminal()
    folder = account_dir(account["login"], args.data_dir)
    saved = {k: v for k, v in account.items() if k != "name"}  # keep the name out of the files
    saved["downloaded_at_utc"] = datetime.now(timezone.utc).isoformat()
    saved["first_deal_utc"] = deals[0].time_utc.isoformat() if deals else None
    save_json(folder / "account.json", saved)
    save_json(folder / "symbols.json", specs)
    save_deals(folder / "deals.csv", deals)
    _print_account(account)
    _print_history_summary(deals)
    print(f"\nSaved {len(deals)} deals and {len(specs)} symbol specs to {folder}")
    _print_balance_check(deals, account["balance"])
    print("\nNext: tradequery show")
    return 0


def _pick_login(args) -> int:
    if args.login:
        return args.login
    root = Path(args.data_dir or data_dir()) / "accounts"
    logins = sorted(p.name for p in root.iterdir() if (p / "deals.csv").is_file()) if root.is_dir() else []
    if len(logins) == 1:
        return int(logins[0])
    if not logins:
        raise SystemExit("No downloaded account found. Run: tradequery download")
    raise SystemExit(f"Several accounts downloaded ({', '.join(logins)}). Pick one with --login.")


def cmd_show(args) -> int:
    login = _pick_login(args)
    folder = account_dir(login, args.data_dir)
    deals = load_deals(folder / "deals.csv")
    account = load_json(folder / "account.json")
    settings = load_settings()
    flows = extract_cash_flows(deals)
    trades = derive_trades(build_trades(deals), flows, settings)

    print(f"Account {login}: {len(deals)} deals -> {len(trades)} closed trades, {len(flows)} cash flows")
    for kind in ("deposit", "withdrawal", "credit", "bonus", "correction", "charge", "other"):
        total = sum(f.amount for f in flows if f.type == kind)
        count = sum(1 for f in flows if f.type == kind)
        if count:
            print(f"  {kind}: {count} x, total {total:.2f}")
    wins = sum(1 for t in trades if t.outcome == "win")
    losses = sum(1 for t in trades if t.outcome == "loss")
    print(f"Wins {wins}, losses {losses}, break-even {len(trades) - wins - losses}")
    if trades:
        print(f"Net profit of closed trades: {sum(t.net_profit for t in trades):.2f}")
        print(f"Balance after the last trade: {balance_curve(trades, flows)[-1].balance:.2f}")

    shown = trades[-args.rows :]
    print(f"\nLast {len(shown)} trades:")
    print(f"{'closed (UTC)':<17} {'symbol':<8} {'side':<4} {'lots':>5} {'magic':>7} {'net':>9} {'hold':>6} {'day':<3} {'hr':>2} {'session':<16} {'result':<9} {'balance':>10}")
    for t in shown:
        print(
            f"{_fmt_time(t.close_time_utc)} {t.symbol:<8} {t.side:<4} {t.lots:>5} {t.magic:>7} "
            f"{t.net_profit:>9.2f} {t.hold_minutes:>6.0f} {t.weekday:<3} {t.hour:>2} {t.session:<16} "
            f"{t.outcome:<9} {t.balance_after:>10.2f}"
        )
    ok = _print_balance_check(deals, account["balance"])
    return 0 if ok else 1


def cmd_demo(args) -> int:
    settings = load_settings()
    account = generate_account()
    flows = extract_cash_flows(account.deals)
    trades = derive_trades(build_trades(account.deals), flows, settings)
    print(f"{len(account.deals)} deals -> {len(trades)} closed trades, {len(flows)} cash flows\n")
    for flow in flows:
        print(f"  {flow.time_utc:%Y-%m-%d %H:%M}  {flow.type:<10} {flow.amount:>10.2f}  {flow.comment}")
    print(f"\nFirst {args.rows} trades:")
    for t in trades[: args.rows]:
        print(
            f"{_fmt_time(t.close_time_utc)} {t.symbol:<7} {t.side:<4} {t.lots:>4} {t.magic:>5} "
            f"{t.net_profit:>8.2f} {t.hold_minutes:>6.0f} {t.weekday:<3} {t.hour:>2} {t.session:<16} "
            f"{t.outcome:<9} {t.balance_after:>9.2f}"
        )
    print(f"\nFinal balance {balance_curve(trades, flows)[-1].balance:.2f}")
    print(f"Planted facts: {account.planted}")
    return 0


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # account names may not fit the console encoding
    parser = argparse.ArgumentParser(prog="tradequery", description=__doc__.strip().splitlines()[0])
    parser.add_argument("--data-dir", help="where account data is stored (default: $TQ_DATA_DIR or ./data)")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="print a synthetic account (no MT5 needed)")
    demo.add_argument("--rows", type=int, default=15)
    demo.set_defaults(func=cmd_demo)
    inspect = sub.add_parser("inspect", help="log in and show the account, symbols and a history summary")
    inspect.add_argument("--all-symbols", action="store_true", help="list every symbol on the broker server")
    inspect.set_defaults(func=cmd_inspect)
    download = sub.add_parser("download", help="save the account's history to the data folder")
    download.set_defaults(func=cmd_download)
    show = sub.add_parser("show", help="analyse a downloaded account without MT5")
    show.add_argument("--login", type=int, help="account number (default: the only downloaded account)")
    show.add_argument("--rows", type=int, default=20, help="how many recent trades to print")
    show.set_defaults(func=cmd_show)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (Mt5Error, CredentialsError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
