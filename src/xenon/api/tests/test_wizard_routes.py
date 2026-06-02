from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from starlette.responses import JSONResponse

from xenon.api import server as server_mod
from xenon.db.queries import combo_wizard as cwq

# --------------------------------------------------------------------------
# Postgres helpers
# --------------------------------------------------------------------------


def _sync_url() -> str:
    """Resolve the per-worker test DB URL at call time.

    Reading at module-import time would snapshot the value BEFORE pytest-xdist's
    Phase 3 conftest rewrites DATABASE_URL_TEST to the per-worker DB clone, so
    every worker would race TRUNCATE on the same shared `xenon_test` database.
    """
    url = os.environ.get(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
    )
    return url.replace("postgresql+asyncpg://", "postgresql+psycopg://")


def _pg_engine():
    return create_engine(_sync_url(), pool_pre_ping=True)


def _truncate(engine):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE xenon.order_events CASCADE"))
        conn.execute(text("TRUNCATE xenon.order_submissions CASCADE"))
        conn.execute(text("TRUNCATE xenon.wizard_protection CASCADE"))
        conn.execute(text("TRUNCATE xenon.wizard_events CASCADE"))
        conn.execute(text("TRUNCATE xenon.wizard_combo_attempts CASCADE"))
        conn.execute(text("TRUNCATE xenon.wizard_sessions CASCADE"))


@pytest.fixture(autouse=True)
def _setup_pg(monkeypatch):
    """Point get_sync_engine() at the per-worker test DB.

    Resolves DATABASE_URL_TEST at call time so pytest-xdist's per-worker DB
    clone (Phase 3) is honored — module-level snapshotting would pin every
    worker to the same shared DB and cross-worker TRUNCATEs would deadlock.
    """
    monkeypatch.setenv("DATABASE_URL", _sync_url())

    import xenon.db.engine as eng_mod

    monkeypatch.setattr(eng_mod, "_sync_engine", None)

    engine = _pg_engine()
    _truncate(engine)
    engine.dispose()
    yield
    engine = _pg_engine()
    _truncate(engine)
    engine.dispose()


@pytest.fixture(autouse=True)
def _force_test_mode_on(monkeypatch):
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    yield


@pytest.fixture
def client():
    return TestClient(server_mod.app)


def _plan_payload() -> dict:
    return {
        "ticker": "SPY",
        "intent": "OPEN",
        "legs": [
            {
                "contract_id": "SPY_20260417_200_C",
                "action": "BUY",
                "right": "C",
                "strike": "200",
                "expiry": "20260417",
                "quantity": 1,
            },
            {
                "contract_id": "SPY_20260417_210_C",
                "action": "SELL",
                "right": "C",
                "strike": "210",
                "expiry": "20260417",
                "quantity": 1,
            },
        ],
        "quotes": {
            "SPY_20260417_200_C": {"bid": "4.50", "ask": "4.70"},
            "SPY_20260417_210_C": {"bid": "2.00", "ask": "2.20"},
        },
        "order_payload": {
            "symbol": "SPY",
            "type": "combo",
            "action": "BUY",
            "quantity": 1,
            "limitPrice": "2.50",
            "legs": [
                {
                    "conId": 1001,
                    "action": "BUY",
                    "ratio": 1,
                    "exchange": "SMART",
                },
                {
                    "conId": 1002,
                    "action": "SELL",
                    "ratio": 1,
                    "exchange": "SMART",
                },
            ],
        },
    }


def _enriched_order_payload(order_payload: dict) -> dict:
    plan = _plan_payload()
    payload = dict(order_payload)
    enriched_legs = []
    for raw_leg, planned_leg in zip(payload.get("legs") or [], plan["legs"], strict=True):
        enriched_legs.append(
            {
                **dict(raw_leg),
                "symbol": plan["ticker"],
                "expiry": planned_leg["expiry"],
                "strike": float(planned_leg["strike"]),
                "right": planned_leg["right"],
                "ratio": planned_leg["quantity"],
            }
        )
    payload["legs"] = enriched_legs
    return payload


