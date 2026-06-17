"""Futu closed-trade structure grouping: OCC parse, sub-lot aggregation,
multi-leg fusion by closing order id, structure classification, blotter shape."""

from __future__ import annotations

from datetime import datetime, timezone

from xenon.api.services.futu_structure import (
    build_blotter_rows,
    classify_structure,
    parse_occ,
)


def _row(ticker, action, qty, cost, proceeds, rpnl, close_oid, *, day=2, open_day=1, close_id=None):
    return {
        "ticker": ticker,
        "futu_code": f"US.{ticker}",
        "action": action,
        "quantity": qty,
        "cost_basis": cost,
        "proceeds": proceeds,
        "realized_pnl": rpnl,
        "opened_at": datetime(2026, 6, open_day, tzinfo=timezone.utc),
        "closed_at": datetime(2026, 6, day, 12, 0, tzinfo=timezone.utc),
        "futu_close_id": close_id or f"{ticker}-{close_oid}",
        "metadata": {"close_order_id": close_oid} if close_oid else {},
    }


# ── OCC parsing ────────────────────────────────────────────────────────────


def test_parse_occ_option():
    leg = parse_occ("ORCL260717C220000")
    assert leg.underlying == "ORCL" and leg.right == "C" and leg.expiry == "260717"
    assert leg.strike == 220.0


def test_parse_occ_fractional_strike():
    assert parse_occ("F261218C14850").strike == 14.85


def test_parse_occ_stock_returns_none():
    assert parse_occ("QQQ") is None
    assert parse_occ("AAOX") is None


# ── sub-lot aggregation (same contract, one closing order) ───────────────────


def test_same_contract_sublots_aggregate_into_one_row():
    """A 50-lot SELL FIFO-split across 5 opens → 5 rows for one contract; they
    collapse to ONE line with summed qty / P&L (the NOK x5 bug)."""
    rows = [
        _row("NOK270617C27000", "SELL", q, 0, 0, pnl, "ORD1")
        for q, pnl in [(10, 100), (15, 150), (10, 90), (10, 80), (5, 40)]
    ]
    out = build_blotter_rows(rows)
    assert len(out) == 1
    r = out[0]
    assert r["symbol"] == "NOK"
    assert r["total_quantity"] == 50
    assert r["realized_pnl"] == 460.0
    assert r["sec_type"] == "OPT"


# ── multi-leg fusion by closing order id ─────────────────────────────────────


def test_two_legs_same_close_order_fuse_into_vertical():
    """AAOI C190 (closed long) + C200 (closed short) under ONE close order →
    a single Bull Call Spread row, symbol = underlying."""
    rows = [
        _row("AAOI270115C190000", "SELL", 10, 0, 56170, 2100, "OCLOSE"),
        _row("AAOI270115C200000", "BUY", 10, 54070, 0, -1900, "OCLOSE"),
    ]
    out = build_blotter_rows(rows)
    assert len(out) == 1
    r = out[0]
    assert r["symbol"] == "AAOI"
    assert "Bull Call Spread" in r["contract_desc"]
    assert "$190" in r["contract_desc"] and "$200" in r["contract_desc"]
    assert r["realized_pnl"] == 200.0
    assert len(r["executions"]) == 2


def test_different_close_orders_stay_separate():
    """Two NOK legs closed by DIFFERENT orders are independent singles — never
    fused (guard against collapsing unrelated same-underlying trades)."""
    rows = [
        _row("NOK270617C27000", "SELL", 50, 0, 13650, 500, "ORD_A"),
        _row("NOK270617C20000", "SELL", 5, 0, 1850, 60, "ORD_B"),
    ]
    out = build_blotter_rows(rows)
    assert len(out) == 2
    descs = {r["contract_desc"].split(" · ")[0] for r in out}
    assert descs == {"Long Call"}  # two separate single-leg longs


def test_rows_without_close_order_id_dont_merge():
    """Legacy rows lacking close_order_id fall back to their own close id and
    stand alone (no accidental fusion by underlying)."""
    rows = [
        _row("AAOI270115C190000", "SELL", 1, 0, 100, 10, "", close_id="c1"),
        _row("AAOI270115C200000", "SELL", 1, 0, 100, 10, "", close_id="c2"),
    ]
    out = build_blotter_rows(rows)
    assert len(out) == 2


# ── classification coverage ──────────────────────────────────────────────────


def _leg_rows(specs, oid="O"):
    return [_row(t, action, q, c, p, pnl, oid) for (t, action, q, c, p, pnl) in specs]


def test_classify_single_long_put():
    out = build_blotter_rows([_row("TSLA270115P400000", "SELL", 1, 0, 5000, 100, "O")])
    assert out[0]["contract_desc"].startswith("Long Put")


def test_classify_short_call_single():
    out = build_blotter_rows([_row("QQQ260821C730000", "BUY", 1, 3789, 0, -50, "O")])
    assert out[0]["contract_desc"].startswith("Short Call")


def test_classify_long_straddle():
    rows = _leg_rows(
        [
            ("ABC260117C100000", "SELL", 1, 0, 500, 50),
            ("ABC260117P100000", "SELL", 1, 0, 400, 40),
        ]
    )
    assert build_blotter_rows(rows)[0]["contract_desc"].startswith("Long Straddle")


def test_classify_put_butterfly():
    rows = _leg_rows(
        [
            ("SPCX260918P145000", "SELL", 4, 0, 5000, 100),
            ("SPCX260918P160000", "BUY", 8, 9000, 0, -100),
            ("SPCX260918P175000", "SELL", 4, 0, 6000, 120),
        ]
    )
    assert build_blotter_rows(rows)[0]["contract_desc"].startswith("Put Butterfly")


def test_classify_stock():
    out = build_blotter_rows([_row("QCOM", "SELL", 20, 0, 5000, 200, "O")])
    assert out[0]["sec_type"] == "STK"
    assert out[0]["symbol"] == "QCOM"
    assert "Stock" in out[0]["contract_desc"]


# ── executions populate date + sort ──────────────────────────────────────────


def test_executions_carry_close_time_and_rows_sorted_desc():
    rows = [
        _row("AAA260117C100000", "SELL", 1, 0, 100, 10, "O1", day=2),
        _row("BBB260117C100000", "SELL", 1, 0, 100, 10, "O2", day=5),
    ]
    out = build_blotter_rows(rows)
    # Most-recent close first.
    assert out[0]["symbol"] == "BBB"
    # Each row's executions carry the close timestamp (drives the DATE column).
    assert out[0]["executions"][-1]["time"].startswith("2026-06-05")
    assert out[1]["executions"][-1]["time"].startswith("2026-06-02")
