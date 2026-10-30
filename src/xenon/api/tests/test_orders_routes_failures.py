"""F5.4 — FastAPI route coverage for cancel/modify failure classification.

Mocks ``_run_ib_script_with_recovery`` to inject subprocess payloads and
asserts the route maps them to the correct HTTP status + reason_code.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

# test_mode must be False so the real classification logic runs.
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text

from xenon.api import server as server_mod  # noqa: E402
from xenon.api.subprocess import ScriptResult  # noqa: E402
from xenon.execution import orders_store  # noqa: E402


@pytest.fixture(autouse=True)
def _force_test_mode_off(monkeypatch):
    # Env-var based — _is_test_mode() re-reads on every call.
    monkeypatch.setenv("XENON_API_TEST_MODE", "0")
    yield


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


def _pg_engine():
    url = os.environ.get(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
    ).replace("postgresql+asyncpg://", "postgresql+psycopg://")
    return create_engine(url, pool_pre_ping=True)


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
    """Create an orders_submissions row so apply_modify has something to key on.

    Stamps the row with the test harness's scope (paper/DU0000000 from
    scripts/tests/conftest.py + src/xenon/api/tests/conftest.py) so the
    scope-filtered apply_modify in /orders/modify finds it.
    """
    sid = "sub-test-1"
    engine = _pg_engine()
    try:
        with engine.begin() as con:
            con.execute(
                text(
                    """
            INSERT INTO xenon.order_submissions (
                submission_id, user_id, client_attempt_id, ticker, security_type,
                action, quantity, multiplier, limit_price, state, ib_order_id,
                modify_sequence, submitted_at, updated_at,
                broker, account_env, broker_account
            ) VALUES (:submission_id, 'local', 'cid-1', 'AAPL', 'STK', 'BUY', 1, 100, '1.23',
                      'WORKING', :ib_order_id, 5, NOW(), NOW(),
                      'IB', 'paper', 'DU0000000')
            """,
                ),
                {"submission_id": sid, "ib_order_id": ib_order_id},
            )
    finally:
        engine.dispose()
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


def test_modify_by_perm_id_with_known_row_advances_sequence(client, tmp_db, monkeypatch):
    """A4: when orderId=0 but permId is known, resolve via perm_id and succeed."""
    sid = "sub-perm-1"
    engine = _pg_engine()
    try:
        with engine.begin() as con:
            con.execute(
                text(
                    """
            INSERT INTO xenon.order_submissions (
                submission_id, user_id, client_attempt_id, ticker, security_type,
                action, quantity, multiplier, limit_price, state, ib_order_id,
                perm_id, modify_sequence, submitted_at, updated_at,
                broker, account_env, broker_account
            ) VALUES (:submission_id, 'local', 'cid-perm', 'AAPL', 'STK', 'BUY', 1, 100, '1.23',
                      'WORKING', '99', '42', 0, NOW(), NOW(),
                      'IB', 'paper', 'DU0000000')
            """,
                ),
                {"submission_id": sid},
            )
    finally:
        engine.dispose()

    async def fake_runner(entry, args, timeout=30):
        return ScriptResult(ok=True, data={"status": "ok", "message": "Modified"})

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fake_runner)

    resp = client.post(
        "/orders/modify",
        json={"orderId": 0, "permId": 42, "newPrice": 1.50, "modifySequence": 1},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body.get("applied_sequence") == 1
    # Sequence should now be 1 in DB
    outcome = orders_store.apply_modify("99", 1)
    assert outcome == {"applied": False, "current_sequence": 1}


def test_modify_without_any_identifier_400s(client, tmp_db, monkeypatch):
    """A4: neither orderId nor permId → 400 ORDER_IDENTIFIER_REQUIRED."""

    async def fake_runner(entry, args, timeout=30):
        raise AssertionError("subprocess must not run without identifier")

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fake_runner)

    resp = client.post(
        "/orders/modify",
        json={"orderId": 0, "permId": 0, "newPrice": 1.50, "modifySequence": 1},
    )
    assert resp.status_code == 400
    body = resp.json()["detail"]
    assert body["reason_code"] == "ORDER_IDENTIFIER_REQUIRED"


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


def test_modify_503_echoes_applied_sequence(client, tmp_db, monkeypatch):
    """B2 — after DB sequence advances, a 503 must echo applied_sequence so
    the client's modifySeqCountsRef can sync and avoid a MODIFY_STALE loop."""
    _seed_submission(tmp_db, ib_order_id="42")

    async def fake_runner(entry, args, timeout=30):
        return ScriptResult(
            ok=False,
            data=None,
            error="IB gateway unreachable",
        )

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fake_runner)

    resp = client.post(
        "/orders/modify",
        json={"orderId": 42, "permId": 0, "newPrice": 1.50, "modifySequence": 9},
    )
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["applied_sequence"] == 9
    assert detail["reason_code"] == "IB_CONNECTION"


