"""Postgres writer for the option chain archive.

Uses psycopg3 (sync, autocommit) — same pattern as the spike script.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)


class PGPersister:
    """Thin wrapper around a psycopg connection for archive writes."""

    def __init__(self, dsn: str) -> None:
        # Strip SQLAlchemy dialect prefix if present.
        self._dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
        self._conn: psycopg.Connection | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def connect(self) -> None:
        self._conn = psycopg.connect(self._dsn, autocommit=True)
        log.info("PGPersister connected")

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    @property
    def conn(self) -> psycopg.Connection:
        if self._conn is None:
            raise RuntimeError("PGPersister not connected — call connect() first")
        return self._conn

    # ------------------------------------------------------------------ #
    # Run ledger
    # ------------------------------------------------------------------ #

    def insert_run(self, ticker: str) -> tuple[int, datetime]:
        """Open a new snapshot_run row. Returns (run_id, snapshot_ts)."""
        snapshot_ts = datetime.now(tz=timezone.utc)
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO archive.snapshot_run (ticker, started_at, status) VALUES (%s, %s, 'running') RETURNING id",
                (ticker, snapshot_ts),
            )
            run_id: int = cur.fetchone()[0]
        return run_id, snapshot_ts

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        contracts_attempted: int,
        contracts_persisted: int,
        duration_ms: int,
        ib_lines_peak: int = 0,
        error: str | None = None,
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE archive.snapshot_run
                SET finished_at         = now(),
                    status              = %s,
                    contracts_attempted = %s,
                    contracts_persisted = %s,
                    duration_ms         = %s,
                    ib_lines_peak       = %s,
                    error               = %s
                WHERE id = %s
                """,
                (
                    status,
                    contracts_attempted,
                    contracts_persisted,
                    duration_ms,
                    ib_lines_peak,
                    error,
                    run_id,
                ),
            )

    # ------------------------------------------------------------------ #
    # Chain rows
    # ------------------------------------------------------------------ #

    def persist_rows(
        self,
        rows: list[dict],
        *,
        run_id: int,
        snapshot_ts: datetime,
    ) -> int:
        """Batch-insert option_chain rows. Returns count of rows inserted."""
        if not rows:
            return 0

        def _safe(v: object) -> object:
            if v is None:
                return None
            if isinstance(v, float) and v != v:  # NaN
                return None
            return v

        records = []
        for r in rows:
            mg = r.get("model_greeks")
            records.append(
                (
                    snapshot_ts,
                    r["con_id"],
                    r["ticker"],
                    r["trading_class"],
                    r["expiry"],
                    r["strike"],
                    r["right"],
                    r["request_ts"],
                    _safe(r.get("quote_ts")),
                    _safe(r.get("greeks_ts")),
                    _safe(r.get("bid")),
                    _safe(r.get("ask")),
                    _safe(r.get("bid_size")),
                    _safe(r.get("ask_size")),
                    _safe(r.get("last")),
                    _safe(r.get("last_size")),
                    _safe(r.get("volume")),
                    _safe(r.get("open_interest")),
                    _safe(mg.impliedVol if mg else None),
                    _safe(mg.delta if mg else None),
                    _safe(mg.gamma if mg else None),
                    _safe(mg.vega if mg else None),
                    _safe(mg.theta if mg else None),
                    _safe(mg.undPrice if mg else r.get("underlying_px")),
                    run_id,
                )
            )

        with self.conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO archive.option_chain (
                    snapshot_ts, con_id, ticker, trading_class, expiry,
                    strike, "right",
                    request_ts, quote_ts, greeks_ts,
                    bid, ask, bid_size, ask_size, last, last_size,
                    volume, open_interest,
                    iv, delta, gamma, vega, theta, underlying_px,
                    run_id
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s
                )
                ON CONFLICT (snapshot_ts, con_id) DO NOTHING
                """,
                records,
            )
            inserted = cur.rowcount

        log.debug("persisted %d/%d rows for run %d", inserted, len(rows), run_id)
        return inserted

    # ------------------------------------------------------------------ #
    # Config read
    # ------------------------------------------------------------------ #

    def load_cadence(self, ticker: str, default: int) -> int:
        """Read cadence_seconds from snapshot_config for this ticker."""
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT cadence_seconds FROM archive.snapshot_config WHERE ticker = %s AND enabled",
                (ticker,),
            )
            row = cur.fetchone()
        return row["cadence_seconds"] if row else default
