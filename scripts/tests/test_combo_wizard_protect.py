"""Tests for combo_wizard.protect — post-fill protection pipeline.

Covers:
- Successful TP + Risk Alert attach transitions PROTECTION_PENDING → PROTECTED
- Retry with backoff on transient failures
- Terminal failure leaves session in PROTECTION_PENDING (not PROTECTED)
- Naked-short guard: TP that would short an uncovered leg is refused, Risk Alert still armed
- "Risk Alert" copy never says "stop-loss"
- Idempotent: re-running on an already-PROTECTED session is a no-op
- Signed combo pricing preserved (no abs() applied)
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text

from xenon.db.queries import combo_wizard as cwq
from xenon.execution.combo_wizard import protect

# --------------------------------------------------------------------------
# Postgres helpers (same pattern as test_combo_wizard_ib_adapter.py)
# --------------------------------------------------------------------------

_TEST_DB_URL = os.environ.get(
    "DATABASE_URL_TEST",
    "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
)
_SYNC_URL = _TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql+psycopg://")


def _pg_engine():
    return create_engine(_SYNC_URL, pool_pre_ping=True)


def _cleanup(engine):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE xenon.position_protection CASCADE"))
        conn.execute(text("TRUNCATE xenon.wizard_events CASCADE"))
        conn.execute(text("TRUNCATE xenon.wizard_combo_attempts CASCADE"))
        conn.execute(text("TRUNCATE xenon.wizard_sessions CASCADE"))


@pytest.fixture(autouse=True)
def _setup_pg(monkeypatch):
    """Point get_sync_engine() at the test database and clean tables."""
    monkeypatch.setenv("DATABASE_URL", _SYNC_URL)
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
            {
                "conId": 1001,
                "sec_type": "OPT",
                "symbol": "AAPL",
                "expiry": "2026-06-19",
                "strike": 200,
                "right": "C",
                "action": "BUY",
                "ratio": 1,
            },
            {
                "conId": 1002,
                "sec_type": "OPT",
                "symbol": "AAPL",
                "expiry": "2026-06-19",
                "strike": 205,
                "right": "C",
                "action": "SELL",
                "ratio": 1,
            },
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
        cwq.create_attempt(
            conn,
            attempt_id=f"att-{session_id}",
            session_id=session_id,
            ticker="AAPL",
            structure_name="Bull Call Spread",
            state="FILLED",
            legs=payload["legs"],
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
                    {
                        "conId": 1001,
                        "sec_type": "OPT",
                        "symbol": "AAPL",
                        "expiry": "2026-06-19",
                        "strike": 200,
                        "right": "C",
                        "action": "BUY",
                        "ratio": 1,
                    },
                    {
                        "conId": 1002,
                        "sec_type": "OPT",
                        "symbol": "AAPL",
                        "expiry": "2026-06-19",
                        "strike": 205,
                        "right": "C",
                        "action": "SELL",
                        "ratio": 1,
                    },
                ],
            },
            broker="IB",
            account_env=account_env,
            broker_account=broker_account,
        )
        cwq.create_attempt(
            conn,
            attempt_id=f"att-{session_id}",
            session_id=session_id,
            ticker="AAPL",
            structure_name="Bull Call Spread",
            state="FILLED",
            legs=[
                {
                    "conId": 1001,
                    "sec_type": "OPT",
                    "symbol": "AAPL",
                    "expiry": "2026-06-19",
                    "strike": 200,
                    "right": "C",
                    "action": "BUY",
                    "ratio": 1,
                },
                {
                    "conId": 1002,
                    "sec_type": "OPT",
                    "symbol": "AAPL",
                    "expiry": "2026-06-19",
                    "strike": 205,
                    "right": "C",
                    "action": "SELL",
                    "ratio": 1,
                },
            ],
            broker="IB",
            account_env=account_env,
            broker_account=broker_account,
        )
    engine.dispose()
    return session_id


def _fetch_protection_row(conn, sid: str):
    return conn.execute(
        text(
            """
            SELECT rule_kind, state, asset_class, position_descriptor, config
            FROM xenon.position_protection
            WHERE rule_kind = 'combo_tp_alert'
              AND position_descriptor->>'wizard_session_id' = :sid
            ORDER BY protection_id DESC
            LIMIT 1
            """
        ),
        {"sid": sid},
    ).fetchone()


# --------------------------------------------------------------------------
# IB stubs
# --------------------------------------------------------------------------


class _StubIB:
    """Injectable stub for the IB side of protect. `tp_attach` and `arm_alert`
    return a dict-shaped ack or raise.
    """

    def __init__(self, *, tp_acks: list, alert_acks: list):
        self._tp_acks = list(tp_acks)
        self._alert_acks = list(alert_acks)
        self.tp_calls: list = []
        self.alert_calls: list = []

    def place_combo_tp(self, *, session_id, legs, target_price, quantity):
        self.tp_calls.append({"session_id": session_id, "target_price": target_price})
        ack = self._tp_acks.pop(0) if self._tp_acks else Exception("no more tp acks")
        if isinstance(ack, Exception):
            raise ack
        return ack

    def register_risk_alert(self, *, session_id, threshold, polarity):
        self.alert_calls.append({"session_id": session_id, "threshold": threshold, "polarity": polarity})
        ack = self._alert_acks.pop(0) if self._alert_acks else Exception("no more alert acks")
        if isinstance(ack, Exception):
            raise ack
        return ack


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


def test_protection_rejects_explicit_scope_mismatch():
    sid = _init_scoped_session(account_env="live", broker_account="U1234567")
    ib = _StubIB(tp_acks=[{"order_id": "tp-1"}], alert_acks=[{"virtual_id": "alert-1"}])

    with pytest.raises(ValueError, match="scope mismatch"):
        protect.attach_protection(
            session_id=sid,
            tp_target_price=Decimal("3.00"),
            alert_net_mid_threshold=Decimal("1.25"),
            ib=ib,
        )

    assert ib.tp_calls == []
    assert ib.alert_calls == []


def test_protection_success_transitions_protected(monkeypatch):
    sid = _init_session(state="FILLED")

    ib = _StubIB(
        tp_acks=[{"order_id": 9001, "perm_id": "p-9001"}],
        alert_acks=[{"virtual_id": "alert-1"}],
    )

    result = protect.attach_protection(
        sid,
        ib=ib,
        tp_target_price=Decimal("3.50"),
        alert_net_mid_threshold=Decimal("1.25"),
        polarity="DEBIT",
        sleep=lambda _s: None,
    )

    assert result["state"] == "PROTECTED"
    assert result["tp_attached"] is True
    assert result["alert_armed"] is True
    assert result["attempts"] == 1

    engine = _pg_engine()
    with engine.connect() as conn:
        session = conn.execute(
            text("SELECT state FROM xenon.wizard_sessions WHERE session_id = :sid"),
            {"sid": sid},
        ).fetchone()
        prot = _fetch_protection_row(conn, sid)
    engine.dispose()

    assert session[0] == "PROTECTED"
    assert prot is not None
    assert prot.rule_kind == "combo_tp_alert"
    assert prot.state == "PENDING_ARM"
    assert prot.asset_class == "debit_combo"
    assert prot.config["auto_place"] is False
    assert prot.position_descriptor["wizard_session_id"] == sid
    assert prot.position_descriptor["asset_class"] == "debit_combo"
    assert prot.position_descriptor["anchor_price"] == 2.5
    assert prot.position_descriptor["legs"][0]["symbol"] == "AAPL"
    assert prot.position_descriptor["legs"][0]["con_id"] == 1001
    config = prot.config
    assert config["tp_enabled"] is True
    assert Decimal(config["tp_target_price"]) == Decimal("3.50")
    assert config["alert_enabled"] is True
    assert Decimal(config["alert_net_mid_threshold"]) == Decimal("1.25")


def test_protection_pending_retries_then_fails_if_tp_attach_never_acks(monkeypatch):
    sid = _init_session(state="FILLED")

    ib = _StubIB(
        tp_acks=[RuntimeError("timeout"), RuntimeError("timeout"), RuntimeError("timeout")],
        alert_acks=[{"virtual_id": "alert-1"}],  # not reached
    )
    sleeps: list[float] = []

    result = protect.attach_protection(
        sid,
        ib=ib,
        tp_target_price=Decimal("3.50"),
        alert_net_mid_threshold=Decimal("1.25"),
        polarity="DEBIT",
        sleep=sleeps.append,
        max_attempts=3,
        base_backoff=2.0,
    )

    assert result["state"] == "PROTECTION_PENDING"
    assert result["tp_attached"] is False
    assert result["attempts"] == 3
    assert sleeps == [2.0, 4.0]

    engine = _pg_engine()
    with engine.connect() as conn:
        state = conn.execute(
            text("SELECT state FROM xenon.wizard_sessions WHERE session_id = :sid"),
            {"sid": sid},
        ).fetchone()[0]
        events = conn.execute(
            text("SELECT kind FROM xenon.wizard_events WHERE session_id = :sid"),
            {"sid": sid},
        ).fetchall()
    engine.dispose()

    assert state == "PROTECTION_PENDING"
    kinds = [row[0] for row in events]
    assert any("PROTECTION" in k for k in kinds)


def test_protection_idempotent_on_already_protected(monkeypatch):
    sid = _init_session(state="PROTECTED")

    ib = _StubIB(tp_acks=[], alert_acks=[])

    result = protect.attach_protection(
        sid,
        ib=ib,
        tp_target_price=Decimal("3.50"),
        alert_net_mid_threshold=Decimal("1.25"),
        polarity="DEBIT",
        sleep=lambda _s: None,
    )

    assert result["state"] == "PROTECTED"
    assert result.get("noop") is True
    assert ib.tp_calls == []
    assert ib.alert_calls == []


def test_protection_rejects_non_filled_session(monkeypatch):
    sid = _init_session(state="working")

    ib = _StubIB(tp_acks=[{"order_id": 9001}], alert_acks=[{"virtual_id": "alert-1"}])

    with pytest.raises(ValueError, match="cannot protect from state working"):
        protect.attach_protection(
            sid,
            ib=ib,
            tp_target_price=Decimal("3.50"),
            alert_net_mid_threshold=Decimal("1.25"),
            polarity="DEBIT",
            sleep=lambda _s: None,
        )

    assert ib.tp_calls == []
    assert ib.alert_calls == []


def test_naked_short_guard_refuses_tp_but_arms_alert(monkeypatch):
    """If the TP would short an uncovered call leg, we skip the TP and route
    to Risk Alert only. This keeps Gate-4 intact."""
    payload = {
        "symbol": "AAPL",
        "type": "combo",
        "action": "BUY",
        "quantity": 1,
        "limitPrice": "-0.20",
        "legs": [
            {"conId": 2001, "action": "SELL", "ratio": 1, "right": "C", "strike": "200"},
            {"conId": 2002, "action": "BUY", "ratio": 1, "right": "P", "strike": "180"},
        ],
    }
    sid = _init_session(state="FILLED", payload=payload)

    ib = _StubIB(
        tp_acks=[{"order_id": 9001, "perm_id": "p"}],
        alert_acks=[{"virtual_id": "alert-1"}],
    )

    result = protect.attach_protection(
        sid,
        ib=ib,
        tp_target_price=Decimal("-0.50"),
        alert_net_mid_threshold=Decimal("-0.80"),
        polarity="CREDIT",
        sleep=lambda _s: None,
    )

    assert result["tp_attached"] is False
    assert result["tp_refused_reason"] == "NAKED_SHORT_GUARD"
    assert result["alert_armed"] is True
    assert ib.tp_calls == []


def test_risk_alert_copy_never_says_stop_loss():
    """Spec section 9.2 -- the popup must say Risk Alert -> Assisted Exit, NOT stop-loss."""
    text_out = protect.risk_alert_popup_copy()
    low = text_out.lower()
    assert "risk alert" in low
    assert "assisted exit" in low
    assert "stop-loss" not in low
    assert "stop loss" not in low


def test_naked_short_guard_error_short_circuits_retry_loop(monkeypatch):
    """If the adapter raises NakedShortGuardError (e.g., IB-201 terminal
    broker reject), the retry loop must NOT re-invoke the adapter 3x --
    retrying a terminal Gate-4 refusal wastes 14+ seconds."""
    from xenon.execution.combo_wizard.ib_adapter import NakedShortGuardError

    sid = _init_session(state="FILLED")

    calls: list[dict] = []

    class _RaisingIB:
        def place_combo_tp(self, **kwargs):
            calls.append(kwargs)
            raise NakedShortGuardError("IB error 201: terminal reject")

        def register_risk_alert(self, **kwargs):
            return {"virtual_id": "alert-1"}

    sleeps: list[float] = []
    result = protect.attach_protection(
        sid,
        ib=_RaisingIB(),
        tp_target_price=Decimal("3.50"),
        alert_net_mid_threshold=Decimal("1.25"),
        polarity="DEBIT",
        sleep=sleeps.append,
        max_attempts=3,
        base_backoff=2.0,
    )

    assert len(calls) == 1
    assert sleeps == []
    assert result["tp_attached"] is False
    assert result["tp_refused_reason"] == "NAKED_SHORT_GUARD"
    assert result["alert_armed"] is True
    assert result["state"] == "PROTECTED"

    engine = _pg_engine()
    with engine.connect() as conn:
        events = conn.execute(
            text("SELECT kind FROM xenon.wizard_events WHERE session_id = :sid"),
            {"sid": sid},
        ).fetchall()
    engine.dispose()

    kinds = [row[0] for row in events]
    assert "PROTECTION_TP_REFUSED" in kinds


def test_risk_alert_failure_keeps_session_pending(monkeypatch):
    """A TP alone is not enough for the wizard's assisted-exit contract.

    If Risk Alert registration fails, the session must not be marked
    PROTECTED because the stop monitor only polls alert_enabled rows.
    """
    sid = _init_session(state="FILLED")

    class _AlertFailingIB:
        def place_combo_tp(self, **kwargs):
            return {"order_id": "tp-1", "perm_id": "tp-perm-1"}

        def register_risk_alert(self, **kwargs):
            raise RuntimeError("alert store unavailable")

    result = protect.attach_protection(
        sid,
        ib=_AlertFailingIB(),
        tp_target_price=Decimal("3.50"),
        alert_net_mid_threshold=Decimal("1.25"),
        polarity="DEBIT",
    )

    assert result["tp_attached"] is True
    assert result["alert_armed"] is False
    assert result["state"] == "PROTECTION_PENDING"

    engine = _pg_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT state FROM xenon.wizard_sessions WHERE session_id = :sid"),
            {"sid": sid},
        ).fetchone()
        prot = _fetch_protection_row(conn, sid)
    engine.dispose()

    assert row[0] == "PROTECTION_PENDING"
    config = prot.config
    assert config["tp_enabled"] is True
    assert config["alert_enabled"] is False


def test_signed_combo_pricing_preserved_for_credit(monkeypatch):
    """CREDIT spreads have negative net prices -- protect must not apply abs()."""
    sid = _init_session(state="FILLED")

    ib = _StubIB(
        tp_acks=[{"order_id": 9001, "perm_id": "p"}],
        alert_acks=[{"virtual_id": "alert-1"}],
    )

    signed_target = Decimal("-0.10")
    signed_threshold = Decimal("-0.45")

    protect.attach_protection(
        sid,
        ib=ib,
        tp_target_price=signed_target,
        alert_net_mid_threshold=signed_threshold,
        polarity="CREDIT",
        sleep=lambda _s: None,
    )

    assert ib.tp_calls[0]["target_price"] == signed_target
    assert ib.alert_calls[0]["threshold"] == signed_threshold

    engine = _pg_engine()
    with engine.connect() as conn:
        prot = _fetch_protection_row(conn, sid)
    engine.dispose()

    config = prot.config
    assert Decimal(config["tp_target_price"]) == signed_target
    assert Decimal(config["alert_net_mid_threshold"]) == signed_threshold
    assert config["polarity"] == "CREDIT"


def test_credit_spread_wizard_protection_uses_credit_spread_key(monkeypatch):
    payload = {
        "symbol": "SPY",
        "type": "combo",
        "action": "BUY",
        "quantity": 1,
        "limitPrice": "-1.00",
        "legs": [
            {
                "conId": 58001,
                "sec_type": "OPT",
                "symbol": "SPY",
                "expiry": "20260516",
                "strike": 580,
                "right": "P",
                "action": "SELL",
                "ratio": 1,
                "fill_price": 1.40,
            },
            {
                "conId": 57501,
                "sec_type": "OPT",
                "symbol": "SPY",
                "expiry": "20260516",
                "strike": 575,
                "right": "P",
                "action": "BUY",
                "ratio": 1,
                "fill_price": 0.40,
            },
        ],
    }
    sid = _init_session(state="FILLED", payload=payload)
    ib = _StubIB(
        tp_acks=[{"order_id": 9001, "perm_id": "p"}],
        alert_acks=[{"virtual_id": "alert-1"}],
    )

    protect.attach_protection(
        sid,
        ib=ib,
        tp_target_price=Decimal("-0.10"),
        alert_net_mid_threshold=Decimal("-0.45"),
        polarity="CREDIT",
        sleep=lambda _s: None,
    )

    engine = _pg_engine()
    with engine.connect() as conn:
        prot = conn.execute(
            text(
                """
                SELECT asset_class, position_key, position_descriptor
                FROM xenon.position_protection
                WHERE position_descriptor->>'wizard_session_id' = :sid
                """
            ),
            {"sid": sid},
        ).one()
    engine.dispose()

    assert prot.asset_class == "credit_spread"
    assert prot.position_descriptor["asset_class"] == "credit_spread"
    assert prot.position_descriptor["credit_received"] == 1.0
    assert prot.position_key == "CS::SPY::20260516::580::575::P"
