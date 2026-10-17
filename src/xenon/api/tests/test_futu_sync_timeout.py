"""API-7 — POST /futu/sync is bounded: a hung OpenD call returns 503.

The tests call the ``futu_sync`` handler directly on the test loop: under a
long-lived loop (uvicorn) the 503 is prompt while the orphaned worker thread
finishes in the background. TestClient would mask that — its per-request loop
joins the default executor at shutdown, stretching the observed latency to the
fake's full wait. Fake clients block on a threading.Event the test controls so
no worker lingers past teardown.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from xenon.api import server
from xenon.clients.futu_exceptions import FutuConnectionError


@pytest.fixture()
def _futu_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "_futu_last_sync_monotonic", None, raising=False)
    monkeypatch.setattr(server, "_futu_client", None, raising=False)
    monkeypatch.setattr(server, "_futu_lock", None, raising=False)
    monkeypatch.setattr(server, "_futu_sync_future", None, raising=False)
    # Small bound keeps the suite fast; production default stays 15s.
    monkeypatch.setattr(server, "FUTU_SYNC_TIMEOUT_S", 0.25)
    # No NAV persistence — a leftover lifespan engine must not make the
    # success-path test reach Postgres.
    monkeypatch.delattr(server.app.state, "db_engine", raising=False)
    return tmp_path


def _hang_until(release: threading.Event):
    def _hang(*_args, **_kwargs):
        release.wait(timeout=30)

    return _hang


async def _await_worker_done(timeout: float = 5.0) -> None:
    """Wait until the in-flight marker completes (fetch AND cleanup done)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        fut = server._futu_sync_future
        if fut is None or fut.done():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("sync worker still running")