def _seed_session(session_id: str, order_payload: dict) -> None:
    engine = _pg_engine()
    with engine.begin() as conn:
        cwq.create_session(
            conn,
            session_id=session_id,
            ticker=_plan_payload()["ticker"],
            state="planned",
            structure_name="Bull Call Spread",
            intent="OPEN",
            payload=_enriched_order_payload(order_payload),
        )
    engine.dispose()


def test_plan_endpoint_returns_combo_mode_and_prices(client):
    resp = client.post("/wizard/plan", json=_plan_payload())

    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "COMBO"
    assert body["natural_price"] == "2.70"
    assert body["mid_price"] == "2.50"
    assert body["session_id"]


def test_plan_endpoint_rejects_mismatched_order_payload(client):
    payload = _plan_payload()
    payload["order_payload"]["legs"][1]["action"] = "BUY"

    resp = client.post("/wizard/plan", json=payload)

    assert resp.status_code == 400
    assert "does not match planned leg" in resp.json()["detail"]


def test_plan_endpoint_rejects_mismatched_top_level_payload(client):
    payload = _plan_payload()
    payload["order_payload"]["symbol"] = "MSFT"

    resp = client.post("/wizard/plan", json=payload)

    assert resp.status_code == 400
    assert "symbol does not match" in resp.json()["detail"]

    payload = _plan_payload()
    payload["order_payload"]["action"] = "SELL"

    resp = client.post("/wizard/plan", json=payload)

    assert resp.status_code == 400
    assert "action does not match" in resp.json()["detail"]


def test_session_list_get_stream_and_abort_endpoints(client):
    plan_resp = client.post("/wizard/plan", json=_plan_payload())
    assert plan_resp.status_code == 200
    session_id = plan_resp.json()["session_id"]

    list_resp = client.get("/wizard/sessions")
    assert list_resp.status_code == 200
    assert any(row["session_id"] == session_id for row in list_resp.json()["sessions"])

    get_resp = client.get(f"/wizard/sessions/{session_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["session_id"] == session_id
    assert get_resp.json()["state"] == "planned"

    stream_resp = client.get(f"/wizard/stream?session_id={session_id}")
    assert stream_resp.status_code == 200
    assert "text/event-stream" in stream_resp.headers["content-type"]
    assert f'"session_id":"{session_id}"' in stream_resp.text.replace(" ", "")

    abort_resp = client.post(f"/wizard/sessions/{session_id}/abort", json={})
    assert abort_resp.status_code == 200
    assert abort_resp.json()["state"] == "ABORTED"


def test_reprice_without_attempt_returns_conflict(client):
    plan_resp = client.post("/wizard/plan", json=_plan_payload())
    assert plan_resp.status_code == 200
    session_id = plan_resp.json()["session_id"]

    resp = client.post(
        f"/wizard/sessions/{session_id}/reprice",
        json={"target_price": "2.35"},
    )

    assert resp.status_code == 409
    assert "No combo attempt" in resp.json()["detail"]


def test_submit_endpoint_reuses_shared_combo_submission_path(client):
    _seed_session("wiz-submit-1", _plan_payload()["order_payload"])

    resp = client.post(
        "/wizard/sessions/wiz-submit-1/submit",
        json={"target_price": "2.45", "price_basis": "MID"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["submission_id"]
    assert body["echo"]["type"] == "combo"
    assert body["echo"]["action"] == "BUY"

    engine = _pg_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT COUNT(*) AS count, MIN(security_type) AS security_type "
                "FROM xenon.order_submissions WHERE client_attempt_id LIKE 'wiz:wiz-submit-1:combo:%'"
            )
        ).one()
    engine.dispose()

    assert row.count == 1
    assert row.security_type == "BAG"


def test_submit_endpoint_claims_session_before_live_order(monkeypatch, client):
    _seed_session("wiz-submit-claim", _plan_payload()["order_payload"])
    observed_claim: dict[str, str | None] = {}

    async def fake_place(body: dict) -> dict:
        engine = _pg_engine()
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT state, current_attempt_id FROM xenon.wizard_sessions WHERE session_id = 'wiz-submit-claim'"
                )
            ).fetchone()
        engine.dispose()
        observed_claim["state"] = row[0]
        observed_claim["current_attempt_id"] = row[1]
        return {
            "status": "ok",
            "orderId": 9001,
            "permId": 99001,
            "echo": body,
            "submission_id": "sub-claim",
        }

    monkeypatch.setattr(server_mod, "_orders_place_from_body", fake_place)

    resp = client.post(
        "/wizard/sessions/wiz-submit-claim/submit",
        json={"target_price": "2.45", "price_basis": "MID"},
    )

    assert resp.status_code == 200
    assert observed_claim["state"] == "submitting"
    assert observed_claim["current_attempt_id"] == resp.json()["attempt_id"]


