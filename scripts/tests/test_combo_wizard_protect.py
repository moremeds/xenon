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

import json
import uuid
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
        if isinstance(reply, Exception):
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
    assert result["submission_id"] == "sub-1"
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
async def test_refused_place_returns_response_and_session_back_to_filled(place):
    sid = _init_session(state="FILLED")
    refusal = JSONResponse(status_code=400, content={"detail": "Uncovered short call on AAPL", "reason_code": "ETF_CALL_UNCOVERED"})
    place.replies[:] = [refusal]

    result = await _protect(sid)

    assert result is refusal
    assert json.loads(result.body)["reason_code"] == "ETF_CALL_UNCOVERED"
    assert _state(sid) == "FILLED"
    assert "PROTECTION_TP_REFUSED" in _event_kinds(sid)
    assert _config(sid) is None


@pytest.mark.asyncio
async def test_place_outcome_unknown_keeps_session_protection_pending(place):
    sid = _init_session(state="FILLED")
    place.replies[:] = [JSONResponse(status_code=502, content={"reason_code": "PLACE_OUTCOME_UNKNOWN"})]

    result = await _protect(sid)

    assert result.status_code == 502
    assert _state(sid) == "PROTECTION_PENDING"
    with pytest.raises(ValueError, match="cannot protect from state PROTECTION_PENDING"):
        await _protect(sid)
    assert len(place.calls) == 1


@pytest.mark.asyncio
async def test_place_exception_releases_claim(place):
    sid = _init_session(state="FILLED")
    place.replies[:] = [RuntimeError("db down")]

    with pytest.raises(RuntimeError):
        await _protect(sid)

    assert _state(sid) == "FILLED"


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
