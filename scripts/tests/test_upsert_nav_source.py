"""upsert_nav_sync source-arg behavior (PR-1 NAV auto-refresh)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.execution.account_scope import AccountScope
from xenon.utils.portfolio_loader import upsert_nav_sync

SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DUQ999999")


def _read_back(scope: AccountScope, day: date) -> dict:
    engine = get_sync_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT nav, source FROM xenon.nav_history "
                "WHERE broker=:b AND account_env=:e AND broker_account=:a AND date=:d"
            ),
            {"b": scope.broker, "e": scope.account_env, "a": scope.broker_account, "d": day},
        ).first()
    return {"nav": row.nav, "source": row.source} if row else {}


def test_omitting_source_writes_server_default_intraday(pg_test_engine):
    upsert_nav_sync(scope=SCOPE, day=date(2026, 6, 1), nav=Decimal("100.00"))
    assert _read_back(SCOPE, date(2026, 6, 1))["source"] == "intraday"


def test_source_close_writes_close(pg_test_engine):
    upsert_nav_sync(scope=SCOPE, day=date(2026, 6, 1), nav=Decimal("100.00"), source="close")
    assert _read_back(SCOPE, date(2026, 6, 1))["source"] == "close"


def test_source_close_overwrites_existing_intraday(pg_test_engine):
    upsert_nav_sync(scope=SCOPE, day=date(2026, 6, 1), nav=Decimal("100.00"))
    assert _read_back(SCOPE, date(2026, 6, 1))["source"] == "intraday"
    upsert_nav_sync(scope=SCOPE, day=date(2026, 6, 1), nav=Decimal("110.00"), source="close")
    row = _read_back(SCOPE, date(2026, 6, 1))
    assert row["source"] == "close"
    assert row["nav"] == Decimal("110.00")


def test_omitting_source_preserves_existing_close(pg_test_engine):
    upsert_nav_sync(scope=SCOPE, day=date(2026, 6, 1), nav=Decimal("100.00"), source="close")
    upsert_nav_sync(scope=SCOPE, day=date(2026, 6, 1), nav=Decimal("105.00"))
    row = _read_back(SCOPE, date(2026, 6, 1))
    assert row["source"] == "close"  # preserved on conflict
    assert row["nav"] == Decimal("105.00")  # nav still updates