def _seed_permid_submission(tmp_db: Path, perm_id: str = "P42") -> str:
    """Insert a submissions row that has a perm_id but NO ib_order_id.

    Models the UI-initiated modify/cancel path where the client only knows
    the permId (placement hasn't populated ib_order_id yet, or the UI dropped
    orderId on a reconnect).
    """
    sid = "sub-permevt-1"
    engine = _pg_engine()
    try:
        with engine.begin() as con:
            con.execute(
                text(
                    """
            INSERT INTO xenon.order_submissions (
                submission_id, user_id, client_attempt_id, ticker, security_type,
                action, quantity, multiplier, limit_price, state, ib_order_id,
                perm_id, modify_sequence, submitted_at, updated_at,
                broker, account_env, broker_account
            ) VALUES (:submission_id, 'local', 'cid-perm-evt', 'AAPL', 'STK', 'BUY', 1, 100, '1.23',
                      'WORKING', :ib_order_id, :perm_id, 0, NOW(), NOW(),
                      'IB', 'paper', 'DU0000000')
            """,
                ),
                {"submission_id": sid, "ib_order_id": "ib-" + perm_id, "perm_id": perm_id},
            )
    finally:
        engine.dispose()
    return sid


def _fetch_events(tmp_db: Path, submission_id: str) -> list[tuple[str, str]]:
    """Return (kind, detail_json) rows for the given submission in insertion order."""
    engine = _pg_engine()
    try:
        with engine.connect() as con:
            rows = con.execute(
                text('SELECT kind, detail FROM xenon.order_events WHERE submission_id = :submission_id ORDER BY "at"'),
                {"submission_id": submission_id},
            ).fetchall()
    finally:
        engine.dispose()
    return [(r[0], json.dumps(r[1]) if isinstance(r[1], dict) else r[1]) for r in rows]


def test_modify_permid_only_writes_event(client, tmp_db, monkeypatch):
    """D1 — permId-only success modify must write an orders_events MODIFY row."""
    sid = _seed_permid_submission(tmp_db, perm_id="42")

    async def fake_runner(entry, args, timeout=30):
        return ScriptResult(ok=True, data={"status": "ok", "message": "Modified"})

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fake_runner)

    resp = client.post(
        "/orders/modify",
        json={"orderId": 0, "permId": 42, "newPrice": 1.50, "modifySequence": 1},
    )
    assert resp.status_code == 200

    events = _fetch_events(tmp_db, sid)
    assert len(events) == 1
    kind, detail_json = events[0]
    assert kind == "MODIFY"
    assert '"http_status": 200' in detail_json
    assert '"applied_sequence": 1' in detail_json


