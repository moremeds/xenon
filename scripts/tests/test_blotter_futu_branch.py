"""fetch_futu_blotter: 30-day historical trades from futu_closed_trades, IB-shape parity."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine

from xenon._test_db import is_pg_reachable, sync_test_db_url
from xenon.db.queries.blotter import fetch_futu_blotter
from xenon.db.schema import futu_closed_trades
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope(broker="FUTU", account_env="paper", broker_account="pytest-blotter")


def _clean():
    return sa.delete(futu_closed_trades).where(
        (futu_closed_trades.c.broker == "FUTU") & (futu_closed_trades.c.broker_account == "pytest-blotter")
    )


@pytest.fixture
def engine():
    if not is_pg_reachable():
        pytest.skip("PG test DB unreachable")
    eng = create_engine(sync_test_db_url(), pool_pre_ping=True)
    with eng.begin() as conn:
        conn.execute(_clean())
    try:
        yield eng
    finally:
        with eng.begin() as conn:
            conn.execute(_clean())
        eng.dispose()


def _row(close_id, ticker, rpnl, *, scope=SCOPE):
    now = datetime.now(timezone.utc)
    return {
        "broker": scope.broker,
        "account_env": scope.account_env,
        "broker_account": scope.broker_account,
        "futu_close_id": close_id,
        "ticker": ticker,
        "futu_code": f"US.{ticker}",
        "structure": None,
        "action": "SELL",
        "quantity": 1,
        "entry_cost": 3.48,
        "exit_cost": 10.40,
        "realized_pnl": rpnl,
        "cost_basis": 3.48,
        "proceeds": 10.40,
        "opened_at": now,
        "closed_at": now,
        "metadata": {},
    }


def test_fetch_futu_blotter_shape(engine):
    with engine.begin() as conn:
        conn.execute(
            sa.insert(futu_closed_trades),
            [_row("d2:d1", "QQQ", 6.92), _row("d4:d3", "QQQ250620C500000", 692.0)],
        )
    with engine.connect() as conn:
        payload = fetch_futu_blotter(conn, scope=SCOPE, days=30)

    assert payload["configured"] is True
    assert payload["source"] == "futu"
    assert payload["summary"]["closed_trades"] == 2
    assert payload["open_trades"] == []
    keys = set(payload["closed_trades"][0])
    # Same keys the frontend HISTORICAL TRADES table reads from the IB blotter.
    assert {
        "symbol",
        "contract_desc",
        "sec_type",
        "is_closed",
        "total_quantity",
        "total_commission",
        "realized_pnl",
        "cost_basis",
        "proceeds",
        "executions",
    } <= keys
    by_type = {t["sec_type"]: t for t in payload["closed_trades"]}
    # Symbol is shortened to the underlying for BOTH rows; the option is
    # disambiguated by sec_type + the structure-name description.
    assert by_type["STK"]["symbol"] == "QQQ"
    assert by_type["OPT"]["symbol"] == "QQQ"  # underlying, not the OCC ticker
    assert "Call" in by_type["OPT"]["contract_desc"]  # structure name, not the raw ticker
    assert by_type["OPT"]["contract_desc"] != "QQQ250620C500000"
    # Executions carry the close time so the frontend DATE column + sort work.
    assert by_type["OPT"]["executions"] and by_type["OPT"]["executions"][-1]["time"]
    assert all(t["is_closed"] for t in payload["closed_trades"])
    assert all(t["total_commission"] == 0.0 for t in payload["closed_trades"])  # Futu: no per-lot commission


def test_fetch_futu_blotter_scope_isolation(engine):
    other = AccountScope(broker="FUTU", account_env="live", broker_account="someone-else")
    with engine.begin() as conn:
        conn.execute(sa.insert(futu_closed_trades), [_row("x:y", "QQQ", 1.0)])
    with engine.connect() as conn:
        assert fetch_futu_blotter(conn, scope=other, days=30)["closed_trades"] == []
