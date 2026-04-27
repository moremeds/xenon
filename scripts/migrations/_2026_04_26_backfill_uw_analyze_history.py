"""Backfill xenon.uw_analyze_snapshots from data/uw_analyze_history/<TICKER>/*.json.

Each on-disk JSON file contains a `current` dict (ticker, report, display, derived,
dark_pool_summary, options_flow_summary, flow_alerts) plus `materialized_changes`
and `archived_at`. Idempotent on (ticker, archived_at) — second run UPDATEs
matching rows rather than inserting duplicates.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from xenon.db.schema import uw_analyze_snapshots


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _row_values(payload: dict) -> dict:
    current = payload.get("current") or {}
    report = current.get("report") or {}
    scores = report.get("scores") if isinstance(report, dict) else None
    score_val = None
    if isinstance(scores, dict):
        score_val = scores.get("flow") or scores.get("composite") or scores.get("total")
    return dict(
        ticker=current.get("ticker"),
        report=report or None,
        display=current.get("display"),
        derived=current.get("derived"),
        dark_pool_summary=current.get("dark_pool_summary"),
        options_flow_summary=current.get("options_flow_summary"),
        flow_alerts=current.get("flow_alerts"),
        materialized_changes=payload.get("materialized_changes"),
        report_fetched_at=_parse_iso(report.get("fetched_at") if isinstance(report, dict) else None),
        archived_at=_parse_iso(payload.get("archived_at")),
        portfolio_score=Decimal(str(score_val)) if score_val is not None else None,
    )


def run(*, history_root: Path | str, db_url: str) -> int:
    """Walk history_root for *.json files; insert/update one row per file.

    Returns count of files processed.
    """
    root = Path(history_root)
    files = sorted(root.rglob("*.json"))
    engine = create_engine(db_url, pool_pre_ping=True)
    processed = 0
    try:
        with engine.begin() as conn:
            for f in files:
                try:
                    payload = json.loads(f.read_text())
                except Exception as exc:  # noqa: BLE001
                    print(f"  skip {f}: parse error {exc}")
                    continue
                values = _row_values(payload)
                if not values["ticker"] or not values["archived_at"]:
                    print(f"  skip {f}: missing ticker or archived_at")
                    continue
                existing = conn.execute(
                    select(uw_analyze_snapshots.c.id)
                    .where(uw_analyze_snapshots.c.ticker == values["ticker"])
                    .where(uw_analyze_snapshots.c.archived_at == values["archived_at"])
                ).scalar()
                if existing:
                    conn.execute(
                        update(uw_analyze_snapshots)
                        .where(uw_analyze_snapshots.c.id == existing)
                        .values(**values, snapshot_at=values["archived_at"])
                    )
                else:
                    conn.execute(pg_insert(uw_analyze_snapshots).values(**values, snapshot_at=values["archived_at"]))
                processed += 1
    finally:
        engine.dispose()
    return processed


if __name__ == "__main__":
    import argparse
    import os

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="data/uw_analyze_history")
    parser.add_argument("--db-url", default=os.environ.get("DATABASE_URL"))
    args = parser.parse_args()
    if not args.db_url:
        raise SystemExit("DATABASE_URL not set; pass --db-url")
    sync_url = args.db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    n = run(history_root=args.root, db_url=sync_url)
    print(f"backfilled {n} uw_analyze_snapshots rows")
