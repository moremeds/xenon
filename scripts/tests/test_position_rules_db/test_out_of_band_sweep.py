"""Daily out-of-band sweep with 70% sanity gate. Spec §6.5."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.db.queries.position_protection import insert_pending_arm
from xenon.execution.account_scope import AccountScope
from xenon.monitor_daemon.handlers.out_of_band_sweep import OutOfBandSweepHandler


@pytest.fixture
def engine():
    return get_sync_engine()


def _scope() -> AccountScope:
    return AccountScope(broker="IB", account_env="paper", broker_account="DU1234567")


def test_sweep_emits_unprotected_for_unknown_position(engine):
    ib = MagicMock()
    ib.connected = True
    ib.positions.return_value = [{"symbol": "OOB-TEST", "qty": 100, "con_id": 1}]
    handler = OutOfBandSweepHandler(engine=engine, ib_client=ib, scope=_scope())
    result = handler.execute()
    assert result["status"] == "ok"
    assert result["unprotected_count"] >= 1


def test_sweep_does_not_treat_option_rule_as_stock_protection(engine):
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "DELETE FROM xenon.position_protection WHERE position_key IN ('STK::OOBSAME', 'OPT::OOBSAME::20260619::100::C')"
        )
    insert_pending_arm(
        engine,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
        position_key="OPT::OOBSAME::20260619::100::C",
        position_descriptor={
            "asset_class": "long_option",
            "anchor_price": 2.0,
            "opened_qty": 1,
            "protected_qty": 1,
            "multiplier": 100,
            "qty_unit": "contract",
            "opened_at": "2026-05-04T14:00:00Z",
            "source": "test",
            "anchor_currency": "USD",
            "legs": [
                {
                    "sec_type": "OPT",
                    "symbol": "OOBSAME",
                    "expiry": "20260619",
                    "strike": 100.0,
                    "right": "C",
                    "action": "BUY",
                    "ratio": 1,
                    "fill_price": 2.0,
                    "con_id": 99,
                }
            ],
        },
        asset_class="long_option",
        rule_kind="stop_loss",
        config={"threshold_pct": -0.2, "anchor": "entry_price"},
    )
    ib = MagicMock()
    ib.connected = True
    ib.positions.return_value = [{"symbol": "OOBSAME", "qty": 100, "con_id": 1, "sec_type": "STK"}]
    handler = OutOfBandSweepHandler(engine=engine, ib_client=ib, scope=_scope())
    result = handler.execute()
    assert result["status"] == "ok"
    assert result["unprotected_count"] == 1


def test_sweep_aborts_when_positions_drop_70_pct(engine):
    ib = MagicMock()
    ib.connected = True
    ib.positions.return_value = [{"symbol": "X", "qty": 1, "con_id": 1}, {"symbol": "Y", "qty": 1, "con_id": 2}]
    handler = OutOfBandSweepHandler(engine=engine, ib_client=ib, scope=_scope())
    handler._last_known_position_count = 10
    result = handler.execute()
    assert result["status"] == "aborted_short_response"


def test_sweep_skips_when_ib_disconnected(engine):
    ib = MagicMock()
    ib.connected = False
    handler = OutOfBandSweepHandler(engine=engine, ib_client=ib, scope=_scope())
    result = handler.execute()
    assert result["status"] == "skipped_disconnected"


def test_last_known_count_is_scoped_by_broker_and_env(engine):
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO events.outbox(channel, source, payload)
                VALUES (
                    'position_rule.transition',
                    'oob_sweep',
                    '{
                        "kind": "oob_sweep_position_count",
                        "broker": "IB",
                        "account_env": "live",
                        "broker_account": "DU1234567",
                        "count": 10
                    }'::jsonb
                )
                """
            )
        )
    ib = MagicMock()
    ib.connected = True
    handler = OutOfBandSweepHandler(engine=engine, ib_client=ib, scope=_scope())
    assert handler._last_known_position_count == 0
