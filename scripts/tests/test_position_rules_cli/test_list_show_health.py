"""xenon-position-rules CLI list/show/health. Spec §12.5."""
from __future__ import annotations

import json
import os
import subprocess

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.db.queries.position_protection import insert_pending_arm


@pytest.fixture
def engine_with_row():
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key = 'STK::CLITEST'"))
    protection_id = insert_pending_arm(
        engine,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
        position_key="STK::CLITEST",
        position_descriptor={
            "asset_class": "stock",
            "anchor_price": 100.0,
            "opened_qty": 100,
            "protected_qty": 100,
            "multiplier": 1,
            "qty_unit": "share",
            "opened_at": "2026-05-04T14:00:00Z",
            "source": "fastapi_orders_place",
            "anchor_currency": "USD",
            "legs": [
                {
                    "sec_type": "STK",
                    "symbol": "CLITEST",
                    "action": "BUY",
                    "ratio": 1,
                    "fill_price": 100.0,
                    "con_id": 1,
                }
            ],
        },
        asset_class="stock",
        rule_kind="stop_loss",
        config={"threshold_pct": -0.08, "anchor": "entry_price"},
    )
    yield protection_id
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key = 'STK::CLITEST'"))


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "XENON_TRADING_MODE": "paper",
        "XENON_BROKER_ACCOUNT": "DU1234567",
        "XENON_BROKER": "IB",
    }
    return subprocess.run(["xenon-position-rules", *args], capture_output=True, text=True, env=env, timeout=30)


def test_list_returns_json(engine_with_row):
    result = _run(["list", "--json"])
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    assert any(row["position_key"] == "STK::CLITEST" for row in rows)


def test_show_returns_full_row(engine_with_row):
    result = _run(["show", str(engine_with_row), "--json"])
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert body["protection_id"] == engine_with_row
    assert body["position_key"] == "STK::CLITEST"


def test_health_includes_market_window(engine_with_row):
    result = _run(["health", "--json"])
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert "market_window" in body
    assert "rule_counts_by_state" in body
    assert "claim_counts_by_status" in body
