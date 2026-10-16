"""API-7 — POST /futu/sync is bounded: a hung OpenD call returns 503.

The tests call the ``futu_sync`` handler directly on the test loop: under a
long-lived loop (uvicorn) the 503 is prompt while the orphaned worker thread
finishes in the background. TestClient would mask that — its per-request loop
joins the default executor at shutdown, stretching the observed latency to the
fake's full wait. Fake clients block on a threading.Event the test controls so
no worker lingers past teardown.
"""

from __future__ import annotations

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
