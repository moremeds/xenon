"""Shared pytest fixtures for src/xenon/api/tests.

B1 — isolate every test's DuckDB writes to a per-test tmp path so a
TestClient(app) lifespan never touches the real data/orders.duckdb.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_orders_db(tmp_path_factory, monkeypatch):
    """Redirect XENON_ORDERS_DB_PATH to a per-test tmp directory.

    The lifespan rehydrate hook reads XENON_ORDERS_DB_PATH; without this
    fixture, tests that boot TestClient(app) in test_mode would hit the
    shared prod DuckDB at data/orders.duckdb.
    """
    tmp_dir = tmp_path_factory.mktemp("orders")
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_dir / "orders.duckdb"))
    yield
