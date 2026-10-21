from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from xenon._test_db import committed_pass_stamp
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from starlette.responses import JSONResponse

from xenon.api import server as server_mod
from xenon.db.queries import combo_wizard as cwq
from xenon.api.services import order_entry

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
        conn.execute(text("TRUNCATE xenon.account_snapshots CASCADE"))


def _seed_gate4_snapshot(engine) -> None:
    """Every wizard submit/reprice drives the real Gate-4 path — seed the
    observation-stamped snapshot (100 SPY shares covers the spread's SELL
    leg) that ib_sync would have written for the harness scope."""
    import json as _json
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    payload = {
        "positions": [
            {
                "ticker": "SPY",
                "structure_type": "Stock",
                "direction": "LONG",
                "contracts": 100,
                "expiry": None,
                "legs": [{"direction": "LONG", "type": "Stock", "contracts": 100, "strike": 0.0, "account": "DU0000000"}],
            }
        ],
        "available_funds": 0,
        "observation": {
            "as_of": now.isoformat(),
            "complete": True,
            "account": "DU0000000",
            "reads": {"positions": True, "open_orders": True, "executions": True},
            # xid8 ordering key + cluster id from a committed txn, as ib_sync stamps them.
            **committed_pass_stamp(),
        },
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO xenon.account_snapshots
                  (account, bankroll, payload, snapshot_at, broker, account_env, broker_account)
                VALUES ('DU0000000', 0, CAST(:p AS jsonb), :at, 'IB', 'paper', 'DU0000000')
                """
            ),
            {"p": _json.dumps(payload), "at": now},
        )


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
    _seed_gate4_snapshot(engine)
    engine.dispose()
    yield
    engine = _pg_engine()
    _truncate(engine)
    engine.dispose()


@pytest.fixture(autouse=True)
def _force_test_mode_on(monkeypatch):
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")

    async def _ib_qualified(body):
        # Stand-in for reqContractDetails (standard SPY options).
        return [
            {"con_id": 7000 + i, "tc": "SPY", "mult": 100, "und_sec": "STK"}
            for i, _ in enumerate(order_entry._gate4_leg_specs(body))
        ]

    monkeypatch.setattr(order_entry, "_resolve_gate4_leg_meta", _ib_qualified)
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

    monkeypatch.setattr(order_entry, "place", fake_place)

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

    monkeypatch.setattr(order_entry, "place", fake_place)

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

    monkeypatch.setattr(order_entry, "cancel", fake_cancel)

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

    monkeypatch.setattr(order_entry, "cancel", fake_cancel)

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

# --------------------------------------------------------------------------
# Wizard protect take-profit goes through order_entry.place: Gate-4 coverage
# + an order_submissions reservation, then the single order owner (PR-10).
# No request-path reconnect (A2); halt + broker readiness are re-checked
# right before the send (A4).
# --------------------------------------------------------------------------

_PROTECT_BODY = {"tp_target_price": "3.50", "alert_net_mid_threshold": "1.25", "polarity": "DEBIT"}


def _set_state(session_id: str, state: str) -> None:
    engine = _pg_engine()
    with engine.begin() as conn:
        cwq.update_session(conn, session_id, state=state)
    engine.dispose()


def _session_state(session_id: str) -> str:
    engine = _pg_engine()
    with engine.connect() as conn:
        state = conn.execute(
            text("SELECT state FROM xenon.wizard_sessions WHERE session_id = :sid"), {"sid": session_id}
        ).scalar_one()
    engine.dispose()
    return state


def _tp_rows(session_id: str) -> list:
    engine = _pg_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT action, security_type, tif, state, limit_price, legs, ib_order_id, perm_id "
                "FROM xenon.order_submissions WHERE client_attempt_id LIKE :pat"
            ),
            {"pat": f"wiz:{session_id}:tp:%"},
        ).all()
    engine.dispose()
    return [row._mapping for row in rows]


@pytest.fixture
def filled_session():
    _seed_session("wiz-protect-owner", _plan_payload()["order_payload"])
    _set_state("wiz-protect-owner", "FILLED")
    return "wiz-protect-owner"


@pytest.fixture
def protect_owner(monkeypatch):
    """Owner over a fake orders pool; ib_pool.acquire must never run; the
    place core is a fake that records its params and sends once."""
    from scripts.tests.order_owner_fakes import install_cores, install_owner

    class _NoAcquirePool:
        def acquire(self, role):
            raise AssertionError("wizard protect must not acquire (reconnect) the orders role")

        def is_connected(self, role):
            raise AssertionError("wizard protect routes through the order owner")

    monkeypatch.setattr(server_mod, "ib_pool", _NoAcquirePool())
    owner, pool = install_owner(monkeypatch)
    calls: list[dict] = []

    def fake_place(ib_client, params):
        calls.append(params)
        ib_client.place_order("tp-contract", "tp-order")
        return {"status": "ok", "outcome": "accepted", "orderId": 9101, "permId": 9102, "initialStatus": "Submitted"}

    install_cores(monkeypatch, place=fake_place)
    return SimpleNamespace(owner=owner, pool=pool, calls=calls)


def _real_place_path(monkeypatch) -> None:
    # Test mode short-circuits _place_reserved before the owner; protect must
    # take the real owner path here.
    monkeypatch.setenv("XENON_API_TEST_MODE", "0")


def test_protect_reserves_tp_through_gate4_and_places_once_on_the_owner(
    monkeypatch, client, filled_session, protect_owner
):
    monkeypatch.delenv("XENON_GATE4_NETTING", raising=False)
    _real_place_path(monkeypatch)

    resp = client.post(f"/wizard/sessions/{filled_session}/protect", json=_PROTECT_BODY)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "PROTECTED"
    assert body["tp_attached"] is True and body["alert_armed"] is True
    assert body["tp_order_id"] == "9102"
    # Exactly one owner send, with a fresh readiness check before it.
    assert len(protect_owner.calls) == 1
    assert protect_owner.pool.client.sent == [("place", ("tp-contract", "tp-order"))]
    assert protect_owner.pool.client.ready_checks == 1
    params = protect_owner.calls[0]
    # SELL envelope closes the BUY-opened combo; per-leg actions are the
    # session's stored structure (never flipped a second time); signed LMT.
    assert params["type"] == "combo" and params["action"] == "SELL" and params["tif"] == "GTC"
    assert params["nonGuaranteed"] == 0
    assert [(leg["action"], leg["strike"], leg["right"]) for leg in params["legs"]] == [
        ("BUY", 200.0, "C"),
        ("SELL", 210.0, "C"),
    ]
    assert params["limitPrice"] == "3.50"
    assert params["account"] == "DU0000000"
    assert params["submission_id"]
    # One Gate-4 reservation row, now WORKING with the IB ids.
    rows = _tp_rows(filled_session)
    assert len(rows) == 1
    row = rows[0]
    assert (row["action"], row["security_type"], row["tif"], row["state"]) == ("SELL", "BAG", "GTC", "WORKING")
    assert (row["ib_order_id"], row["perm_id"]) == ("9101", "9102")
    assert [leg["action"] for leg in row["legs"]["legs"]] == ["BUY", "SELL"]
    assert _session_state(filled_session) == "PROTECTED"


def _replace_snapshot(positions: list[dict]) -> None:
    """Latest scoped snapshot = exactly `positions` (shape ib_sync writes)."""
    import json as _json
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    payload = {
        "positions": positions,
        "available_funds": 0,
        "observation": {
            "as_of": now.isoformat(),
            "complete": True,
            "account": "DU0000000",
            "reads": {"positions": True, "open_orders": True, "executions": True},
            **committed_pass_stamp(),
        },
    }
    engine = _pg_engine()
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE xenon.account_snapshots CASCADE"))
        conn.execute(
            text(
                """
                INSERT INTO xenon.account_snapshots
                  (account, bankroll, payload, snapshot_at, broker, account_env, broker_account)
                VALUES ('DU0000000', 0, CAST(:p AS jsonb), :at, 'IB', 'paper', 'DU0000000')
                """
            ),
            {"p": _json.dumps(payload), "at": now},
        )
    engine.dispose()


def _spy_call(qty: int, strike: float) -> dict:
    """The filled vertical's legs as ib_sync records them (expiry of _plan_payload)."""
    return {
        "ticker": "SPY",
        "expiry": "2026-04-17",
        "legs": [
            {
                "direction": "LONG" if qty >= 0 else "SHORT",
                "type": "Call",
                "contracts": abs(qty),
                "strike": strike,
                "account": "DU0000000",
                "tc": "SPY",
                "mult": 100,
                "und_sec": "STK",
            }
        ],
    }


def test_protect_guaranteed_tp_closes_vertical_under_gate4_netting(monkeypatch, client, filled_session, protect_owner):
    """Real Gate-4 evaluation: the account holds only the filled call debit
    vertical (long 200C, short 210C) and nothing else works. The TP is a
    GUARANTEED BAG, so with netting on its legs net per key → no new demand:
    accepted, one reservation row recording guaranteed=True, one owner send
    with NonGuaranteed=0."""
    _replace_snapshot([_spy_call(1, 200.0), _spy_call(-1, 210.0)])
    monkeypatch.setenv("XENON_GATE4_NETTING", "1")
    _real_place_path(monkeypatch)

    resp = client.post(f"/wizard/sessions/{filled_session}/protect", json=_PROTECT_BODY)

    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "PROTECTED"
    assert len(protect_owner.calls) == 1
    assert protect_owner.calls[0]["nonGuaranteed"] == 0
    rows = _tp_rows(filled_session)
    assert len(rows) == 1
    assert rows[0]["legs"]["guaranteed"] is True
    assert [leg["action"] for leg in rows[0]["legs"]["legs"]] == ["BUY", "SELL"]


def test_protect_guaranteed_tp_without_netting_is_refused_before_ib(monkeypatch, client, filled_session, protect_owner):
    """Same holdings, netting off (the default: XENON_GATE4_NETTING unset).
    Leg by leg the SELL 200C would leave the held short 210C uncovered, so
    Gate 4 refuses with its leg-out code: no IB call, no reservation,
    top-level reason_code, session back to FILLED."""
    _replace_snapshot([_spy_call(1, 200.0), _spy_call(-1, 210.0)])
    monkeypatch.delenv("XENON_GATE4_NETTING", raising=False)
    _real_place_path(monkeypatch)

    resp = client.post(f"/wizard/sessions/{filled_session}/protect", json=_PROTECT_BODY)

    assert resp.status_code == 400, resp.text
    assert resp.json()["reason_code"] == "COMBO_SPLIT_REQUIRED"
    assert protect_owner.calls == [] and protect_owner.pool.client.sent == []
    assert _tp_rows(filled_session) == []
    assert _session_state(filled_session) == "FILLED"


def test_protect_refused_when_another_working_order_uses_the_cover(monkeypatch, client, filled_session, protect_owner):
    """A working reservation changes the TP decision. The only cover is 100
    SPY shares (autouse snapshot, no option holdings). Alone, the TP passes
    leg by leg (its SELL 200C is covered by the shares — see the owner test
    above). Once another working order uses those shares, the SELL 200C leg
    is uncovered → refused before IB, no reservation, session back to FILLED.

    The code is COMBO_SPLIT_REQUIRED, not ETF_CALL_UNCOVERED: a guaranteed
    same-key vertical close nets to zero call demand (coverage.py: netting
    per Key, Key has no strike), so it can never *increase* uncovered calls
    under netting and gate4 reports the leg-out code (gate4.py split_required
    = not netting_enabled() and _accepted_with_netting)."""
    monkeypatch.delenv("XENON_GATE4_NETTING", raising=False)
    _seed_session("wiz-protect-other", _plan_payload()["order_payload"])
    # Test mode: the other order reserves through Gate 4 and goes WORKING
    # without the owner.
    other = client.post("/wizard/sessions/wiz-protect-other/submit", json={"target_price": "2.45"})
    assert other.status_code == 200, other.text
    _real_place_path(monkeypatch)

    resp = client.post(f"/wizard/sessions/{filled_session}/protect", json=_PROTECT_BODY)

    assert resp.status_code == 400, resp.text
    assert resp.json()["reason_code"] == "COMBO_SPLIT_REQUIRED"
    assert protect_owner.calls == [] and protect_owner.pool.client.sent == []
    assert _tp_rows(filled_session) == []
    assert _session_state(filled_session) == "FILLED"


def test_protect_refused_when_orders_halted(monkeypatch, client, filled_session, protect_owner):
    _real_place_path(monkeypatch)
    protect_owner.owner._latch_halt("test")
    resp = client.post(f"/wizard/sessions/{filled_session}/protect", json=_PROTECT_BODY)
    assert (resp.status_code, resp.json()["reason_code"]) == (503, "ORDERS_HALTED")
    assert protect_owner.calls == [] and protect_owner.pool.client.sent == []
    assert _session_state(filled_session) == "FILLED"


def test_protect_refused_when_orders_disconnected_without_reconnect(monkeypatch, client, filled_session, protect_owner):
    _real_place_path(monkeypatch)
    protect_owner.pool.connected["orders"] = False
    resp = client.post(f"/wizard/sessions/{filled_session}/protect", json=_PROTECT_BODY)
    assert (resp.status_code, resp.json()["reason_code"]) == (503, "IB_BROKER_DISCONNECTED")
    assert protect_owner.pool.reconnects == [] and protect_owner.calls == []
    assert _session_state(filled_session) == "FILLED"


def test_protect_broker_not_ready_at_send_sends_nothing(monkeypatch, client, filled_session, protect_owner):
    """Admitted (socket up, readiness not LOST) but the round trip before the
    core does not confirm the broker → refused before the place core runs."""
    _real_place_path(monkeypatch)
    protect_owner.pool.client.readiness = "unknown"
    resp = client.post(f"/wizard/sessions/{filled_session}/protect", json=_PROTECT_BODY)
    assert (resp.status_code, resp.json()["reason_code"]) == (503, "IB_BROKER_DISCONNECTED")
    assert protect_owner.calls == []
    assert protect_owner.pool.client.sent == []


def test_protect_outcome_unknown_keeps_session_claimed(monkeypatch, client, filled_session, protect_owner):
    """IB may hold the TP: the session must not return to FILLED, or a second
    protect would place a duplicate TP."""
    from scripts.tests.order_owner_fakes import install_cores

    _real_place_path(monkeypatch)

    def died_after_send(ib_client, params):
        ib_client.place_order("tp-contract", "tp-order")
        return {"status": "uncertain", "message": "socket closed after send"}

    install_cores(monkeypatch, place=died_after_send)
    resp = client.post(f"/wizard/sessions/{filled_session}/protect", json=_PROTECT_BODY)
    assert (resp.status_code, resp.json()["reason_code"]) == (502, "PLACE_OUTCOME_UNKNOWN")
    assert _session_state(filled_session) == "PROTECTION_PENDING"
    again = client.post(f"/wizard/sessions/{filled_session}/protect", json=_PROTECT_BODY)
    assert again.status_code == 409
    assert len(_tp_rows(filled_session)) == 1
