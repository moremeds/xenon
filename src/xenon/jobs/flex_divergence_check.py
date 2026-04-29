"""Nightly PG↔Flex divergence job (V.4).

Compares yesterday's PG blotter rows against IB Flex same-day output for the
active scope. Writes one ``xenon.flex_divergence_runs`` row and surfaces the
result on ``GET /health.flex_divergence``.

Gracefully no-ops when Flex is not configured.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import insert, select

from xenon.api.subprocess import run_module
from xenon.db.engine import get_sync_engine
from xenon.db.queries.blotter import compare_blotter_rows, fetch_blotter_pg
from xenon.db.schema import flex_divergence_runs
from xenon.execution.account_scope import AccountScope, resolve_from_env

logger = logging.getLogger(__name__)

_NY = ZoneInfo("America/New_York")


def yesterday_session_window() -> tuple[datetime, datetime]:
    """Return [yesterday 00:00 ET, today 00:00 ET) as UTC datetimes."""
    now_ny = datetime.now(_NY)
    today_midnight_ny = datetime.combine(now_ny.date(), time(0, 0), tzinfo=_NY)
    yesterday_midnight_ny = today_midnight_ny - timedelta(days=1)
    return yesterday_midnight_ny.astimezone(timezone.utc), today_midnight_ny.astimezone(timezone.utc)


def compute_divergence(pg: dict[str, Any], flex: dict[str, Any]) -> dict[str, Any]:
    pg_rows = {r["perm_id"]: r for r in pg.get("closed_trades", []) if r.get("perm_id")}
    flex_rows = {r["perm_id"]: r for r in flex.get("closed_trades", []) if r.get("perm_id")}
    overlap = sorted(set(pg_rows) & set(flex_rows))
    diverged = [pid for pid in overlap if compare_blotter_rows(pg_rows[pid], flex_rows[pid])]
    return {
        "total_compared": len(overlap),
        "divergence_count": len(diverged),
        "notes": {"diverged_perm_ids": diverged[:50]},
    }


def record_run(*, scope: AccountScope, summary: dict[str, Any]) -> int:
    engine = get_sync_engine()
    with engine.begin() as conn:
        result = conn.execute(
            insert(flex_divergence_runs)
            .values(
                scope_broker=scope.broker,
                scope_account_env=scope.account_env,
                scope_broker_account=scope.broker_account,
                total_compared=int(summary["total_compared"]),
                divergence_count=int(summary["divergence_count"]),
                notes=summary.get("notes"),
            )
            .returning(flex_divergence_runs.c.id)
        )
        return int(result.scalar_one())


def latest_run(*, scope: AccountScope) -> dict[str, Any] | None:
    engine = get_sync_engine()
    with engine.connect() as conn:
        row = conn.execute(
            select(flex_divergence_runs)
            .where(
                flex_divergence_runs.c.scope_broker == scope.broker,
                flex_divergence_runs.c.scope_account_env == scope.account_env,
                flex_divergence_runs.c.scope_broker_account == scope.broker_account,
            )
            .order_by(flex_divergence_runs.c.ran_at.desc())
            .limit(1)
        ).first()
    return dict(row._mapping) if row is not None else None


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nightly PG↔Flex divergence check.")
    parser.add_argument("--apply", action="store_true", help="Insert a flex_divergence_runs row.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    scope = resolve_from_env()
    start_utc, _end_utc = yesterday_session_window()
    days = max(1, int((datetime.now(timezone.utc) - start_utc).total_seconds() // 86400) + 1)

    engine = get_sync_engine()
    with engine.connect() as conn:
        pg_payload = fetch_blotter_pg(conn, scope=scope, days=days)

    flex_result = asyncio.run(run_module("xenon.trade_blotter.flex_query", ["--json"], timeout=120))
    if not flex_result.ok:
        print(json.dumps({"skipped": True, "reason": "flex_unavailable"}, indent=2))
        return 0

    summary = compute_divergence(pg_payload, flex_result.data or {})
    if args.apply:
        run_id = record_run(scope=scope, summary=summary)
        print(json.dumps({"applied": True, "run_id": run_id, **summary}, indent=2))
    else:
        print(json.dumps({"dry_run": True, **summary}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
