"""F7.3 — synthetic rehydrate probe for observability readiness.

The probe injects a synthetic PENDING row, backdates it past the timeout,
runs rehydrate_on_boot against a fake (empty) IB client, and verifies that
an orders_events row is written (REHYDRATE_RECONCILED with PENDING_TIMEOUT).

The probe is gated on ``XENON_API_TEST_MODE`` OR ``DEV_PROBES=1`` and must
return 404 outside of those modes.
"""

from __future__ import annotations

import os

# Keep lifespan in test mode so IB Gateway / pool startup is skipped.
os.environ["XENON_API_TEST_MODE"] = "1"

import duckdb  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from xenon.api import server as server_mod  # noqa: E402


@pytest.fixture
def _isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "orders.duckdb"
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(db_path))
    from xenon.execution.orders_store import init_store

    init_store(str(db_path))
    yield db_path


def test_synthetic_probe_writes_event(monkeypatch, _isolated_db):
    """Probe should insert a PENDING row, run rehydrate, record an event."""
    monkeypatch.setenv("DEV_PROBES", "1")
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")

    with TestClient(server_mod.app) as client:
        r = client.post("/dev/rehydrate/synthetic")

    assert r.status_code == 200, r.text
    body = r.json()
    assert "submission_id" in body
    assert body["events_added"] >= 1

    # Confirm the event was actually written to the DB
    con = duckdb.connect(str(_isolated_db))
    try:
        rows = con.execute(
            "SELECT kind FROM orders_events WHERE submission_id = ?",
            [body["submission_id"]],
        ).fetchall()
    finally:
        con.close()
    assert len(rows) >= 1
    assert any("REHYDRATE" in r[0] for r in rows)


def test_synthetic_probe_disabled_in_production(monkeypatch):
    """When test_mode is off AND DEV_PROBES unset, the probe returns 404."""
    monkeypatch.delenv("DEV_PROBES", raising=False)
    monkeypatch.setenv("XENON_API_TEST_MODE", "0")

    with TestClient(server_mod.app) as client:
        r = client.post("/dev/rehydrate/synthetic")

    assert r.status_code == 404
