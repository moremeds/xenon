"""Backfill xenon.vcg_series from data/vcg.json.

Current snapshot becomes one row with full signal+attribution payload.
Each `history[i]` entry becomes one row with a synthesized signal-shaped
payload built from the history fields.

Idempotent: vcg_series has UNIQUE(scanned_at) and we use ON CONFLICT DO NOTHING.
"""

from __future__ import annotations

import json
from datetime import datetime, time, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import insert as pg_insert

from xenon.db.schema import vcg_series


def _history_row_payload(item: dict) -> dict:
    """Build a payload that vcg_series generated columns can extract from."""
    return {
        "signal": {
            "vcg": item.get("vcg"),
            "vcg_adj": item.get("vcg_adj"),
            "residual": item.get("residual"),
            "beta1_vvix": item.get("beta1"),
            "beta2_vix": item.get("beta2"),
            "vix": item.get("vix"),
            "vvix": item.get("vvix"),
            "credit_price": item.get("credit"),
            "ro": item.get("ro"),
            "edr": item.get("edr"),
            "tier": item.get("tier"),
            "bounce": item.get("bounce"),
        },
        "history_source": True,
    }


def _date_to_ts(d: str) -> datetime:
    """Trading-day date ('YYYY-MM-DD') → 20:00 UTC (~16:00 ET equity close)."""
    return datetime.combine(datetime.fromisoformat(d).date(), time(20, 0), tzinfo=timezone.utc)


def _parse_iso_utc(s: str | None) -> datetime | None:
    """Parse an ISO timestamp and force UTC tz.

    System-wide policy: every datetime persisted is timezone-aware UTC. The
    data/vcg.json `scan_time` field is naive, so we attach UTC explicitly
    rather than letting Postgres infer it from the session timezone.
    """
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def run(*, json_path: Path | str, db_url: str) -> int:
    data = json.loads(Path(json_path).read_text())
    history = data.get("history", []) or []
    market_open = data.get("market_open")
    credit_proxy = data.get("credit_proxy")
    rows_to_insert: list[dict] = []
    for item in history:
        d = item.get("date")
        if not d:
            continue
        rows_to_insert.append(
            dict(
                scanned_at=_date_to_ts(d),
                market_open=False,  # history items are EOD
                credit_proxy=credit_proxy,
                payload=_history_row_payload(item),
            )
        )
    current_ts = _parse_iso_utc(data.get("scan_time")) or datetime.now(tz=timezone.utc)
    rows_to_insert.append(
        dict(
            scanned_at=current_ts,
            market_open=market_open,
            credit_proxy=credit_proxy,
            payload=data,
        )
    )

    engine = create_engine(db_url, pool_pre_ping=True)
    inserted = 0
    try:
        with engine.begin() as conn:
            for row in rows_to_insert:
                stmt = (
                    pg_insert(vcg_series)
                    .values(**row)
                    .on_conflict_do_nothing(index_elements=[vcg_series.c.scanned_at])
                    .returning(vcg_series.c.id)
                )
                if conn.execute(stmt).first() is not None:
                    inserted += 1
    finally:
        engine.dispose()
    return inserted


if __name__ == "__main__":
    import argparse
    import os

    parser = argparse.ArgumentParser()
    parser.add_argument("--json", default="data/vcg.json")
    parser.add_argument("--db-url", default=os.environ.get("DATABASE_URL"))
    args = parser.parse_args()
    if not args.db_url:
        raise SystemExit("DATABASE_URL not set; pass --db-url")
    sync_url = args.db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    n = run(json_path=args.json, db_url=sync_url)
    print(f"backfilled {n} vcg rows")
