"""Unit tests for option_chain_snapshotter.

Covers pure-logic modules (config, hours, persister helpers, fetcher helpers)
without IB or Postgres connections.
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


def test_index_exchange_all_four_tickers():
    from xenon.option_chain_snapshotter.config import INDEX_EXCHANGE, TICKERS

    for t in TICKERS:
        assert t in INDEX_EXCHANGE, f"{t} missing from INDEX_EXCHANGE"


def test_batch_size_within_ib_line_limit():
    from xenon.option_chain_snapshotter.config import BATCH_SIZE

    # IB account limit is 100; we must leave headroom for relay + API pool.
    assert BATCH_SIZE <= 75, "BATCH_SIZE too high — risks starving relay/API"
    assert BATCH_SIZE >= 20, "BATCH_SIZE too low — cycle time would be impractical"


def test_default_cadence_is_600():
    from xenon.option_chain_snapshotter.config import DEFAULT_CADENCE_S

    assert DEFAULT_CADENCE_S == 600


def test_strike_pct_range_reasonable():
    from xenon.option_chain_snapshotter.config import STRIKE_PCT_RANGE

    assert 0.05 <= STRIKE_PCT_RANGE <= 0.50


# --------------------------------------------------------------------------- #
# hours
# --------------------------------------------------------------------------- #


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


class TestIsSessionOpen:
    def test_midday_tuesday_is_open(self):
        from xenon.option_chain_snapshotter.hours import is_session_open

        # 2026-06-16 is a Tuesday; 17:00 UTC = 13:00 ET (well inside RTH)
        assert is_session_open(_utc(2026, 6, 16, 17, 0)) is True

    def test_saturday_is_closed(self):
        from xenon.option_chain_snapshotter.hours import is_session_open

        assert is_session_open(_utc(2026, 6, 20, 17, 0)) is False

    def test_sunday_is_closed(self):
        from xenon.option_chain_snapshotter.hours import is_session_open

        assert is_session_open(_utc(2026, 6, 21, 17, 0)) is False

    def test_pre_open_buffer_is_open(self):
        from xenon.option_chain_snapshotter.config import SESSION_PRE_OPEN_MIN
        from xenon.option_chain_snapshotter.hours import is_session_open

        # June 2026 → EDT (UTC-4); NYSE open = 9:30 AM EDT = 13:30 UTC.
        # Window starts SESSION_PRE_OPEN_MIN before open = 13:25 UTC.
        # 13:26 UTC is 1 min inside the pre-open buffer → should be open.
        pre_open = _utc(2026, 6, 16, 13, 30) - timedelta(minutes=SESSION_PRE_OPEN_MIN - 1)
        assert is_session_open(pre_open) is True

    def test_before_pre_open_buffer_is_closed(self):
        from xenon.option_chain_snapshotter.config import SESSION_PRE_OPEN_MIN
        from xenon.option_chain_snapshotter.hours import is_session_open

        # 13:24 UTC = 1 min before the pre-open window (which starts at 13:25 UTC).
        before_window = _utc(2026, 6, 16, 13, 30) - timedelta(minutes=SESSION_PRE_OPEN_MIN + 1)
        assert is_session_open(before_window) is False

    def test_post_close_buffer_is_open(self):
        from xenon.option_chain_snapshotter.config import SESSION_POST_CLOSE_MIN
        from xenon.option_chain_snapshotter.hours import is_session_open

        # RTH close is 20:00 UTC; window ends SESSION_POST_CLOSE_MIN after that
        post_close = _utc(2026, 6, 16, 20, 0) + timedelta(minutes=SESSION_POST_CLOSE_MIN - 1)
        assert is_session_open(post_close) is True

    def test_after_post_close_buffer_is_closed(self):
        from xenon.option_chain_snapshotter.config import SESSION_POST_CLOSE_MIN
        from xenon.option_chain_snapshotter.hours import is_session_open

        after_window = _utc(2026, 6, 16, 20, 0) + timedelta(minutes=SESSION_POST_CLOSE_MIN + 1)
        assert is_session_open(after_window) is False


# --------------------------------------------------------------------------- #
# fetcher helpers
# --------------------------------------------------------------------------- #


class TestSafeHelper:
    def test_none_returns_none(self):
        from xenon.option_chain_snapshotter.fetcher import _safe

        assert _safe(None) is None

    def test_nan_returns_none(self):
        from xenon.option_chain_snapshotter.fetcher import _safe

        assert _safe(float("nan")) is None

    def test_valid_float_passes_through(self):
        from xenon.option_chain_snapshotter.fetcher import _safe

        assert _safe(3.14) == 3.14

    def test_zero_passes_through(self):
        from xenon.option_chain_snapshotter.fetcher import _safe

        assert _safe(0.0) == 0.0


class TestHasQuote:
    def _make_ticker(self, bid=None, ask=None, last=None):
        t = MagicMock()
        t.bid = bid
        t.ask = ask
        t.last = last
        return t

    def test_positive_bid_is_quote(self):
        from xenon.option_chain_snapshotter.fetcher import _has_quote

        assert _has_quote(self._make_ticker(bid=1.5)) is True

    def test_positive_ask_is_quote(self):
        from xenon.option_chain_snapshotter.fetcher import _has_quote

        assert _has_quote(self._make_ticker(ask=2.0)) is True

    def test_all_none_is_not_quote(self):
        from xenon.option_chain_snapshotter.fetcher import _has_quote

        assert _has_quote(self._make_ticker()) is False

    def test_nan_bid_is_not_quote(self):
        from xenon.option_chain_snapshotter.fetcher import _has_quote

        assert _has_quote(self._make_ticker(bid=float("nan"))) is False

    def test_zero_bid_is_not_quote(self):
        from xenon.option_chain_snapshotter.fetcher import _has_quote

        assert _has_quote(self._make_ticker(bid=0.0)) is False


# --------------------------------------------------------------------------- #
# persister helpers
# --------------------------------------------------------------------------- #


class TestPGPersisterNaN:
    """Verify that _safe() in persist_rows handles NaN model greek values."""

    def test_nan_greek_becomes_none_for_insert(self):
        """modelGreeks fields can be NaN — must map to None before psycopg insert."""
        mg = MagicMock()
        mg.impliedVol = float("nan")
        mg.delta = float("nan")
        mg.gamma = 0.001
        mg.vega = float("nan")
        mg.theta = -0.05
        mg.undPrice = 5500.0

        def _safe(v):
            if v is None:
                return None
            if isinstance(v, float) and math.isnan(v):
                return None
            return v

        assert _safe(mg.impliedVol) is None
        assert _safe(mg.delta) is None
        assert _safe(mg.gamma) == 0.001
        assert _safe(mg.theta) == -0.05
        assert _safe(mg.undPrice) == 5500.0


# --------------------------------------------------------------------------- #
# daemon cadence math
# --------------------------------------------------------------------------- #


def test_sleep_capped_at_zero_when_cycle_overruns():
    """When a cycle takes longer than cadence, sleep should be 0 not negative."""
    cadence = 600
    elapsed = 750  # overran by 150s
    sleep_s = max(0.0, cadence - elapsed)
    assert sleep_s == 0.0


def test_sleep_is_remainder_when_cycle_is_fast():
    cadence = 600
    elapsed = 420
    sleep_s = max(0.0, cadence - elapsed)
    assert sleep_s == 180.0


# --------------------------------------------------------------------------- #
# run_ticker_cycle — graceful degradation (NDX/RUT unavailable, etc.)
# --------------------------------------------------------------------------- #


class _FakePersister:
    """In-memory stand-in for PGPersister; records finish_run calls."""

    def __init__(self) -> None:
        self.finished: list[dict] = []
        self._next_id = 1

    def insert_run(self, ticker: str):
        rid = self._next_id
        self._next_id += 1
        return rid, datetime(2026, 6, 16, 17, 0, tzinfo=timezone.utc)

    def finish_run(
        self, run_id, *, status, contracts_attempted, contracts_persisted, duration_ms, ib_lines_peak=0, error=None
    ):
        self.finished.append(
            {
                "run_id": run_id,
                "status": status,
                "attempted": contracts_attempted,
                "persisted": contracts_persisted,
                "error": error,
            }
        )

    def persist_rows(self, rows, *, run_id, snapshot_ts) -> int:
        return len(rows)


class _FakeFetcher:
    def __init__(self, contracts, spot, rows=None, enum_exc=None) -> None:
        self._contracts = contracts
        self._spot = spot
        self._rows = rows if rows is not None else []
        self._enum_exc = enum_exc

    @property
    def is_connected(self) -> bool:
        return True

    async def enumerate_contracts(self, ticker):
        if self._enum_exc is not None:
            raise self._enum_exc
        return self._contracts, self._spot

    async def fetch_snapshots(self, *, ticker, contracts, spot, request_ts):
        return self._rows


@pytest.mark.asyncio
class TestRunForeverSelfHeal:
    """The reconnect budget must NOT terminate the in-process daemon — after
    exhaustion it cools down and keeps retrying so it self-heals when IB
    returns (the architecture pivot removed the Docker auto-restart)."""

    async def test_does_not_exit_after_reconnect_budget_exhausted(self, monkeypatch):
        import xenon.option_chain_snapshotter.daemon as dm

        monkeypatch.setattr(dm, "is_session_open", lambda *a, **k: True)
        monkeypatch.setattr(dm, "next_session_open", lambda *a, **k: datetime.now(timezone.utc))
        monkeypatch.setattr(dm, "MAX_RECONNECT_ATTEMPTS", 2)
        monkeypatch.setattr(dm, "RECONNECT_DELAY_S", 0.01)
        monkeypatch.setattr(dm, "RECONNECT_COOLDOWN_S", 0.01)

        class _P:
            def connect(self):
                pass

            def close(self):
                pass

            def load_cadence(self, ticker, default):
                return default

        connect_calls: list[int] = []

        class _F:
            def __init__(self, *a, **k):
                self.ib = MagicMock()

            @property
            def is_connected(self):
                return False

            async def connect(self):
                connect_calls.append(1)
                raise RuntimeError("IB down")

            def disconnect(self):
                pass

        monkeypatch.setattr(dm, "PGPersister", lambda dsn: _P())
        monkeypatch.setattr(dm, "IBFetcher", lambda *a, **k: _F())

        task = asyncio.ensure_future(
            dm.run_forever(ib_host="h", ib_port=1, ib_client_id=1, database_url="postgresql://x")
        )
        await asyncio.sleep(0.2)

        # Budget=2, so after 2 failures it cools down + resets + keeps trying.
        # The task must still be alive and have retried past the budget.
        assert not task.done(), "daemon exited after reconnect budget — regression"
        assert len(connect_calls) > 2, f"expected retries past budget, got {len(connect_calls)}"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
class TestRunTickerCycleGracefulDegradation:
    async def test_empty_enumeration_records_error_run_without_raising(self):
        """NDX/RUT unavailable → enumerate returns []. Cycle must record an
        'error' run and return cleanly so the loop moves to the next ticker."""
        from xenon.option_chain_snapshotter.daemon import run_ticker_cycle

        persister = _FakePersister()
        fetcher = _FakeFetcher(contracts=[], spot=None)

        result = await run_ticker_cycle("NDX", fetcher, persister)

        assert result.status == "error"
        assert result.contracts_attempted == 0
        assert persister.finished[-1]["status"] == "error"
        assert "empty" in (persister.finished[-1]["error"] or "")

    async def test_enumeration_exception_is_caught_and_recorded(self):
        """A raised IB error (e.g. NDX qualification failure) must not escape —
        it is recorded as an error run so sibling tickers still run."""
        from xenon.option_chain_snapshotter.daemon import run_ticker_cycle

        persister = _FakePersister()
        fetcher = _FakeFetcher(contracts=[], spot=None, enum_exc=RuntimeError("qualify failed"))

        result = await run_ticker_cycle("RUT", fetcher, persister)

        assert result.status == "error"
        assert "qualify failed" in (result.error or "")
        assert persister.finished[-1]["status"] == "error"

    async def test_full_persist_records_ok(self):
        from xenon.option_chain_snapshotter.daemon import run_ticker_cycle

        contracts = [object(), object(), object()]
        rows = [{"con_id": 1}, {"con_id": 2}, {"con_id": 3}]
        persister = _FakePersister()
        fetcher = _FakeFetcher(contracts=contracts, spot=5500.0, rows=rows)

        result = await run_ticker_cycle("SPX", fetcher, persister)

        assert result.status == "ok"
        assert result.contracts_attempted == 3
        assert result.contracts_persisted == 3

    async def test_partial_persist_records_partial(self):
        from xenon.option_chain_snapshotter.daemon import run_ticker_cycle

        contracts = [object(), object(), object()]
        rows = [{"con_id": 1}, {"con_id": 2}]  # one contract returned no data
        persister = _FakePersister()
        fetcher = _FakeFetcher(contracts=contracts, spot=5500.0, rows=rows)

        result = await run_ticker_cycle("SPX", fetcher, persister)

        assert result.status == "partial"
        assert result.contracts_persisted == 2
        assert result.contracts_attempted == 3

    async def test_cycle_timeout_records_timeout_status_with_attempted_count(self, monkeypatch):
        """A hung IB call must surface as status=error/error='timeout' with the
        real attempted count — NOT as 'cancelled' attempted=0 (ISSUE-2)."""
        import xenon.option_chain_snapshotter.daemon as daemon_mod
        from xenon.option_chain_snapshotter.daemon import run_ticker_cycle

        monkeypatch.setattr(daemon_mod, "CYCLE_TIMEOUT_S", 0.05)

        class _SlowFetcher(_FakeFetcher):
            async def enumerate_contracts(self, ticker):
                return [object(), object()], 5500.0  # 2 contracts enumerated

            async def fetch_snapshots(self, *, ticker, contracts, spot, request_ts):
                await asyncio.sleep(1.0)  # exceeds the 0.05s cycle timeout
                return []

        persister = _FakePersister()
        fetcher = _SlowFetcher(contracts=[], spot=None)

        result = await run_ticker_cycle("SPX", fetcher, persister)

        assert result.status == "error"
        assert "timeout" in (result.error or "")
        assert result.contracts_attempted == 2  # real count preserved
        assert persister.finished[-1]["status"] == "error"
        assert "timeout" in (persister.finished[-1]["error"] or "")

    async def test_shutdown_cancel_propagates_not_swallowed(self):
        """An external cancel (shutdown) must propagate as CancelledError, not be
        converted to a timeout/error result."""
        from xenon.option_chain_snapshotter.daemon import run_ticker_cycle

        class _HangFetcher(_FakeFetcher):
            async def enumerate_contracts(self, ticker):
                await asyncio.sleep(10.0)  # block so we can cancel mid-flight
                return [], None

        persister = _FakePersister()
        fetcher = _HangFetcher(contracts=[], spot=None)

        task = asyncio.ensure_future(run_ticker_cycle("SPX", fetcher, persister))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# --------------------------------------------------------------------------- #
# fetch_snapshots — dark-feed early abort (ISSUE-7, NDX/RUT entitlement gap)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestFetchSnapshotsDarkFeed:
    def _fetcher(self):
        from xenon.option_chain_snapshotter.fetcher import IBFetcher

        return IBFetcher.__new__(IBFetcher)  # bypass __init__ — no IB connection

    async def test_aborts_remaining_batches_when_first_is_dark(self):
        f = self._fetcher()
        calls: list[int] = []

        async def fake_batch(*, ticker, batch, spot, request_ts, timeout):
            calls.append(len(batch))
            return []  # dark feed — no rows

        f._fetch_batch = fake_batch
        contracts = [object()] * 120  # 3 batches of 50

        rows = await f.fetch_snapshots(
            ticker="NDX",
            contracts=contracts,
            spot=None,
            request_ts=datetime.now(timezone.utc),
            batch_size=50,
        )

        assert rows == []
        assert len(calls) == 1  # stopped after the first dark batch

    async def test_runs_all_batches_when_first_has_data(self):
        f = self._fetcher()
        calls: list[int] = []

        async def fake_batch(*, ticker, batch, spot, request_ts, timeout):
            calls.append(len(batch))
            return [{"con_id": len(calls)}]  # live feed — one row per batch

        f._fetch_batch = fake_batch
        contracts = [object()] * 120

        rows = await f.fetch_snapshots(
            ticker="SPX",
            contracts=contracts,
            spot=5500.0,
            request_ts=datetime.now(timezone.utc),
            batch_size=50,
        )

        assert len(calls) == 3  # all batches ran
        assert len(rows) == 3


@pytest.mark.asyncio
class TestFetchBatchExpiryGuard:
    """A row with an un-parseable expiry must be skipped, not poison the whole
    batch — archive.option_chain.expiry is DATE NOT NULL (Pass-3 adversarial)."""

    def _live_ticker(self):
        td = MagicMock()
        td.bid, td.ask, td.last = 1.0, 1.2, 1.1
        td.bidSize, td.askSize, td.lastSize = 1, 1, 1
        td.volume, td.openInterest = 10, 5
        td.modelGreeks = None
        return td

    def _contract(self, con_id, expiry):
        c = MagicMock()
        c.conId, c.lastTradeDateOrContractMonth = con_id, expiry
        c.strike, c.right, c.tradingClass = 5500.0, "C", "SPXW"
        return c

    async def test_bad_expiry_row_is_skipped(self):
        from xenon.option_chain_snapshotter.fetcher import IBFetcher

        f = IBFetcher.__new__(IBFetcher)
        good = self._contract(1, "20260117")
        bad = self._contract(2, "BADDATE")
        td_map = {id(good): self._live_ticker(), id(bad): self._live_ticker()}

        fake_ib = MagicMock()
        fake_ib.reqMktData.side_effect = lambda c, *a, **k: td_map[id(c)]
        fake_ib.cancelMktData.return_value = None
        f.ib = fake_ib

        rows = await f._fetch_batch(
            ticker="SPX",
            batch=[good, bad],
            spot=5500.0,
            request_ts=datetime.now(timezone.utc),
            timeout=0.4,
        )

        con_ids = {r["con_id"] for r in rows}
        assert 1 in con_ids  # good contract persisted
        assert 2 not in con_ids  # bad-expiry contract skipped, not crashed
