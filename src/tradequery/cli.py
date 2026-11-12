"""Command line.

  tradequery demo        print a synthetic account (no MT5, no credentials)
  tradequery sample      store the synthetic sample account, to try the page and questions
  tradequery rebuild     recompute the derived tables of stored accounts (after an upgrade)
  tradequery inspect     log in to MT5; show account, symbols, history summary
  tradequery download    save account, deals and symbol specs to data/accounts/<login>.db
  tradequery import FILE load an MT5 HTML history report into data/accounts/<login>.db
  tradequery compare FILE check a report against the stored (synced) history
  tradequery metrics     named metrics (win rate, profit factor, streaks, drawdown, ...)
  tradequery tags        detector tags with evidence (revenge, overtrading, no stop loss, oversized, ...)
  tradequery sequences   martingale and grid sequences: depth, lot multiplier, result
  tradequery import-calendar [FILE]   load the economic calendar export and join it to trades
  tradequery news        trades and drawdowns that overlapped scheduled news
  tradequery query SPEC  run a QuerySpec (JSON) on a stored account (--examples all: the plan's questions)
  tradequery ask [QUESTION]   ask in plain English (Gemini); no question: interactive with follow-ups
  tradequery llm-models  list the Gemini models your API key can use
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
from .settings import CredentialsError, data_dir, load_credentials, load_settings
from dataclasses import dataclass, field

from . import db, detectors, metrics, news, pricepath
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


def _read_from_terminal(since_for=None, then=None):
    """Log in, read, log out. Returns (account, deals, specs, all_symbol_names).

    ``since_for(login)`` gives the time to read deals from (None = all history).
    ``then(mt5, account, deals)`` runs while still logged in (used to fetch bars).
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
        if then:
            then(mt5, account, deals)
    finally:
        mt5_client.disconnect(mt5)
    return account, deals, specs, names


@dataclass
class BarFetch:
    bars: list = field(default_factory=list)
    fetched: int = 0  # windows asked for
    cached: int = 0  # windows skipped because a previous sync stored them
    empty: int = 0  # windows the broker returned no bars for (older than its history)


