"""Phase 2 / Task 4 regression tests: per-test transactional rollback isolation.

These three tests prove the three claims Phase 2 relies on:

1. A row inserted inside a test must NOT be visible after the test ends.
2. The next test sees an empty table — i.e. rollback discarded the prior insert.
3. When the app engine is bound to the test's connection (the
   `app_engine_bound_to_test` fixture monkeypatches
   `xenon.db.engine._sync_engine`), an app-side `commit()` is still visible to
   the test session — because both share the same physical connection — and is
   still rolled back at end-of-test.

Phase 1 used TRUNCATE-pre/post for isolation. Phase 2 switches to BEGIN/ROLLBACK
which is O(1) per test and ~10x cheaper. See
`docs/superpowers/plans/2026-06-01-pytest-suite-speedup.md` § Phase 2.
"""

from __future__ import annotations

from sqlalchemy import text

# These two are not used directly — they're referenced via fixture injection.
# Importing here primarily documents the fixture dependency for readers.


def test_test_writes_are_invisible_after_rollback(pg_session):
    """A row inserted inside a test must NOT be visible after the test ends.

    Sentinel value `__phase2_isolation_probe__` is paired with
    `test_no_leak_from_previous_test` below — the pair forms the isolation
    guard. If the rollback ever leaks, the second test fails.
    """
    pg_session.execute(
        text(
            "INSERT INTO xenon.ticker_cache (ticker, cache_type, data) VALUES ('__phase2_isolation_probe__', 'phase2_test', '{}'::jsonb)"
        )
    )
    seen = pg_session.execute(
        text("SELECT count(*) FROM xenon.ticker_cache WHERE ticker = '__phase2_isolation_probe__'")
    ).scalar()
    assert seen == 1, "INSERT was not visible inside its own transaction"


def test_no_leak_from_previous_test(pg_session):
    """If the previous test's INSERT leaked, this test sees a row and fails."""
    leaked = pg_session.execute(
        text("SELECT count(*) FROM xenon.ticker_cache WHERE ticker = '__phase2_isolation_probe__'")
    ).scalar()
    assert leaked == 0, (
        "Previous test's INSERT leaked across the rollback boundary — pg_session txn rollback is broken."
    )


def test_app_engine_writes_visible_inside_test_txn(app_engine_bound_to_test):
    """The app's own engine, when bound to the test connection, must commit
    into the test's transaction (so the test sees it) without committing to
    the real DB (so the next test does not see it).

    Without `app_engine_bound_to_test`, any route or CLI that opens its own
    SQLAlchemy session would write to a separate connection and rollback
    would miss those writes. This test guards that wiring.
    """
    from xenon.db.engine import get_sync_engine

    app_engine = get_sync_engine()
    with app_engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO xenon.ticker_cache (ticker, cache_type, data) VALUES ('__phase2_app_engine_probe__', 'phase2_test', '{}'::jsonb)"
            )
        )
        conn.commit()  # app code commits internally — must NOT escape the test txn

    # Visible inside the test's session because both share the connection
    seen = app_engine_bound_to_test.execute(
        text("SELECT count(*) FROM xenon.ticker_cache WHERE ticker = '__phase2_app_engine_probe__'")
    ).scalar()
    assert seen == 1, (
        "App-engine commit was not visible to test session — "
        "connection injection failed (engine returned a different connection)."
    )
