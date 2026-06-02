"""xenon-futu-history-sync — operator CLI that runs M3 → M4 → M5 in sequence.

Usage
-----
    uv run xenon-futu-history-sync [--since YYYY-MM-DD]

Flow
----
1. Resolve scope from env (XENON_TRADING_MODE + XENON_BROKER_ACCOUNT,
   broker pinned to 'FUTU').
2. Connect to Futu OpenD, fetch_account() for today's net_liquidation.
3. backfill_history_sync() — pulls deals + cashflows, UPSERTs into
   xenon.futu_trades + xenon.futu_cash_flow.
4. backfill_futu_nav() — walks backward from today's NAV, UPSERTs
   nav_history rows.
5. Print summary.

Idempotent: re-runs UPSERT the same primary-key tuples; safe to run
nightly.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Optional

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from xenon.api.services.futu_history_sync import backfill_history_sync
from xenon.api.services.futu_nav_backfill import backfill_futu_nav
from xenon.clients.futu_client import FutuClient
from xenon.execution.account_scope import AccountScope

logger = logging.getLogger(__name__)

# First persisted-trade date in the user's account from the inception probe.
# Used as the default `--since` so re-runs don't re-walk dead time.
_DEFAULT_INCEPTION = date(2024, 1, 1)


def _default_client_factory() -> FutuClient:
    return FutuClient()


async def run_history_sync(
    engine: AsyncEngine,
    scope: AccountScope,
    since: date,
    today_date: Optional[date] = None,
    client_factory: Optional[Callable[[], Any]] = None,
) -> dict:
    """Orchestrate M3 (fetch) → M4 (persist trades/cashflows) → M5 (walk NAV).

    Returns a merged counts dict for the caller to print.
    Holds one Futu client across the whole sequence — connect once, do all
    three fetches, disconnect on the way out (even on exception).
    """
    if today_date is None:
        today_date = datetime.now(timezone.utc).date()
    factory = client_factory or _default_client_factory
    client = factory()
    client.connect()
    try:
        # Today's NAV anchor from accinfo_query.
        acct = client.fetch_account()
        today_nav = Decimal(str(acct["account_summary"]["net_liquidation"]))

        # M4 uses its own client_factory; pass a no-op factory that returns
        # the SAME already-connected client so we don't open a second OpenD
        # context. backfill_history_sync calls connect()/disconnect() too,
        # but both are idempotent / safe to re-invoke.
        sync_result = await backfill_history_sync(
            engine,
            scope,
            since=datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc),
            client_factory=lambda: client,
        )

        # M5 backward walk from today's anchor.
        n_nav = await backfill_futu_nav(
            engine,
            scope,
            today_nav=today_nav,
            today_date=today_date,
            since=since,
        )
    finally:
        # backfill_history_sync already calls disconnect once; calling it
        # again is harmless (FutuClient.disconnect is no-op if already
        # disconnected) but we keep the try/finally to cover paths that
        # fail BEFORE backfill_history_sync runs.
        try:
            client.disconnect()
        except Exception:
            pass

    return {
        "today_nav": float(today_nav),
        "nav_rows_written": n_nav,
        **sync_result,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="xenon-futu-history-sync",
        description=(
            "Pull historical Futu trades + cashflows and rebuild the backward "
            "NAV walk into xenon.nav_history. Idempotent — safe to run nightly."
        ),
    )
    p.add_argument(
        "--since",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        default=_DEFAULT_INCEPTION,
        help="Earliest date to fetch (YYYY-MM-DD). Default: 2024-01-01.",
    )
    return p


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = _build_arg_parser().parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print(
            "DATABASE_URL is not set. Run via `scripts/infra/dev.sh paper` or set it explicitly.",
            flush=True,
        )
        return 2

    # Scope from env. Broker is pinned to FUTU; trading mode + account
    # come from XENON_TRADING_MODE / XENON_BROKER_ACCOUNT per the standard
    # subprocess pattern.
    scope = AccountScope.resolve_from_env(broker="FUTU")

    async def _run():
        engine = create_async_engine(db_url, pool_pre_ping=True)
        try:
            return await run_history_sync(engine, scope, since=args.since)
        finally:
            await engine.dispose()

    result = asyncio.run(_run())

    print()
    print(f"xenon-futu-history-sync — scope={scope}")
    print(f"  today's net_liquidation:  ${result['today_nav']:,.2f}")
    print(f"  trades fetched:           {result['trades_fetched']:>6}")
    print(f"  trades inserted (UPSERT): {result['trades_inserted']:>6}")
    print(f"  non-US deals filtered:    {result['deals_filtered_non_us']:>6}")
    print(f"  cashflows fetched:        {result['cashflows_fetched']:>6}")
    print(f"  cashflows inserted:       {result['cashflows_inserted']:>6}")
    print(f"  nav_history rows written: {result['nav_rows_written']:>6}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
