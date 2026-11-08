"""A stand-in for the MetaTrader5 module, with the real library's constant values."""

import calendar
from types import SimpleNamespace as NS

from tradequery.mt5 import SYMBOL_SPEC_FIELDS


def epoch(year, month, day, hour, minute=0):
    """What the real library returns: SERVER time encoded as UTC epoch seconds."""
    return calendar.timegm((year, month, day, hour, minute, 0))


def raw_deal(**overrides):
    fields = dict(
        ticket=1, order=0, time=epoch(2026, 7, 15, 12), type=0, entry=0, magic=0, position_id=0,
        reason=0, volume=0.0, price=0.0, commission=0.0, swap=0.0, profit=0.0, fee=0.0,
        symbol="", comment="",
    )
    fields.update(overrides)
    return NS(**fields)


def raw_account(**overrides):
    fields = dict(
        login=12345, name="Test Name", server="Srv-Live", company="Test Broker", currency="USD",
        leverage=500, balance=1000.5, equity=1010.0, margin_mode=2, trade_mode=2, trade_allowed=False,
    )
    fields.update(overrides)
    return NS(**fields)


def raw_symbol(name, **overrides):
    fields = {field: 1 for field in SYMBOL_SPEC_FIELDS}
    fields.update(name=name, **overrides)
    return NS(**fields)


class FakeMt5:
    DEAL_TYPE_BUY, DEAL_TYPE_SELL, DEAL_TYPE_BALANCE, DEAL_TYPE_CREDIT = 0, 1, 2, 3
    DEAL_TYPE_CHARGE, DEAL_TYPE_CORRECTION, DEAL_TYPE_BONUS, DEAL_TYPE_COMMISSION = 4, 5, 6, 7
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY = 0, 1, 2, 3
    DEAL_REASON_CLIENT, DEAL_REASON_MOBILE, DEAL_REASON_WEB, DEAL_REASON_EXPERT = 0, 1, 2, 3
    DEAL_REASON_SL, DEAL_REASON_TP, DEAL_REASON_SO = 4, 5, 6
    ACCOUNT_MARGIN_MODE_RETAIL_NETTING, ACCOUNT_MARGIN_MODE_EXCHANGE = 0, 1
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
    ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_CONTEST, ACCOUNT_TRADE_MODE_REAL = 0, 1, 2

    TIMEFRAME_M1 = 1

    def __init__(self, *, login_ok=True, deals=(), orders=(), account=None, symbols=(), hidden=(), rates=None):
        self.rates = rates or {}  # symbol -> list of dicts like the library's structured array rows
        self.rate_requests = []
        self.login_ok = login_ok
        self.deals = None if deals is None else list(deals)
        self.orders = list(orders)
        self.account = account
        self.symbols = {s.name: s for s in symbols}
        self.hidden = {s.name: s for s in hidden}
        self.init_kwargs = None
        self.shut_down = False

    def initialize(self, **kwargs):
        self.init_kwargs = kwargs
        return self.login_ok

    def last_error(self):
        return (-6, "Authorization failed")

    def shutdown(self):
        self.shut_down = True

    def account_info(self):
        return self.account

    def history_deals_get(self, date_from, date_to):
        return self.deals

    def history_orders_get(self, date_from, date_to):
        return self.orders

    def symbol_info(self, name):
        return self.symbols.get(name)

    def symbol_select(self, name, enable):
        if name in self.hidden:
            self.symbols[name] = self.hidden.pop(name)
            return True
        return False

    def symbols_get(self):
        return list(self.symbols.values())

    def copy_rates_range(self, symbol, timeframe, date_from, date_to):
        """Like the library: from/to are server time encoded as UTC; rows carry server epochs."""
        self.rate_requests.append((symbol, timeframe, date_from, date_to))
        lo, hi = date_from.timestamp(), date_to.timestamp()
        return [r for r in self.rates.get(symbol, []) if lo <= r["time"] <= hi]