def test_submit_endpoint_rejects_duplicate_submission(client):
    _seed_session("wiz-submit-duplicate", _plan_payload()["order_payload"])

    first = client.post(
        "/wizard/sessions/wiz-submit-duplicate/submit",
        json={"target_price": "2.45", "price_basis": "MID"},
    )
    second = client.post(
        "/wizard/sessions/wiz-submit-duplicate/submit",
        json={"target_price": "2.40", "price_basis": "CUSTOM"},
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert "already has a submitted combo attempt" in second.json()["detail"]

    engine = _pg_engine()
    with engine.connect() as conn:
        count = conn.execute(
            text(
                "SELECT COUNT(*) FROM xenon.order_submissions"
                " WHERE client_attempt_id LIKE 'wiz:wiz-submit-duplicate:combo:%'"
            )
        ).scalar()
    engine.dispose()

    assert count == 1


def test_submit_endpoint_rejects_aborted_session(client):
    plan_resp = client.post("/wizard/plan", json=_plan_payload())
    assert plan_resp.status_code == 200
    session_id = plan_resp.json()["session_id"]

    abort_resp = client.post(f"/wizard/sessions/{session_id}/abort", json={})
    submit_resp = client.post(
        f"/wizard/sessions/{session_id}/submit",
        json={"target_price": "2.45", "price_basis": "MID"},
    )

    assert abort_resp.status_code == 200
    assert submit_resp.status_code == 409
    assert "cannot submit from state ABORTED" in submit_resp.json()["detail"]


def test_abort_endpoint_rejects_submitting_session(client):
    _seed_session("wiz-abort-submitting", _plan_payload()["order_payload"])

    engine = _pg_engine()
    with engine.begin() as conn:
        cwq.update_session(
            conn,
            "wiz-abort-submitting",
            state="submitting",
            current_attempt_id="attempt-submitting",
        )
    engine.dispose()

    resp = client.post("/wizard/sessions/wiz-abort-submitting/abort", json={})

    assert resp.status_code == 409
    assert "cannot abort from state submitting" in resp.json()["detail"]


def test_submit_endpoint_preserves_order_helper_error_and_releases_claim(monkeypatch, client):
    _seed_session("wiz-submit-blocked", _plan_payload()["order_payload"])

    async def fake_place(_body: dict) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"detail": "Preflight blocked", "reason_code": "NAKED_SHORT"},
        )

    monkeypatch.setattr(server_mod, "_orders_place_from_body", fake_place)

    resp = client.post(
        "/wizard/sessions/wiz-submit-blocked/submit",
        json={"target_price": "2.45", "price_basis": "MID"},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Preflight blocked"

    engine = _pg_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT state, current_attempt_id FROM xenon.wizard_sessions WHERE session_id = 'wiz-submit-blocked'")
        ).fetchone()
        attempts = conn.execute(
            text("SELECT COUNT(*) FROM xenon.wizard_combo_attempts WHERE session_id = 'wiz-submit-blocked'")
        ).scalar()
    engine.dispose()

    assert row == ("planned", None)
    assert attempts == 0


