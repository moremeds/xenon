from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_fills, order_submissions, outbox
from xenon.execution import orders_store


def _insert_submission(submission_id: str = "sub-fill-001") -> None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(order_submissions).values(
                submission_id=submission_id,
                user_id="user-1",
                client_attempt_id=f"attempt-{submission_id}",
                ticker="AAPL",
                security_type="STK",
                action="BUY",
                quantity=100,
                multiplier=1,
                state="WORKING",
                submitted_at=datetime.now(timezone.utc),
                broker="IB",
                account_env="paper",
                broker_account="DU123456",
            )
        )


def _record_fill(**overrides) -> bool:
    values = {
        "exec_id": "exec-fill-001",
        "submission_id": "sub-fill-001",
        "combo_attempt_id": None,
        "perm_id": "777",
        "ib_order_id": "42",
        "con_id": 265598,
        "ticker": "AAPL",
        "side": "BUY",
        "qty": 100,
        "price": Decimal("190.1250"),
        "commission": Decimal("1.2500"),
        "filled_at": datetime(2026, 4, 28, 14, 30, tzinfo=timezone.utc),
        "metadata": {"source": "test"},
        "broker": "IB",
        "account_env": "paper",
        "broker_account": "DU123456",
    }
    values.update(overrides)
    return orders_store.record_fill(**values)


def test_record_fill_inserts_row_and_emits_outbox_in_same_txn():
    _insert_submission()

    inserted = _record_fill()

    assert inserted is True
    engine = get_sync_engine()
    with engine.connect() as conn:
        fill = conn.execute(select(order_fills).where(order_fills.c.exec_id == "exec-fill-001")).one()._mapping
        events = conn.execute(select(outbox).where(outbox.c.channel == "fill.recorded")).all()

    assert fill["submission_id"] == "sub-fill-001"
    assert fill["ticker"] == "AAPL"
    assert fill["side"] == "BUY"
    assert fill["qty"] == 100
    assert fill["price"] == Decimal("190.1250")
    assert len(events) == 1
    event = events[0]._mapping
    assert event["source"] == "record_fill"
    assert event["payload"]["exec_id"] == "exec-fill-001"
    assert event["payload"]["submission_id"] == "sub-fill-001"
    assert event["payload"]["broker_account"] == "DU123456"


def test_record_fill_replay_is_idempotent():
    _insert_submission()

    assert _record_fill() is True
    assert _record_fill() is False

    engine = get_sync_engine()
    with engine.connect() as conn:
        fill_count = conn.execute(select(order_fills.c.exec_id).where(order_fills.c.exec_id == "exec-fill-001")).all()
        event_count = conn.execute(select(outbox.c.id).where(outbox.c.channel == "fill.recorded")).all()

    assert len(fill_count) == 1
    assert len(event_count) == 1


def test_record_fill_rejects_missing_source_keys():
    with pytest.raises(IntegrityError):
        _record_fill(
            exec_id="exec-missing-source",
            submission_id=None,
            combo_attempt_id=None,
            metadata=None,
        )


def test_record_fill_rejects_legacy_unknown_scope():
    _insert_submission()

    with pytest.raises(ValueError, match="explicit account scope"):
        _record_fill(account_env="legacy_unknown")

    with pytest.raises(ValueError, match="explicit account scope"):
        _record_fill(broker_account="legacy_unknown")
