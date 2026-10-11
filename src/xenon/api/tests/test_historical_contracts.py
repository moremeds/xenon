"""Tests for historical endpoint contract construction and bar passthrough."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from xenon.api.routes.historical import (
    ContractSpec,
    HistoricalBarsRequest,
    QualifyRequest,
    historical_bars,
    make_ib_contract,
    qualify_contracts,
)


class FakePool:
    """Mimics ib_async: a known contract gets its conId filled in place; an
    unknown or ambiguous one comes back as None and keeps conId=0."""

    def __init__(self, known=True):
        self.known = known
        self.calls = []

    @asynccontextmanager
    async def acquire(self, role):
        yield SimpleNamespace(qualify_contracts="qualify", get_historical_data="hist")

    async def run_sync(self, role, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))
        if fn == "qualify":
            if not self.known:
                return [None for _ in args]
            for c in args:
                c.conId = 12087792
            return list(args)
        # IB returns volume=-1 for MIDPOINT bars.
        return [SimpleNamespace(date="20261009", open=1.1, high=1.2, low=1.0, close=1.15, volume=-1)]


def _request(pool):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ib_pool=pool)))


@pytest.mark.parametrize("pair", ["EURUSD", "USDJPY", "GBPUSD", "USDCAD", "USDSEK", "USDCHF"])
def test_cash_builds_idealpro_forex(pair):
    c = make_ib_contract(ContractSpec(sec_type="CASH", symbol=pair[:3], currency=pair[3:]))
    assert (c.secType, c.symbol, c.currency, c.exchange) == ("CASH", pair[:3], pair[3:], "IDEALPRO")


@pytest.mark.parametrize(("given", "expected"), [("SMART", "IDEALPRO"), ("FXCONV", "FXCONV")])
def test_cash_exchange_smart_means_idealpro_else_passthrough(given, expected):
    c = make_ib_contract(ContractSpec(sec_type="CASH", symbol="EUR", currency="USD", exchange=given))
    assert c.exchange == expected


def test_cmdty_builds_commodity():
    c = make_ib_contract(ContractSpec(sec_type="CMDTY", symbol="XAUUSD"))
    assert (c.secType, c.symbol, c.currency, c.exchange) == ("CMDTY", "XAUUSD", "USD", "SMART")


def test_fx_midpoint_request_passes_through_unchanged():
    pool = FakePool()
    req = HistoricalBarsRequest(
        contract=ContractSpec(sec_type="CASH", symbol="EUR", currency="USD", exchange="IDEALPRO"),
        duration="2 Y",
        bar_size="1 day",
        what_to_show="MIDPOINT",
        use_rth=False,
    )

    out = asyncio.run(historical_bars(req, _request(pool)))

    fn, args, kwargs = pool.calls[-1]
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


def test_bars_unknown_contract_is_404_not_empty_200():
    pool = FakePool(known=False)
    req = HistoricalBarsRequest(contract=ContractSpec(sec_type="CMDTY", symbol="WTI"))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(historical_bars(req, _request(pool)))

    assert exc.value.status_code == 404
    assert "CMDTY WTI" in exc.value.detail
    assert [fn for fn, _, _ in pool.calls] == ["qualify"]  # never asks IB for bars


def test_qualify_unknown_contract_is_404_naming_it():
    req = QualifyRequest(contracts=[ContractSpec(sec_type="CMDTY", symbol="WTI")])

    with pytest.raises(HTTPException) as exc:
        asyncio.run(qualify_contracts(req, _request(FakePool(known=False))))

    assert exc.value.status_code == 404
    assert "CMDTY WTI" in exc.value.detail


def test_qualify_known_contract_returns_con_id():
    req = QualifyRequest(contracts=[ContractSpec(sec_type="CASH", symbol="EUR", currency="USD")])

    out = asyncio.run(qualify_contracts(req, _request(FakePool())))

    assert out["contracts"][0]["conId"] == 12087792


def test_bars_ib_timeout_is_504_not_empty_200(monkeypatch):
    """ib_async cancels a request that runs past its timeout and returns []."""
    import xenon.api.routes.historical as hist

    ticks = iter([0.0, hist._IB_HIST_TIMEOUT_S])
    monkeypatch.setattr(hist, "time", SimpleNamespace(monotonic=lambda: next(ticks)))

    class TimingOutPool(FakePool):
        async def run_sync(self, role, fn, *args, **kwargs):
            out = await super().run_sync(role, fn, *args, **kwargs)
            return [] if fn == "hist" else out

    req = HistoricalBarsRequest(contract=ContractSpec(sec_type="CASH", symbol="USD", currency="CAD"), duration="17 Y")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(historical_bars(req, _request(TimingOutPool())))

    assert exc.value.status_code == 504
    assert "17 Y" in exc.value.detail


def test_fut_includes_expired_so_recent_legs_resolve():
    c = make_ib_contract(ContractSpec(sec_type="FUT", symbol="GC", exchange="COMEX", last_trade_date="202512"))
    assert (c.secType, c.symbol, c.exchange, c.lastTradeDateOrContractMonth) == ("FUT", "GC", "COMEX", "202512")
    assert c.includeExpired is True


def test_contfut_builds_continuous_future():
    c = make_ib_contract(ContractSpec(sec_type="CONTFUT", symbol="GC", exchange="COMEX"))
    assert (c.secType, c.symbol, c.exchange, c.currency) == ("CONTFUT", "GC", "COMEX", "USD")


@pytest.mark.parametrize("sec_type", ["FUT", "CONTFUT"])
@pytest.mark.parametrize("exchange", ["SMART", ""])
def test_futures_without_exchange_is_422_before_ib(sec_type, exchange):
    """IB never answers a SMART futures lookup, and "" is ambiguous (GC: COMEX + QBALGO)."""
    pool = FakePool()
    spec = ContractSpec(sec_type=sec_type, symbol="GC", exchange=exchange, last_trade_date="202612")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(historical_bars(HistoricalBarsRequest(contract=spec), _request(pool)))

    assert exc.value.status_code == 422
    assert "COMEX" in exc.value.detail
    assert pool.calls == []


def test_contfut_with_end_date_is_422_before_ib():
    """IB rejects it with error 10339 and ib_async returns [] at once: an empty 200."""
    pool = FakePool()
    req = HistoricalBarsRequest(
        contract=ContractSpec(sec_type="CONTFUT", symbol="GC", exchange="COMEX"), end_date_time="20200101-00:00:00"
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(historical_bars(req, _request(pool)))

    assert exc.value.status_code == 422
    assert "end_date_time" in exc.value.detail
    assert pool.calls == []


def test_contfut_bars_pass_through():
    pool = FakePool()
    req = HistoricalBarsRequest(
        contract=ContractSpec(sec_type="CONTFUT", symbol="GC", exchange="COMEX"), duration="10 Y"
    )

    out = asyncio.run(historical_bars(req, _request(pool)))

    assert [fn for fn, _, _ in pool.calls] == ["qualify", "hist"]
    assert pool.calls[-1][1][0].secType == "CONTFUT"
    assert len(out["bars"]) == 1
