"""Tests for historical endpoint contract construction and bar passthrough."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from xenon.api.routes.historical import (
    ContractSpec,
    HistoricalBarsRequest,
    historical_bars,
    make_ib_contract,
)


@pytest.mark.parametrize("pair", ["EURUSD", "USDJPY", "GBPUSD", "USDCAD", "USDSEK", "USDCHF"])
def test_cash_builds_idealpro_forex(pair):
    c = make_ib_contract(ContractSpec(sec_type="CASH", symbol=pair[:3], currency=pair[3:]))
    assert (c.secType, c.symbol, c.currency, c.exchange) == ("CASH", pair[:3], pair[3:], "IDEALPRO")


def test_cash_keeps_explicit_exchange():
    c = make_ib_contract(ContractSpec(sec_type="CASH", symbol="EUR", currency="USD", exchange="IDEALPRO"))
    assert c.exchange == "IDEALPRO"


def test_cmdty_builds_commodity():
    c = make_ib_contract(ContractSpec(sec_type="CMDTY", symbol="XAUUSD"))
    assert (c.secType, c.symbol, c.currency, c.exchange) == ("CMDTY", "XAUUSD", "USD", "SMART")


def test_fx_midpoint_request_passes_through_unchanged():
    calls = []

    class FakePool:
        @asynccontextmanager
        async def acquire(self, role):
            yield SimpleNamespace(qualify_contracts="qualify", get_historical_data="hist")

        async def run_sync(self, role, fn, *args, **kwargs):
            calls.append((fn, args, kwargs))
            if fn == "qualify":
                return list(args)
            # IB returns volume=-1 for MIDPOINT bars.
            return [SimpleNamespace(date="20261009", open=1.1, high=1.2, low=1.0, close=1.15, volume=-1)]

    req = HistoricalBarsRequest(
        contract=ContractSpec(sec_type="CASH", symbol="EUR", currency="USD", exchange="IDEALPRO"),
        duration="2 Y",
        bar_size="1 day",
        what_to_show="MIDPOINT",
        use_rth=False,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ib_pool=FakePool())))

    out = asyncio.run(historical_bars(req, request))

    fn, args, kwargs = calls[-1]
    assert fn == "hist"
    assert args[0].secType == "CASH"
    assert kwargs == {
        "end_date": "",
        "duration": "2 Y",
        "bar_size": "1 day",
        "what_to_show": "MIDPOINT",
        "use_rth": False,
    }
    assert out["bars"] == [{"date": "2026-10-09", "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.15, "volume": -1}]
