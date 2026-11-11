"""Derived layers stored for querying: per-trade facts, streaks, drawdown episodes, exposure, equity.

The query layer (spec.py / compiler.py) can only filter, group and sort what is
stored, so everything computed by metrics, pricepath, exposure, sequences and
news is written here on every rebuild. Nothing in this module calculates a new
kind of number; it gathers what the other modules already compute.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from . import exposure as exposure_mod
from . import metrics, news, pricepath
from .models import Bar, CashFlow, Trade
from .settings import Settings

EQUITY_EPISODES_KEPT = 50  # an equity curve has many tiny dips; keep the deepest


def canonical_symbol(symbol: str, aliases: dict[str, str] | None = None) -> str:
    """One name for broker variants: XAUUSD-STDc, XAUUSD.m and XAUUSDm are all XAUUSD.

    An alias in settings.toml [symbols.aliases] wins; otherwise a leading
    six-letter pair is kept, else the part before the first '.', '-', '_' or space.
    """
    if aliases and symbol in aliases:
        return aliases[symbol]
    pair = re.match(r"^([A-Z]{6})", symbol)
    if pair:
        return pair.group(1)
    return re.split(r"[.\-_ ]", symbol, maxsplit=1)[0].upper() or symbol


@dataclass(frozen=True)
class TradeFact:
    position_id: int
    canonical_symbol: str
    month: str  # YYYY-MM of the close, broker server time
    mae: float | None
    mfe: float | None
    minutes_in_profit: int | None
    minutes_in_loss: int | None
    spread_at_open: int | None
    risk_amount: float | None
    planned_rr: float | None
    r_multiple: float | None
    sequence_id: int | None
    streak_id: int | None
    drawdown_episode_id: int | None  # the closed-trade drawdown it fell in, peak to trough
    news_overlap: bool
    equity_drawdown_episode_id: int | None = None  # the equity drawdown it closed in, peak to trough


@dataclass(frozen=True)
class StreakRow:
    streak_id: int
    type: str
    length: int
    start_utc: datetime
    end_utc: datetime
    net_pnl: float


@dataclass(frozen=True)
class EpisodeRow:
    episode_id: int
    basis: str  # balance (closed trades) or equity (with floating P&L)
    peak_time_utc: datetime | None
    trough_time_utc: datetime
    recovery_time_utc: datetime | None
    peak_value: float
    trough_value: float
    depth_abs: float
    depth_pct: float | None
    duration_days: float
    trade_count: int
    news_loss_share: float | None
    added_capital: float


@dataclass
class Facts:
    trade_facts: list[TradeFact]
    streaks: list[StreakRow]
    episodes: list[EpisodeRow]
    exposure: list[exposure_mod.ExposureEvent]
    equity: list[pricepath.EquityPoint]
    baskets: list[pricepath.BasketDrawdown] = field(default_factory=list)  # needs one-minute bars


def _episode_rows(episodes, basis, start_id, trades, by_pid, links, first_time, last_time) -> list[EpisodeRow]:
    rows = []
    for i, ep in enumerate(episodes):
        if basis == "balance":
            members = list(ep.position_ids)
        else:  # equity: the trades closed between the peak and the trough
            members = [t.position_id for t in trades
                       if (ep.peak_time is None or t.close_time_utc > ep.peak_time) and t.close_time_utc <= ep.trough_time]
        share, _ = news.news_loss_share(members, by_pid, links)
        start = ep.peak_time or first_time
        end = ep.recovery_time or last_time
        rows.append(EpisodeRow(
            start_id + i, basis, ep.peak_time, ep.trough_time, ep.recovery_time, ep.peak_value, ep.trough_value,
            ep.depth, ep.depth_pct, round((end - start).total_seconds() / 86400, 3), len(members), share,
            ep.added_capital,
        ))
    return rows


def build(
    trades: list[Trade],
    cash_flows: list[CashFlow],
    settings: Settings,
    *,
    bars_by_symbol: dict[str, list[Bar]] | None = None,
    symbol_specs: dict[str, dict] | None = None,
    sequences=(),
    links=(),
    opening_balance: float = 0.0,
    aliases: dict[str, str] | None = None,
) -> Facts:
    specs = symbol_specs or {}
    bars = bars_by_symbol or {}
    by_pid = {t.position_id: t for t in trades}
    links = list(links)
    if not trades:
        return Facts([], [], [], [], [])
    first_time = min(t.open_time_utc for t in trades)
    last_time = max(t.close_time_utc for t in trades)

    paths = pricepath.price_paths(trades, bars, specs)
    per_unit = {s: metrics.value_per_price_unit([t for t in trades if t.symbol == s], specs.get(s))
                for s in {t.symbol for t in trades}}
    sequence_of = {pid: s.sequence_id for s in sequences for pid in s.position_ids}
    news_hit = {l.position_id for l in links}

    runs = metrics.streaks(trades, settings.breakeven_breaks_streak)
    streak_rows = [StreakRow(i, r.kind, r.length, r.start, r.end, r.net) for i, r in enumerate(runs, start=1)]
    streak_of = {pid: row.streak_id for row, r in zip(streak_rows, runs) for pid in r.position_ids}

    balance_eps = metrics.drawdown_episodes(trades, cash_flows)
    episodes = _episode_rows(balance_eps, "balance", 1, trades, by_pid, links, first_time, last_time)
    episode_of = {}
    for row, ep in zip(episodes, balance_eps):
        for pid in ep.position_ids:
            episode_of.setdefault(pid, row.episode_id)

    curve = pricepath.equity_curve(trades, cash_flows, bars, opening_balance=opening_balance,
                                   symbol_specs=specs) if paths else []
    if curve:
        deepest = sorted((e for e in pricepath.equity_drawdowns(curve, cash_flows) if e.depth > 0),
                         key=lambda e: -e.depth)[:EQUITY_EPISODES_KEPT]
        deepest.sort(key=lambda e: e.trough_time)
        equity_rows = _episode_rows(deepest, "equity", len(episodes) + 1, trades, by_pid, links, first_time, last_time)
        episodes += equity_rows
    else:
        equity_rows = []
    # the same members _episode_rows counts: trades closed after the peak, up to the trough
    equity_of = {}
    for row in equity_rows:
        for t in trades:
            if (row.peak_time_utc is None or t.close_time_utc > row.peak_time_utc) and t.close_time_utc <= row.trough_time_utc:
                equity_of.setdefault(t.position_id, row.episode_id)

    facts = []
    for t in trades:
        path = paths.get(t.position_id)
        risk = metrics.risk_amount(t, per_unit[t.symbol])
        facts.append(TradeFact(
            position_id=t.position_id,
            canonical_symbol=canonical_symbol(t.symbol, aliases),
            month=settings.clock.from_utc(t.close_time_utc).strftime("%Y-%m"),
            mae=path.mae if path else None,
            mfe=path.mfe if path else None,
            minutes_in_profit=path.minutes_in_profit if path else None,
            minutes_in_loss=path.minutes_in_loss if path else None,
            spread_at_open=path.spread_at_open if path else None,
            risk_amount=round(risk, 2) if risk else None,
            planned_rr=metrics.planned_rr(t),
            r_multiple=metrics.r_multiple(t, per_unit[t.symbol]),
            sequence_id=sequence_of.get(t.position_id),
            streak_id=streak_of.get(t.position_id),
            drawdown_episode_id=episode_of.get(t.position_id),
            news_overlap=t.position_id in news_hit,
            equity_drawdown_episode_id=equity_of.get(t.position_id),
        ))
    baskets = pricepath.basket_drawdowns(
        sequences, trades, bars, symbol_specs=specs, covered=set(paths), cash_flows=cash_flows, curve=curve,
        opening_balance=opening_balance) if paths else []
    return Facts(facts, streak_rows, episodes, exposure_mod.exposure_events(trades), curve, baskets)
