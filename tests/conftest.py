from datetime import datetime, timezone

import pytest

from tradequery.models import Deal, Trade

UTC = timezone.utc


@pytest.fixture
def make_deal():
    """Build a Deal with sensible defaults; override any field by keyword."""

    def make(**overrides):
        fields = dict(
            ticket=1,
            position_id=0,
            time_utc=datetime(2026, 7, 15, 9, 0, tzinfo=UTC),
            type="buy",
            entry="in",
            symbol="XAUUSD",
            volume=0.1,
            price=1.0,
            commission=0.0,
            swap=0.0,
            profit=0.0,
        )
        fields.update(overrides)
        return Deal(**fields)

    return make


@pytest.fixture
def make_trade():
    """Build a Trade with sensible defaults; override any field by keyword."""

    def make(**overrides):
        fields = dict(
            position_id=1,
            symbol="XAUUSD",
            side="buy",
            lots=0.1,
            magic=0,
            comment="",
            open_time_utc=datetime(2026, 7, 15, 9, 0, tzinfo=UTC),
            close_time_utc=datetime(2026, 7, 15, 10, 0, tzinfo=UTC),
            open_price=100.0,
            close_price=101.0,
            sl=0.0,
            tp=0.0,
            close_reason="",
            commission=0.0,
            swap=0.0,
            profit=0.0,
        )
        fields.update(overrides)
        return Trade(**fields)

    return make
