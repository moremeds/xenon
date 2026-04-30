from __future__ import annotations

from decimal import Decimal

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from xenon.db.schema import cri_series, scan_results


async def save_scan(conn: AsyncConnection, *, scan_type: str, payload: dict) -> None:
    await conn.execute(insert(scan_results).values(scan_type=scan_type, payload=payload))


async def get_latest_scan(conn: AsyncConnection, *, scan_type: str) -> dict | None:
    stmt = (
        select(scan_results)
        .where(scan_results.c.scan_type == scan_type)
        .order_by(scan_results.c.scanned_at.desc(), scan_results.c.id.desc())
        .limit(1)
    )
    row = (await conn.execute(stmt)).first()
    return dict(row._mapping) if row else None


async def save_cri_datapoint(
    conn: AsyncConnection,
    *,
    cri_level: Decimal,
    alert: bool = False,
    payload: dict | None = None,
) -> None:
    await conn.execute(insert(cri_series).values(cri_level=cri_level, alert=alert, payload=payload))


async def get_cri_series(conn: AsyncConnection, *, limit: int = 100) -> list[dict]:
    stmt = select(cri_series).order_by(cri_series.c.recorded_at).limit(limit)
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]


def save_gex_snapshot(conn, *, payload: dict) -> int:
    """Insert a row into gex_snapshots. Conn is a sync SA connection."""
    from datetime import date as _date

    from sqlalchemy import insert as _insert

    from xenon.db.schema import gex_snapshots

    data_date = payload.get("data_date")
    if isinstance(data_date, str):
        try:
            data_date = _date.fromisoformat(data_date)
        except ValueError:
            data_date = None
    return conn.execute(
        _insert(gex_snapshots)
        .values(
            ticker=payload.get("ticker"),
            data_date=data_date,
            payload=payload,
        )
        .returning(gex_snapshots.c.id)
    ).scalar()


def save_vcg_scan(
    conn,
    *,
    payload: dict,
    market_open: bool | None = None,
    credit_proxy: str | None = None,
) -> int:
    """Insert a row into vcg_series. Conn is a sync SA connection."""
    from sqlalchemy import insert as _insert

    from xenon.db.schema import vcg_series

    return conn.execute(
        _insert(vcg_series)
        .values(
            market_open=market_open,
            credit_proxy=credit_proxy,
            payload=payload,
        )
        .returning(vcg_series.c.id)
    ).scalar()


def save_cri_scan(conn, *, payload: dict) -> int:
    """Insert a row into cri_series from a CRI scanner JSON payload.

    Mirrors save_vcg_scan: sync conn, plain INSERT, returns the new id.
    The cri_level NOT NULL column is derived from payload['cri']['score'].

    Rejects malformed payloads (missing/None/NaN/non-finite score) with
    ValueError. The regime gate's binding tier picks the worst of
    (vcg_tier, cri_tier); a silent zero-fill would surface as cri_tier=
    NORMAL (the safest tier) and bias the gate toward "permissive". A bad
    scan must not pretend the regime is normal — the caller can retry or
    skip.

    No ON CONFLICT clause: the regime_state view (Phase 1) picks the
    latest row, so multiple rows per calendar day from the 30-min
    scheduled cadence + manual /regime/scan refreshes are intentional.
    """
    import math

    from sqlalchemy import insert as _insert

    from xenon.db.schema import cri_series

    cri = payload.get("cri") or {}
    score = cri.get("score")
    if score is None:
        raise ValueError("save_cri_scan: payload['cri']['score'] is required")
    try:
        score_f = float(score)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"save_cri_scan: payload['cri']['score'] is not numeric ({score!r})") from exc
    if not math.isfinite(score_f):
        raise ValueError(f"save_cri_scan: payload['cri']['score'] is non-finite ({score_f!r})")
    cri_level = Decimal(str(score_f))

    return conn.execute(
        _insert(cri_series).values(cri_level=cri_level, alert=False, payload=payload).returning(cri_series.c.id)
    ).scalar()


async def get_latest_vcg(conn: AsyncConnection) -> dict | None:
    """Return the most recent vcg_series payload, or None when no scans exist.

    The payload is the full UI shape (signal, history, market_open,
    credit_proxy, scan_time) that vcg_scan.py emits — vcg_series.payload
    is structurally complete on every row, so callers don't need to
    reconstruct the 20d history from separate rows.
    """
    from xenon.db.schema import vcg_series

    stmt = select(vcg_series.c.payload).order_by(vcg_series.c.scanned_at.desc(), vcg_series.c.id.desc()).limit(1)
    row = (await conn.execute(stmt)).first()
    if row is None:
        return None
    payload = row.payload
    return dict(payload) if payload else None


async def get_latest_gex(conn: AsyncConnection, *, ticker: str = "SPX") -> dict | None:
    """Return the most recent gex_snapshots payload for a ticker."""
    from xenon.db.schema import gex_snapshots

    stmt = (
        select(gex_snapshots.c.payload)
        .where(gex_snapshots.c.ticker == ticker.upper())
        .order_by(gex_snapshots.c.scanned_at.desc(), gex_snapshots.c.id.desc())
        .limit(1)
    )
    row = (await conn.execute(stmt)).first()
    if row is None:
        return None
    payload = row.payload
    return dict(payload) if payload else None
