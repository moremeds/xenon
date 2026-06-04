"""Persist FUTU NAV into xenon.nav_history (shared between FastAPI + CLI).

Spec: docs/superpowers/specs/2026-05-31-performance-rebuild-design.md
      § Persistence flow (FUTU NAV); Decisions §9, §10, §13.

Pre-execution corrections applied:
  - #4 race-safe cross-env guard (catch IntegrityError + re-query → 409)
  - #18 SIMULATE→"paper" via account_scope.env_from_trd_env
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from xenon.db.schema import nav_history
from xenon.execution.account_scope import AccountScope, env_from_trd_env
from xenon.utils.market_calendar import current_session_date_et

logger = logging.getLogger(__name__)


class NavAccountEnvConflict(Exception):
    """Raised on cross-env collision for (broker, broker_account, date).

    FastAPI handler maps this to HTTP 409.
    """

    def __init__(self, scope: AccountScope, existing_env: str, date_: date):
        super().__init__(
            f"NAV account_env conflict: existing={existing_env!r} "
            f"new={scope.account_env!r} for "
            f"({scope.broker}, {scope.broker_account}, {date_})"
        )
        self.scope = scope
        self.existing_env = existing_env
        self.date = date_


def _safe_extract_net_liq(payload: dict) -> float | None:
    """Extract net_liquidation from a Futu sync payload. None on missing/malformed."""
    try:
        v = payload["account_summary"]["net_liquidation"]
    except (KeyError, TypeError):
        return None
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def _existing_account_env(conn, broker: str, broker_account: str, date_: date) -> str | None:
    row = (
        await conn.execute(
            sa.select(nav_history.c.account_env).where(
                (nav_history.c.broker == broker)
                & (nav_history.c.broker_account == broker_account)
                & (nav_history.c.date == date_)
            )
        )
    ).first()
    return row.account_env if row else None


async def _prev_nav(conn, scope: AccountScope, today: date) -> float | None:
    row = (
        await conn.execute(
            sa.select(nav_history.c.nav)
            .where(
                (nav_history.c.broker == scope.broker)
                & (nav_history.c.account_env == scope.account_env)
                & (nav_history.c.broker_account == scope.broker_account)
                & (nav_history.c.date < today)
            )
            .order_by(nav_history.c.date.desc())
            .limit(1)
        )
    ).first()
    return float(row.nav) if row else None


async def persist_futu_nav(
    engine: AsyncEngine,
    futu_client: Any,
    matched_trd_env: str | None,
    payload: dict,
) -> None:
    """Persist a FUTU NAV row scoped to the currently-connected account.

    Early-returns (logs warning, no row written) on:
      - futu_client._acc_id is None (transient OpenD disconnect mid-call)
      - matched_trd_env not in {"REAL", "SIMULATE"} (caller didn't validate)
      - payload missing or malformed net_liquidation

    Raises NavAccountEnvConflict (→ 409) on cross-env collision, including the
    race where two concurrent writers both pass the app-level check and the
    losing INSERT hits the partial unique index (Decisions §13).
    """
    if futu_client._acc_id is None:
        logger.warning("persist_futu_nav skipped: _acc_id is None")
        return
    if matched_trd_env not in {"REAL", "SIMULATE"}:
        logger.warning("persist_futu_nav skipped: unknown matched_trd_env=%r", matched_trd_env)
        return
    net_liq = _safe_extract_net_liq(payload)
    if net_liq is None:
        logger.warning("persist_futu_nav skipped: payload missing net_liquidation")
        return

    scope = AccountScope(
        broker="FUTU",
        account_env=env_from_trd_env(matched_trd_env),
        broker_account=str(futu_client._acc_id),
    )
    today = current_session_date_et()

    from decimal import Decimal

    from xenon.utils.portfolio_loader import upsert_nav_async

    # _prev_nav uses a short read-only transaction; the upsert opens its own.
    async with engine.begin() as conn:
        prev_nav = await _prev_nav(conn, scope, today)
    daily_pnl = (net_liq - prev_nav) if prev_nav is not None else None

    # Pass-2 T6: delegate to async wrapper. Race-safe pattern (SELECT +
    # IntegrityError catch + re-query winner) lives inside the shared helper.
    await upsert_nav_async(
        engine,
        scope=scope,
        day=today,
        nav=Decimal(str(net_liq)),
        daily_pnl=Decimal(str(daily_pnl)) if daily_pnl is not None else None,
        source="intraday",
    )
