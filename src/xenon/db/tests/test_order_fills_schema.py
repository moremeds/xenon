from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError

from xenon.db.schema import order_fills, order_submissions


def test_order_fills_pk_is_exec_id():
    pk_columns = [col.name for col in order_fills.primary_key.columns]
    assert pk_columns == ["exec_id"]


@pytest.mark.asyncio
async def test_order_fills_check_requires_source(conn):
    """Rows need a submission, combo attempt, or legacy source marker."""
    with pytest.raises(IntegrityError):
        await conn.execute(
            insert(order_fills).values(
                exec_id="exec-missing-source",
                ticker="AAPL",
                side="BUY",
                qty=100,
                price=Decimal("190.1250"),
                filled_at=datetime.now(timezone.utc),
                broker="IB",
                account_env="paper",
                broker_account="DU123456",
            )
        )


@pytest.mark.asyncio
async def test_order_fills_replay_is_idempotent(conn):
    """The immutable IB exec_id key rejects duplicate replay inserts."""
    await conn.execute(
        insert(order_submissions).values(
            submission_id="sub-fill-001",
            user_id="user-1",
            client_attempt_id="attempt-1",
            ticker="AAPL",
            security_type="STK",
            action="BUY",
            quantity=100,
            state="WORKING",
            submitted_at=datetime.now(timezone.utc),
            broker="IB",
            account_env="paper",
            broker_account="DU123456",
        )
    )
    fill_values = {
        "exec_id": "exec-replay-001",
        "submission_id": "sub-fill-001",
        "perm_id": "555111",
        "ib_order_id": "42",
        "con_id": 265598,
        "ticker": "AAPL",
        "side": "BUY",
        "qty": 100,
        "price": Decimal("190.1250"),
        "filled_at": datetime.now(timezone.utc),
        "metadata": {"source": "test"},
        "broker": "IB",
        "account_env": "paper",
        "broker_account": "DU123456",
    }

    await conn.execute(insert(order_fills).values(**fill_values))
    with pytest.raises(IntegrityError):
        await conn.execute(insert(order_fills).values(**fill_values))
