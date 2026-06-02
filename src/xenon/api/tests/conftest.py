"""Shared pytest fixtures for src/xenon/api/tests."""

from __future__ import annotations

import pytest

from xenon._test_db import (
    _BoundEngine,
    _ensure_worker_db,  # noqa: F401 — autouse session fixture (Phase 3 xdist DB clone)
    app_engine_bound_to_test,  # noqa: F401 — re-exported for fixture discovery
    async_test_db_url,
    is_pg_reachable,
    pg_session,  # noqa: F401 — re-exported for fixture discovery
    truncate_all_xenon_tables,
)


@pytest.fixture(autouse=True)
def _postgres_orders_test_db(monkeypatch, request):
    """Autouse: per-test txn rollback (Phase 2) with committed_db opt-out.

    Mirrors the dual-path autouse in `scripts/tests/conftest.py`:
      - Default → BEGIN/ROLLBACK on a connection bound to `get_sync_engine()`.
      - `@pytest.mark.committed_db` → Phase 1 TRUNCATE pre+post (required for
        tests whose helpers open their own `create_engine()` or whose route
        flows commit via the async engine — the sync-side binding can't reach
        either of those).

    Async engine is initialized regardless of path because routes that touch
    `get_engine()` (e.g. `GET /portfolio`) raise without it. Tests that use
    `TestClient(app)` without `with` skip the lifespan, so we seed init here.
    """
    # Worker-aware: under pytest-xdist this resolves to the per-worker DB
    # (xenon_test_gwN) so route tests don't write to the master template.
    # Also rewrite DATABASE_URL_TEST so test helpers that build their own
    # engine from that env var (instead of going through sync_test_db_url)
    # also land on the worker DB.
    url = async_test_db_url()
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATABASE_URL_TEST", url)

    try:
        import xenon.db.engine as engine_mod

        # Async engine: required by route handlers; not affected by Phase 2.
        engine_mod._engine = None  # type: ignore[attr-defined]
        engine_mod.init_engine(url)
    except Exception:
        pass

    if not is_pg_reachable():
        yield
        return

    if request.node.get_closest_marker("committed_db"):
        try:
            import xenon.db.engine as engine_mod

            monkeypatch.setattr(engine_mod, "_sync_engine", None)
        except Exception:
            pass
        truncate_all_xenon_tables()
        yield
        truncate_all_xenon_tables()
        return

    conn = request.getfixturevalue("pg_session")
    try:
        import xenon.db.engine as engine_mod

        monkeypatch.setattr(engine_mod, "_sync_engine", _BoundEngine(conn))
    except Exception:
        pass
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
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU0000000")
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
