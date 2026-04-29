import os

from sqlalchemy import select

from xenon.db.schema import outbox


def test_channel_constants_pass_outbox_check():
    from xenon.db.events import CHANNEL_FILL_RECORDED, CHANNEL_TRADE_CLOSED

    assert CHANNEL_FILL_RECORDED == "fill.recorded"
    assert CHANNEL_TRADE_CLOSED == "trade.closed"
    assert len(CHANNEL_FILL_RECORDED) <= 63
    assert len(CHANNEL_TRADE_CLOSED) <= 63


def test_emit_outbox_in_txn_inserts_with_sync_connection(monkeypatch):
    from xenon.db.events import CHANNEL_FILL_RECORDED, emit_outbox_in_txn
    from xenon.db.engine import get_sync_engine

    test_url = os.environ.get(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
    ).replace("postgresql+asyncpg://", "postgresql+psycopg://")
    monkeypatch.setenv("DATABASE_URL", test_url)

    import xenon.db.engine as engine_mod

    monkeypatch.setattr(engine_mod, "_sync_engine", None)
    engine = get_sync_engine()

    with engine.begin() as sync_conn:
        event_id = emit_outbox_in_txn(
            sync_conn,
            channel=CHANNEL_FILL_RECORDED,
            source="test",
            payload={"exec_id": "exec-001"},
        )

    with engine.connect() as sync_conn:
        row = sync_conn.execute(select(outbox).where(outbox.c.id == event_id)).one()

    assert row.channel == CHANNEL_FILL_RECORDED
    assert row.source == "test"
    assert row.payload == {"exec_id": "exec-001"}
