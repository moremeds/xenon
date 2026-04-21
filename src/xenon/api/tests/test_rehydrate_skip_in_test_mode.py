"""B1 — rehydrate must skip in test_mode when XENON_ORDERS_DB_PATH is unset.

Without this guard, pytest's TestClient(app) would read/write the shared
prod data/orders.duckdb.
"""

from __future__ import annotations

import os

import pytest

os.environ["XENON_API_TEST_MODE"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from xenon.api import server as server_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _force_test_mode_on(monkeypatch):
    prior = server_mod.test_mode
    server_mod.test_mode = True
    yield
    server_mod.test_mode = prior


def test_rehydrate_skipped_in_test_mode_without_env(monkeypatch, caplog):
    """test_mode + no XENON_ORDERS_DB_PATH → skip with a log, no rehydrate call."""
    # Override the autouse conftest fixture — simulate an unset env var.
    monkeypatch.delenv("XENON_ORDERS_DB_PATH", raising=False)

    called: list[dict] = []

    def fake_rehydrate(**kwargs):
        called.append(kwargs)
        return []

    monkeypatch.setattr(
        "xenon.execution.single_leg_rehydrate.rehydrate_on_boot",
        fake_rehydrate,
    )

    with caplog.at_level("INFO"):
        with TestClient(server_mod.app):
            pass

    assert called == [], "rehydrate must not run when env var is unset in test_mode"
    assert any("skipping rehydrate" in rec.message for rec in caplog.records), (
        f"expected skip log, got: {[r.message for r in caplog.records]}"
    )


def test_rehydrate_runs_when_env_var_set(monkeypatch, tmp_path):
    """Explicit opt-in — when XENON_ORDERS_DB_PATH is set, rehydrate runs."""
    db_path = tmp_path / "custom.duckdb"
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(db_path))

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

    assert len(called) == 1
    assert called[0]["db_path"] == str(db_path)
