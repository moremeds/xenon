"""Audit duplicate/absent close claims for position rules."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from xenon.db.engine import get_sync_engine


def _parse_since(value: str) -> datetime:
    now = datetime.now(timezone.utc)
    if value.endswith("d"):
        return now - timedelta(days=int(value[:-1]))
    if value.endswith("h"):
        return now - timedelta(hours=int(value[:-1]))
    raise ValueError(f"unrecognized --since: {value!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="14d")
    parser.add_argument("--scope-account")
    args = parser.parse_args(argv)
    cutoff = _parse_since(args.since)
    violations: list[dict] = []

    with get_sync_engine().connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT pp.protection_id, pp.position_key,
                       COUNT(c.claim_id) FILTER (WHERE c.status = 'FILLED') AS filled_claims
                FROM xenon.position_protection pp
                LEFT JOIN xenon.position_close_claims c
                  ON c.broker = pp.broker
                 AND c.account_env = pp.account_env
                 AND c.broker_account = pp.broker_account
                 AND c.position_key = pp.position_key
                WHERE pp.state = 'CLOSED'
                  AND pp.closed_at >= :cutoff
                  AND (CAST(:account AS text) IS NULL OR pp.broker_account = :account)
                GROUP BY pp.protection_id, pp.position_key
                HAVING COUNT(c.claim_id) FILTER (WHERE c.status = 'FILLED') != 1
                """
            ),
            {"cutoff": cutoff, "account": args.scope_account},
        ).all()
        for row in rows:
            violations.append(
                {
                    "rule": "closed_row_must_have_one_filled_claim",
                    "protection_id": row.protection_id,
                    "position_key": row.position_key,
                    "filled_claims": int(row.filled_claims),
                }
            )

        rows = conn.execute(
            text(
                """
                SELECT broker_account, position_key, DATE(submitted_at) AS day, COUNT(*) AS count
                FROM xenon.position_close_claims
                WHERE submitted_at >= :cutoff
                  AND status IN ('SUBMITTED','FILLED','ABANDONED')
                  AND (CAST(:account AS text) IS NULL OR broker_account = :account)
                GROUP BY broker_account, position_key, DATE(submitted_at)
                HAVING COUNT(*) > 1
                """
            ),
            {"cutoff": cutoff, "account": args.scope_account},
        ).all()
        for row in rows:
            violations.append(
                {
                    "rule": "at_most_one_close_per_position_per_day",
                    "broker_account": row.broker_account,
                    "position_key": row.position_key,
                    "day": row.day.isoformat(),
                    "count": int(row.count),
                }
            )

    print(json.dumps({"window_since": cutoff.isoformat(), "violations": violations, "count": len(violations)}))
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
