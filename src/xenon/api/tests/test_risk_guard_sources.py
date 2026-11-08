"""Risk guard sources: freshness, skew gate, feed health, agreement (spec 2026-11-08 §5.1).
Prices are synthetic."""

from datetime import datetime, timedelta, timezone

import pytest

from xenon.api.services.risk_guard.sources import Quote, SourceState, agree, feed_health

T0 = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)


def _q(price, t, close=100.0, live=True):
    return Quote(price=price, prior_close=close, source_time=t, live=live)


def _feed(src: SourceState, start_mono: float, prices, step=1.0, session="rth"):
    for i, p in enumerate(prices):
        src.observe(
            start_mono + i * step, T0 + timedelta(seconds=i * step), _q(p, T0 + timedelta(seconds=i * step)), session
        )
    return start_mono + (len(prices) - 1) * step


def test_new_tick_updates_window_and_freshness():
    s = SourceState("SPX")
    last = _feed(s, 1000.0, [100.0, 99.0])
    assert s.fresh(last) and s.last == 99.0 and s.prior_close == 100.0
    assert not s.fresh(last + 21)
    assert s.age(last + 5) == pytest.approx(5)


def test_same_source_time_is_not_a_new_tick():
    s = SourceState("SPX")
    s.observe(1000.0, T0, _q(100.0, T0), "rth")
    s.observe(1030.0, T0 + timedelta(seconds=30), _q(100.0, T0), "rth")  # frozen ticker
    assert not s.fresh(1030.0)


def test_unchanged_print_with_new_time_is_valid():
    s = SourceState("SPX")
    s.observe(1000.0, T0, _q(100.0, T0), "rth")
    s.observe(1010.0, T0 + timedelta(seconds=10), _q(100.0, T0 + timedelta(seconds=10)), "rth")
    assert s.fresh(1025.0)


def test_skewed_source_time_rejected():
    s = SourceState("SPX")
    s.observe(1000.0, T0, _q(100.0, T0 - timedelta(seconds=6)), "rth")
    assert s.last is None and s.rejected == 1
    s.observe(1001.0, T0, _q(100.0, T0 + timedelta(seconds=6)), "rth")
    assert s.rejected == 2


def test_delayed_or_missing_quote_ignored():
    s = SourceState("SPX")
    s.observe(1000.0, T0, _q(100.0, T0, live=False), "rth")
    s.observe(1000.0, T0, None, "rth")
    s.observe(1000.0, T0, _q(None, T0), "rth")
    s.observe(1000.0, T0, _q(100.0, None), "rth")
    assert s.last is None


def _pair(spx_prices, spy_prices):
    spx, spy = SourceState("SPX"), SourceState("SPY")
    a = _feed(spx, 1000.0, spx_prices)
    _feed(spy, 1000.0, spy_prices)
    return spx, spy, a


def test_feed_stale_when_every_source_stale_in_rth():
    spx, spy, last = _pair([100.0] * 10, [50.0] * 10)
    causes = feed_health([spx, spy], last + 25, rth=True)
    assert set(causes) == {"FEED_STALE"}


def test_one_stale_source_is_source_stale():
    spx, spy, last = _pair([100.0] * 30, [50.0] * 5)
    causes = feed_health([spx, spy], last, rth=True)
    assert set(causes) == {"SOURCE_STALE"} and "SPY" in causes["SOURCE_STALE"]


def test_no_stale_alarm_outside_rth():
    spx, spy, last = _pair([100.0] * 10, [50.0] * 10)
    assert feed_health([spx, spy], last + 100, rth=False) == {}


def test_divergence_on_5m_returns():
    spx, spy, last = _pair([100.0 - i * 0.01 for i in range(301)], [50.0] * 301)  # -3% vs 0%
    causes = feed_health([spx, spy], last, rth=True)
    assert "FEED_DIVERGENCE" in causes
    spx2, spy2, last2 = _pair([100.0 - i * 0.0008 for i in range(301)], [50.0] * 301)  # -0.24 pp
    assert "FEED_DIVERGENCE" not in feed_health([spx2, spy2], last2, rth=True)


def test_agree_takes_milder_value_and_needs_both():
    assert agree([-0.031, -0.035], blind=False) == -0.031
    assert agree([-0.031, None], blind=False) is None
    assert agree([-0.031, None], blind=True) == -0.031
    assert agree([None, None], blind=True) is None
    assert agree([-0.031], blind=False) is None
    assert agree([-0.031], blind=True) == -0.031


