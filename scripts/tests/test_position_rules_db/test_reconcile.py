"""Boot reconcile. Spec §10.4."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.db.queries.position_close_claims import mark_submitted, try_claim
from xenon.db.queries.position_protection import insert_pending_arm
from xenon.execution.account_scope import AccountScope
from xenon.execution.brackets.executor.reconcile import boot_reconcile


@pytest.fixture
def engine():
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_close_claims WHERE position_key LIKE 'TEST::%'"))
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key LIKE 'TEST::%'"))
    yield engine
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_close_claims WHERE position_key LIKE 'TEST::%'"))
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key LIKE 'TEST::%'"))


def _descriptor():
    return {
        "asset_class": "stock",
        "anchor_price": 100.0,
        "opened_qty": 1,
        "protected_qty": 1,
        "multiplier": 1,
        "qty_unit": "share",
        "opened_at": "2026-05-04T14:00:00Z",
        "source": "fastapi_orders_place",
        "anchor_currency": "USD",
        "legs": [
            {
                "sec_type": "STK",
                "symbol": "AAPL",
                "action": "BUY",
                "ratio": 1,
                "fill_price": 100.0,
                "con_id": 1,
            }
        ],
    }


def test_boot_reconcile_snaps_inflight_claim_to_filled(engine):
    pid = insert_pending_arm(
        engine,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
        position_key="TEST::AAPL_RECON",
        position_descriptor=_descriptor(),
        asset_class="stock",
        rule_kind="stop_loss",
        config={"threshold_pct": -0.08, "anchor": "entry_price"},
    )
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE xenon.position_protection SET state='TRIGGERED' WHERE protection_id=:pid"),
            {"pid": pid},
        )
    claim_id = try_claim(
        engine,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
        position_key="TEST::AAPL_RECON",
        claimed_by_protection_id=pid,
        claim_kind="synthetic_close",
    )
    mark_submitted(engine, claim_id=claim_id, broker_perm_id=12345)

    ib_client = MagicMock()
    ib_client.connected = True
    ib_client.find_executions_by_order_ref.return_value = [
        {"orderRef": f"xenon-pr-{claim_id}", "permId": 12345}
    ]
    ib_client.find_open_orders_by_order_ref.return_value = []

    boot_reconcile(
        engine=engine,
        ib_client=ib_client,
        scope=AccountScope(broker="IB", account_env="paper", broker_account="DU1234567"),
    )

    with engine.connect() as conn:
        protection = conn.execute(
            text("SELECT state FROM xenon.position_protection WHERE protection_id=:pid"),
            {"pid": pid},
        ).first()
        claim = conn.execute(
            text("SELECT status FROM xenon.position_close_claims WHERE claim_id=:claim_id"),
            {"claim_id": claim_id},
        ).first()
    assert protection.state == "CLOSED"
    assert claim.status == "FILLED"
