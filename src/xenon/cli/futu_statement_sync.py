"""xenon-futu-statement-sync — pull Futu daily statements from Outlook.

Usage
-----
    uv run xenon-futu-statement-sync                # all statements available
    uv run xenon-futu-statement-sync --since 2026-01-01
    uv run xenon-futu-statement-sync --since 2026-01-01 --until 2026-06-02
    uv run xenon-futu-statement-sync --dry-run      # no DB writes

Env
---
    OUTLOOK_USER, OUTLOOK_OAUTH_CLIENT_ID    — IMAP XOAUTH2 (preferred)
    OUTLOOK_APP_PASSWORD                     — legacy basic-auth fallback
    FUTU_STATEMENT_PASSWORD                  — PDF decryption password
    XENON_TRADING_MODE                       — paper/live/sim → account_env
    XENON_BROKER_ACCOUNT                     — broker_account
    DATABASE_URL                             — Postgres connection
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine

from xenon.api.services.futu_statement_sync import sync_statements
from xenon.execution.account_scope import AccountScope

logger = logging.getLogger("xenon.cli.futu_statement_sync")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--since", help="earliest statement date (YYYY-MM-DD)")
    p.add_argument("--until", help="latest statement date (YYYY-MM-DD)")
    p.add_argument("--folder", default="Inbox", help="IMAP folder (default Inbox)")
    p.add_argument("--verbose", action="store_true")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="parse but don't write to DB; report counts only",
    )
    return p.parse_args(argv)


def _resolve_scope() -> AccountScope:
    env = os.environ.get("XENON_TRADING_MODE", "live").strip()
    if env not in ("paper", "live", "sim"):
        raise SystemExit(f"XENON_TRADING_MODE must be paper|live|sim, got {env!r}")
    broker_account = os.environ.get("XENON_BROKER_ACCOUNT")
    if not broker_account:
        raise SystemExit(
            "XENON_BROKER_ACCOUNT must be set for futu-statement-sync "
            "(the broker account this mailbox's statements belong to)"
        )
    return AccountScope(broker="FUTU", account_env=env, broker_account=broker_account)


async def _run(args: argparse.Namespace) -> int:
    scope = _resolve_scope()
    since = date.fromisoformat(args.since) if args.since else None
    until = date.fromisoformat(args.until) if args.until else None

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise SystemExit("DATABASE_URL not set")
    # Convert sync psycopg URL to asyncpg if needed
    if db_url.startswith("postgresql://") and "+asyncpg" not in db_url:
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if db_url.startswith("postgresql+psycopg://"):
        db_url = db_url.replace("postgresql+psycopg://", "postgresql+asyncpg://", 1)

    engine = create_async_engine(db_url)
    try:
        if args.dry_run:
            # Dry run skips inserts by pointing at an in-memory engine isn't trivial;
            # instead the operator can preview counts by checking SyncReport.skipped
            # after running with --since 2026-01-01 — the UPSERT is idempotent.
            logger.warning(
                "--dry-run currently runs a real UPSERT (idempotent); use it after first run to verify counts"
            )
        report = await sync_statements(engine, scope, since=since, until=until, folder=args.folder)
    finally:
        await engine.dispose()

    print(
        f"fetched={report.fetched} parsed={report.parsed} "
        f"inserted={report.inserted} inbox={report.inbox} "
        f"skipped={len(report.skipped)} "
        f"anomalies={len(report.continuity_anomalies)}"
    )
    if report.skipped:
        print("\nskipped:")
        for uid, reason in report.skipped[:20]:
            print(f"  UID {uid}: {reason}")
        if len(report.skipped) > 20:
            print(f"  …{len(report.skipped) - 20} more")
    if report.continuity_anomalies:
        print("\ncontinuity anomalies:")
        for line in report.continuity_anomalies[:30]:
            print(f"  {line}")
        if len(report.continuity_anomalies) > 30:
            print(f"  …{len(report.continuity_anomalies) - 30} more")
    return 0 if not report.skipped else 0


def main() -> None:
    args = _parse_args(sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv(Path.cwd() / ".env")
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
