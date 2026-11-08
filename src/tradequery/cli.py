"""Command line.

  tradequery demo        print a synthetic account (no MT5, no credentials)
  tradequery inspect     log in to MT5; show account, symbols, history summary
  tradequery download    save account, deals and symbol specs to data/accounts/<login>.db
  tradequery import FILE load an MT5 HTML history report into data/accounts/<login>.db
  tradequery compare FILE check a report against the stored (synced) history
  tradequery show        analyse a downloaded account (no MT5 needed)

inspect and download need Windows Python + the MT5 terminal: run them with
``make inspect`` / ``make sync``, which use the Wine container.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import mt5 as mt5_client
from .mt5 import Mt5Error
from .settings import CredentialsError, load_credentials, load_settings
from . import db
from .crosscheck import compare
from .importers.mt5_report import ReportError, parse_report
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


# An incremental sync re-reads this far before the newest stored deal, so late
# corrections by the broker are picked up; overlapping deals are upserted by ticket.
SYNC_OVERLAP = timedelta(days=1)


def _sync_start(path: Path, full: bool) -> datetime | None:
    """Where an incremental sync starts: a day before the newest stored deal (None = everything)."""
    if full or not path.is_file():
        return None
    conn = db.connect(path, read_only=True)
    try:
        latest = db.latest_deal_time(conn)
    finally:
        conn.close()
    return latest - SYNC_OVERLAP if latest else None


def _read_from_terminal(since_for=None):
    """Log in, read, log out. Returns (account, deals, specs, all_symbol_names).

    ``since_for(login)`` gives the time to read deals from (None = all history).
    """
    settings = load_settings()
    mt5 = mt5_client.connect(load_credentials())
    try:
        account = mt5_client.fetch_account(mt5)
        since = since_for(account["login"]) if since_for else None
        # An empty incremental read just means no new deals: no need to wait for a download.
        deals = mt5_client.fetch_deals(mt5, settings.clock, date_from=since, wait_seconds=0 if since else 60)
        specs = mt5_client.fetch_symbol_specs(mt5, {d.symbol for d in deals if d.symbol})
        names = mt5_client.list_symbol_names(mt5)
    finally:
        mt5_client.disconnect(mt5)
    return account, deals, specs, names


def _print_balance_check(deals, mt5_balance: float, opening_balance: float = 0.0) -> bool:
    computed = round(opening_balance + balance_from_deals(deals), 2)
    diff = round(computed - mt5_balance, 2)
    start = f"opening {opening_balance:.2f} + deals" if opening_balance else "computed from deals"
    print(f"\nBalance check: {start} {computed:.2f} | MT5 balance {mt5_balance:.2f} | difference {diff:.2f}")
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
    account, deals, specs, _ = _read_from_terminal(
        since_for=lambda login: _sync_start(db.db_path(login, args.data_dir), args.full)
    )
    path = db.db_path(account["login"], args.data_dir)
    since = _sync_start(path, args.full)
    conn = db.connect(path)
    try:
        with conn:  # all or nothing
            try:
                previous_opening = db.load_account(conn)["opening_balance"]
            except LookupError:
                previous_opening = None
            new = db.upsert_deals(conn, deals)
            stored = db.load_deals(conn)
            # Brokers keep a limited history. Whatever the stored deals do not
            # explain was already in the account before the first one.
            opening = round(account["balance"] - balance_from_deals(stored), 2)
            db.save_account(
                conn,
                dict(account, opening_balance=opening),
                earliest_history=stored[0].time_utc if stored else None,
                synced_at=datetime.now(timezone.utc),
            )
            db.save_symbols(conn, specs)
            trades, flows = db.rebuild_derived(conn, load_settings())
    finally:
        conn.close()
    _print_account(account)
    if since:
        print(
            f"\nIncremental sync: read {len(deals)} deals since {_fmt_time(since)} UTC "
            f"(a day before the newest stored deal). Use --full to re-read everything."
        )
    else:
        _print_history_summary(deals)
    print(
        f"\nSaved to {path}: {len(stored)} deals ({new} new), {len(trades)} closed trades, "
        f"{len(flows)} cash flows, {len(specs)} symbol specs"
    )
    if abs(opening) >= 0.01 and stored:
        print(
            f"\nThe broker's history starts at {_fmt_time(stored[0].time_utc)} UTC with the account already "
            f"holding {opening:.2f} (MT5 balance minus every stored deal). Running balances start from there."
        )
        if previous_opening and abs(previous_opening - opening) >= 0.01:
            print(
                f"WARNING: an imported report put the opening balance at {previous_opening:.2f}. "
                "Some deals may be missing from one of the two sources."
            )
        elif previous_opening:
            print("This matches the opening balance from the imported report.")
    _print_balance_check(stored, account["balance"], opening)
    print("\nNext: tradequery show")
    return 0


def _pick_login(args) -> int:
    if args.login:
        return args.login
    logins = db.list_accounts(args.data_dir)
    if len(logins) == 1:
        return logins[0]
    if not logins:
        raise SystemExit("No downloaded account found. Run: tradequery download")
    raise SystemExit(f"Several accounts downloaded ({', '.join(map(str, logins))}). Pick one with --login.")


def cmd_show(args) -> int:
    login = _pick_login(args)
    conn = db.connect(db.db_path(login, args.data_dir), read_only=True)
    try:
        account = db.load_account(conn)
        deals = db.load_deals(conn)
        flows = db.load_cash_flows(conn)
        trades = db.load_trades(conn)
    finally:
        conn.close()

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
        print(f"Balance after the last closed trade: {trades[-1].balance_after:.2f}")

    shown = trades[-args.rows :]
    print(f"\nLast {len(shown)} trades:")
    print(f"{'closed (UTC)':<17} {'symbol':<8} {'side':<4} {'lots':>5} {'magic':>7} {'net':>9} {'hold':>6} {'day':<3} {'hr':>2} {'session':<16} {'result':<9} {'balance':>10}")
    for t in shown:
        print(
            f"{_fmt_time(t.close_time_utc)} {t.symbol:<8} {t.side:<4} {t.lots:>5} {t.magic:>7} "
            f"{t.net_profit:>9.2f} {t.hold_minutes:>6.0f} {t.weekday:<3} {t.hour:>2} {t.session:<16} "
            f"{t.outcome:<9} {t.balance_after:>10.2f}"
        )
    ok = _print_balance_check(deals, account["balance"], account["opening_balance"])
    return 0 if ok else 1


def cmd_compare(args) -> int:
    report = parse_report(args.report, load_settings().clock)
    conn = db.connect(db.db_path(report.login, args.data_dir), read_only=True)
    try:
        stored = db.load_deals(conn)
    finally:
        conn.close()
    result = compare(report.deals, stored)
    print(f"Report for account {report.login}: {len(report.deals)} deals, "
          f"{_fmt_time(report.deals[0].time_utc)} to {_fmt_time(report.deals[-1].time_utc)} UTC")
    print(f"Compared {result.deals_compared} deals and {result.trades_compared} closed trades with the stored history.")
    for label, items in (("Deals only in the report", result.only_in_report),
                         ("Deals only in the stored history", result.only_in_store)):
        if items:
            print(f"{label}: {len(items)} (first: {', '.join(map(str, items[:10]))})")
    for line in (result.deal_differences + result.trade_differences)[: args.show]:
        print("  " + line)
    differences = len(result.deal_differences) + len(result.trade_differences)
    if differences:
        print(f"{differences} field differences in total.")
    if result.ok:
        print("OK: the report and the stored history give the same deals and the same trades.")
        return 0
    return 1


def cmd_import(args) -> int:
    settings = load_settings()
    report = parse_report(args.report, settings.clock)
    path = db.db_path(report.login, args.data_dir)
    conn = db.connect(path)
    try:
        with conn:  # all or nothing
            new = db.upsert_deals(conn, report.deals, overwrite=False)
            try:
                db.load_account(conn)
                kept = True  # a synced account row is richer than the report's; keep it
            except LookupError:
                kept = False
                db.save_account(
                    conn,
                    dict(report.account(), opening_balance=report.opening_balance),
                    earliest_history=report.deals[0].time_utc,
                    synced_at=datetime.now(timezone.utc),
                )
            trades, flows = db.rebuild_derived(conn, settings)
            account = db.load_account(conn)
            stored = db.load_deals(conn)
    finally:
        conn.close()

    print(f"Report for account {report.login} on {report.server} ({report.currency}, {report.margin_mode})")
    print(f"Deals {_fmt_time(report.deals[0].time_utc)} to {_fmt_time(report.deals[-1].time_utc)} UTC")
    print(f"Balance {report.opening_balance:.2f} before the first deal, {report.closing_balance:.2f} after the last")
    print("Running balance: every deal matches the report's Balance column.")
    print(
        f"\nSaved to {path}: {len(report.deals)} deals in the report ({new} new), "
        f"{len(trades)} closed trades, {len(flows)} cash flows"
    )
    if kept:
        print("The account was already synced; its stored deals and account facts were kept.")
    if report.unmatched_exits:
        print(
            f"{report.unmatched_exits} exit deals close positions opened before the report starts; "
            "they count toward the balance but not as closed trades."
        )
    _print_balance_check(stored, account["balance"], account["opening_balance"])
    print("\nNext: tradequery show")
    return 0


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
    download.add_argument("--full", action="store_true",
                          help="re-read the whole history instead of only what is new")
    download.set_defaults(func=cmd_download)
    imp = sub.add_parser("import", help="load an MT5 HTML history report into the account's database")
    imp.add_argument("report", help="path to the ReportHistory-<login>.html file")
    imp.set_defaults(func=cmd_import)
    comp = sub.add_parser("compare", help="check an MT5 HTML report against the stored (synced) history")
    comp.add_argument("report", help="path to the ReportHistory-<login>.html file")
    comp.add_argument("--show", type=int, default=20, help="how many differences to print")
    comp.set_defaults(func=cmd_compare)
    show = sub.add_parser("show", help="analyse a downloaded account without MT5")
    show.add_argument("--login", type=int, help="account number (default: the only downloaded account)")
    show.add_argument("--rows", type=int, default=20, help="how many recent trades to print")
    show.set_defaults(func=cmd_show)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (Mt5Error, CredentialsError, ReportError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # the Wine container runs `python -m tradequery.cli`
    sys.exit(main())
