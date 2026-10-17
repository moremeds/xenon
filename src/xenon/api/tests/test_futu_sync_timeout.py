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
    """Wait until the in-flight sync worker thread actually exits."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        fut = server._futu_sync_future
        if fut is None or fut.done():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("sync worker still running")


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
    """FutuClient stand-in whose fetch blocks until released or reset.

    ``disconnect()`` is the reset path — it flips the client to disconnected
    so the next request reconnects, like ``FutuClient.disconnect`` does.
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

    def __init__(self) -> None:
        self.release = threading.Event()
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
        self.connected = False

    def fetch_portfolio(self, force: bool = False) -> dict:
        self.fetch_calls += 1
        self.release.wait(timeout=30)
        return dict(self.ENVELOPE)


@pytest.mark.asyncio
async def test_timeout_never_starts_second_worker_and_recovers(_futu_state, monkeypatch) -> None:
    """Strict single-flight: while the timed-out worker is still alive, new
    requests 503 without queueing another worker on the client's RLock; the
    client is reset so the next request reconnects."""
    fake = _HungClient()
    monkeypatch.setattr(server, "_get_futu_client", lambda: fake)

    try:
        # First request hangs in fetch → 503 at the bound, and the timeout
        # path must reset the client (disconnect) so the SDK call can unwind.
        with pytest.raises(HTTPException) as excinfo:
            await server.futu_sync()
        assert excinfo.value.status_code == 503
        assert fake.disconnect_calls == 1
        assert fake.fetch_calls == 1

        # Worker is still blocked — a repeat request must 503 immediately and
        # must NOT start a second worker or a reconnect attempt.
        t0 = time.monotonic()
        with pytest.raises(HTTPException) as excinfo2:
            await server.futu_sync()
        assert excinfo2.value.status_code == 503
        assert time.monotonic() - t0 < 0.25
        assert fake.fetch_calls == 1
        assert fake.connect_calls == 0
    finally:
        fake.release.set()
    await _await_worker_done()

    # Hung call returned after the reset — the endpoint recovers: client was
    # marked disconnected, so this request reconnects and fetches normally.
    body = await server.futu_sync()
    assert body["ok"] is True
    assert fake.connect_calls == 1
    assert fake.fetch_calls == 2
