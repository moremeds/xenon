import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.engine import get_sync_engine
from xenon.db.schema import journal_entries


def _trade_log(path):
    path.write_text(
        json.dumps(
            {
                "trades": [
                    {
                        "id": 77,
                        "ticker": "AAPL",
                        "date": "2026-04-28",
                        "time": "14:30:00",
                        "structure": "Long Call",
                        "decision": "EXECUTED",
                        "entry_cost": 1250.5,
                        "notes": "legacy note",
                    }
                ]
            }
        )
    )


def _journal_rows():
    engine = get_sync_engine()
    with engine.connect() as conn:
        return conn.execute(select(journal_entries).order_by(journal_entries.c.id)).all()


def test_backfill_trade_log_writes_journal_entries(tmp_path):
    from scripts.migrations import _2026_04_28_backfill_journal_from_trade_log as backfill

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
    rows = _journal_rows()
    assert len(rows) == 1
    row = rows[0]._mapping
    assert row["ticker"] == "AAPL"
    assert row["decision"] == "LEGACY_IMPORT"
    assert row["note"] == "legacy note"
    assert row["authored_by"] == "legacy_backfill"
    assert row["authored_at"] == datetime(2026, 4, 28, 14, 30, tzinfo=timezone.utc)
    assert row["broker"] == "IB"
    assert row["account_env"] == "paper"
    assert row["broker_account"] == "DU123456"
    assert row["metadata"]["legacy_source"] == "trade_log_json"
    assert row["metadata"]["legacy_id"]
    assert row["metadata"]["legacy_entry"]["structure"] == "Long Call"


def test_backfill_trade_log_journal_is_idempotent(tmp_path):
    from scripts.migrations import _2026_04_28_backfill_journal_from_trade_log as backfill

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
    assert len(_journal_rows()) == 1


def test_backfill_journal_requires_explicit_scope(tmp_path):
    from scripts.migrations import _2026_04_28_backfill_journal_from_trade_log as backfill

    src = tmp_path / "trade_log.json"
    _trade_log(src)

    with pytest.raises(ValueError, match="explicit account scope"):
        backfill.run(
            json_path=src,
            db_url=_sync_test_db_url(),
            broker="IB",
            account_env="legacy_unknown",
            broker_account="DU123456",
        )
