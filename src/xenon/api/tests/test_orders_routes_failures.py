"""F5.4 — FastAPI route coverage for cancel/modify failure classification.

Mocks ``_run_ib_script_with_recovery`` to inject subprocess payloads and
asserts the route maps them to the correct HTTP status + reason_code.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# test_mode must be False so the real classification logic runs. Set BEFORE
# importing the server module so the module-level flag reads the right env.
os.environ["XENON_API_TEST_MODE"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from xenon.api import server as server_mod  # noqa: E402
from xenon.api.subprocess import ScriptResult  # noqa: E402
from xenon.execution import orders_store  # noqa: E402


@pytest.fixture(autouse=True)
def _force_test_mode_off():
    prior = server_mod.test_mode
    server_mod.test_mode = False
    yield
    server_mod.test_mode = prior


@pytest.fixture
def tmp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "orders.duckdb"
        monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(path))
        orders_store.init_store(path)
        yield path


@pytest.fixture
def client():
    return TestClient(server_mod.app)


def _patch_runner(monkeypatch, payload: dict | None, *, ok: bool = True, error: str | None = None):
    async def fake_runner(entry, args, timeout=30):
        return ScriptResult(ok=ok, data=payload, error=error)

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fake_runner)


def test_cancel_returns_503_on_connection(client, tmp_db, monkeypatch):
    _patch_runner(
        monkeypatch,
        {
            "status": "error",
            "message": "IB gateway unreachable",
            "classification": "connection",
        },
    )
    resp = client.post("/orders/cancel", json={"orderId": 42, "permId": 0})
    assert resp.status_code == 503
    body = resp.json()["detail"]
    assert body["reason_code"] == "IB_CONNECTION"
    assert body["classification"] == "connection"
    assert body["http_status"] == 503


def test_cancel_returns_409_on_ownership(client, tmp_db, monkeypatch):
    _patch_runner(
        monkeypatch,
        {
            "status": "error",
            "message": "clientId 26 still in use after 3 retries",
            "classification": "ownership",
            "originalClientId": 26,
        },
    )
    resp = client.post("/orders/cancel", json={"orderId": 42, "permId": 0})
    assert resp.status_code == 409
    body = resp.json()["detail"]
    assert body["reason_code"] == "OWNERSHIP"
    assert body["classification"] == "ownership"


def test_cancel_returns_4xx_on_ib_reject_preserves_upstream(client, tmp_db, monkeypatch):
    _patch_runner(
        monkeypatch,
        {
            "status": "error",
            "message": "Order not found",
            "classification": "ib_reject",
            "upstream": {"code": 10147, "message": "Order not found"},
        },
    )
    resp = client.post("/orders/cancel", json={"orderId": 42, "permId": 0})
    # 10147 → order vanished → 404
    assert resp.status_code == 404
    body = resp.json()["detail"]
    assert body["reason_code"] == "IB_REJECT"
    assert body["upstream"] == {"code": 10147, "message": "Order not found"}
    assert body["message"] == "Order not found"


def test_cancel_ib_reject_generic_code_returns_400(client, tmp_db, monkeypatch):
    _patch_runner(
        monkeypatch,
        {
            "status": "error",
            "message": "Can't modify cancelled order",
            "classification": "ib_reject",
            "upstream": {"code": 201, "message": "Can't modify cancelled order"},
        },
    )
    resp = client.post("/orders/cancel", json={"orderId": 42, "permId": 0})
    assert resp.status_code == 400
    body = resp.json()["detail"]
    assert body["reason_code"] == "IB_REJECT"
    assert body["upstream"]["code"] == 201


def _seed_submission(tmp_db: Path, ib_order_id: str = "42") -> str:
    """Create an orders_submissions row so apply_modify has something to key on."""
    import duckdb

    sid = "sub-test-1"
    con = duckdb.connect(str(tmp_db))
    try:
        con.execute(
            """
            INSERT INTO orders_submissions (
                submission_id, user_id, client_attempt_id, ticker, security_type,
                action, quantity, multiplier, limit_price, state, ib_order_id,
                modify_sequence, submitted_at, updated_at
            ) VALUES (?, 'local', 'cid-1', 'AAPL', 'STK', 'BUY', 1, 100, '1.23',
                      'WORKING', ?, 5, NOW(), NOW())
            """,
            [sid, ib_order_id],
        )
    finally:
        con.close()
    return sid


def test_modify_returns_409_on_stale_sequence(client, tmp_db, monkeypatch):
    _seed_submission(tmp_db, ib_order_id="42")
    # Subprocess must NOT be called when the gate rejects; fail loudly if it is.

    async def fake_runner(entry, args, timeout=30):
        raise AssertionError("subprocess must not run on stale modifySequence")

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fake_runner)

    resp = client.post(
        "/orders/modify",
        json={"orderId": 42, "permId": 0, "newPrice": 1.50, "modifySequence": 3},
    )
    assert resp.status_code == 409
    body = resp.json()["detail"]
    assert body["reason_code"] == "MODIFY_STALE"
    assert body["applied"] == 5


def test_modify_rejects_missing_sequence(client, tmp_db, monkeypatch):
    async def fake_runner(entry, args, timeout=30):
        raise AssertionError("subprocess must not run without modifySequence")

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fake_runner)

    resp = client.post(
        "/orders/modify",
        json={"orderId": 42, "permId": 0, "newPrice": 1.50},
    )
    assert resp.status_code == 400
    body = resp.json()["detail"]
    assert body["reason_code"] == "MODIFY_SEQUENCE_REQUIRED"


def test_modify_unknown_order_returns_404(client, tmp_db, monkeypatch):
    async def fake_runner(entry, args, timeout=30):
        raise AssertionError("subprocess must not run on unknown order")

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fake_runner)

    resp = client.post(
        "/orders/modify",
        json={"orderId": 9999, "permId": 0, "newPrice": 1.50, "modifySequence": 1},
    )
    assert resp.status_code == 404
    body = resp.json()["detail"]
    assert body["reason_code"] == "ORDER_NOT_FOUND"


def test_modify_happy_path_advances_sequence_and_runs_subprocess(client, tmp_db, monkeypatch):
    _seed_submission(tmp_db, ib_order_id="42")
    called = {}

    async def fake_runner(entry, args, timeout=30):
        called["entry"] = entry
        called["args"] = args
        return ScriptResult(ok=True, data={"status": "ok", "message": "Modified"})

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fake_runner)

    resp = client.post(
        "/orders/modify",
        json={"orderId": 42, "permId": 0, "newPrice": 1.50, "modifySequence": 7},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert called["entry"] == "xenon-ib-order-manage"

    # sequence should be advanced in the DB
    outcome = orders_store.apply_modify("42", 7)  # same sequence → not applied
    assert outcome["applied"] is False
    assert outcome["current_sequence"] == 7
