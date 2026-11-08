"""Read account, symbols and history from a running MT5 terminal.

Needs a Windows 64-bit Python the MetaTrader5 package supports (3.8 to 3.13) and
the MT5 terminal. In the Docker setup both live inside Wine (docker/mt5/). Log in with the INVESTOR
(read-only) password. Everything here only reads.

Conversions that matter:
- Times from the library are server time encoded as UTC epoch seconds; they go
  through ServerClock.from_mt5_epoch.
- The exchange ``fee`` is folded into ``commission``.
- For balance operations (deposit, withdrawal, credit, ...) the signed amount is
  stored in ``profit``; for commission/interest/tax type deals it is the sum of
  profit, commission and swap.
- ``sl`` and ``tp`` of an entry deal come from its opening order, so they are
  the stops at entry (0 if added later).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .settings import Mt5Credentials
from .models import Deal
from .timeutil import ServerClock

WIDE_START = datetime(2000, 1, 1, tzinfo=timezone.utc)

_DEAL_TYPES = (
    "BUY SELL BALANCE CREDIT CHARGE CORRECTION BONUS COMMISSION COMMISSION_DAILY COMMISSION_MONTHLY "
    "COMMISSION_AGENT_DAILY COMMISSION_AGENT_MONTHLY INTEREST BUY_CANCELED SELL_CANCELED DIVIDEND "
    "DIVIDEND_FRANKED TAX"
).split()
_DEAL_ENTRIES = "IN OUT INOUT OUT_BY".split()
_DEAL_REASONS = "CLIENT MOBILE WEB EXPERT SL TP SO ROLLOVER VMARGIN SPLIT".split()

SYMBOL_SPEC_FIELDS = (
    "name description path currency_base currency_profit currency_margin digits point "
    "trade_contract_size trade_tick_size trade_tick_value swap_long swap_short swap_mode "
    "swap_rollover3days volume_min volume_max volume_step"
).split()


class Mt5Error(RuntimeError):
    """Something went wrong talking to MT5."""


def _names(mt5, prefix: str, names: list[str]) -> dict[int, str]:
    """Map the library's numeric constants (e.g. DEAL_TYPE_BUY) to lower-case names."""
    return {getattr(mt5, prefix + n): n.lower() for n in names if hasattr(mt5, prefix + n)}


def import_mt5():
    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        raise Mt5Error(
            "The MetaTrader5 package is not installed. It only works inside Windows Python "
            "(in this project: the Wine container). Run the sync with: make sync"
        ) from exc
    return mt5


def connect(credentials: Mt5Credentials, mt5=None):
    """Start or attach to the terminal and log in. Returns the mt5 module."""
    mt5 = mt5 or import_mt5()
    options = dict(
        login=credentials.login,
        password=credentials.password,
        server=credentials.server,
        timeout=60000,
    )
    if credentials.path:
        options["path"] = credentials.path
    if not mt5.initialize(**options):
        raise Mt5Error(f"MT5 login failed: {mt5.last_error()}")
    return mt5


def disconnect(mt5) -> None:
    mt5.shutdown()


def fetch_account(mt5) -> dict:
    info = mt5.account_info()
    if info is None:
        raise Mt5Error(f"could not read the account: {mt5.last_error()}")
    margin_modes = _names(mt5, "ACCOUNT_MARGIN_MODE_", ["RETAIL_NETTING", "EXCHANGE", "RETAIL_HEDGING"])
    trade_modes = _names(mt5, "ACCOUNT_TRADE_MODE_", ["DEMO", "CONTEST", "REAL"])
    return {
        "login": int(info.login),
        "name": info.name,
        "server": info.server,
        "company": info.company,
        "currency": info.currency,
        "leverage": info.leverage,
        "balance": float(info.balance),
        "equity": float(info.equity),
        "margin_mode": margin_modes.get(info.margin_mode, str(info.margin_mode)),
        "trade_mode": trade_modes.get(info.trade_mode, str(info.trade_mode)),
        "trade_allowed": bool(info.trade_allowed),  # False for an investor (read-only) login
    }


def fetch_deals(
    mt5, clock: ServerClock, date_from: datetime | None = None, date_to: datetime | None = None
) -> list[Deal]:
    """Every deal in the date range (default: all history), oldest first."""
    date_from = date_from or WIDE_START
    date_to = date_to or datetime.now(timezone.utc) + timedelta(days=2)

    raw_deals = mt5.history_deals_get(date_from, date_to)
    if raw_deals is None:
        raise Mt5Error(f"could not read the deal history: {mt5.last_error()}")
    stops = {o.ticket: (float(o.sl), float(o.tp)) for o in (mt5.history_orders_get(date_from, date_to) or [])}

    type_names = _names(mt5, "DEAL_TYPE_", _DEAL_TYPES)
    entry_names = _names(mt5, "DEAL_ENTRY_", _DEAL_ENTRIES)
    reason_names = _names(mt5, "DEAL_REASON_", _DEAL_REASONS)

    deals = []
    for raw in raw_deals:
        kind = type_names.get(raw.type, f"type_{raw.type}")
        commission = float(raw.commission) + float(getattr(raw, "fee", 0.0))
        time_utc = clock.from_mt5_epoch(raw.time)
        if kind in ("buy", "sell"):
            entry = entry_names.get(raw.entry, f"entry_{raw.entry}")
            sl, tp = stops.get(raw.order, (0.0, 0.0)) if entry == "in" else (0.0, 0.0)
            deals.append(
                Deal(
                    ticket=int(raw.ticket),
                    position_id=int(raw.position_id),
                    time_utc=time_utc,
                    type=kind,
                    entry=entry,
                    symbol=raw.symbol,
                    volume=float(raw.volume),
                    price=float(raw.price),
                    commission=commission,
                    swap=float(raw.swap),
                    profit=float(raw.profit),
                    magic=int(raw.magic),
                    reason=reason_names.get(raw.reason, f"reason_{raw.reason}"),
                    comment=raw.comment,
                    sl=sl,
                    tp=tp,
                )
            )
        else:
            deals.append(
                Deal(
                    ticket=int(raw.ticket),
                    position_id=0,
                    time_utc=time_utc,
                    type=kind,
                    entry="",
                    symbol=raw.symbol or "",
                    volume=0.0,
                    price=0.0,
                    commission=0.0,
                    swap=0.0,
                    profit=float(raw.profit) + commission + float(raw.swap),
                    comment=raw.comment,
                )
            )
    deals.sort(key=lambda d: (d.time_utc, d.ticket))
    return deals


def fetch_symbol_specs(mt5, names) -> list[dict]:
    """Contract specs for the named symbols. Unknown names get an "error" entry."""
    specs = []
    for name in sorted(set(names)):
        info = mt5.symbol_info(name)
        if info is None:
            mt5.symbol_select(name, True)  # adds it to Market Watch so the terminal loads it
            info = mt5.symbol_info(name)
        if info is None:
            specs.append({"name": name, "error": "symbol not found on this server"})
        else:
            specs.append({field: getattr(info, field, None) for field in SYMBOL_SPEC_FIELDS})
    return specs


def list_symbol_names(mt5) -> list[str]:
    symbols = mt5.symbols_get()
    return sorted(s.name for s in symbols) if symbols else []
