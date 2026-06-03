"""upsert_nav_sync source-arg behavior post migration 2026_06_03_nav_history_source_in_pk.

Under the post-migration schema, ``source`` is part of the PK so a
``close`` row and an ``intraday`` row for the same scope+date coexist as
two separate audit rows (per Pass-2 E1(a) design — nav_history IS the
audit table; close-vs-intraday divergence is monitored via
``xenon-nav-reconcile``). This module pins the new contract:

* Omitting ``source`` → server default ``intraday``.
* ``source='close'`` writes a close row.
* The two coexist; writing close does NOT overwrite intraday.
* Re-running upsert against the SAME ``(scope, date, source)`` triple
  is idempotent (UPDATEs the matching row).

The pre-migration "close overwrites intraday on conflict" semantics were
removed deliberately; the audit table needs both rows so we can detect
EOD divergence after the fact.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.execution.account_scope import AccountScope
from xenon.utils.portfolio_loader import upsert_nav_sync

SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DUQ999999")


def _read_one(scope: AccountScope, day: date, source: str) -> dict:
    """Read the exact (scope, date, source) row — PK is now 5-col."""
    engine = get_sync_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT nav, source FROM xenon.nav_history "
                "WHERE broker=:b AND account_env=:e AND broker_account=:a "
                "AND date=:d AND source=:s"
            ),
            {
                "b": scope.broker,
                "e": scope.account_env,
                "a": scope.broker_account,
                "d": day,
                "s": source,
            },
        ).first()
    return {"nav": row.nav, "source": row.source} if row else {}


def _count(scope: AccountScope, day: date) -> int:
    """Count all rows for (scope, date) — both sources combined."""
    engine = get_sync_engine()
    with engine.begin() as conn:
        return conn.execute(
            text(
                "SELECT COUNT(*) FROM xenon.nav_history "
                "WHERE broker=:b AND account_env=:e AND broker_account=:a AND date=:d"
            ),
            {
                "b": scope.broker,
                "e": scope.account_env,
                "a": scope.broker_account,
                "d": day,
            },
        ).scalar()


def test_omitting_source_writes_server_default_intraday(pg_test_engine):
    upsert_nav_sync(scope=SCOPE, day=date(2026, 6, 1), nav=Decimal("100.00"))
    assert _read_one(SCOPE, date(2026, 6, 1), "intraday")["nav"] == Decimal("100.00")


def test_source_close_writes_close(pg_test_engine):
    upsert_nav_sync(scope=SCOPE, day=date(2026, 6, 1), nav=Decimal("100.00"), source="close")
    assert _read_one(SCOPE, date(2026, 6, 1), "close")["nav"] == Decimal("100.00")


def test_close_and_intraday_coexist_separately(pg_test_engine):
    """Post-migration design: writing close AFTER intraday creates a SECOND row,
    not an UPDATE. Both audit rows survive so xenon-nav-reconcile can compare."""
    upsert_nav_sync(scope=SCOPE, day=date(2026, 6, 1), nav=Decimal("100.00"))  # intraday
    upsert_nav_sync(scope=SCOPE, day=date(2026, 6, 1), nav=Decimal("110.00"), source="close")
    assert _count(SCOPE, date(2026, 6, 1)) == 2
    assert _read_one(SCOPE, date(2026, 6, 1), "intraday")["nav"] == Decimal("100.00")
    assert _read_one(SCOPE, date(2026, 6, 1), "close")["nav"] == Decimal("110.00")


def test_close_replay_is_idempotent(pg_test_engine):
    """Re-running upsert with same (scope, date, source) UPDATEs the row."""
    upsert_nav_sync(scope=SCOPE, day=date(2026, 6, 1), nav=Decimal("100.00"), source="close")
    upsert_nav_sync(scope=SCOPE, day=date(2026, 6, 1), nav=Decimal("105.00"), source="close")
    assert _count(SCOPE, date(2026, 6, 1)) == 1
    assert _read_one(SCOPE, date(2026, 6, 1), "close")["nav"] == Decimal("105.00")


def test_intraday_replay_is_idempotent(pg_test_engine):
    """Same scope+date with no source → both writes hit the intraday row."""
    upsert_nav_sync(scope=SCOPE, day=date(2026, 6, 1), nav=Decimal("100.00"))
    upsert_nav_sync(scope=SCOPE, day=date(2026, 6, 1), nav=Decimal("102.00"))
    assert _count(SCOPE, date(2026, 6, 1)) == 1
    assert _read_one(SCOPE, date(2026, 6, 1), "intraday")["nav"] == Decimal("102.00")