def test_abort_endpoint_cancels_working_combo_before_marking_aborted(monkeypatch, client):
    _seed_session("wiz-abort-working", _plan_payload()["order_payload"])
    submit_resp = client.post(
        "/wizard/sessions/wiz-abort-working/submit",
        json={"target_price": "2.45", "price_basis": "MID"},
    )
    assert submit_resp.status_code == 200
    cancel_calls: list[dict] = []

    async def fake_cancel(body: dict) -> dict:
        cancel_calls.append(body)
        engine = _pg_engine()
        with engine.connect() as conn:
            state = conn.execute(
                text("SELECT state FROM xenon.wizard_sessions WHERE session_id = 'wiz-abort-working'")
            ).scalar()
        engine.dispose()
        assert state == "working"
        return {"status": "ok", "message": "Cancelled", "echo": body}

    monkeypatch.setattr(server_mod, "_orders_cancel_from_body", fake_cancel, raising=False)

    abort_resp = client.post("/wizard/sessions/wiz-abort-working/abort", json={})

    assert abort_resp.status_code == 200
    assert cancel_calls == [
        {
            "orderId": submit_resp.json()["orderId"],
            "permId": submit_resp.json()["permId"],
        }
    ]
    assert abort_resp.json()["state"] == "ABORTED"


def test_abort_endpoint_preserves_cancel_error_and_keeps_working(monkeypatch, client):
    _seed_session("wiz-abort-cancel-error", _plan_payload()["order_payload"])
    submit_resp = client.post(
        "/wizard/sessions/wiz-abort-cancel-error/submit",
        json={"target_price": "2.45", "price_basis": "MID"},
    )
    assert submit_resp.status_code == 200

    async def fake_cancel(_body: dict) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": {"reason_code": "IB_CONNECTION", "message": "offline"}},
        )

    monkeypatch.setattr(server_mod, "_orders_cancel_from_body", fake_cancel, raising=False)

    abort_resp = client.post("/wizard/sessions/wiz-abort-cancel-error/abort", json={})

    assert abort_resp.status_code == 503
    assert abort_resp.json()["detail"]["reason_code"] == "IB_CONNECTION"

    engine = _pg_engine()
    with engine.connect() as conn:
        state = conn.execute(
            text("SELECT state FROM xenon.wizard_sessions WHERE session_id = 'wiz-abort-cancel-error'")
        ).scalar()
    engine.dispose()
    assert state == "working"


