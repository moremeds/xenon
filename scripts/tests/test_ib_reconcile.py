"""
Tests for ib_reconcile.py — grouping executions by contract, not just symbol.

Bug: group_executions_by_symbol() merges different option contracts for the
same underlying (e.g., EWY P$130 and EWY C$141) into one group, producing
net_quantity=0 → "CLOSED" when both legs are actually new opens.

Fix: group by (symbol, sec_type, strike, expiry, right) for options.
"""

import pytest
import builtins
from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock


# Import the functions under test
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import func, insert, select

from xenon.db.engine import get_sync_engine
from xenon.db.schema import account_snapshots, order_fills
from xenon.execution.account_scope import AccountScope
from xenon.execution import ib_reconcile
from xenon.execution.ib_reconcile import group_executions_by_symbol, find_new_trades
from xenon.execution.orders_store import record_fill


def _make_execution(
    symbol,
    sec_type,
    side,
    shares,
    price,
    strike=None,
    expiry=None,
    right=None,
    commission=0,
    realized_pnl=0,
    exec_id=None,
    perm_id=None,
    ib_order_id=None,
    con_id=None,
):
    """Helper to create an execution dict matching ib_reconcile format."""
    return {
        "exec_id": exec_id,
        "perm_id": perm_id,
        "ib_order_id": ib_order_id,
        "con_id": con_id,
        "time": datetime(2026, 3, 10, 10, 0, 0),
        "symbol": symbol,
        "sec_type": sec_type,
        "side": side,
        "shares": shares,
        "price": price,
        "exchange": "SMART",
        "commission": commission,
        "realized_pnl": realized_pnl,
        "strike": strike,
        "expiry": expiry,
        "right": right,
    }


class TestGroupExecutionsByContract:
    """Executions for different contracts on the same symbol must stay separate."""

    def test_same_symbol_different_strikes_grouped_separately(self):
        """EWY P$130 (buy) and EWY C$141 (sell) should NOT merge into net_quantity=0."""
        executions = [
            _make_execution("EWY", "OPT", "BOT", 25, 2.00, strike=130, expiry="20260313", right="P"),
            _make_execution("EWY", "OPT", "SLD", 25, 2.20, strike=141, expiry="20260313", right="C"),
        ]
        grouped = group_executions_by_symbol(executions)

        # Must produce two separate groups, not one with net_quantity=0
        ewy_groups = [g for g in grouped.values() if g["symbol"] == "EWY"]
        assert len(ewy_groups) == 2, (
            f"Expected 2 separate EWY groups (P$130 + C$141), got {len(ewy_groups)}"
        )

        # Verify each group has correct net_quantity
        qtys = sorted([g["net_quantity"] for g in ewy_groups])
        assert qtys == [-25, 25], f"Expected [-25, 25], got {qtys}"

    def test_same_symbol_same_contract_merges(self):
        """Multiple fills for the same contract should still merge."""
        executions = [
            _make_execution("AAOI", "OPT", "SLD", 25, 20.30, strike=105, expiry="20260320", right="C"),
            _make_execution("AAOI", "OPT", "SLD", 25, 22.00, strike=105, expiry="20260320", right="C"),
        ]
        grouped = group_executions_by_symbol(executions)

        aaoi_groups = [g for g in grouped.values() if g["symbol"] == "AAOI"]
        assert len(aaoi_groups) == 1
        assert aaoi_groups[0]["net_quantity"] == -50

    def test_stock_executions_group_by_symbol_only(self):
        """Stock executions don't have strike/expiry, should group by symbol."""
        executions = [
            _make_execution("TSLA", "STK", "BOT", 100, 250.00),
            _make_execution("TSLA", "STK", "BOT", 100, 251.00),
        ]
        grouped = group_executions_by_symbol(executions)

        tsla_groups = [g for g in grouped.values() if g["symbol"] == "TSLA"]
        assert len(tsla_groups) == 1
        assert tsla_groups[0]["net_quantity"] == 200

    def test_collar_legs_not_marked_closed(self):
        """A collar (buy put + sell call) should NOT result in action=CLOSED."""
        executions = [
            _make_execution("EWY", "OPT", "BOT", 25, 2.00, strike=130, expiry="20260313", right="P"),
            _make_execution("EWY", "OPT", "SLD", 25, 2.20, strike=141, expiry="20260313", right="C"),
        ]
        grouped = group_executions_by_symbol(executions)

        for g in grouped.values():
            assert g["action"] != "CLOSED", (
                f"Collar leg {g['symbol']} strike={g.get('strike')} incorrectly marked CLOSED"
            )


