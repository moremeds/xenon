"""xenon.trades retains the IB-only constraint; Futu rows route to futu_trades."""

from sqlalchemy import text

from xenon.db.engine import get_sync_engine


def test_trades_ck_broker_is_ib_only():
    with get_sync_engine().connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT pg_get_constraintdef(c.oid)
                  FROM pg_constraint c
                  JOIN pg_class t       ON t.oid = c.conrelid
                  JOIN pg_namespace n   ON n.oid = t.relnamespace
                 WHERE c.conname = 'ck_trades_broker_ib_only'
                   AND n.nspname = 'xenon'
                   AND t.relname = 'trades'
                """
            )
        ).first()
    assert row is not None, "ck_trades_broker_ib_only must still exist"
    assert "'IB'" in row[0]