def test_reprice_endpoint_modifies_live_combo_order_not_leg_orders(client):
    _seed_session("wiz-reprice-1", _plan_payload()["order_payload"])

    submit_resp = client.post(
        "/wizard/sessions/wiz-reprice-1/submit",
        json={"target_price": "2.45", "price_basis": "MID"},
    )
    assert submit_resp.status_code == 200
    submit_body = submit_resp.json()

    resp = client.post(
        "/wizard/sessions/wiz-reprice-1/reprice",
        json={"target_price": "2.35"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["echo"]["orderId"] == submit_body["orderId"]
    assert body["echo"]["newPrice"] == "2.35"


def test_reprice_endpoint_rejects_aborted_session(client):
    _seed_session("wiz-reprice-aborted", _plan_payload()["order_payload"])

    submit_resp = client.post(
        "/wizard/sessions/wiz-reprice-aborted/submit",
        json={"target_price": "2.45", "price_basis": "MID"},
    )
    abort_resp = client.post("/wizard/sessions/wiz-reprice-aborted/abort", json={})
    reprice_resp = client.post(
        "/wizard/sessions/wiz-reprice-aborted/reprice",
        json={"target_price": "2.35"},
    )

    assert submit_resp.status_code == 200
    assert abort_resp.status_code == 200
    assert reprice_resp.status_code == 409
    assert "cannot reprice from state ABORTED" in reprice_resp.json()["detail"]


@pytest.mark.parametrize("state", ["PROTECTION_PENDING", "PROTECTED"])
def test_reprice_endpoint_rejects_protection_states(client, state):
    _seed_session("wiz-reprice-protection", _plan_payload()["order_payload"])

    submit_resp = client.post(
        "/wizard/sessions/wiz-reprice-protection/submit",
        json={"target_price": "2.45", "price_basis": "MID"},
    )
    assert submit_resp.status_code == 200

    engine = _pg_engine()
    with engine.begin() as conn:
        cwq.update_session(conn, "wiz-reprice-protection", state=state)
    engine.dispose()

    reprice_resp = client.post(
        "/wizard/sessions/wiz-reprice-protection/reprice",
        json={"target_price": "2.35"},
    )

    assert reprice_resp.status_code == 409
    assert f"cannot reprice from state {state}" in reprice_resp.json()["detail"]


def test_reprice_endpoint_advances_modify_sequence(client):
    _seed_session("wiz-reprice-seq", _plan_payload()["order_payload"])

    submit_resp = client.post(
        "/wizard/sessions/wiz-reprice-seq/submit",
        json={"target_price": "2.45", "price_basis": "MID"},
    )
    assert submit_resp.status_code == 200

    first = client.post(
        "/wizard/sessions/wiz-reprice-seq/reprice",
        json={"target_price": "2.35"},
    )
    second = client.post(
        "/wizard/sessions/wiz-reprice-seq/reprice",
        json={"target_price": "2.30"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["echo"]["modifySequence"] == 1
    assert second.json()["echo"]["modifySequence"] == 2


def test_reprice_endpoint_advances_from_shared_order_store_sequence(client):
    _seed_session("wiz-reprice-external-seq", _plan_payload()["order_payload"])

    submit_resp = client.post(
        "/wizard/sessions/wiz-reprice-external-seq/submit",
        json={"target_price": "2.45", "price_basis": "MID"},
    )
    assert submit_resp.status_code == 200
    submit_body = submit_resp.json()

    engine = _pg_engine()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE xenon.order_submissions SET modify_sequence = 4 WHERE client_attempt_id = :caid"),
            {"caid": submit_body["client_attempt_id"]},
        )
    engine.dispose()

    resp = client.post(
        "/wizard/sessions/wiz-reprice-external-seq/reprice",
        json={"target_price": "2.35"},
    )

    assert resp.status_code == 200
    assert resp.json()["echo"]["modifySequence"] == 5


@pytest.mark.parametrize("state", ["planned", "working", "ABORTED"])
def test_protect_endpoint_rejects_non_filled_sessions_before_ib_pool(client, state):
    _seed_session("wiz-protect-state", _plan_payload()["order_payload"])

    engine = _pg_engine()
    with engine.begin() as conn:
        cwq.update_session(conn, "wiz-protect-state", state=state)
    engine.dispose()

    resp = client.post(
        "/wizard/sessions/wiz-protect-state/protect",
        json={
            "tp_target_price": "3.50",
            "alert_net_mid_threshold": "1.25",
            "polarity": "DEBIT",
        },
    )

    assert resp.status_code == 409
    assert f"cannot protect from state {state}" in resp.json()["detail"]


def test_protect_endpoint_is_idempotent_for_already_protected_without_ib_pool(client):
    _seed_session("wiz-protect-idempotent", _plan_payload()["order_payload"])

    engine = _pg_engine()
    with engine.begin() as conn:
        cwq.update_session(conn, "wiz-protect-idempotent", state="PROTECTED")
    engine.dispose()

    resp = client.post(
        "/wizard/sessions/wiz-protect-idempotent/protect",
        json={
            "tp_target_price": "3.50",
            "alert_net_mid_threshold": "1.25",
            "polarity": "DEBIT",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["state"] == "PROTECTED"
    assert resp.json()["noop"] is True
