"""Tests for persist_futu_nav (spec § Persistence flow + Decisions §13).

Covers happy-path persistence, daily_pnl computation across day boundaries,
cross-env collision raising NavAccountEnvConflict, and the guard-list
(missing _acc_id, missing matched_trd_env, missing net_liquidation).
"""
from datetime import date
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa

from xenon.api.services.futu_nav_persistence import (
    NavAccountEnvConflict,
    persist_futu_nav,
)
from xenon.db.schema import nav_history

pytestmark = pytest.mark.asyncio  # Correction #7: required for strict asyncio mode


# ---------- helpers ----------


def _client(acc_id=42):
    c = MagicMock()
    c._acc_id = acc_id
    return c


def _payload(net_liq=100000.00):
    return {"account_summary": {"net_liquidation": net_liq}}


async def _purge(async_engine, account):
    async with async_engine.begin() as conn:
        await conn.execute(
            sa.delete(nav_history).where(nav_history.c.broker_account == account)
        )


# ---------- happy path ----------


async def test_first_call_inserts_FUTU_paper_row(async_engine):
    """SIMULATE→paper per correction #18."""
    await _purge(async_engine, "T_INS_PAPER")
    await persist_futu_nav(async_engine, _client(acc_id="T_INS_PAPER"), "SIMULATE", _payload())
    async with async_engine.begin() as conn:
        row = (
            await conn.execute(
                sa.select(nav_history).where(
                    (nav_history.c.broker == "FUTU")
                    & (nav_history.c.broker_account == "T_INS_PAPER")
                )
            )
        ).first()
    await _purge(async_engine, "T_INS_PAPER")
    assert row is not None
    assert row.account_env == "paper"
    assert float(row.nav) == 100000.00
    assert row.daily_pnl is None
    assert row.source == "intraday"


async def test_REAL_maps_to_live(async_engine):
    await _purge(async_engine, "T_INS_LIVE")
    await persist_futu_nav(async_engine, _client(acc_id="T_INS_LIVE"), "REAL", _payload())
    async with async_engine.begin() as conn:
        env = (
            await conn.execute(
                sa.select(nav_history.c.account_env).where(
                    nav_history.c.broker_account == "T_INS_LIVE"
                )
            )
        ).scalar()
    await _purge(async_engine, "T_INS_LIVE")
    assert env == "live"


async def test_daily_pnl_computed_from_prev_row(async_engine):
    await _purge(async_engine, "T_PNL")
    async with async_engine.begin() as conn:
        await conn.execute(
            sa.insert(nav_history).values(
                broker="FUTU",
                account_env="live",
                broker_account="T_PNL",
                date=date(2025, 1, 1),  # arbitrary past date
                nav="95000.00",
                daily_pnl="0.00",
                source="intraday",
            )
        )
    await persist_futu_nav(async_engine, _client(acc_id="T_PNL"), "REAL", _payload(net_liq=100000.00))
    async with async_engine.begin() as conn:
        rows = (
            await conn.execute(
                sa.select(nav_history.c.date, nav_history.c.daily_pnl)
                .where(nav_history.c.broker_account == "T_PNL")
                .order_by(nav_history.c.date.asc())
            )
        ).all()
    await _purge(async_engine, "T_PNL")
    today_row = [r for r in rows if r.date != date(2025, 1, 1)][0]
    assert float(today_row.daily_pnl) == 5000.00


async def test_ignores_payload_daily_pnl(async_engine):
    """payload['daily_pnl'] is lifetime unrealized — never trust it."""
    await _purge(async_engine, "T_IGN_DPNL")
    await persist_futu_nav(
        async_engine,
        _client(acc_id="T_IGN_DPNL"),
        "REAL",
        {"account_summary": {"net_liquidation": 100000.00, "daily_pnl": 9999.99}},
    )
    async with async_engine.begin() as conn:
        dp = (
            await conn.execute(
                sa.select(nav_history.c.daily_pnl).where(
                    nav_history.c.broker_account == "T_IGN_DPNL"
                )
            )
        ).scalar()
    await _purge(async_engine, "T_IGN_DPNL")
    assert dp is None  # no prev row → None, NOT 9999.99


# ---------- cross-env collision ----------


async def test_cross_env_collision_raises(async_engine):
    await _purge(async_engine, "T_COLLIDE")
    from xenon.utils.market_calendar import current_session_date_et

    today = current_session_date_et()
    async with async_engine.begin() as conn:
        await conn.execute(
            sa.insert(nav_history).values(
                broker="FUTU",
                account_env="live",
                broker_account="T_COLLIDE",
                date=today,
                nav="100000.00",
                daily_pnl="0.00",
                source="intraday",
            )
        )
    with pytest.raises(NavAccountEnvConflict):
        await persist_futu_nav(async_engine, _client(acc_id="T_COLLIDE"), "SIMULATE", _payload())
    await _purge(async_engine, "T_COLLIDE")


# ---------- guard list ----------


async def test_acc_id_None_returns_early(async_engine, caplog):
    c = MagicMock()
    c._acc_id = None
    await persist_futu_nav(async_engine, c, "REAL", _payload())
    assert any("_acc_id is None" in r.message for r in caplog.records)


async def test_unknown_matched_trd_env_returns_early(async_engine, caplog):
    await persist_futu_nav(async_engine, _client(acc_id="X"), "BOGUS", _payload())
    assert any("unknown matched_trd_env" in r.message for r in caplog.records)


async def test_missing_net_liquidation_returns_early(async_engine, caplog):
    await persist_futu_nav(
        async_engine, _client(acc_id="X"), "REAL", {"account_summary": {}}
    )
    assert any("missing net_liquidation" in r.message for r in caplog.records)
