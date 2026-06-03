"""Task 0 — nav_history writer unification (Pass-2 E1-a + writer migration).

All four legacy writers
  - upsert_nav_sync (the unified surface)
  - ib_sync._append_nav_snapshot
  - persist_futu_nav
  - db.queries.portfolio.upsert_nav
funnel through ``xenon.utils.portfolio_loader._build_upsert_stmt``. The CI
guard in ``scripts/checks/no_pg_insert_nav_history.py`` enforces this at
import-graph level; these tests pin the behavior at runtime.

Pass-2 E1(a): the new PK ``(broker, account_env, broker_account, date, source)``
lets intraday + close rows coexist for the same scope+date — nav_history IS
the audit table.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from xenon.api.services.futu_nav_persistence import NavAccountEnvConflict
from xenon.db.engine import get_sync_engine
from xenon.execution.account_scope import AccountScope
from xenon.utils.portfolio_loader import (
    _upsert_nav_sync_unguarded,
    upsert_nav_async,
    upsert_nav_sync,
)

SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DUQ_UNIF1")
DAY = date(2026, 6, 1)


def _read_back(scope: AccountScope, day: date, source: str | None = None) -> list[dict]:
    engine = get_sync_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT nav, source, account_env FROM xenon.nav_history "
                "WHERE broker=:b AND account_env=:e AND broker_account=:a AND date=:d "
                + ("AND source=:s " if source else "")
                + "ORDER BY source"
            ),
            {
                "b": scope.broker,
                "e": scope.account_env,
                "a": scope.broker_account,
                "d": day,
                "s": source,
            },
        ).fetchall()
    return [{"nav": r.nav, "source": r.source, "account_env": r.account_env} for r in rows]


# ---------- Cross-env guard ----------


def test_upsert_nav_sync_default_enforces_guard(pg_test_engine):
    """Pass-2 T3: guard is default-ON. Cross-env write raises without opt-in."""
    upsert_nav_sync(scope=SCOPE, day=DAY, nav=Decimal("100"))
    other = AccountScope(broker="IB", account_env="live", broker_account=SCOPE.broker_account)
    with pytest.raises(NavAccountEnvConflict):
        upsert_nav_sync(scope=other, day=DAY, nav=Decimal("200"))


def test_upsert_nav_sync_unguarded_escape_hatch(pg_test_engine):
    """Pass-2 T3: ``_upsert_nav_sync_unguarded`` bypasses the cross-env guard."""
    upsert_nav_sync(scope=SCOPE, day=DAY, nav=Decimal("100"))
    other = AccountScope(broker="IB", account_env="live", broker_account=SCOPE.broker_account)
    # The unique index on (broker, broker_account, date, source) still blocks
    # bypassing the guard from creating two rows with different account_env
    # for the same (scope, source). What the escape hatch enables is the
    # legacy unscoped backfill path that knows it's the sole writer.
    _upsert_nav_sync_unguarded(scope=other, day=DAY, nav=Decimal("200"), source="close")
    # Both rows visible because they differ on `source`.
    rows = _read_back(other, DAY)
    assert {r["source"] for r in rows} >= {"close"}


# ---------- Two-source-same-date coexistence (Pass-2 T1) ----------


def test_two_source_rows_per_date_coexist(pg_test_engine):
    """Post-migration: intraday + close rows for the same scope+date coexist."""
    upsert_nav_sync(scope=SCOPE, day=DAY, nav=Decimal("100000"), source="intraday")
    upsert_nav_sync(scope=SCOPE, day=DAY, nav=Decimal("100050"), source="close")
    rows = _read_back(SCOPE, DAY)
    assert len(rows) == 2
    assert {r["source"] for r in rows} == {"intraday", "close"}
    by_src = {r["source"]: r["nav"] for r in rows}
    assert by_src["intraday"] == Decimal("100000.00")
    assert by_src["close"] == Decimal("100050.00")


def test_idempotent_same_source_overwrites_nav(pg_test_engine):
    """Same (scope, date, source) updates the existing row; no duplicate insert."""
    upsert_nav_sync(scope=SCOPE, day=DAY, nav=Decimal("100"), source="intraday")
    upsert_nav_sync(scope=SCOPE, day=DAY, nav=Decimal("105"), source="intraday")
    rows = _read_back(SCOPE, DAY, source="intraday")
    assert len(rows) == 1
    assert rows[0]["nav"] == Decimal("105.00")


# ---------- Async wrapper (Pass-2 T6) ----------


@pytest.mark.asyncio
async def test_upsert_nav_async_basic(async_engine):
    """Async wrapper writes through AsyncEngine; visible from sync read-back."""
    futu = AccountScope(broker="FUTU", account_env="live", broker_account="FUTU_UNIF1")
    await upsert_nav_async(
        async_engine,
        scope=futu,
        day=DAY,
        nav=Decimal("50000"),
        source="intraday",
    )
    rows = _read_back(futu, DAY)
    assert rows == [{"nav": Decimal("50000.00"), "source": "intraday", "account_env": "live"}]


@pytest.mark.asyncio
async def test_upsert_nav_async_cross_env_guard(async_engine):
    """Pass-2 T6: async wrapper enforces the cross-env guard by default."""
    futu = AccountScope(broker="FUTU", account_env="live", broker_account="FUTU_UNIF2")
    await upsert_nav_async(async_engine, scope=futu, day=DAY, nav=Decimal("100"), source="intraday")
    other = AccountScope(broker="FUTU", account_env="paper", broker_account=futu.broker_account)
    with pytest.raises(NavAccountEnvConflict):
        await upsert_nav_async(async_engine, scope=other, day=DAY, nav=Decimal("200"), source="intraday")


# ---------- IB writer delegation ----------


def test_ib_append_nav_snapshot_delegates_to_upsert_nav_sync(monkeypatch, pg_test_engine):
    """``_append_nav_snapshot`` funnels through the shared writer surface."""
    from xenon.execution import ib_sync

    monkeypatch.setenv("XENON_BROKER", "IB")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DUQ_DELEGATE1")
    monkeypatch.delenv("XENON_READ_ONLY", raising=False)

    calls: list[dict] = []

    def _spy(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("xenon.utils.portfolio_loader.upsert_nav_sync", _spy)
    # Re-import the symbol inside ib_sync._append_nav_snapshot — it imports
    # upsert_nav_sync lazily, so monkeypatching the module attribute is the
    # right entry point.
    ib_sync._append_nav_snapshot(net_liq=12345.67, daily_pnl=42.0)

    assert len(calls) == 1
    call = calls[0]
    assert call["scope"].broker == "IB"
    assert call["scope"].account_env == "paper"
    assert call["scope"].broker_account == "DUQ_DELEGATE1"
    assert call["nav"] == Decimal("12345.67")
    assert call["daily_pnl"] == Decimal("42.00")
    assert call["source"] == "intraday"


def test_ib_append_nav_snapshot_skipped_under_read_only(monkeypatch, pg_test_engine):
    """``XENON_READ_ONLY=1`` contract preserved post-migration."""
    from xenon.execution import ib_sync

    monkeypatch.setenv("XENON_READ_ONLY", "1")
    calls: list[dict] = []
    monkeypatch.setattr(
        "xenon.utils.portfolio_loader.upsert_nav_sync",
        lambda **kw: calls.append(kw),
    )
    ib_sync._append_nav_snapshot(net_liq=100.0)
    assert calls == []


# ---------- FUTU writer delegation (Pass-2 T6) ----------


@pytest.mark.asyncio
async def test_persist_futu_nav_delegates_to_upsert_nav_async(monkeypatch, async_engine):
    """``persist_futu_nav`` routes through the async wrapper, not sync."""
    from xenon.api.services import futu_nav_persistence

    calls: list[dict] = []

    async def _spy(engine, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("xenon.utils.portfolio_loader.upsert_nav_async", _spy)

    class _FakeFutuClient:
        _acc_id = "FUTU_DELEGATE1"

    payload = {"account_summary": {"net_liquidation": 50_000.0}}
    await futu_nav_persistence.persist_futu_nav(async_engine, _FakeFutuClient(), "REAL", payload)

    assert len(calls) == 1
    call = calls[0]
    assert call["scope"].broker == "FUTU"
    assert call["scope"].broker_account == "FUTU_DELEGATE1"
    assert call["source"] == "intraday"
    assert call["nav"] == Decimal("50000.0")
