"""End-to-end Postgres handler test. Spec §13.3."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.execution.account_scope import AccountScope
from xenon.execution.brackets.arm_hook import on_fill_event
from xenon.execution.brackets.executor.ib_executor import PlaceResult
from xenon.execution.brackets.executor.marks import Quote
from xenon.execution.orders_store import record_fill
from xenon.monitor_daemon.handlers.position_rules import PositionRulesHandler


@pytest.fixture
def engine():
    engine = get_sync_engine()
    with engine.begin() as conn:
        _cleanup(conn)
        conn.execute(
            text(
                """
                INSERT INTO xenon.bracket_policies
                    (asset_class, rule_kind, auto_place, config)
                VALUES
                    ('stock', 'stop_loss', TRUE, '{"threshold_pct": -0.08, "anchor": "entry_price"}'),
                    ('stock', 'trailing_tp', TRUE, '{"trail_pct": 0.05, "activation_pct": 0.0, "anchor": "mfe"}')
                ON CONFLICT DO NOTHING
                """
            )
        )
    yield engine
    with engine.begin() as conn:
        _cleanup(conn)


def _cleanup(conn) -> None:
    conn.execute(text("DELETE FROM xenon.position_close_claims WHERE position_key = 'STK::TESTINT-A'"))
    conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key = 'STK::TESTINT-A'"))
    conn.execute(text("DELETE FROM xenon.order_fills WHERE exec_id LIKE 'TESTINT-%'"))


def _scope() -> AccountScope:
    return AccountScope(broker="IB", account_env="paper", broker_account="DU1234567")


def test_full_fill_to_trigger_pipeline(engine):
    filled_at = datetime.now(timezone.utc)
    assert record_fill(
        exec_id="TESTINT-1",
        submission_id=None,
        combo_attempt_id=None,
        perm_id="1",
        ib_order_id="1",
        con_id=10001,
        ticker="TESTINT-A",
        side="BUY",
        qty=100,
        price=Decimal("100.00"),
        filled_at=filled_at,
        metadata={"sec_type": "STK", "legacy_source": "position_rules_integration_test"},
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
    )

    on_fill_event(
        engine,
        {
            "exec_id": "TESTINT-1",
            "submission_id": None,
            "combo_attempt_id": None,
            "perm_id": "1",
            "ib_order_id": "1",
            "ticker": "TESTINT-A",
            "side": "BUY",
            "qty": 100,
            "price": "100.00",
            "filled_at": filled_at.isoformat(),
            "metadata": {"sec_type": "STK", "legacy_source": "position_rules_integration_test"},
            "broker": "IB",
            "account_env": "paper",
            "broker_account": "DU1234567",
            "con_id": 10001,
        },
    )

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT rule_kind, state FROM xenon.position_protection
                WHERE position_key = 'STK::TESTINT-A'
                ORDER BY rule_kind
                """
            )
        ).all()
    assert {row.rule_kind for row in rows} == {"stop_loss", "trailing_tp"}
    assert all(row.state == "PENDING_ARM" for row in rows)

    executor = MagicMock()
    executor.attach_native_stp.return_value = PlaceResult(perm_id=8888, ib_order_id=1, status="Submitted", raw={})
    executor.flatten_mkt.return_value = PlaceResult(perm_id=9999, ib_order_id=2, status="Submitted", raw={})

    ib_client = MagicMock()
    ib_client.connected = True
    ib_client.get_order_state.return_value = {"status": "Submitted", "permId": 8888}
    ib_client.get_quote.return_value = Quote(symbol="TESTINT-A", price=100.0, ts=datetime.now(timezone.utc))
    ib_client.positions.return_value = [{"symbol": "TESTINT-A", "qty": 100, "con_id": 10001}]
    ib_client.find_open_orders_by_order_ref.return_value = []
    ib_client.find_executions_by_order_ref.return_value = []

    handler = PositionRulesHandler(engine=engine, executor=executor, ib_client=ib_client, scope=_scope())
    handler.execute()

    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT rule_kind, state FROM xenon.position_protection
                WHERE position_key = 'STK::TESTINT-A'
                """
            )
        ).all()
        conn.execute(
            text(
                """
                UPDATE xenon.position_protection
                SET state_data = '{"mfe": 110.0}'::jsonb
                WHERE position_key = 'STK::TESTINT-A'
                  AND rule_kind = 'trailing_tp'
                """
            )
        )

    assert {row.rule_kind: row.state for row in rows} == {
        "stop_loss": "ARMED",
        "trailing_tp": "ARMED",
    }

    ib_client.get_quote.return_value = Quote(symbol="TESTINT-A", price=91.0, ts=datetime.now(timezone.utc))
    handler.execute()

    with engine.connect() as conn:
        states = dict(
            conn.execute(
                text(
                    """
                    SELECT rule_kind, state FROM xenon.position_protection
                    WHERE position_key = 'STK::TESTINT-A'
                    """
                )
            ).all()
        )
        claim_count = conn.execute(
            text(
                """
                SELECT COUNT(*) FROM xenon.position_close_claims
                WHERE position_key = 'STK::TESTINT-A'
                  AND status IN ('PENDING','SUBMITTED','FILLED')
                """
            )
        ).scalar_one()

    assert claim_count == 1
    assert sorted(states.values()) == ["SUPERSEDED", "TRIGGERED"]
    executor.flatten_mkt.assert_called_once()
