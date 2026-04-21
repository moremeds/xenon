"""F7.2 — FastAPI lifespan integration for single-leg rehydrate.

Verify that ``rehydrate_on_boot`` is invoked exactly once during FastAPI
startup, and that its failure does not block the server from serving
``/health``.
"""

from __future__ import annotations

import os

import pytest

# Keep lifespan in test mode so IB Gateway / pool startup is skipped; the
# rehydrate hook still runs in that branch.
os.environ["XENON_API_TEST_MODE"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from xenon.api import server as server_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _force_test_mode_on(monkeypatch):
    prior = server_mod.test_mode
    server_mod.test_mode = True
    yield
    server_mod.test_mode = prior


def test_rehydrate_runs_on_startup(monkeypatch):
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

    assert len(called) == 1, f"expected 1 rehydrate invocation, got {len(called)}"
    kwargs = called[0]
    assert "db_path" in kwargs
    assert "ib_client_factory" in kwargs
    assert callable(kwargs["ib_client_factory"])


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
