"""Verify nav_history schema extension + sync loader.

Prerequisite for deleting data/nav_history.jsonl and data/nav_history_ib.json.
The IB Flex NAV importer (`fetch_ib_nav_series` in portfolio_performance.py)
returns `{date, total, cash, stock, options}`; PG must persist all four.
"""

from __future__ import annotations

import inspect

from xenon.db.queries.portfolio import upsert_nav
from xenon.db.schema import nav_history
from xenon.utils.portfolio_loader import load_nav_history_sync


def test_nav_history_schema_has_breakdown_columns():
    cols = {c.name for c in nav_history.c}
    assert "total" in cols
    assert "cash" in cols
    assert "stock_value" in cols
    assert "options_value" in cols
    # Existing columns preserved
    assert "nav" in cols
    assert "daily_pnl" in cols


def test_nav_history_breakdown_columns_are_nullable():
    """Breakdown columns must be optional — ib_sync's daily upsert sends
    nav only; the IB Flex importer fills the breakdown when available.
    """
    for name in ("total", "cash", "stock_value", "options_value"):
        col = nav_history.c[name]
        assert col.nullable, f"nav_history.{name} must be nullable"


def test_upsert_nav_signature_accepts_breakdown_kwargs():
    sig = inspect.signature(upsert_nav)
    params = sig.parameters
    for name in ("total", "cash", "stock_value", "options_value"):
        assert name in params, f"upsert_nav missing kwarg {name!r}"
        assert params[name].default is None, f"upsert_nav.{name} must default to None"


def test_load_nav_history_sync_signature():
    sig = inspect.signature(load_nav_history_sync)
    assert "scope" in sig.parameters
