import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_fills, outbox, trades


def _trade_log(path):
    path.write_text(
        json.dumps(
            {
                "trades": [
                    {
                        "ticker": "AAPL",
                        "action": "BUY",
                        "quantity": 10,
                        "fill_price": 190.25,
                        "commission": 1.25,
                        "opened_at": "2026-04-28T14:30:00Z",
                        "structure": "Long Stock",
                        "decision": "EXECUTED",
                    }
                ]
            }
        )
    )


def _rows():
    engine = get_sync_engine()
    with engine.connect() as conn:
        fill_rows = conn.execute(select(order_fills).order_by(order_fills.c.exec_id)).all()
        trade_rows = conn.execute(select(trades).order_by(trades.c.id)).all()
        outbox_rows = conn.execute(select(outbox).order_by(outbox.c.id)).all()
    return fill_rows, trade_rows, outbox_rows


def test_backfill_trade_log_writes_order_fills_and_trades(tmp_path):
    from scripts.migrations import _2026_04_28_backfill_fills_from_trade_log as backfill

    src = tmp_path / "trade_log.json"
    _trade_log(src)

    inserted = backfill.run(
        json_path=src,
        db_url=_sync_test_db_url(),
        broker="IB",
        account_env="paper",
        broker_account="DU123456",
    )

    assert inserted == 1
    fill_rows, trade_rows, outbox_rows = _rows()
    assert len(fill_rows) == 1
    fill = fill_rows[0]._mapping
    assert fill["submission_id"] is None
    assert fill["ticker"] == "AAPL"
    assert fill["side"] == "BUY"
    assert fill["qty"] == 10
    assert fill["price"] == Decimal("190.2500")
    assert fill["commission"] == Decimal("1.2500")
    assert fill["filled_at"] == datetime(2026, 4, 28, 14, 30, tzinfo=timezone.utc)
    assert fill["metadata"]["legacy_source"] == "trade_log_json"
    assert fill["metadata"]["legacy_id"]

    assert len(trade_rows) == 1
    trade = trade_rows[0]._mapping
    assert trade["submission_id"] is None
    assert trade["ticker"] == "AAPL"
    assert trade["quantity"] == 10
    assert trade["entry_cost"] == Decimal("1903.7500")
    assert trade["metadata"]["legacy_id"] == fill["metadata"]["legacy_id"]

    assert [row._mapping["channel"] for row in outbox_rows] == ["fill.recorded"]


def test_backfill_trade_log_is_idempotent(tmp_path):
    from scripts.migrations import _2026_04_28_backfill_fills_from_trade_log as backfill

    src = tmp_path / "trade_log.json"
    _trade_log(src)

    kwargs = {
        "json_path": src,
        "db_url": _sync_test_db_url(),
        "broker": "IB",
        "account_env": "paper",
        "broker_account": "DU123456",
    }
    assert backfill.run(**kwargs) == 1
    assert backfill.run(**kwargs) == 0

    fill_rows, trade_rows, outbox_rows = _rows()
    assert len(fill_rows) == 1
    assert len(trade_rows) == 1
    assert [row._mapping["channel"] for row in outbox_rows] == ["fill.recorded"]


def test_backfill_requires_explicit_scope(tmp_path):
    from scripts.migrations import _2026_04_28_backfill_fills_from_trade_log as backfill

    src = tmp_path / "trade_log.json"
    _trade_log(src)

    with pytest.raises(ValueError, match="explicit account scope"):
        backfill.run(
            json_path=src,
            db_url=_sync_test_db_url(),
            broker="IB",
            account_env="paper",
            broker_account="legacy_unknown",
        )
