"""Tests for db.queries.combo_wizard — sync query functions against Postgres."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine as _create_sync
from sqlalchemy import text

from xenon.db.queries import combo_wizard as cwq


@pytest.fixture
def sconn():
    """Sync Connection for combo_wizard query functions."""
    url = os.environ.get(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
    ).replace("postgresql+asyncpg://", "postgresql+psycopg://")
    engine = _create_sync(url)
    with engine.begin() as conn:
        yield conn
    engine.dispose()


# ── wizard_sessions ──


def test_create_and_get_session(sconn):
    cwq.create_session(
        sconn,
        session_id="wiz-001",
        ticker="AAPL",
        state="planned",
        structure_name="Bull Call Spread",
        intent="OPEN",
        payload={"legs": [{"strike": 200}, {"strike": 210}]},
    )
    row = cwq.get_session(sconn, "wiz-001")
    assert row is not None
    assert row["ticker"] == "AAPL"
    assert row["state"] == "planned"
    assert row["payload"] == {"legs": [{"strike": 200}, {"strike": 210}]}


def test_list_sessions(sconn):
    cwq.create_session(sconn, session_id="wiz-a", ticker="AAPL", state="planned")
    cwq.create_session(sconn, session_id="wiz-b", ticker="MSFT", state="working")
    rows = cwq.list_sessions(sconn)
    assert len(rows) == 2


def test_update_session(sconn):
    cwq.create_session(sconn, session_id="wiz-u", ticker="SPY", state="planned")
    cwq.update_session(sconn, "wiz-u", state="working")
    row = cwq.get_session(sconn, "wiz-u")
    assert row["state"] == "working"


def test_claim_session_for_submit(sconn):
    cwq.create_session(sconn, session_id="wiz-c", ticker="TSLA", state="planned")
    claimed = cwq.claim_session_for_submit(sconn, "wiz-c", "att-1")
    assert claimed is not None
    assert claimed["state"] == "submitting"
    assert claimed["current_attempt_id"] == "att-1"
    second = cwq.claim_session_for_submit(sconn, "wiz-c", "att-2")
    assert second is None


def test_release_submit_claim(sconn):
    cwq.create_session(sconn, session_id="wiz-r", ticker="GOOG", state="planned")
    cwq.claim_session_for_submit(sconn, "wiz-r", "att-x")
    cwq.release_submit_claim(sconn, "wiz-r", "att-x")
    row = cwq.get_session(sconn, "wiz-r")
    assert row["state"] == "planned"
    assert row["current_attempt_id"] is None


def test_list_rehydratable(sconn):
    cwq.create_session(sconn, session_id="s1", ticker="A", state="working")
    cwq.create_session(sconn, session_id="s2", ticker="B", state="planned")
    cwq.create_session(sconn, session_id="s3", ticker="C", state="protected")
    rows = cwq.list_rehydratable(sconn)
    ids = {r["session_id"] for r in rows}
    assert ids == {"s1", "s3"}


# ── wizard_combo_attempts ──


def test_create_and_get_attempt(sconn):
    cwq.create_session(sconn, session_id="wiz-att", ticker="NVDA", state="working")
    now = datetime.now(timezone.utc)
    cwq.create_attempt(
        sconn,
        attempt_id="att-1",
        session_id="wiz-att",
        ticker="NVDA",
        state="WORKING",
        limit_price=Decimal("3.50"),
        submitted_at=now,
        updated_at=now,
    )
    att = cwq.get_latest_attempt(sconn, "wiz-att")
    assert att is not None
    assert att["attempt_id"] == "att-1"
    assert att["state"] == "WORKING"


def test_update_attempt(sconn):
    cwq.create_session(sconn, session_id="wiz-ua", ticker="AMD", state="working")
    now = datetime.now(timezone.utc)
    cwq.create_attempt(
        sconn, attempt_id="att-u1", session_id="wiz-ua", ticker="AMD", state="WORKING", submitted_at=now, updated_at=now
    )
    cwq.update_attempt(sconn, "att-u1", state="FILLED", filled_qty=5)
    att = cwq.get_latest_attempt(sconn, "wiz-ua")
    assert att["state"] == "FILLED"
    assert att["filled_qty"] == 5


# ── wizard_events ──


def test_record_event(sconn):
    cwq.create_session(sconn, session_id="wiz-ev", ticker="META", state="working")
    cwq.record_event(sconn, session_id="wiz-ev", kind="SUBMITTED", detail={"price": 2.50})
    from sqlalchemy import text

    row = sconn.execute(text("SELECT kind, detail FROM xenon.wizard_events WHERE session_id = 'wiz-ev'")).first()
    assert row is not None
    assert row[0] == "SUBMITTED"


# ── position_protection-backed combo TP alert ──


def _combo_legs() -> list[dict]:
    return [
        {
            "sec_type": "OPT",
            "symbol": "QQQ",
            "expiry": "2026-06-19",
            "strike": 430,
            "right": "C",
            "action": "BUY",
            "ratio": 1,
        },
        {
            "sec_type": "OPT",
            "symbol": "QQQ",
            "expiry": "2026-06-19",
            "strike": 435,
            "right": "C",
            "action": "SELL",
            "ratio": 1,
        },
    ]


def test_upsert_protection(sconn):
    now = datetime.now(timezone.utc)
    cwq.create_session(
        sconn,
        session_id="wiz-p",
        ticker="QQQ",
        state="protected",
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
    )
    cwq.create_attempt(
        sconn,
        attempt_id="att-p",
        session_id="wiz-p",
        ticker="QQQ",
        state="FILLED",
        legs=_combo_legs(),
        submitted_at=now,
        updated_at=now,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
    )
    cwq.upsert_protection(sconn, "wiz-p", config={"tp_enabled": True, "tp_target_price": 5.0})

    row = sconn.execute(
        text(
            """
            SELECT rule_kind, state, asset_class, position_descriptor, config
            FROM xenon.position_protection
            WHERE rule_kind = 'combo_tp_alert'
              AND position_descriptor->>'wizard_session_id' = 'wiz-p'
            """
        )
    ).first()
    assert row is not None
    assert row.rule_kind == "combo_tp_alert"
    assert row.state == "PENDING_ARM"
    assert row.asset_class == "debit_combo"
    assert row.position_descriptor["legs"] == _combo_legs()
    assert row.config["auto_place"] is False
    assert row.config["tp_enabled"] is True
    # Existing active rows are frozen; reconfiguration supersedes + inserts.
    cwq.upsert_protection(sconn, "wiz-p", config={"tp_enabled": False, "alert_enabled": True})
    rows = sconn.execute(
        text(
            """
            SELECT state, config
            FROM xenon.position_protection
            WHERE rule_kind = 'combo_tp_alert'
              AND position_descriptor->>'wizard_session_id' = 'wiz-p'
            ORDER BY protection_id
            """
        )
    ).fetchall()
    assert [row.state for row in rows] == ["SUPERSEDED", "PENDING_ARM"]
    assert rows[-1].config["tp_enabled"] is False


def test_list_protected_sessions(sconn):
    now = datetime.now(timezone.utc)
    cwq.create_session(
        sconn,
        session_id="wiz-lp",
        ticker="SPY",
        state="PROTECTED",
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
    )
    cwq.create_attempt(
        sconn,
        attempt_id="att-lp",
        session_id="wiz-lp",
        ticker="SPY",
        state="FILLED",
        legs=[{**leg, "symbol": "SPY"} for leg in _combo_legs()],
        submitted_at=now,
        updated_at=now,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
    )
    cwq.upsert_protection(sconn, "wiz-lp", config={"alert_enabled": True, "alert_net_mid_threshold": 0.5})
    rows = cwq.list_protected_sessions(sconn, broker="IB", account_env="paper", broker_account="DU1234567")
    assert len(rows) >= 1
    assert any(r["session_id"] == "wiz-lp" for r in rows)


# ── orders helpers ──


def test_get_order_modify_sequence(sconn):
    from xenon.db.schema import order_submissions

    now = datetime.now(timezone.utc)
    sconn.execute(
        order_submissions.insert().values(
            submission_id="sub-ms",
            user_id="u1",
            client_attempt_id="ca-ms",
            ticker="TSLA",
            security_type="STK",
            action="BUY",
            quantity=100,
            limit_price=Decimal("200"),
            state="WORKING",
            ib_order_id="42",
            perm_id="999",
            modify_sequence=3,
            submitted_at=now,
            updated_at=now,
        )
    )
    seq = cwq.get_order_modify_sequence(sconn, ib_order_id="42")
    assert seq == 3
    seq2 = cwq.get_order_modify_sequence(sconn, perm_id="999")
    assert seq2 == 3
