"""One-shot replay: resolve pre-existing UNKNOWN order_submissions rows.

Spec: docs/plans/2026-04-28-postgres-migration-completion-IMPL.md §W1.3.

Re-runs the three-source rehydrate path against rows whose state is 'UNKNOWN'.
Idempotent: rows already resolved by an earlier pass are not re-touched.
Surfaces failures in the returned summary rather than silencing them.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Callable

from sqlalchemy import select

from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_submissions
from xenon.execution import orders_store
from xenon.execution.account_scope import AccountScope, resolve_from_env
from xenon.execution.single_leg_rehydrate import rehydrate_on_boot

logger = logging.getLogger(__name__)


def _count_unknown(scope: AccountScope) -> int:
    engine = get_sync_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(order_submissions.c.submission_id).where(
                order_submissions.c.state == "UNKNOWN",
                order_submissions.c.broker == scope.broker,
                order_submissions.c.account_env == scope.account_env,
                order_submissions.c.broker_account == scope.broker_account,
            )
        ).fetchall()
    return len(rows)


def replay_unknown(
    *,
    scope: AccountScope,
    ib_client_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Resolve UNKNOWN submissions in scope through the rehydrate path.

    Returns:
        dict with keys ``resolved``, ``still_unknown``, ``scanned``, ``errors``.
    """
    if ib_client_factory is None:
        from xenon.api import ib_pool

        if ib_pool is None:
            return {"resolved": 0, "still_unknown": 0, "scanned": 0, "errors": ["ib_pool not initialized"]}
        ib_client_factory = lambda: ib_pool.get("sync").ib  # type: ignore[union-attr]

    scanned = _count_unknown(scope)
    if scanned == 0:
        return {"resolved": 0, "still_unknown": 0, "scanned": 0, "errors": []}

    errors: list[str] = []
    try:
        decisions = rehydrate_on_boot(
            ib_client_factory,
            orders_store,
            broker=scope.broker,
            account_env=scope.account_env,
            broker_account=scope.broker_account,
            states=("UNKNOWN",),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("rehydrate raised during UNKNOWN replay")
        errors.append(f"rehydrate failed: {exc}")
        decisions = []

    resolved = sum(1 for d in decisions if not d.noop and d.to_state and d.to_state != "UNKNOWN")
    still_unknown = scanned - resolved
    return {"resolved": resolved, "still_unknown": still_unknown, "scanned": scanned, "errors": errors}


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay UNKNOWN order_submissions through rehydrate.")
    parser.add_argument("--apply", action="store_true", help="Execute replay (default is dry-run summary).")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    scope = resolve_from_env()
    if not args.apply:
        count = _count_unknown(scope)
        print(json.dumps({"dry_run": True, "unknown_count": count, "scope": scope.as_dict()}, indent=2))
        return 0

    summary = replay_unknown(scope=scope)
    print(json.dumps({"applied": True, "scope": scope.as_dict(), **summary}, indent=2))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(_main())
