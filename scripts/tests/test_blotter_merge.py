"""Tests for PG+Flex overlay merge (W3.4)."""

from __future__ import annotations

from xenon.db.queries.blotter import compare_blotter_rows, merge_pg_and_flex


def _trade(perm_id, *, realized_pnl=12.34, total_commission=0.5, symbol="AAPL"):
    return {
        "perm_id": perm_id,
        "symbol": symbol,
        "sec_type": "OPT",
        "is_closed": True,
        "net_quantity": 0,
        "total_quantity": 1,
        "total_commission": total_commission,
        "realized_pnl": realized_pnl,
        "cost_basis": 100,
        "proceeds": 110,
        "executions": [{"time": "2026-04-28T14:30:00Z"}],
    }


def test_compare_blotter_rows_returns_differing_field_names():
    pg = _trade("p1", realized_pnl=12.34)
    flex_match = _trade("p1", realized_pnl=12.34)
    flex_diff = _trade("p1", realized_pnl=99.99)
    assert compare_blotter_rows(pg, flex_match) == []
    assert compare_blotter_rows(pg, flex_diff) == ["realized_pnl"]


def test_merge_pg_only_when_flex_empty():
    pg = {
        "configured": True,
        "source": "postgres",
        "as_of": "2026-04-28T16:00:00Z",
        "closed_trades": [_trade("p1")],
        "open_trades": [],
        "summary": {"closed_trades": 1, "open_trades": 0, "total_commissions": 0.5, "realized_pnl": 12.34},
    }
    flex = {"closed_trades": [], "open_trades": []}
    merged = merge_pg_and_flex(pg, flex)
    assert merged["source"] == "postgres"
    assert merged["closed_trades"][0]["divergence"] is False


def test_merge_disjoint_perm_ids_recomputes_summary():
    pg = {
        "configured": True,
        "source": "postgres",
        "as_of": "2026-04-28T16:00:00Z",
        "closed_trades": [_trade("p1", realized_pnl=10, total_commission=1)],
        "open_trades": [],
        "summary": {"closed_trades": 1, "open_trades": 0, "total_commissions": 1, "realized_pnl": 10},
    }
    flex = {"closed_trades": [_trade("p2", realized_pnl=20, total_commission=2)], "open_trades": []}
    merged = merge_pg_and_flex(pg, flex)
    assert merged["source"] == "postgres+flex"
    assert merged["summary"]["closed_trades"] == 2
    assert abs(merged["summary"]["total_commissions"] - 3) < 1e-6
    assert abs(merged["summary"]["realized_pnl"] - 30) < 1e-6


def test_merge_flags_divergent_realized_pnl():
    pg = {
        "configured": True,
        "source": "postgres",
        "as_of": "2026-04-28T16:00:00Z",
        "closed_trades": [_trade("p1", realized_pnl=10)],
        "open_trades": [],
        "summary": {"closed_trades": 1, "open_trades": 0, "total_commissions": 0.5, "realized_pnl": 10},
    }
    flex = {"closed_trades": [_trade("p1", realized_pnl=20)], "open_trades": []}
    merged = merge_pg_and_flex(pg, flex)
    [row] = merged["closed_trades"]
    assert row["divergence"] is True
    assert row["divergence_fields"] == ["realized_pnl"]


def test_rows_without_perm_id_pass_through_pg_side():
    pg_row = _trade(None)
    pg = {
        "configured": True,
        "source": "postgres",
        "as_of": None,
        "closed_trades": [pg_row],
        "open_trades": [],
        "summary": {"closed_trades": 1, "open_trades": 0, "total_commissions": 0.5, "realized_pnl": 12.34},
    }
    flex = {"closed_trades": [], "open_trades": []}
    merged = merge_pg_and_flex(pg, flex)
    assert merged["closed_trades"][0]["divergence"] is False