# ── IBQuoteFeed (fake ib_async surface) ───────────────────────────────


class _Ticker:
    def __init__(self, last, close=100.0, t=T0, md=1):
        self.last, self.close, self.time, self.marketDataType = last, close, t, md


class _IB:
    def __init__(self):
        self.req, self.tickers, self.cancelled = [], {}, []

    def cancelMktData(self, contract):
        self.cancelled.append(contract.symbol)
        self.tickers.pop(contract.symbol, None)

    def ticker(self, contract):
        return self.tickers.get(contract.symbol)

    def qualifyContracts(self, c):
        return [c]

    def reqMktData(self, contract, generic, snapshot, regulatory):
        assert snapshot is False
        self.req.append(contract.symbol)
        t = _Ticker(float("nan") if contract.symbol == "SPY" else 7600.0, md=3 if contract.symbol == "SPY" else 1)
        self.tickers[contract.symbol] = t
        return t


class _Client:
    def __init__(self):
        self.ib = _IB()


def test_ib_feed_subscribes_once_and_maps_fields():
    from xenon.api.services.risk_guard.sources import IBQuoteFeed

    feed, c = IBQuoteFeed(pool=None), _Client()
    out = feed._read(c)
    assert out["SPX"] == Quote(7600.0, 100.0, T0, True)
    assert out["SPY"].price is None and out["SPY"].live is False  # NaN last, delayed data
    feed._read(c)
    assert c.ib.req == ["SPX", "SPY"]


def test_ib_feed_resubscribes_on_new_client_or_dropped_ticker():
    from xenon.api.services.risk_guard.sources import IBQuoteFeed

    feed, c1 = IBQuoteFeed(pool=None), _Client()
    feed._read(c1)
    c1.ib.tickers.pop("SPX")  # IB dropped the line
    feed._read(c1)
    assert c1.ib.req == ["SPX", "SPY", "SPX"]
    c2 = _Client()
    feed._read(c2)
    assert c2.ib.req == ["SPX", "SPY"]


def test_ib_feed_extra_indexes_one_new_per_read_and_cancel_on_removal():
    from xenon.api.services.risk_guard.sources import IBQuoteFeed

    feed, c = IBQuoteFeed(pool=None), _Client()
    out = feed._read(c, ("NDX", "RUT"))
    assert c.ib.req == ["SPX", "SPY", "NDX"] and "RUT" not in out  # one new extra per read
    out = feed._read(c, ("NDX", "RUT"))
    assert c.ib.req == ["SPX", "SPY", "NDX", "RUT"] and set(out) == {"SPX", "SPY", "NDX", "RUT"}
    assert (feed._contracts["NDX"].exchange, feed._contracts["RUT"].exchange) == ("NASDAQ", "RUSSELL")
    feed._read(c, ("RUT",))
    assert c.ib.cancelled == ["NDX"] and "NDX" not in feed._contracts


def test_ib_feed_resubscribes_once_after_data_lost_1101():
    from xenon.api.services.risk_guard.sources import IBQuoteFeed

    feed, c = IBQuoteFeed(pool=None), _Client()
    feed._read(c)
    c.readiness_code, c.readiness_event_at = 1101, 1000.0  # socket kept, market data lost
    feed._read(c)
    feed._read(c)
    assert c.ib.cancelled == ["SPX", "SPY"] and c.ib.req == ["SPX", "SPY", "SPX", "SPY"]


@pytest.mark.asyncio
async def test_ib_feed_retries_a_down_data_role_at_most_every_30s(monkeypatch):
    """The pool reconnects only inside acquire: a down role gets one bounded
    attempt per RECONNECT_EVERY_S, else the guard stays blind after a restart."""
    import xenon.api.ib_pool as ib_pool
    from xenon.api.services.risk_guard import sources

    calls = []

    async def fake_call(pool, fn, *args, timeout=None):
        calls.append(timeout)
        raise ConnectionError("IB pool: data is not connected")

    class _Pool:
        def is_connected(self, role):
            return False

    clock = [1000.0]
    monkeypatch.setattr(ib_pool, "call_data_bounded", fake_call)
    monkeypatch.setattr(sources.time, "monotonic", lambda: clock[0])
    feed = sources.IBQuoteFeed(_Pool())
    assert await feed.read() == {} and feed.last_error == "ConnectionError"
    clock[0] += 10
    assert await feed.read() == {} and feed.last_error == "data role not connected"
    clock[0] += 25
    assert await feed.read() == {}
    assert calls == [3.0, 3.0]
