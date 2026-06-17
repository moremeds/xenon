"""FUTU_AUTO_IMPORT journal entries: idempotent upsert + top-level payload keys."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from xenon.db.engine import get_sync_engine
from xenon.db.queries.journal import list_journal_entries, upsert_futu_auto_import_entry
from xenon.execution.account_scope import AccountScope

# Use a pytest-only sentinel account (never a real numeric Futu account id) so
# the absolute-count assertions can't collide with real synced FUTU_AUTO_IMPORT
# rows in the shared core_test DB. Under the Phase-2 autouse BEGIN/ROLLBACK
# fixture, get_sync_engine() is bound to the test's rolled-back connection, so
# this scope sees no residuals and nothing leaks — matches the `pytest-sync`
# convention in scripts/tests/test_futu_orders_sync.py.
_SCOPE = AccountScope(broker="FUTU", account_env="paper", broker_account="pytest-jauto")


def _closed(close_id: str = "d2:d1") -> dict:
    return {
        "futu_close_id": close_id,
        "ticker": "QQQ",
        "quantity": 1,
        "cost_basis": 348.0,
        "proceeds": 1040.0,
        "realized_pnl": 692.0,
        "opened_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
        "closed_at": datetime(2026, 6, 15, tzinfo=timezone.utc),
    }


def test_futu_auto_import_is_idempotent_and_lifts_payload_keys():
    engine = get_sync_engine()
    ct = _closed()
    with engine.begin() as conn:
        a = upsert_futu_auto_import_entry(conn, scope=_SCOPE, closed_trade=ct)
        b = upsert_futu_auto_import_entry(conn, scope=_SCOPE, closed_trade=ct)  # dedup
    assert a is not None and b is not None
    # journal_entry_to_payload lifts metadata to TOP LEVEL (no nested "metadata").
    assert a["decision"] == "FUTU_AUTO_IMPORT"
    assert float(a["realized_pnl"]) == 692.0
    assert float(a["quantity"]) == 1.0
    assert float(a["entry_cost"]) == 348.0
    assert a["trade_id"] is None

    cutoff = datetime(2026, 6, 15, tzinfo=timezone.utc) - timedelta(days=1)
    with engine.connect() as conn:
        rows = list_journal_entries(conn, scope=_SCOPE, cutoff=cutoff, limit=50)
    futu = [r for r in rows if r.get("decision") == "FUTU_AUTO_IMPORT"]
    assert len(futu) == 1  # second upsert deduped


def test_distinct_close_ids_create_distinct_entries():
    engine = get_sync_engine()
    with engine.begin() as conn:
        upsert_futu_auto_import_entry(conn, scope=_SCOPE, closed_trade=_closed("s1:b1"))
        upsert_futu_auto_import_entry(conn, scope=_SCOPE, closed_trade=_closed("s1:b2"))
    cutoff = datetime(2026, 6, 14, tzinfo=timezone.utc)
    with engine.connect() as conn:
        rows = list_journal_entries(conn, scope=_SCOPE, cutoff=cutoff, limit=50)
    futu = [r for r in rows if r.get("decision") == "FUTU_AUTO_IMPORT"]
    assert len(futu) == 2
