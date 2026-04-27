"""Backfill xenon.uw_api_stats from data/uw_api_stats_history.json.

JSON shape:
  {"updated_at": ..., "schema_version": 1, "buckets":
    {"<iso-ts>": {"requests_2xx": N, "requests_4xx": N, "requests_5xx": N,
                  "cached": N, "sum_latency_ms": F, "latency_count": N}, ...}}

Idempotent: each bucket upserts on bucket_hour PK.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import insert as pg_insert

from xenon.db.schema import uw_api_stats


def _parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def run(*, json_path: Path | str, db_url: str) -> int:
    """Returns the number of buckets processed."""
    data = json.loads(Path(json_path).read_text())
    buckets = data.get("buckets", {})
    engine = create_engine(db_url, pool_pre_ping=True)
    try:
        with engine.begin() as conn:
            for ts_str, b in buckets.items():
                bucket_hour = _parse_iso(ts_str)
                s2 = int(b.get("requests_2xx", 0))
                s4 = int(b.get("requests_4xx", 0))
                s5 = int(b.get("requests_5xx", 0))
                values = dict(
                    bucket_hour=bucket_hour,
                    requests=s2 + s4 + s5,
                    cache_hits=int(b.get("cached", 0)),
                    latency_sum=Decimal(str(b.get("sum_latency_ms", 0))),
                    latency_count=int(b.get("latency_count", 0)),
                    status_2xx=s2,
                    status_4xx=s4,
                    status_5xx=s5,
                )
                stmt = pg_insert(uw_api_stats).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[uw_api_stats.c.bucket_hour],
                    set_={k: stmt.excluded[k] for k in values if k != "bucket_hour"},
                )
                conn.execute(stmt)
    finally:
        engine.dispose()
    return len(buckets)


if __name__ == "__main__":
    import argparse
    import os

    parser = argparse.ArgumentParser()
    parser.add_argument("--json", default="data/uw_api_stats_history.json")
    parser.add_argument("--db-url", default=os.environ.get("DATABASE_URL"))
    args = parser.parse_args()
    if not args.db_url:
        raise SystemExit("DATABASE_URL not set; pass --db-url")
    sync_url = args.db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    n = run(json_path=args.json, db_url=sync_url)
    print(f"backfilled {n} buckets")