def fetch_missing_bars(mt5, clock, path: Path, new_deals) -> BarFetch:
    """One-minute bars for the windows in which positions were open, except those already stored.

    A window is skipped when it already has stored bars and ends before the
    newest stored bar of its symbol (a previous sync fetched it).
    """
    stored = []
    conn = db.connect(path, read_only=True) if path.is_file() else None
    try:
        if conn:
            stored = db.load_deals(conn)
        merged = {d.ticket: d for d in stored}
        merged.update({d.ticket: d for d in new_deals})
        trades = build_trades(sorted(merged.values(), key=lambda d: (d.time_utc, d.ticket)))
        result = BarFetch()
        first = True
        for symbol, windows in pricepath.bar_windows(trades).items():
            newest = None
            if conn:
                row = conn.execute("SELECT MAX(time_utc) FROM bars WHERE symbol = ?", (symbol,)).fetchone()
                newest = db.parse_time(row[0]) if row and row[0] else None
            for start, end in windows:
                if conn and newest and end <= newest and db.bar_count(conn, symbol, start, end):
                    result.cached += 1
                    continue
                # only the first request waits for the terminal to download history
                bars = mt5_client.fetch_bars(mt5, clock, symbol, start, end, tries=3 if first else 1)
                first = False
                result.fetched += 1
                result.empty += not bars
                result.bars.extend(bars)
        return result
    finally:
        if conn:
            conn.close()


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
    fetched = BarFetch()

    def bars_while_logged_in(mt5, account, deals):
        nonlocal fetched
        if not args.no_bars:
            fetched = fetch_missing_bars(mt5, load_settings().clock, db.db_path(account["login"], args.data_dir), deals)

    account, deals, specs, _ = _read_from_terminal(
        since_for=lambda login: _sync_start(db.db_path(login, args.data_dir), args.full),
        then=bars_while_logged_in,
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
            db.save_bars(conn, fetched.bars)
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
    if fetched.fetched or fetched.cached:
        print(
            f"One-minute bars: {len(fetched.bars)} saved from {fetched.fetched} time windows"
            f" ({fetched.cached} windows already stored"
            + (f", {fetched.empty} older than the broker's bar history" if fetched.empty else "") + ")"
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


def _money(v) -> str:
    return "n/a" if v is None else f"{v:,.2f}"


def _pct(v) -> str:
    return "n/a" if v is None else f"{v * 100:.2f}%"


def _num(v, digits=2) -> str:
    return "n/a" if v is None else f"{v:.{digits}f}"


def _parse_day(text: str | None) -> datetime | None:
    return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc) if text else None


def cmd_metrics(args) -> int:
    login = _pick_login(args)
    conn = db.connect(db.db_path(login, args.data_dir), read_only=True)
    try:
        account = db.load_account(conn)
        trades = db.load_trades(conn)
        flows = db.load_cash_flows(conn)
        specs = {s["symbol"]: s for s in db.load_symbols(conn)}
        bars = db.load_bars(conn)
    finally:
        conn.close()
    start, end = _parse_day(args.since), _parse_day(args.until)
    if end:
        end += timedelta(days=1)  # --until is inclusive
    chosen = metrics.filter_trades(trades, symbol=args.symbol, magic=args.magic, start=start, end=end)
    chosen_flows = [f for f in flows if (not start or f.time_utc >= start) and (not end or f.time_utc < end)]
    m = metrics.compute_metrics(
        chosen, chosen_flows, opening_balance=account["opening_balance"] if not start else 0.0,
        symbol_specs=specs, breakeven_breaks_streak=load_settings().breakeven_breaks_streak,
    )
    currency = account.get("currency") or ""
    scope = [f"symbol {args.symbol}" if args.symbol else "", f"magic {args.magic}" if args.magic is not None else "",
             f"from {args.since}" if args.since else "", f"to {args.until}" if args.until else ""]
    print(f"Account {login} ({currency}): {', '.join(s for s in scope if s) or 'all closed trades'}")
    if not chosen:
        print("No closed trades match.")
        return 0
    ci = m["win_rate_ci95"]
    lines = [
        ("Results", None),
        ("Closed trades", f"{m['trade_count']}"),
        ("Win rate", f"{_pct(m['win_rate'])}  (95% range {_pct(ci[0])} to {_pct(ci[1])})"),
        ("Net profit", _money(m["net_profit"])),
        ("Gross profit / loss", f"{_money(m['gross_profit'])} / {_money(m['gross_loss'])}"),
        ("Profit factor", _num(m["profit_factor"])),
        ("Expectancy per trade", _money(m["expectancy"])),
        ("Avg hold (all / wins / losses)", f"{_num(m['avg_hold_minutes'], 0)} / {_num(m['avg_hold_minutes_winners'], 0)}"
                                           f" / {_num(m['avg_hold_minutes_losers'], 0)} min"),
        ("Costs", None),
        ("Swap total", f"{_money(m['swap_total'])}  ({_pct(m['swap_share'])} of gross profit)"),
        ("Commission total", _money(m["commission_total"])),
        ("Risk", None),
    ]
    if m["avg_r"] is None:
        lines.append(("R-multiples", f"n/a: none of the {m['trade_count']} trades had a stop loss at entry"))
    else:
        lines += [("Average R / total R", f"{_num(m['avg_r'])} / {_num(m['total_r'])}"
                                          f"  ({m['r_excluded']} trades without a stop loss left out)"),
                  ("Planned risk-reward", _num(m["planned_rr"]))]
    episodes = metrics.drawdown_episodes(chosen, chosen_flows)
    worst = max(episodes, key=lambda e: e.depth, default=None)
    lines += [
        ("Max drawdown (closed trades)", f"{_money(m['max_drawdown'])}  ({_pct(m['max_drawdown_pct'])} of the money at stake)"),
    ]
    if worst:
        recovered = _fmt_time(worst.recovery_time) if worst.recovery_time else "not recovered"
        start_text = _fmt_time(worst.peak_time) if worst.peak_time else "start of history"
        lines.append(("  worst episode", f"peak {start_text}, trough {_fmt_time(worst.trough_time)}, "
                                         f"{recovered}; {len(worst.position_ids)} trades to the trough"))
    if m["peak_lots_open_time"]:
        lines.append(("Peak lots open", f"{m['peak_lots_open']:.2f} at {_fmt_time(m['peak_lots_open_time'])}"
                                        f"  (time-weighted average {_num(m['avg_lots_open'])})"))
    pp = pricepath.compute_price_path_metrics(chosen, chosen_flows, bars, symbol_specs=specs,
                                              opening_balance=account["opening_balance"])
    lines.append(("Price path (one-minute bars, approximate)", None))
    if not pp["price_path_coverage"]:
        lines.append(("Coverage", "no one-minute bars stored; run make sync to download them"))
    else:
        worst_float = pp["worst_floating_pnl"]
        lines += [
            ("Coverage", f"{_pct(pp['price_path_coverage'])} of trades have bars for their whole life"),
            ("Avg worst / best open result", f"{_money(pp['avg_mae'])} / {_money(pp['avg_mfe'])}  (MAE / MFE)"),
            ("Deepest single trade (MAE)", _money(pp["worst_mae"])),
            ("Avg minutes in profit / loss", f"{_num(pp['avg_minutes_in_profit'], 0)} / {_num(pp['avg_minutes_in_loss'], 0)}"),
            ("Avg spread at open", f"{_num(pp['avg_spread_at_open'], 0)} points"),
            ("Max equity drawdown", f"{_money(pp['max_equity_drawdown'])}  ({_pct(pp['max_equity_drawdown_pct'])} of the money"
                                    f" at stake; worst case within the minute {_money(pp['max_equity_drawdown_low'])})"),
        ]
        ep = pp["max_equity_drawdown_episode"]
        if ep:
            start_text = _fmt_time(ep.peak_time) if ep.peak_time else "start of history"
            recovered = _fmt_time(ep.recovery_time) if ep.recovery_time else "not recovered"
            lines.append(("  worst equity episode", f"peak {start_text}, trough {_fmt_time(ep.trough_time)}, {recovered}"))
            if ep.added_capital:
                lines.append(("  refilled during it", f"deposits of {_money(ep.added_capital)} were made before the trough"))
        if worst_float:
            lines.append(("Worst floating loss", f"{_money(worst_float[0])} at {_fmt_time(worst_float[1])}"))
    runs = metrics.streaks(chosen, load_settings().breakeven_breaks_streak)
    longest_loss = max((r for r in runs if r.kind == "loss"), key=lambda r: r.length, default=None)
    lines += [
        ("Streaks", None),
        ("Longest win / loss streak", f"{m['max_win_streak']} / {m['max_loss_streak']}"),
        ("Current streak", f"{m['current_streak']:+d}" if m["current_streak"] else "0"),
        ("Average streak length", _num(m["avg_streak_length"])),
    ]
    if longest_loss:
        lines.append(("  longest losing streak", f"{_fmt_time(longest_loss.start)} to {_fmt_time(longest_loss.end)}, "
                                                 f"net {_money(longest_loss.net)}"))
    lines += [
        ("Money in and out", None),
        ("Deposits / withdrawals", f"{_money(m['total_deposits'])} / {_money(m['total_withdrawals'])}"),
        ("Net deposits", _money(m["net_deposits"])),
    ]
    if m["credits_and_bonuses"]:
        lines.append(("Credits and bonuses", _money(m["credits_and_bonuses"])))
    if not start and account["opening_balance"]:
        lines.append(("Balance before the history", _money(account["opening_balance"])))
    lines.append(("Return (simple)", f"{_pct(m['return_pct'])}  of opening balance + deposits"))
    for label, value in lines:
        print(f"\n{label}" if value is None else f"  {label:<32} {value}")
    print("\nMax drawdown uses closed trades only; equity drawdown adds floating losses from one-minute bars."
          " Both have deposits and withdrawals removed. Bars show a minute's high and low but not their order,"
          " so excursions and equity are approximate.")
    return 0


def cmd_tags(args) -> int:
    login = _pick_login(args)
    conn = db.connect(db.db_path(login, args.data_dir), read_only=True)
    try:
        tags = db.load_tags(conn, args.rule)
        trades = {t.position_id: t for t in db.load_trades(conn)}
    finally:
        conn.close()
    print(f"Account {login}: {len(trades)} closed trades")
    rules = [args.rule] if args.rule else list(detectors.RULES)
    for rule in rules:
        mine = [t for t in tags if t.rule == rule]
        share = f"{len(mine) / len(trades) * 100:.1f}%" if trades else "n/a"
        print(f"\n{rule}: {len(mine)} trades ({share})")
        for tag in mine[: args.examples]:
            t = trades.get(tag.position_id)
            when = _fmt_time(t.open_time_utc) if t else "?"
            print(f"  #{tag.position_id}  {when}  {t.lots if t else '?'} lots  net {t.net_profit if t else '?'}: {tag.evidence}")
        if len(mine) > args.examples:
            print(f"  ... and {len(mine) - args.examples} more (--examples N to show more)")
    if not tags and not args.rule:
        print("\nNo tags stored. Run make sync (or import a report) to run the detectors.")
    return 0


def cmd_sequences(args) -> int:
    login = _pick_login(args)
    conn = db.connect(db.db_path(login, args.data_dir), read_only=True)
    try:
        found = db.load_sequences(conn)
        trade_count = len(db.load_trades(conn))
    finally:
        conn.close()
    if args.type:
        found = [s for s in found if s.type == args.type]
    found = [s for s in found if s.depth >= args.min_depth]
    print(f"Account {login}: {len(found)} sequences" + (f" of type {args.type}" if args.type else "")
          + (f" with depth {args.min_depth}+" if args.min_depth > 1 else ""))
    if not found:
        print("None. (Run make sync or import a report to detect sequences.)")
        return 0
    in_sequences = sum(s.depth for s in found)
    print(f"  {in_sequences} of {trade_count} closed trades are part of one")
    for kind in sorted({s.type for s in found}):
        mine = [s for s in found if s.type == kind]
        wins = [s for s in mine if s.is_win]
        print(f"  {kind}: {len(mine)} ({len(wins)} won {sum(s.net for s in wins):,.2f}; "
              f"{len(mine) - len(wins)} lost {sum(s.net for s in mine if not s.is_win):,.2f})")
    depths = {}
    for s in found:
        depths.setdefault(s.depth, []).append(s)
    print("\nBy depth (positions in the sequence):")
    for depth in sorted(depths):
        mine = depths[depth]
        wins = sum(1 for s in mine if s.is_win)
        print(f"  depth {depth:>2}: {len(mine):>4} sequences, {wins} won, net {sum(s.net for s in mine):>10,.2f}, "
              f"max multiplier {max(s.max_lot_multiplier for s in mine):g}x")
    key = {"depth": lambda s: (-s.depth, s.net), "net": lambda s: s.net, "time": lambda s: s.start_utc}[args.sort]
    print(f"\nSequences by {args.sort} (first {args.limit}):")
    print(f"  {'#':>4} {'type':<16} {'start (UTC)':<16} {'depth':>5} {'lots':>12} {'mult':>6} {'open':>4} {'net':>11}")
    for s in sorted(found, key=key)[: args.limit]:
        lots = f"{s.first_lot:g}->{s.max_lot:g}"
        print(f"  {s.sequence_id:>4} {s.type:<16} {_fmt_time(s.start_utc):<16} {s.depth:>5} {lots:>12} "
              f"{s.max_lot_multiplier:>5g}x {s.max_open:>4} {s.net:>11,.2f}")
    deep_wins = [s for s in found if s.is_win and s.depth >= 5]
    if deep_wins:
        print(f"\n{len(deep_wins)} sequences reached depth 5+ and still closed in profit: the risk taken does not show in their result.")
    return 0


def cmd_import_calendar(args) -> int:
    settings = load_settings()
    login = _pick_login(args)
    path = args.file or Path(args.data_dir or data_dir()) / "news" / "calendar.csv"
    if not Path(path).is_file():
        raise SystemExit(f"No calendar export at {path}. Run: make calendar FROM=YYYY.MM.DD TO=YYYY.MM.DD")
    events, note = news.parse_calendar(path, settings.news, settings.clock)
    conn = db.connect(db.db_path(login, args.data_dir))
    try:
        with conn:
            new = db.save_news_events(conn, events)
            trades, _ = db.rebuild_derived(conn, settings)
            links = db.load_trade_news(conn)
    finally:
        conn.close()
    floor = news.IMPORTANCE.get(settings.news.min_importance, 3)
    counted = sum(1 for e in events if news.IMPORTANCE.get(e.importance, 0) >= floor)
    print(f"Calendar: {len(events)} events ({new} new), {counted} of importance {settings.news.min_importance} or above")
    if events:
        print(f"  {_fmt_time(events[0].time_utc)} to {_fmt_time(events[-1].time_utc)} UTC")
    print(f"  Time alignment: {note}")
    overlapping = len({l.position_id for l in links})
    print(f"Account {login}: {overlapping} of {len(trades)} closed trades overlapped an event "
          f"({settings.news.minutes_before:g} min before to {settings.news.minutes_after:g} min after)")
    print("\nNext: tradequery news")
    return 0


def cmd_news(args) -> int:
    settings = load_settings()
    login = _pick_login(args)
    conn = db.connect(db.db_path(login, args.data_dir), read_only=True)
    try:
        events = db.load_news_events(conn)
        links = db.load_trade_news(conn)
        trades = db.load_trades(conn)
        flows = db.load_cash_flows(conn)
    finally:
        conn.close()
    if not events:
        print("No calendar stored. Run: make news FROM=YYYY.MM.DD TO=YYYY.MM.DD")
        return 0
    by_id = {e.value_id: e for e in events}
    by_pid = {t.position_id: t for t in trades}
    inside, outside = news.inside_outside(trades, links)
    print(f"Account {login}: news overlap ({settings.news.min_importance}-importance events, "
          f"{settings.news.minutes_before:g} min before to {settings.news.minutes_after:g} min after)")
    print("Overlap is timing only: it does not mean the news caused the result.\n")
    for label, s in (("Inside news windows", inside), ("Outside news windows", outside)):
        print(f"  {label:<22} {s.trades:>5} trades, win rate {_pct(s.win_rate)}, net {_money(s.net)}")
    phases = {}
    for l in links:
        phases[l.phase] = phases.get(l.phase, 0) + 1
    if phases:
        print("  Overlaps by phase: " + ", ".join(f"{k} {v}" for k, v in sorted(phases.items())))
    per_event: dict[int, set[int]] = {}
    for l in links:
        per_event.setdefault(l.value_id, set()).add(l.position_id)
    if per_event:
        print("\nEvents with the most overlapping trades:")
        top = sorted(per_event.items(), key=lambda kv: -len(kv[1]))[: args.limit]
        for vid, pids in top:
            e = by_id[vid]
            net = sum(by_pid[p].net_profit for p in pids if p in by_pid)
            print(f"  {_fmt_time(e.time_utc)} UTC  {e.currency} {e.name:<40.40} {len(pids):>4} trades, net {net:>10,.2f}")
    episodes = sorted(metrics.drawdown_episodes(trades, flows), key=lambda e: -e.depth)[:3]
    if episodes:
        print("\nLargest drawdowns (closed trades) and the news they overlapped:")
        for ep in episodes:
            share, vids = news.news_loss_share(ep.position_ids, by_pid, links)
            start = _fmt_time(ep.peak_time) if ep.peak_time else "start"
            names = sorted({f"{by_id[v].name} ({by_id[v].time_utc:%m-%d})" for v in vids})
            print(f"  {_money(ep.depth)} from {start} to {_fmt_time(ep.trough_time)}: "
                  f"{_pct(share)} of the loss came from trades that overlapped news"
                  + (f": {', '.join(names[:4])}" + (" ..." if len(names) > 4 else "") if names else ""))
    return 0


def _cell(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        if value == 0:
            return "0"
        return f"{value:.4f}" if abs(value) < 1 else f"{value:,.2f}"  # shares and rates keep 4 decimals
    return str(value)


def _print_result(result, show_sql: bool) -> None:
    if result.kind != "query":
        print(f"[{result.kind}] {result.message}")
        return
    print(f"Interpretation: {result.interpretation}")
    if not result.rows:
        print("  (no rows)")
    else:
        table = [[str(c) for c in result.columns]] + [[_cell(v) for v in row] for row in result.rows]
        widths = [max(len(r[i]) for r in table) for i in range(len(table[0]))]
        for n, row in enumerate(table):
            print("  " + "  ".join(v.rjust(w) if n else v.ljust(w) for v, w in zip(row, widths)))
    if result.truncated:
        print("  (more rows than shown; raise the spec's limit, up to 500)")
    for note in result.notes:
        print(f"  Note: {note}")
    if show_sql:
        print("  SQL: " + result.sql.replace("\n", " "))


def cmd_query(args) -> int:
    from .examples import EXAMPLES
    from .execute import execute
    from .spec import SpecError, parse_spec

    settings = load_settings()
    path = db.db_path(_pick_login(args), args.data_dir)
    if args.examples:
        chosen = EXAMPLES if args.examples == "all" else [EXAMPLES[int(i) - 1] for i in args.examples.split(",")]
    else:
        text = Path(args.file).read_text(encoding="utf-8") if args.file else args.spec
        if not text:
            raise SystemExit("Give a spec as JSON text, --file spec.json, or --examples all")
        chosen = [(None, text)]
    for question, spec in chosen:
        if question:
            print(f"\nQ: {question}")
        try:
            parsed = parse_spec(spec)
        except SpecError as error:
            print(f"Spec rejected: {error}")
            return 2
        _print_result(execute(parsed, path, settings), args.sql)
    return 0


def _print_answer(answer, provider, show_spec: bool, show_sql: bool) -> None:
    import json as _json

    if answer.spec is not None and show_spec:
        print("Spec: " + _json.dumps(answer.spec.model_dump(mode="json", exclude_defaults=True)))
    if answer.text and answer.result is None:
        print(answer.text)  # clarify or unsupported
    if answer.result is not None:
        if answer.verified and answer.text:
            print(answer.text + "\n")
        _print_result(answer.result, show_sql)
        if not answer.verified and not answer.error:
            print("  (The written explanation used numbers that are not in the table, so only the table is shown.)")
    if answer.error:
        print(answer.error)
    print(f"[{provider.name} {provider.model}: {answer.usage.requests} requests, "
          f"{answer.usage.input_tokens} in / {answer.usage.output_tokens} out tokens]")


def cmd_ask(args) -> int:
    from .ask import Session, ask
    from .llm import LLMError, provider_from_env

    settings = load_settings()
    path = db.db_path(_pick_login(args), args.data_dir)
    try:
        provider = provider_from_env()
    except LLMError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    session = Session()
    if args.question:
        answer = ask(" ".join(args.question), path, settings, provider, session)
        _print_answer(answer, provider, args.spec, args.sql)
        return 0 if not answer.error else 1
    print(f"Ask about account {path.stem} ({provider.name} {provider.model}). Follow-ups refine the last "
          "question; 'new' starts over; an empty line quits.")
    while True:
        try:
            question = input("\n> ").strip()
        except EOFError:
            break
        if not question:
            break
        if question.lower() == "new":
            session = Session()
            print("Starting over.")
            continue
        _print_answer(ask(question, path, settings, provider, session), provider, args.spec, args.sql)
    return 0


def cmd_llm_models(args) -> int:
    from .llm import GeminiProvider, LLMError, choose_model
    import os

    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        print("Error: no GEMINI_API_KEY in .env", file=sys.stderr)
        return 2
    try:
        provider = GeminiProvider(key, model="unused")
        names = provider.list_models()
    except LLMError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    print("Models this key can use for text:")
    for name in names:
        print("  " + name.removeprefix("models/"))
    print(f"\nChosen when GEMINI_MODEL is not set: {choose_model(names) or 'none'}")
    configured = os.environ.get("GEMINI_MODEL", "").strip()
    if configured:
        print(f"Set in .env: GEMINI_MODEL={configured}")
    return 0


def cmd_eval(args) -> int:
    import os
    import tempfile

    from . import evaluate
    from .llm import LLMError, provider_from_env

    if args.model:
        os.environ["GEMINI_MODEL"] = args.model
    settings = load_settings()
    if args.smart and args.questions == "evals/questions.jsonl":
        args.questions = "evals/smart.jsonl"
    if args.smart and args.out == "evals/results":
        args.out = "evals/results/smart"
    questions = evaluate.load_questions(args.questions)
    if args.ids:
        wanted = set(args.ids.split(","))
        questions = [q for q in questions if q["id"] in wanted]
    if args.limit:
        questions = questions[: args.limit]
    try:
        provider = provider_from_env()
    except LLMError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    root = Path(tempfile.mkdtemp(prefix="tq-eval-"))
    path = evaluate.build_eval_account(root, settings)
    if args.smart:
        return _eval_smart(questions, path, settings, provider, args)
    per_q = 2 if args.explain else 1
    print(f"Eval: {len(questions)} questions on the synthetic eval account with {provider.name} {provider.model}; "
          f"about {len(questions) * per_q} requests, paced at {args.rpm:g} per minute.")

    def progress(i, total, o):
        mark = "PASS" if o.passed else "fail"
        extra = f" ({o.error})" if o.error and not o.passed else ""
        print(f"  [{i:>2}/{total}] {mark} {o.id:<10} {o.kind or '-':<11} {o.seconds:>5.1f}s{extra}", flush=True)

    report = evaluate.run_suite(questions, path, settings, provider, explain=args.explain, rpm=args.rpm,
                                progress=progress)
    json_path, md_path = evaluate.save(report, args.out)
    print("\n" + evaluate.markdown(report))
    print(f"Saved {json_path} and {md_path}")
    return 0


def _eval_smart(questions, path, settings, provider, args) -> int:
    from . import smart_eval

    print(f"Smart eval: {len(questions)} questions, each in normal and smart mode, with {provider.name} "
          f"{provider.model}; about {len(questions) * 5} requests, paced at {args.rpm:g} per minute.")

    def progress(i, total, o):
        mark = "good" if o.good else "; ".join(o.problems) or "wrong result"
        print(f"  [{i:>2}/{total}] {o.id:<10} normal {'PASS' if o.normal.passed else 'fail'} · smart "
              f"{'PASS' if o.smart.passed else 'fail'} · {len(o.checks)} checks · {mark}", flush=True)

    report = smart_eval.run_compare(questions, path, settings, provider, rpm=args.rpm, progress=progress)
    json_path, md_path = smart_eval.save(report, args.out)
    print("\n" + smart_eval.markdown(report).split("Per question:")[0])
    print(f"Saved {json_path} and {md_path}")
    return 0


def cmd_sample(args) -> int:
    from .evaluate import EVAL_LOGIN, build_eval_account

    path = build_eval_account(args.data_dir or data_dir(), load_settings())
    print(f"Stored the synthetic sample account {EVAL_LOGIN} at {path}: 48 made-up trades on three symbols,")
    print("with a planted martingale, losing and winning streaks, deposits, a withdrawal and four calendar events.")
    print("Try it: make web (then pick the account in the page), or make ask Q=\"What is my win rate?\"")
    return 0


def cmd_rebuild(args) -> int:
    """Recompute every derived table from the stored deals, for each account (after an upgrade)."""
    root = args.data_dir or data_dir()
    settings = load_settings()
    for login in ([args.login] if args.login else db.list_accounts(root)):
        conn = db.connect(db.db_path(login, root))
        try:
            with conn:
                trades, _ = db.rebuild_derived(conn, settings)
        finally:
            conn.close()
        print(f"Rebuilt account {login}: {len(trades)} closed trades")
    return 0


def cmd_backup(args) -> int:
    from .backup import backup

    target = backup(args.data_dir or data_dir(), keep=args.keep)
    print(f"Backed up {len(list((target / 'accounts').glob('*.db')))} account database(s) to {target}")
    print(f"Keeping the newest {args.keep}. Copy data/backups to another drive now and then.")
    return 0


def cmd_serve(args) -> int:
    from .api import serve

    serve(host=args.host, port=args.port)
    return 0


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
    from .ingest import import_report

    done = import_report(args.report, args.data_dir, load_settings())
    report, path, new, kept, account, stored = (done.report, done.path, done.new_deals, done.kept_account,
                                                done.account, done.stored)

    print(f"Report for account {report.login} on {report.server} ({report.currency}, {report.margin_mode})")
    print(f"Deals {_fmt_time(report.deals[0].time_utc)} to {_fmt_time(report.deals[-1].time_utc)} UTC")
    print(f"Balance {report.opening_balance:.2f} before the first deal, {report.closing_balance:.2f} after the last")
    print("Running balance: every deal matches the report's Balance column.")
    print(
        f"\nSaved to {path}: {len(report.deals)} deals in the report ({new} new), "
        f"{done.trades} closed trades, {done.flows} cash flows"
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
    download.add_argument("--no-bars", action="store_true", help="skip downloading one-minute bars")
    download.set_defaults(func=cmd_download)
    imp = sub.add_parser("import", help="load an MT5 HTML history report into the account's database")
    imp.add_argument("report", help="path to the ReportHistory-<login>.html file")
    imp.set_defaults(func=cmd_import)
    met = sub.add_parser("metrics", help="named metrics for a stored account")
    met.add_argument("--login", type=int, help="account number (default: the only downloaded account)")
    met.add_argument("--symbol", help="only this symbol")
    met.add_argument("--magic", type=int, help="only this magic number (0 = manual trades)")
    met.add_argument("--since", help="first day, YYYY-MM-DD (UTC, by close time)")
    met.add_argument("--until", help="last day, YYYY-MM-DD (UTC, inclusive)")
    met.set_defaults(func=cmd_metrics)
    tg = sub.add_parser("tags", help="detector tags (revenge, overtrading, no stop loss, oversized) with evidence")
    tg.add_argument("--login", type=int, help="account number (default: the only downloaded account)")
    tg.add_argument("--rule", choices=detectors.RULES, help="only this rule")
    tg.add_argument("--examples", type=int, default=5, help="how many tagged trades to show per rule")
    tg.set_defaults(func=cmd_tags)
    sq = sub.add_parser("sequences", help="martingale and grid sequences with depth, lot multiplier and result")
    sq.add_argument("--login", type=int, help="account number (default: the only downloaded account)")
    sq.add_argument("--type", choices=("martingale", "grid", "martingale_grid"), help="only this type")
    sq.add_argument("--min-depth", type=int, default=1, help="only sequences with at least this many positions")
    sq.add_argument("--sort", choices=("depth", "net", "time"), default="depth", help="order of the list")
    sq.add_argument("--limit", type=int, default=15, help="how many sequences to list")
    sq.set_defaults(func=cmd_sequences)
    ic = sub.add_parser("import-calendar", help="load an economic calendar export and join it to the trades")
    ic.add_argument("file", nargs="?", help="the export from make calendar (default: <data>/news/calendar.csv)")
    ic.add_argument("--login", type=int, help="account number (default: the only downloaded account)")
    ic.set_defaults(func=cmd_import_calendar)
    nw = sub.add_parser("news", help="trades and drawdowns that overlapped scheduled news")
    nw.add_argument("--login", type=int, help="account number (default: the only downloaded account)")
    nw.add_argument("--limit", type=int, default=10, help="how many events to list")
    nw.set_defaults(func=cmd_news)
    qu = sub.add_parser("query", help="run a QuerySpec (JSON) on a stored account")
    qu.add_argument("spec", nargs="?", help="the spec as JSON text")
    qu.add_argument("--file", help="read the spec from a JSON file")
    qu.add_argument("--examples", help="run the plan's example questions: all, or numbers like 2,5")
    qu.add_argument("--login", type=int, help="account number (default: the only downloaded account)")
    qu.add_argument("--sql", action="store_true", help="also print the SQL that ran")
    qu.set_defaults(func=cmd_query)
    ak = sub.add_parser("ask", help="ask a question in plain English (no question: an interactive session)")
    ak.add_argument("question", nargs="*", help="the question; leave out for an interactive session with follow-ups")
    ak.add_argument("--login", type=int, help="account number (default: the only downloaded account)")
    ak.add_argument("--spec", action="store_true", help="also print the QuerySpec the model wrote")
    ak.add_argument("--sql", action="store_true", help="also print the SQL that ran")
    ak.set_defaults(func=cmd_ask)
    ev = sub.add_parser("eval", help="run the eval questions against the language model and score them")
    ev.add_argument("--questions", default="evals/questions.jsonl", help="the questions file")
    ev.add_argument("--out", default="evals/results", help="where to save the report")
    ev.add_argument("--ids", help="only these question ids, comma separated")
    ev.add_argument("--limit", type=int, help="only the first N questions")
    ev.add_argument("--model", help="a model name to use instead of GEMINI_MODEL")
    ev.add_argument("--rpm", type=float, default=9, help="model requests per minute (free tiers are strict)")
    ev.add_argument("--smart", action="store_true",
                    help="ask each question in normal and smart mode and compare (default questions: evals/smart.jsonl)")
    ev.add_argument("--no-explain", dest="explain", action="store_false",
                    help="skip the written answer (one request per question; no number-check score)")
    ev.set_defaults(func=cmd_eval)
    sm = sub.add_parser("sample", help="store the synthetic sample account (no MT5 needed)")
    sm.set_defaults(func=cmd_sample)
    rb = sub.add_parser("rebuild", help="recompute the derived tables of stored accounts (after an upgrade)")
    rb.add_argument("--login", type=int, help="only this account (default: all)")
    rb.set_defaults(func=cmd_rebuild)
    bk = sub.add_parser("backup", help="copy every account database and the news files to data/backups/")
    bk.add_argument("--keep", type=int, default=10, help="how many backups to keep (newest first)")
    bk.set_defaults(func=cmd_backup)
    sv = sub.add_parser("serve", help="run the web page and API")
    sv.add_argument("--host", default="0.0.0.0", help="address to listen on (inside Docker: all)")
    sv.add_argument("--port", type=int, default=8000)
    sv.set_defaults(func=cmd_serve)
    lm = sub.add_parser("llm-models", help="list the Gemini models your API key can use")
    lm.set_defaults(func=cmd_llm_models)
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
    except (Mt5Error, CredentialsError, ReportError, news.CalendarError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # the Wine container runs `python -m tradequery.cli`
    sys.exit(main())
