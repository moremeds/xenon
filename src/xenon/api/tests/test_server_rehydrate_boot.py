"""FastAPI lifespan integration for single-leg rehydrate."""

from __future__ import annotations

import os

import pytest

# Keep lifespan in test mode so IB Gateway / pool startup is skipped.
os.environ["XENON_API_TEST_MODE"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from xenon.api import server as server_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _force_test_mode_on(monkeypatch):
    # Env-var based — _is_test_mode() re-reads on every call, so import
    # order can't freeze a stale value from another test.
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    yield


def test_rehydrate_skips_in_test_mode_startup(monkeypatch):
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

    assert called == []


def test_rehydrate_failure_does_not_block_boot(monkeypatch):
    def broken(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "xenon.execution.single_leg_rehydrate.rehydrate_on_boot",
        broken,
    )

    with TestClient(server_mod.app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
