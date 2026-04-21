#!/usr/bin/env python3.13
"""One-time TZ fix for pre-upgrade orders.duckdb timestamps.

Background (PR-C/D review D2)
-----------------------------
Before the PR-C/D patch, ``orders_store`` wrote ``datetime.now(timezone.utc)``
but did NOT pin the DuckDB session TimeZone. DuckDB converted those aware
values to the host's local TZ before stripping tzinfo, so rows on non-UTC
hosts were stored as *local wall-clock* naive timestamps.

After the patch, the session TimeZone is pinned to UTC for both writes and
reads. ``_submitted_at_epoch`` treats every naive value as UTC. On upgrade,
pre-patch rows are reinterpreted as UTC → age off by the host's UTC offset →
fresh PENDING rows risk being auto-FAILED with PENDING_TIMEOUT.

This script rewrites ``submitted_at`` and ``updated_at`` in
``orders_submissions`` (and ``at`` in ``orders_events``) from a declared
pre-upgrade local TZ into UTC wall-clock, matching the post-patch reader.

Usage
-----
Dry-run (default, no writes, prints a preview)::

    python3.13 scripts/migrations/2026_04_21_orders_submitted_at_to_utc.py \
        --from-tz America/Los_Angeles

Apply the rewrite in place::

    python3.13 scripts/migrations/2026_04_21_orders_submitted_at_to_utc.py \
        --from-tz America/Los_Angeles --apply

Point at a non-default DB::

    ... --db path/to/orders.duckdb

Safety
------
* Idempotency: the migration is NOT idempotent. Run once per DB. A sentinel
  ``INSERT INTO orders_events`` with ``kind='MIGRATION_TZ_UTC_V1'`` is written
  on ``--apply`` and the script aborts if it already exists.
* A backup is recommended: ``cp data/orders.duckdb data/orders.duckdb.bak``
  before running ``--apply``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import duckdb

SENTINEL_KIND = "MIGRATION_TZ_UTC_V1"


def _default_db_path() -> Path:
    env = os.environ.get("XENON_ORDERS_DB_PATH")
    return Path(env) if env else Path("data/orders.duckdb")


def _already_applied(con: duckdb.DuckDBPyConnection) -> bool:
    try:
        row = con.execute(
            "SELECT 1 FROM orders_events WHERE kind = ? LIMIT 1",
            [SENTINEL_KIND],
        ).fetchone()
    except duckdb.Error:
        return False
    return row is not None


def _preview_rows(con: duckdb.DuckDBPyConnection, from_tz: str, n: int = 5) -> list[tuple]:
    return con.execute(
        f"""
        SELECT submission_id,
               submitted_at AS before_submitted_at,
               (submitted_at AT TIME ZONE ?) AT TIME ZONE 'UTC'
                   AS after_submitted_at,
               updated_at AS before_updated_at,
               (updated_at AT TIME ZONE ?) AT TIME ZONE 'UTC'
                   AS after_updated_at
          FROM orders_submissions
         ORDER BY submitted_at
         LIMIT {int(n)}
        """,
        [from_tz, from_tz],
    ).fetchall()


def _oldest_age_hours(con: duckdb.DuckDBPyConnection, from_tz: str) -> float | None:
    row = con.execute(
        """
        SELECT (submitted_at AT TIME ZONE ?) AT TIME ZONE 'UTC' AS after_utc
          FROM orders_submissions
         ORDER BY submitted_at
         LIMIT 1
        """,
        [from_tz],
    ).fetchone()
    if row is None or row[0] is None:
        return None
    after = row[0]
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - after).total_seconds() / 3600.0


def _apply(con: duckdb.DuckDBPyConnection, from_tz: str) -> dict:
    counts: dict = {}
    # 1) orders_submissions: submitted_at + updated_at
    con.execute(
        """
        UPDATE orders_submissions
           SET submitted_at = (submitted_at AT TIME ZONE ?) AT TIME ZONE 'UTC',
               updated_at   = (updated_at   AT TIME ZONE ?) AT TIME ZONE 'UTC'
        """,
        [from_tz, from_tz],
    )
    counts["orders_submissions"] = con.execute("SELECT COUNT(*) FROM orders_submissions").fetchone()[0]

    # 2) orders_events: "at"
    try:
        con.execute(
            'UPDATE orders_events SET "at" = ("at" AT TIME ZONE ?) AT TIME ZONE \'UTC\'',
            [from_tz],
        )
        counts["orders_events"] = con.execute("SELECT COUNT(*) FROM orders_events").fetchone()[0]
    except duckdb.Error:
        counts["orders_events"] = 0

    # 3) sentinel — pick an arbitrary submission to anchor the FK. If there are
    # no submissions (empty DB), we skip the sentinel row; the migration is a
    # no-op anyway.
    row = con.execute("SELECT submission_id FROM orders_submissions LIMIT 1").fetchone()
    if row is not None:
        con.execute(
            'INSERT INTO orders_events (event_id, submission_id, kind, detail, "at") VALUES (?, ?, ?, ?, ?)',
            [
                str(uuid.uuid4()),
                row[0],
                SENTINEL_KIND,
                json.dumps({"from_tz": from_tz, "script": Path(__file__).name}),
                datetime.now(timezone.utc),
            ],
        )
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--from-tz",
        required=True,
        help="IANA timezone name of the host when pre-upgrade rows were written (e.g. America/Los_Angeles). Required.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to orders.duckdb. Defaults to $XENON_ORDERS_DB_PATH or data/orders.duckdb.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually apply the rewrite. Without this flag, run in dry-run mode.",
    )
    parser.add_argument(
        "--preview-rows",
        type=int,
        default=5,
        help="Number of rows to show in dry-run preview (default 5).",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else _default_db_path()
    if not db_path.exists():
        print(f"ERROR: DB path does not exist: {db_path}", file=sys.stderr)
        return 2

    # Open without session TZ — we want raw wall-clock reads.
    con = duckdb.connect(str(db_path))
    try:
        if _already_applied(con):
            print(
                f"SKIP: migration already applied (sentinel event kind={SENTINEL_KIND} exists)",
                file=sys.stderr,
            )
            return 0

        total = con.execute("SELECT COUNT(*) FROM orders_submissions").fetchone()[0]
        print(f"orders_submissions rows: {total}")

        preview = _preview_rows(con, args.from_tz, n=args.preview_rows)
        print(f"\nPreview (first {len(preview)}):")
        for row in preview:
            print(
                f"  sub={row[0]}\n"
                f"    submitted_at  before={row[1]}  after={row[2]}\n"
                f"    updated_at    before={row[3]}  after={row[4]}"
            )

        if not args.apply:
            print("\nDry-run only. Re-run with --apply to write changes.")
            return 0

        counts = _apply(con, args.from_tz)
        print(f"\nApplied. Row counts: {counts}")

        age = _oldest_age_hours(con, args.from_tz)
        if age is not None:
            # Sanity assertion: after migration, oldest row should not be
            # unreasonably in the future or impossibly old.
            if age < -24 or age > 24 * 365 * 5:
                print(
                    f"WARNING: oldest row age = {age:.1f}h — outside sanity "
                    "bounds (±24h future, <5y old). Check --from-tz.",
                    file=sys.stderr,
                )
            else:
                print(f"Sanity check: oldest row age = {age:.1f}h (OK)")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