def test_modify_permid_only_writes_failure_event(client, tmp_db, monkeypatch):
    """D1 — permId-only failure modify must still write an orders_events row."""
    sid = _seed_permid_submission(tmp_db, perm_id="42")

    async def fake_runner(entry, args, timeout=30):
        return ScriptResult(
            ok=True,
            data={
                "status": "error",
                "classification": "connection",
                "message": "IB gateway unreachable",
            },
        )

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fake_runner)

    resp = client.post(
        "/orders/modify",
        json={"orderId": 0, "permId": 42, "newPrice": 1.50, "modifySequence": 1},
    )
    assert resp.status_code == 503

    events = _fetch_events(tmp_db, sid)
    assert len(events) == 1
    kind, detail_json = events[0]
    assert kind == "MODIFY"
    assert '"reason_code": "IB_CONNECTION"' in detail_json
    assert '"applied_sequence": 1' in detail_json


def test_cancel_permid_only_writes_event(client, tmp_db, monkeypatch):
    """D1 — permId-only cancel must write an orders_events CANCEL row."""
    sid = _seed_permid_submission(tmp_db, perm_id="42")

    async def fake_runner(entry, args, timeout=30):
        return ScriptResult(ok=True, data={"status": "ok", "message": "Cancelled"})

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fake_runner)

    resp = client.post("/orders/cancel", json={"orderId": 0, "permId": 42})
    assert resp.status_code == 200

    events = _fetch_events(tmp_db, sid)
    assert len(events) == 1
    kind, detail_json = events[0]
    assert kind == "CANCEL"
    assert '"http_status": 200' in detail_json


def _fetch_submission_state(submission_id: str) -> tuple[str, str | None]:
    """Return (state, reason_code) for the given submission row."""
    engine = _pg_engine()
    try:
        with engine.connect() as con:
            row = con.execute(
                text("SELECT state, reason_code FROM xenon.order_submissions WHERE submission_id = :sid"),
                {"sid": submission_id},
            ).first()
    finally:
        engine.dispose()
    return (row[0], row[1]) if row else ("", None)


def test_cancel_success_marks_submission_cancelled(client, tmp_db, monkeypatch):
    """Successful cancel must transition order_submissions.state to CANCELLED.

    Without this the Open Orders panel keeps showing the row (the /orders
    reader filters on WORKING/PENDING/PARTIALLY_FILLED) even though the
    order was cancelled at IB. UI's cancel poll then times out and reverts.
    """
    sid = _seed_permid_submission(tmp_db, perm_id="42")

    async def fake_runner(entry, args, timeout=30):
        return ScriptResult(ok=True, data={"status": "ok", "message": "Cancelled"})

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fake_runner)

    resp = client.post("/orders/cancel", json={"orderId": 0, "permId": 42})
    assert resp.status_code == 200

    state, reason_code = _fetch_submission_state(sid)
    assert state == "CANCELLED"
    assert reason_code == "USER_CANCEL"


def test_cancel_failure_does_not_mark_submission_cancelled(client, tmp_db, monkeypatch):
    """A failed cancel (subprocess error / IB reject) must NOT touch state.

    If IB refused the cancel the order is still working — we cannot lie to
    the panel by marking it CANCELLED locally.
    """
    sid = _seed_permid_submission(tmp_db, perm_id="42")

    async def fake_runner(entry, args, timeout=30):
        return ScriptResult(
            ok=True,
            data={
                "status": "error",
                "classification": "ib_reject",
                "message": "Order not found",
                "upstream": {"code": 10147, "message": "Order not found"},
            },
        )

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fake_runner)

    resp = client.post("/orders/cancel", json={"orderId": 0, "permId": 42})
    assert resp.status_code == 404

    state, _ = _fetch_submission_state(sid)
    assert state == "WORKING"


def test_dev_probe_not_in_auth_exempt_paths():
    """B3 — defense in depth: Clerk middleware must still cover the probe."""
    assert "/dev/rehydrate/synthetic" not in server_mod.AUTH_EXEMPT_PATHS


def test_place_reason_code_constants_exist():
    """B5 + B6 — preflight enum carries the literals we write from place()."""
    from xenon.execution.preflight import ReasonCode

    assert ReasonCode.IB_REJECT.value == "IB_REJECT"
    assert ReasonCode.SUBPROCESS_ERROR.value == "SUBPROCESS_ERROR"
