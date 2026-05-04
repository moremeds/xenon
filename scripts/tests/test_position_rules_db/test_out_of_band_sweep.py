"""Daily out-of-band sweep with 70% sanity gate. Spec §6.5."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from xenon.db.engine import get_sync_engine
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
