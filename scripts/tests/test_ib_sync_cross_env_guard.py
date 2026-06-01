"""ib_sync._append_nav_snapshot cross-env guard (Decisions §13 + correction #2)."""
from datetime import date, datetime

import pytest
import pytz
import sqlalchemy as sa

from xenon.api.services.futu_nav_persistence import NavAccountEnvConflict
from xenon.db.schema import nav_history
from xenon.execution.ib_sync import _append_nav_snapshot


def _today_et():
    return datetime.now(pytz.timezone("America/New_York")).date()


def test_append_raises_when_existing_row_has_different_env(sync_engine, monkeypatch):
    """The function reads scope from env vars; we set env first then prime
    the DB with an opposing env for today, then expect the conflict."""
    monkeypatch.setenv("XENON_BROKER", "IB")
    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "U_GUARD_TEST")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://xenon_app:xenon_dev@localhost:2000/core_dev")
    # Force the sync engine module to repick up DATABASE_URL.
    import xenon.db.engine as eng_mod
    eng_mod._sync_engine = None  # type: ignore[attr-defined]

    today = _today_et()
    # Seed a paper row for today; the function thinks it's live → must raise.
    with sync_engine.begin() as c:
        c.execute(sa.delete(nav_history).where(nav_history.c.broker_account == "U_GUARD_TEST"))
        c.execute(
            sa.insert(nav_history).values(
                broker="IB",
                account_env="paper",
                broker_account="U_GUARD_TEST",
                date=today,
                nav="100000.00",
                daily_pnl="0.00",
                source="intraday",
            )
        )

    try:
        with pytest.raises(NavAccountEnvConflict):
            _append_nav_snapshot(100000.00, 0.00)
    finally:
        with sync_engine.begin() as c:
            c.execute(sa.delete(nav_history).where(nav_history.c.broker_account == "U_GUARD_TEST"))
        eng_mod._sync_engine = None  # type: ignore[attr-defined]


def test_append_proceeds_when_env_matches(sync_engine, monkeypatch):
    monkeypatch.setenv("XENON_BROKER", "IB")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU_GUARD_OK")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://xenon_app:xenon_dev@localhost:2000/core_dev")
    import xenon.db.engine as eng_mod
    eng_mod._sync_engine = None  # type: ignore[attr-defined]

    try:
        with sync_engine.begin() as c:
            c.execute(sa.delete(nav_history).where(nav_history.c.broker_account == "DU_GUARD_OK"))
        _append_nav_snapshot(50000.00, 0.00)
        with sync_engine.begin() as c:
            row = c.execute(
                sa.select(nav_history).where(nav_history.c.broker_account == "DU_GUARD_OK")
            ).first()
        assert row is not None
        assert float(row.nav) == 50000.00
    finally:
        with sync_engine.begin() as c:
            c.execute(sa.delete(nav_history).where(nav_history.c.broker_account == "DU_GUARD_OK"))
        eng_mod._sync_engine = None  # type: ignore[attr-defined]