async def _wait_for(predicate, timeout: float = 5.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{what} not met within {timeout}s")


@pytest.mark.asyncio
async def test_futu_sync_hung_connect_returns_503_fast(_futu_state, monkeypatch) -> None:
    release = threading.Event()
    fake = MagicMock()
    fake.is_connected.return_value = False
    fake.connect.side_effect = _hang_until(release)
    monkeypatch.setattr(server, "_get_futu_client", lambda: fake)

    try:
        t0 = time.monotonic()
        with pytest.raises(HTTPException) as excinfo:
            await server.futu_sync()
        elapsed = time.monotonic() - t0
        assert excinfo.value.status_code == 503
        # Bounded — pre-fix this awaited the fake's full 30s block.
        assert elapsed < 5

        # The singleflight lock was released — a second call is not queued
        # behind the still-hung worker thread.
        with pytest.raises(HTTPException) as excinfo2:
            await server.futu_sync()
        assert excinfo2.value.status_code == 503
    finally:
        release.set()


@pytest.mark.asyncio
async def test_futu_sync_hung_fetch_returns_503_fast(_futu_state, monkeypatch) -> None:
    release = threading.Event()
    fake = MagicMock()
    fake.is_connected.return_value = True
    fake.fetch_portfolio.side_effect = _hang_until(release)
    monkeypatch.setattr(server, "_get_futu_client", lambda: fake)

    try:
        t0 = time.monotonic()
        with pytest.raises(HTTPException) as excinfo:
            await server.futu_sync()
        elapsed = time.monotonic() - t0
        assert excinfo.value.status_code == 503
        assert elapsed < 5
    finally:
        release.set()


@pytest.mark.asyncio
async def test_futu_sync_503_does_not_poison_next_success(_futu_state, monkeypatch) -> None:
    """After a timed-out attempt, a healthy client still syncs normally."""
    release = threading.Event()
    hung = MagicMock()
    hung.is_connected.return_value = True
    hung.fetch_portfolio.side_effect = _hang_until(release)
    monkeypatch.setattr(server, "_get_futu_client", lambda: hung)

    try:
        with pytest.raises(HTTPException) as excinfo:
            await server.futu_sync()
        assert excinfo.value.status_code == 503
    finally:
        release.set()
    # The timed-out worker must fully exit before the next request proceeds —
    # while it lives, futu_sync refuses to start another worker.
    await _await_worker_done()

    ok = MagicMock()
    ok.is_connected.return_value = True
    ok.fetch_portfolio.return_value = {
        "fetched_at": "2026-10-16T12:00:00.000Z",
        "data_as_of": "2026-10-16T12:00:00.000Z",
        "account_id": "1",
        "positions": [],
        "count": 0,
        "is_stale": False,
        "warnings": [],
        "account_summary": None,
    }
    monkeypatch.setattr(server, "_get_futu_client", lambda: ok)
    body = await server.futu_sync()
    assert body["ok"] is True
    assert body["count"] == 0


class _HungClient:
    """FutuClient stand-in whose fetch blocks until reset.

    ``disconnect()`` blocks on its own event (modelling an SDK close that
    itself waits on the SDK's RLock) and — like a real ctx close — unwinds
    the hung fetch when it completes, so the test never releases the fetch
    by hand on the primary path.
    """

    ENVELOPE = {
        "fetched_at": "2026-10-16T12:00:00.000Z",
        "data_as_of": "2026-10-16T12:00:00.000Z",
        "account_id": "1",
        "positions": [],
        "count": 0,
        "is_stale": False,
        "warnings": [],
        "account_summary": None,
    }

    def __init__(self, disconnect_block_s: float = 5.0) -> None:
        self.release_fetch = threading.Event()
        self.release_disconnect = threading.Event()
        self.disconnect_entered = threading.Event()
        self.disconnect_block_s = disconnect_block_s
        self.connected = True
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.fetch_calls = 0

    def is_connected(self) -> bool:
        return self.connected

    def connect(self) -> None:
        self.connect_calls += 1
        self.connected = True

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.disconnect_entered.set()
        self.release_disconnect.wait(timeout=self.disconnect_block_s)
        # Real-ctx-close semantics: the hung SDK call unwinds and the client
        # is marked disconnected so the next request reconnects.
        self.connected = False
        self.release_fetch.set()

    def fetch_portfolio(self, force: bool = False) -> dict:
        self.fetch_calls += 1
        self.release_fetch.wait(timeout=30)
        if not self.connected:
            raise FutuConnectionError("context closed by reset")
        return dict(self.ENVELOPE)


@pytest.mark.asyncio
async def test_timeout_cleanup_off_loop_no_extra_worker_then_recovers(
    _futu_state, monkeypatch
) -> None:
    """Strict single-flight with off-loop cleanup: a blocking disconnect()
    must not stall the event loop, no second worker starts while fetch or
    cleanup is still alive, and the endpoint recovers once both finish."""
    fake = _HungClient()
    monkeypatch.setattr(server, "_get_futu_client", lambda: fake)

    heartbeat = {"ticks": 0, "stop": False}

    async def _beat() -> None:
        while not heartbeat["stop"]:
            heartbeat["ticks"] += 1
            await asyncio.sleep(0.01)

    beat = asyncio.create_task(_beat())
    try:
        # (a) fetch hangs → 503 at the bound even though the reset's
        # disconnect() blocks for seconds — it runs on a daemon thread.
        t0 = time.monotonic()
        with pytest.raises(HTTPException) as excinfo:
            await server.futu_sync()
        assert excinfo.value.status_code == 503
        assert time.monotonic() - t0 < 2
        assert fake.fetch_calls == 1

        # Cleanup started off-loop; the loop itself kept beating while a
        # blocking disconnect() is still in flight.
        await _wait_for(lambda: fake.disconnect_entered.is_set(), what="cleanup start")
        ticks_before = heartbeat["ticks"]
        await asyncio.sleep(0.1)
        assert heartbeat["ticks"] > ticks_before

        # (b) fetch and cleanup both still blocked → immediate 503, no worker.
        t0 = time.monotonic()
        with pytest.raises(HTTPException) as excinfo2:
            await server.futu_sync()
        assert excinfo2.value.status_code == 503
        assert time.monotonic() - t0 < 0.25
        assert fake.fetch_calls == 1
        assert fake.connect_calls == 0

        # Fetch released on its own (SDK call returned late) while cleanup is
        # still blocked → still 503, still no worker.
        fake.release_fetch.set()
        with pytest.raises(HTTPException) as excinfo3:
            await server.futu_sync()
        assert excinfo3.value.status_code == 503
        assert fake.fetch_calls == 1
        assert fake.connect_calls == 0
    finally:
        heartbeat["stop"] = True
        await beat
        fake.release_fetch.set()
        fake.release_disconnect.set()
    await _await_worker_done()

    # (c) fetch + cleanup both finished → next request reconnects + succeeds.
    body = await server.futu_sync()
    assert body["ok"] is True
    assert fake.connect_calls == 1
    assert fake.fetch_calls == 2


@pytest.mark.asyncio
async def test_cleanup_thread_spawn_failure_still_recovers(_futu_state, monkeypatch) -> None:
    """If the cleanup daemon thread can't start (RuntimeError: cannot start
    new thread), the published marker must still complete when the fetch
    exits — the timeout response stays a normal 503, never a 500."""
    fake = _HungClient()
    monkeypatch.setattr(server, "_get_futu_client", lambda: fake)

    real_start = threading.Thread.start

    def _boom_start(self):
        if self.name == "xenon-futu-cleanup":
            raise RuntimeError("cannot start new thread")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", _boom_start)

    try:
        t0 = time.monotonic()
        with pytest.raises(HTTPException) as excinfo:
            await server.futu_sync()
        # Normal timeout 503 — the spawn failure must not escape as a 500.
        assert excinfo.value.status_code == 503
        assert time.monotonic() - t0 < 2
        assert fake.fetch_calls == 1
        # No cleanup ran — and none was attempted on the event loop.
        assert fake.disconnect_calls == 0

        # Fetch still blocked → still 503, no new worker.
        with pytest.raises(HTTPException) as excinfo2:
            await server.futu_sync()
        assert excinfo2.value.status_code == 503
        assert fake.fetch_calls == 1
    finally:
        fake.release_fetch.set()
        fake.release_disconnect.set()

    # Marker completes via the fetch done-callback — endpoint unsticks.
    await _await_worker_done()
    body = await server.futu_sync()
    assert body["ok"] is True
    assert fake.fetch_calls == 2
    assert fake.disconnect_calls == 0
