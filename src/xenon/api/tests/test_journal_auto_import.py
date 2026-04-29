"""Tests for IB_AUTO_IMPORT journal listener (W4.7)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import insert, select

from xenon.db.engine import get_sync_engine
from xenon.db.events import CHANNEL_TRADE_CLOSED, emit_outbox_in_txn
from xenon.db.queries.journal import (
    list_journal_entries,
    upsert_auto_import_entry,
)
from xenon.db.schema import outbox, trades
from xenon.execution.account_scope import AccountScope

_SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DU111111")


def _insert_closed_trade(conn, *, ticker: str = "AAPL") -> int:
    result = conn.execute(
        insert(trades)
        .values(
            ticker=ticker,
            action="BUY",
            entry_cost=100,
            exit_cost=120,
            realized_pnl=20,
            quantity=1,
            opened_at=datetime(2026, 4, 28, 14, tzinfo=timezone.utc),
            closed_at=datetime(2026, 4, 28, 15, tzinfo=timezone.utc),
            state="CLOSED",
            broker=_SCOPE.broker,
            account_env=_SCOPE.account_env,
            broker_account=_SCOPE.broker_account,
        )
        .returning(trades.c.id)
    )
    return int(result.scalar_one())


def test_upsert_auto_import_creates_entry_once():
    engine = get_sync_engine()
    with engine.begin() as conn:
        trade_id = _insert_closed_trade(conn)
        first = upsert_auto_import_entry(conn, trade_id=trade_id)
        second = upsert_auto_import_entry(conn, trade_id=trade_id)

    assert first is not None
    assert first["id"] is not None
    assert first["id"] == second["id"], "second call must return same row, not insert"
    assert first["decision"] == "IB_AUTO_IMPORT"

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    with engine.connect() as conn:
        rows = list_journal_entries(conn, scope=_SCOPE, cutoff=cutoff, limit=10)
    auto_imports = [r for r in rows if r["trade_id"] == trade_id]
    assert len(auto_imports) == 1


def test_upsert_auto_import_skips_unknown_trade():
    engine = get_sync_engine()
    with engine.begin() as conn:
        result = upsert_auto_import_entry(conn, trade_id=999_999_999)
    assert result is None


def test_upsert_resolves_scope_from_trade_row_not_from_caller():
    """Listener does not know scope ahead of time — must read it from trades."""
    engine = get_sync_engine()
    with engine.begin() as conn:
        trade_id = _insert_closed_trade(conn, ticker="MSFT")
        result = upsert_auto_import_entry(conn, trade_id=trade_id)
    assert result is not None
    # The journal entry payload exposes scope indirectly via stored fields; we
    # verify by re-reading with the scope we expect to match.
    with engine.connect() as conn:
        rows = list_journal_entries(conn, scope=_SCOPE)
    assert any(r["trade_id"] == trade_id and r["decision"] == "IB_AUTO_IMPORT" for r in rows)


def test_listener_handles_notify_id_payload():
    """NOTIFY trigger emits NEW.id::text — listener must fetch the outbox row."""
    from xenon.api.services.journal_auto_import import JournalAutoImportSubscriber

    engine = get_sync_engine()
    with engine.begin() as conn:
        trade_id = _insert_closed_trade(conn, ticker="GOOG")
        outbox_id = emit_outbox_in_txn(
            conn,
            channel=CHANNEL_TRADE_CLOSED,
            source="test",
            payload={"trade_id": trade_id, "ticker": "GOOG"},
        )

    subscriber = JournalAutoImportSubscriber()
    subscriber.handle_notification_id(outbox_id)

    with engine.connect() as conn:
        rows = list_journal_entries(conn, scope=_SCOPE)
    autos = [r for r in rows if r["trade_id"] == trade_id and r["decision"] == "IB_AUTO_IMPORT"]
    assert len(autos) == 1


def test_listener_acks_consumed_by_after_processing():
    from xenon.api.services.journal_auto_import import (
        CONSUMER_ID,
        JournalAutoImportSubscriber,
    )

    engine = get_sync_engine()
    with engine.begin() as conn:
        trade_id = _insert_closed_trade(conn, ticker="NVDA")
        outbox_id = emit_outbox_in_txn(
            conn,
            channel=CHANNEL_TRADE_CLOSED,
            source="test",
            payload={"trade_id": trade_id, "ticker": "NVDA"},
        )

    subscriber = JournalAutoImportSubscriber()
    subscriber.handle_notification_id(outbox_id)

    with engine.connect() as conn:
        row = conn.execute(select(outbox.c.consumed_by).where(outbox.c.id == outbox_id)).first()
    assert row is not None
    assert CONSUMER_ID in (row.consumed_by or [])


def test_listener_backfills_unconsumed_events_on_start():
    """Events emitted before the listener boots must still create journal entries."""
    from xenon.api.services.journal_auto_import import JournalAutoImportSubscriber

    engine = get_sync_engine()
    with engine.begin() as conn:
        trade_id = _insert_closed_trade(conn, ticker="META")
        emit_outbox_in_txn(
            conn,
            channel=CHANNEL_TRADE_CLOSED,
            source="test",
            payload={"trade_id": trade_id, "ticker": "META"},
        )

    subscriber = JournalAutoImportSubscriber()
    subscriber.replay_unconsumed()

    with engine.connect() as conn:
        rows = list_journal_entries(conn, scope=_SCOPE)
    autos = [r for r in rows if r["trade_id"] == trade_id and r["decision"] == "IB_AUTO_IMPORT"]
    assert len(autos) == 1
