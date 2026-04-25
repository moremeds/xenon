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


@pytest.fixture(autouse=True)
def _trading_mode_paper_default(monkeypatch):
    """Default every test to paper + a paper-prefixed account so the lifespan
    guard verifies. Tests that care about mismatch override these.

    Why this is autouse + reload-based: tests in `test_health_trading_mode.py`
    and `test_orders_mode_guard.py` reload `trading_mode` (and `server`) with
    `live` to exercise the mismatch path. `importlib.reload` is NOT reverted
    by monkeypatch, so without resetting here, `trading_mode.MODE` would leak
    as `"live"` into subsequent tests — making every paper-prefixed account
    fail `verify_account` and tripping the new 503 guard on routes that have
    nothing to do with trading mode.
    """
    import importlib

    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    try:
        import xenon.api.trading_mode as tm

        importlib.reload(tm)
        import xenon.api.server as server

        monkeypatch.setattr(
            server,
            "_get_managed_account_for_health",
            lambda: "DU0000000",
            raising=False,
        )
        # Some tests construct TestClient(app) without a `with` block, so the
        # lifespan that normally seeds app.state never runs. Pre-seed here so
        # require_mode_verified does not trip as a side-effect on routes that
        # have nothing to do with trading mode. Tests that DO run the lifespan
        # (TestClient via `with`) will overwrite these values during startup.
        server.app.state.trading_mode = tm.MODE
        server.app.state.account = "DU0000000"
        server.app.state.mode_verified = True
    except Exception:
        pass
    yield
