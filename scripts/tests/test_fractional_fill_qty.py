"""Fractional-share executions must not be truncated to qty=0.

Discovered 2026-06-13: recurring fractional buys (QQQ/SPY placed outside
Xenon) were recorded with qty=0 because record_external_fills coerced
IB's float `shares` through int(), and order_fills.qty was an Integer
column. Result: the executed-orders panel showed quantity 0 and net
price "—" for every fractional stock fill.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_fills
from xenon.execution.account_scope import AccountScope
from xenon.execution.ib_reconcile import record_external_fills

SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DU0000000")


def _fractional_execution() -> dict:
    return {
        "exec_id": "frac.test.01.01",
        "perm_id": None,
        "ib_order_id": None,
        "con_id": 320227571,
        "time": datetime(2026, 6, 11, 19, 17, 15, tzinfo=timezone.utc),
        "symbol": "QQQ",
        "sec_type": "STK",
        "side": "BOT",
        "shares": 0.4977,
        "price": 703.34,
        "exchange": "IBKRATS",
        "commission": 0.35,
        "realized_pnl": 0.0,
        "strike": None,
        "expiry": None,
        "right": None,
    }


def test_fractional_shares_survive_recording() -> None:
    result = record_external_fills([_fractional_execution()], scope=SCOPE)
    assert result["inserted"] == 1

    engine = get_sync_engine()
    with engine.connect() as conn:
        qty = conn.execute(select(order_fills.c.qty).where(order_fills.c.exec_id == "frac.test.01.01")).scalar_one()
    assert Decimal(str(qty)) == Decimal("0.4977")
