"""Tests for combo_wizard.protect — post-fill protection pipeline.

The TP goes through ``order_entry.place`` (Gate 4 + reservation + order
owner); here that seam is faked. Route-level tests against the real Gate-4
path live in ``src/xenon/api/tests/test_wizard_routes.py``.

Covers:
- TP placed + Risk Alert armed → PROTECTED; TP body is a GTC SELL BAG with
  the stored per-leg actions and the signed target
- A refused place returns the response unchanged and the session goes back
  to FILLED; PLACE_OUTCOME_UNKNOWN keeps it PROTECTION_PENDING
- Risk Alert failure keeps the session PROTECTION_PENDING
- "Risk Alert" copy never says "stop-loss"
- Idempotent: re-running on an already-PROTECTED session is a no-op
- Signed combo pricing preserved (no abs() applied)
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from starlette.responses import JSONResponse

# Phase 3: opt out of Phase 2 BoundEngine binding — this file owns its
# own TRUNCATE-based teardown that needs committed cross-test state.
pytestmark = pytest.mark.committed_db
from sqlalchemy import create_engine, text

from xenon.api.services import order_entry
from xenon.db.queries import combo_wizard as cwq
from xenon.execution.combo_wizard import protect

# --------------------------------------------------------------------------
# Postgres helpers
# --------------------------------------------------------------------------

from xenon._test_db import sync_test_db_url as _sync_url  # worker-aware URL resolver


def _pg_engine():
    return create_engine(_sync_url(), pool_pre_ping=True)


def _cleanup(engine):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE xenon.wizard_protection CASCADE"))
        conn.execute(text("TRUNCATE xenon.wizard_events CASCADE"))
        conn.execute(text("TRUNCATE xenon.wizard_combo_attempts CASCADE"))
        conn.execute(text("TRUNCATE xenon.wizard_sessions CASCADE"))


@pytest.fixture(autouse=True)
def _setup_pg(monkeypatch):
    """Point get_sync_engine() at the test database and clean tables."""
    monkeypatch.setenv("DATABASE_URL", _sync_url())
    import xenon.db.engine as eng_mod

    monkeypatch.setattr(eng_mod, "_sync_engine", None)

    engine = _pg_engine()
    _cleanup(engine)
    engine.dispose()
    yield
    engine = _pg_engine()
    _cleanup(engine)
    engine.dispose()


# --------------------------------------------------------------------------
# Session seeding
# --------------------------------------------------------------------------


def _init_session(*, state: str = "FILLED", payload: dict | None = None) -> str:
    session_id = f"wiz-{uuid.uuid4().hex[:12]}"
    payload = payload or {
        "symbol": "AAPL",
        "type": "combo",
        "action": "BUY",
        "quantity": 1,
        "limitPrice": "2.50",
        "legs": [
            {"conId": 1001, "action": "BUY", "ratio": 1},
            {"conId": 1002, "action": "SELL", "ratio": 1},
        ],
    }
    engine = _pg_engine()
    with engine.begin() as conn:
        cwq.create_session(
            conn,
            session_id=session_id,
            ticker="AAPL",
            state=state,
            structure_name="Bull Call Spread",
            intent="OPEN",
            payload=payload,
        )
    engine.dispose()
    return session_id


def _init_scoped_session(*, account_env: str, broker_account: str, state: str = "FILLED") -> str:
    session_id = f"wiz-{uuid.uuid4().hex[:12]}"
    engine = _pg_engine()
    with engine.begin() as conn:
        cwq.create_session(
            conn,
            session_id=session_id,
            ticker="AAPL",
            state=state,
            structure_name="Bull Call Spread",
            intent="OPEN",
            payload={
                "symbol": "AAPL",
                "type": "combo",
                "action": "BUY",
                "quantity": 1,
                "legs": [
                    {"conId": 1001, "action": "BUY", "ratio": 1},
                    {"conId": 1002, "action": "SELL", "ratio": 1},
                ],
            },
            broker="IB",
            account_env=account_env,
            broker_account=broker_account,
        )
    engine.dispose()
    return session_id


# --------------------------------------------------------------------------
# order_entry.place stub
# --------------------------------------------------------------------------


@pytest.fixture
def place(monkeypatch):
    """Fake order_entry.place: records each body, answers from `replies`."""
    calls: list[dict] = []
    replies: list = [{"status": "ok", "orderId": 9001, "permId": 7001, "submission_id": "sub-1"}]

    async def fake(body: dict):
        calls.append(dict(body))
        reply = replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    monkeypatch.setattr(order_entry, "place", fake)
    return type("Place", (), {"calls": calls, "replies": replies})


def _state(sid: str) -> str:
    engine = _pg_engine()
    with engine.connect() as conn:
        state = conn.execute(
            text("SELECT state FROM xenon.wizard_sessions WHERE session_id = :sid"), {"sid": sid}
        ).scalar_one()
    engine.dispose()
    return state


def _event_kinds(sid: str) -> list[str]:
    engine = _pg_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT kind FROM xenon.wizard_events WHERE session_id = :sid"), {"sid": sid}
        ).fetchall()
    engine.dispose()
    return [row[0] for row in rows]


def _config(sid: str) -> dict | None:
    engine = _pg_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT config FROM xenon.wizard_protection WHERE session_id = :sid"), {"sid": sid}
        ).fetchone()
    engine.dispose()
    return row[0] if row else None


async def _protect(sid: str, *, target: str = "3.50", threshold: str = "1.25", polarity: str = "DEBIT"):
    return await protect.attach_protection(
        sid,
        tp_target_price=Decimal(target),
        alert_net_mid_threshold=Decimal(threshold),
        polarity=polarity,
    )


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_protection_rejects_explicit_scope_mismatch(place):
    sid = _init_scoped_session(account_env="live", broker_account="U1234567")

    with pytest.raises(ValueError, match="scope mismatch"):
        await _protect(sid, target="3.00")

    assert place.calls == []


@pytest.mark.asyncio
async def test_protection_success_places_gtc_sell_bag_and_transitions_protected(place):
    sid = _init_session(state="FILLED")

    result = await _protect(sid)

    assert result["state"] == "PROTECTED"
    assert result["tp_attached"] is True
    assert result["alert_armed"] is True
    assert result["tp_order_id"] == "7001"
    assert result["client_attempt_id"].startswith(f"wiz:{sid}:tp:")
    assert len(place.calls) == 1
    body = place.calls[0]
    assert (body["type"], body["action"], body["tif"], body["limitPrice"]) == ("combo", "SELL", "GTC", "3.50")
    # Per-leg actions are the stored structure; the SELL envelope reverses them.
    assert [leg["action"] for leg in body["legs"]] == ["BUY", "SELL"]
    assert body["quantity"] == 1
    assert body["client_attempt_id"].startswith(f"wiz:{sid}:tp:")
    assert _state(sid) == "PROTECTED"
    config = _config(sid)
    assert config["tp_enabled"] is True
    assert config["tp_ib_order_id"] == "7001"
    assert Decimal(config["tp_target_price"]) == Decimal("3.50")
    assert config["alert_enabled"] is True
    assert "PROTECTION_TP_ATTACHED" in _event_kinds(sid)


@pytest.mark.asyncio
async def test_tp_body_is_guaranteed_gtc_sell_bag_with_only_closing_fields(place):
    """The TP is always a GUARANTEED BAG (nonGuaranteed=0), whatever the
    entry payload held: a non-guaranteed close can leg out and leave the
    short call naked. Entry-only / stale keys never ride on the TP."""
    payload = {
        "symbol": "AAPL",
        "type": "combo",
        "action": "BUY",
        "quantity": 2,
        "limitPrice": "2.50",
        "tif": "DAY",
        "nonGuaranteed": 1,
        "orderRef": "leg_wizard:stale",
        "outsideRth": True,
        "account": "U9999999",
        "acknowledge_limit_override": True,
        "replaces": {"submission_id": "old"},
        "client_attempt_id": "wiz:old:combo:x",
        "legs": [
            {"symbol": "AAPL", "expiry": "20261218", "strike": 200.0, "right": "C", "action": "BUY", "ratio": 1,
             "conId": 1001, "limitPrice": 4.6, "exchange": "SMART"},
            {"symbol": "AAPL", "expiry": "20261218", "strike": 210.0, "right": "C", "action": "SELL", "ratio": 1,
             "conId": 1002, "limitPrice": 2.1, "exchange": "SMART"},
        ],
    }
    sid = _init_session(state="FILLED", payload=payload)

    await _protect(sid, target="-0.10", polarity="CREDIT")

    body = place.calls[0]
    assert set(body) == {"type", "symbol", "action", "quantity", "limitPrice", "tif", "nonGuaranteed", "legs", "client_attempt_id"}
    assert (body["type"], body["action"], body["tif"], body["nonGuaranteed"]) == ("combo", "SELL", "GTC", 0)
    assert body["limitPrice"] == "-0.10"  # signed, no abs()
    assert (body["symbol"], body["quantity"]) == ("AAPL", 2)
    assert body["client_attempt_id"].startswith(f"wiz:{sid}:tp:")
    # Stored per-leg actions, NOT flipped (the SELL envelope reverses them at IB).
    assert body["legs"] == [
        {"symbol": "AAPL", "expiry": "20261218", "strike": 200.0, "right": "C", "action": "BUY", "ratio": 1, "conId": 1001},
        {"symbol": "AAPL", "expiry": "20261218", "strike": 210.0, "right": "C", "action": "SELL", "ratio": 1, "conId": 1002},
    ]


@pytest.mark.asyncio
async def test_refused_place_returns_response_and_session_back_to_filled(place):
    sid = _init_session(state="FILLED")
    refusal = JSONResponse(status_code=400, content={"detail": "Uncovered short call on AAPL", "reason_code": "ETF_CALL_UNCOVERED"})
    place.replies[:] = [refusal]

    result = await _protect(sid)

    assert result is refusal
    assert json.loads(result.body)["reason_code"] == "ETF_CALL_UNCOVERED"
    assert _state(sid) == "FILLED"
    assert "PROTECTION_TP_REFUSED" in _event_kinds(sid)
    # Only the stored intent remains; nothing enabled.
    config = _config(sid)
    assert (config["tp_enabled"], config["alert_enabled"]) == (False, False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, reason_code, state",
    [
        (502, "IB_REJECT", "FILLED"),  # explicit IB reject: row REJECTED, nothing at IB
        (503, "ORDERS_HALTED", "FILLED"),  # owner proved nothing was sent
        (502, "SOMETHING_NEW", "PROTECTION_PENDING"),  # unknown 5xx: may be at IB
        (200, None, "PROTECTION_PENDING"),  # duplicate-of reply: an order exists
    ],
)
async def test_only_proven_not_sent_responses_release_the_claim(place, status, reason_code, state):
    sid = _init_session(state="FILLED")
    place.replies[:] = [JSONResponse(status_code=status, content={"reason_code": reason_code})]

    result = await _protect(sid)

    assert result.status_code == status
    assert _state(sid) == state


@pytest.mark.asyncio
async def test_place_outcome_unknown_keeps_session_protection_pending(place, monkeypatch):
    from types import SimpleNamespace

    from xenon.execution import orders_store

    sid = _init_session(state="FILLED")
    place.replies[:] = [JSONResponse(status_code=502, content={"reason_code": "PLACE_OUTCOME_UNKNOWN"})]

    result = await _protect(sid)

    assert result.status_code == 502
    assert _state(sid) == "PROTECTION_PENDING"
    stored_cid = _config(sid)["tp_client_attempt_id"]
    assert stored_cid == place.calls[0]["client_attempt_id"]
    # The stored attempt's row is still UNKNOWN: a retry keeps the claim (409), no send.
    seen = []

    def _lookup(user_id, cid, **scope):
        seen.append(cid)
        return SimpleNamespace(state="UNKNOWN", perm_id=None, ib_order_id=None)

    monkeypatch.setattr(orders_store, "lookup_by_attempt", _lookup)
    again = await _protect(sid)
    assert (again.status_code, json.loads(again.body)["reason_code"]) == (409, "PLACE_OUTCOME_UNKNOWN")
    assert seen == [stored_cid]
    assert len(place.calls) == 1
    assert _state(sid) == "PROTECTION_PENDING"


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [RuntimeError("db down"), asyncio.CancelledError()], ids=["error", "cancelled"])
async def test_place_exception_keeps_claim_fail_safe(place, exc):
    """order_entry.place raising says nothing about whether the owner job
    already started (a cancelled request after the start re-raises from
    OrderOwner.run while the worker may send). Fail safe: keep
    PROTECTION_PENDING so a second protect cannot place a second TP."""
    sid = _init_session(state="FILLED")
    place.replies[:] = [exc]

    with pytest.raises(type(exc)):
        await _protect(sid)

    assert _state(sid) == "PROTECTION_PENDING"
    assert "PROTECTION_PENDING" in _event_kinds(sid)


@pytest.mark.asyncio
async def test_protection_idempotent_on_already_protected(place):
    sid = _init_session(state="PROTECTED")

    result = await _protect(sid)

    assert result["state"] == "PROTECTED"
    assert result.get("noop") is True
    assert place.calls == []


@pytest.mark.asyncio
async def test_protection_rejects_non_filled_session(place):
    sid = _init_session(state="working")

    with pytest.raises(ValueError, match="cannot protect from state working"):
        await _protect(sid)

    assert place.calls == []
    assert _state(sid) == "working"


def test_risk_alert_copy_never_says_stop_loss():
    """Spec section 9.2 -- the popup must say Risk Alert -> Assisted Exit, NOT stop-loss."""
    text_out = protect.risk_alert_popup_copy()
    low = text_out.lower()
    assert "risk alert" in low
    assert "assisted exit" in low
    assert "stop-loss" not in low
    assert "stop loss" not in low


@pytest.mark.asyncio
async def test_risk_alert_failure_keeps_session_pending(place, monkeypatch):
    """A TP alone is not enough for the wizard's assisted-exit contract.

    If Risk Alert registration fails, the session must not be marked
    PROTECTED because the Risk Alert task only polls alert_enabled rows.
    """
    sid = _init_session(state="FILLED")

    def _fail(*_a, **_k):
        raise RuntimeError("alert store unavailable")

    monkeypatch.setattr(protect, "_register_risk_alert", _fail)

    result = await _protect(sid)

    assert result["tp_attached"] is True
    assert result["alert_armed"] is False
    assert result["state"] == "PROTECTION_PENDING"
    assert _state(sid) == "PROTECTION_PENDING"
    config = _config(sid)
    assert config["tp_enabled"] is True
    assert config["alert_enabled"] is False


@pytest.mark.asyncio
async def test_signed_combo_pricing_preserved_for_credit(place):
    """CREDIT spreads have negative net prices -- protect must not apply abs()."""
    sid = _init_session(state="FILLED")

    await _protect(sid, target="-0.10", threshold="-0.45", polarity="CREDIT")

    assert place.calls[0]["limitPrice"] == "-0.10"
    config = _config(sid)
    assert Decimal(config["tp_target_price"]) == Decimal("-0.10")
    assert Decimal(config["alert_net_mid_threshold"]) == Decimal("-0.45")
    assert "RISK_ALERT_REGISTERED" in _event_kinds(sid)


@pytest.mark.asyncio
async def test_retry_while_first_protect_is_in_flight_gets_409_without_send(monkeypatch):
    """Between the claim and the reservation insert no order_submissions row
    exists; a concurrent retry must not read "no row" as "nothing sent"."""
    sid = _init_session(state="FILLED")
    gate = asyncio.Event()
    calls: list[dict] = []

    async def slow_place(body):
        calls.append(body)
        await gate.wait()
        return {"status": "ok", "orderId": 1, "permId": 2}

    monkeypatch.setattr(order_entry, "place", slow_place)
    first = asyncio.ensure_future(_protect(sid))
    await asyncio.sleep(0)
    while not calls:
        await asyncio.sleep(0.01)

    second = await _protect(sid)
    assert (second.status_code, json.loads(second.body)["reason_code"]) == (409, "PLACE_OUTCOME_UNKNOWN")

    gate.set()
    assert (await first)["state"] == "PROTECTED"
    assert len(calls) == 1


async def _pending_with_dead_tp(place, monkeypatch, *, evidence) -> str:
    """First protect is UNKNOWN; the stored TP row then reads CANCELLED with a
    permId and ``evidence`` (an ExecutionEvidence or an exception to raise)."""
    from types import SimpleNamespace

    from xenon.execution import orders_store

    monkeypatch.delenv("XENON_IB_ACTIVITY_POLLER", raising=False)
    monkeypatch.delenv("XENON_IB_ACTIVITY_POLL_S", raising=False)
    sid = _init_session(state="FILLED")
    place.replies[:] = [JSONResponse(status_code=502, content={"reason_code": "PLACE_OUTCOME_UNKNOWN"})]
    await _protect(sid)
    monkeypatch.setattr(
        orders_store,
        "lookup_by_attempt",
        lambda *_a, **_k: SimpleNamespace(
            submission_id="sub-1", state="CANCELLED", perm_id="77", ib_order_id="7", quantity=1
        ),
    )

    def _evidence(*_a, **_k):
        if isinstance(evidence, Exception):
            raise evidence
        return evidence

    monkeypatch.setattr(orders_store, "execution_evidence", _evidence)
    return sid


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evidence_kind",
    ["lookup_raises", "leg_fills_without_envelope"],
)
async def test_dead_tp_with_unknown_executions_keeps_claim_and_sends_nothing(place, monkeypatch, evidence_kind):
    from xenon.execution import orders_store

    evidence = (
        RuntimeError("db down")
        if evidence_kind == "lookup_raises"
        else orders_store.ExecutionEvidence(
            filled_qty=0, fill_rows=2, envelope_qty=Decimal(0), terminal_at=datetime.now(timezone.utc) - timedelta(hours=1)
        )
    )
    sid = await _pending_with_dead_tp(place, monkeypatch, evidence=evidence)

    again = await _protect(sid)

    body = json.loads(again.body)
    assert (again.status_code, body["reason_code"]) == (409, "PROTECT_TP_PARTIAL")
    assert body["filled"] is None  # executed quantity unknown
    assert len(place.calls) == 1  # only the first, uncertain send
    assert _state(sid) == "PROTECTION_PENDING"
    assert "PROTECTION_TP_PARTIAL" in _event_kinds(sid)


@pytest.mark.asyncio
async def test_dead_tp_with_proven_zero_executions_places_one_new_tp(place, monkeypatch):
    from xenon.execution import orders_store

    sid = await _pending_with_dead_tp(
        place, monkeypatch, evidence=orders_store.ExecutionEvidence(
            filled_qty=0, fill_rows=0, envelope_qty=Decimal(0), terminal_at=datetime.now(timezone.utc) - timedelta(hours=1)
        ),
    )
    place.replies.append({"status": "ok", "orderId": 9002, "permId": 7002})

    again = await _protect(sid)

    assert again["state"] == "PROTECTED"
    assert len(place.calls) == 2
    assert place.calls[1]["quantity"] == place.calls[0]["quantity"]
