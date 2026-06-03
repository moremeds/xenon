"""Rehydrate lifespan behavior in API test mode."""

from __future__ import annotations

import os

import pytest

os.environ["XENON_API_TEST_MODE"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from xenon.api import server as server_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _force_test_mode_on(monkeypatch):
    # Set the env var that _is_test_mode() reads at call time. Mutating
    # server_mod.test_mode would be ineffective now — gates go through
    # _is_test_mode(), not the module attribute.
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    yield


def test_rehydrate_skipped_in_test_mode(monkeypatch):
    """test_mode skips boot rehydrate; route tests call rehydrate explicitly."""
    called: list[dict] = []

    def fake_rehydrate(**kwargs):
        called.append(kwargs)
        return []

    monkeypatch.setattr(
        "xenon.execution.single_leg_rehydrate.rehydrate_on_boot",
        fake_rehydrate,
    )

    with TestClient(server_mod.app):
        pass

    assert called == [], "rehydrate must not run in test_mode"


@pytest.mark.asyncio
async def test_rehydrate_runs_outside_test_mode(monkeypatch):
    """Outside test mode, boot rehydrate runs against Postgres."""
    called: list[dict] = []

    def fake_rehydrate(**kwargs):
        called.append(kwargs)
        return []

    monkeypatch.setattr(
        "xenon.execution.single_leg_rehydrate.rehydrate_on_boot",
        fake_rehydrate,
    )
    monkeypatch.setattr(server_mod, "_is_test_mode", lambda: False)

    # Minimal pool stand-in: rehydrate dispatch now flows through
    # ib_pool.run_sync, so the fake must satisfy that surface.
    class _FakePool:
        async def run_sync(self, role, fn, *args, **kwargs):
            return fn(*args, **kwargs)

        def get_with_reconnect_sync(self, role):
            raise RuntimeError("test fake — should not be called")

    monkeypatch.setattr(server_mod, "ib_pool", _FakePool())

    await server_mod._run_rehydrate_on_boot()

    assert len(called) == 1
    assert "db_path" not in called[0]