class TestFindNewTradesWithContracts:
    """find_new_trades should produce separate entries for each contract."""

    def test_collar_produces_two_new_trades(self):
        """EWY collar should produce two new_trades entries, not one."""
        executions = [
            _make_execution("EWY", "OPT", "BOT", 25, 2.00, strike=130, expiry="20260313", right="P"),
            _make_execution("EWY", "OPT", "SLD", 25, 2.20, strike=141, expiry="20260313", right="C"),
        ]
        trade_log = {"trades": []}

        new_trades = find_new_trades(executions, trade_log)

        ewy_trades = [t for t in new_trades if t["symbol"] == "EWY"]
        assert len(ewy_trades) == 2, (
            f"Expected 2 EWY new_trades for collar legs, got {len(ewy_trades)}"
        )

    def test_new_trade_includes_contract_details(self):
        """Each new_trade entry should include strike, expiry, right for options."""
        executions = [
            _make_execution("EWY", "OPT", "BOT", 25, 2.00, strike=130, expiry="20260313", right="P"),
        ]
        trade_log = {"trades": []}

        new_trades = find_new_trades(executions, trade_log)

        assert len(new_trades) == 1
        t = new_trades[0]
        assert t.get("strike") == 130
        assert t.get("expiry") == "20260313"
        assert t.get("right") == "P"


def _scope() -> AccountScope:
    return AccountScope(broker="IB", account_env="paper", broker_account="DU0000000")


def _legacy_id(perm_id: str) -> str:
    return f"ib_reconcile:perm:{perm_id}"


def _seed_existing_fill(execution: dict, scope: AccountScope) -> None:
    record_fill(
        exec_id=execution["exec_id"],
        submission_id=None,
        combo_attempt_id=None,
        perm_id=str(execution["perm_id"]),
        ib_order_id=str(execution["ib_order_id"]),
        con_id=execution["con_id"],
        ticker=execution["symbol"],
        side="BUY" if execution["side"] == "BOT" else "SELL",
        qty=int(execution["shares"]),
        price=Decimal(str(execution["price"])),
        commission=Decimal(str(execution["commission"])),
        filled_at=execution["time"],
        metadata={
            "legacy_source": "ib_reconcile",
            "legacy_id": _legacy_id(str(execution["perm_id"])),
        },
        broker=scope.broker,
        account_env=scope.account_env,
        broker_account=scope.broker_account,
    )


def _fill_count() -> int:
    engine = get_sync_engine()
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(order_fills)).scalar_one())


def test_load_portfolio_snapshot_reads_account_snapshots_not_json(monkeypatch):
    scope = _scope()
    engine = get_sync_engine()
    payload = {"positions": [{"ticker": "AAPL", "quantity": 10}], "last_sync": "2026-03-10T14:00:00Z"}
    with engine.begin() as conn:
        conn.execute(
            insert(account_snapshots).values(
                account=scope.broker_account,
                bankroll=Decimal("100000.00"),
                payload=payload,
                broker=scope.broker,
                account_env=scope.account_env,
                broker_account=scope.broker_account,
            )
        )

    real_open = builtins.open

    def fail_on_portfolio_json(path, *args, **kwargs):
        if "portfolio.json" in str(path):
            raise AssertionError("ib_reconcile must not read portfolio.json")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fail_on_portfolio_json)

    snapshot = ib_reconcile.load_portfolio_snapshot(scope=scope)

    assert snapshot["positions"][0]["ticker"] == "AAPL"
    assert snapshot["last_sync"] == "2026-03-10T14:00:00Z"


def test_record_external_fills_skips_existing_exec_ids_and_aggregates_affected_groups(monkeypatch):
    scope = _scope()
    executions = [
        _make_execution("AAPL", "STK", "BOT", 10, 190.10, exec_id="E1", perm_id="100", ib_order_id="5001", con_id=265598),
        _make_execution("AAPL", "STK", "BOT", 5, 190.20, exec_id="E2", perm_id="100", ib_order_id="5001", con_id=265598),
        _make_execution("MSFT", "STK", "BOT", 3, 410.00, exec_id="E3", perm_id="200", ib_order_id="5002", con_id=272093),
        _make_execution("MSFT", "STK", "BOT", 2, 411.00, exec_id="E4", perm_id="200", ib_order_id="5002", con_id=272093),
        _make_execution("TSLA", "STK", "SLD", 1, 250.00, exec_id="E5", perm_id="300", ib_order_id="5003", con_id=76792991),
    ]
    for execution in executions[:3]:
        _seed_existing_fill(execution, scope)

    aggregated: list[str] = []
    monkeypatch.setattr(
        ib_reconcile,
        "aggregate_trade_from_fills",
        lambda *, legacy_id: aggregated.append(legacy_id),
    )

    result = ib_reconcile.record_external_fills(executions, scope=scope)

    assert result["inserted"] == 2
    assert result["replayed"] == 3
    assert _fill_count() == 5
    assert result["affected_legacy_ids"] == [_legacy_id("200"), _legacy_id("300")]
    assert aggregated == [_legacy_id("200"), _legacy_id("300")]
