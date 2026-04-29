from datetime import datetime, timezone

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError

from xenon.db.schema import order_submissions, trades


def test_trades_links_to_order_fill_sources():
    col_names = {col.name for col in trades.columns}
    assert {"submission_id", "combo_attempt_id", "state"}.issubset(col_names)

    fk_targets = {
        str(fk.column)
        for column in (trades.c.submission_id, trades.c.combo_attempt_id)
        for fk in column.foreign_keys
    }
    assert "order_submissions.submission_id" in fk_targets
    assert "wizard_combo_attempts.attempt_id" in fk_targets


@pytest.mark.asyncio
async def test_trades_state_defaults_to_open(conn):
    await conn.execute(
        insert(trades).values(
            ticker="AAPL",
            action="BUY",
            quantity=100,
            broker="IB",
            account_env="paper",
            broker_account="DU123456",
        )
    )
    row = (await conn.execute(trades.select())).first()
    assert row.state == "OPEN"


@pytest.mark.asyncio
async def test_trades_state_check_rejects_unknown_state(conn):
    await conn.execute(
        insert(order_submissions).values(
            submission_id="sub-trade-001",
            user_id="user-1",
            client_attempt_id="attempt-1",
            ticker="AAPL",
            security_type="STK",
            action="BUY",
            quantity=100,
            state="FILLED",
            submitted_at=datetime.now(timezone.utc),
            broker="IB",
            account_env="paper",
            broker_account="DU123456",
        )
    )

    with pytest.raises(IntegrityError):
        await conn.execute(
            insert(trades).values(
                ticker="AAPL",
                action="BUY",
                quantity=100,
                submission_id="sub-trade-001",
                state="UNKNOWN",
                broker="IB",
                account_env="paper",
                broker_account="DU123456",
            )
        )
