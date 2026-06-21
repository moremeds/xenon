"""Create archive schema for option chain snapshots.

Revision ID: 2026_06_21_option_chain_archive
Revises: 2026_06_02_cf_open
Create Date: 2026-06-21

Creates the archive schema with five tables:
  snapshot_config, option_universe, snapshot_run, option_chain, underlying_ohlcv
Plus the v_staleness operator view.

TimescaleDB hypertables + compression are attempted but silently skipped when
the extension is not installed on this database.
"""

from __future__ import annotations

from alembic import op

revision: str = "2026_06_21_option_chain_archive"
down_revision: str | None = "2026_06_02_cf_open"
branch_labels = None
depends_on = None


def _try_timescaledb(sql: str) -> None:
    """Run a TimescaleDB-specific statement; silently skip if not available."""
    try:
        op.execute(sql)
    except Exception:
        pass


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS archive")

    # 1. snapshot_config — per-ticker poller config
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS archive.snapshot_config (
            ticker          TEXT PRIMARY KEY,
            cadence_seconds INT NOT NULL DEFAULT 600,
            enabled         BOOLEAN NOT NULL DEFAULT TRUE,
            contract_scope  TEXT NOT NULL DEFAULT 'full',
            notes           TEXT,
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        INSERT INTO archive.snapshot_config (ticker, cadence_seconds, enabled, contract_scope) VALUES
            ('SPX', 600, TRUE, 'full'),
            ('NDX', 600, TRUE, 'full'),
            ('RUT', 600, TRUE, 'full'),
            ('VIX', 600, TRUE, 'full')
        ON CONFLICT (ticker) DO NOTHING
        """
    )

    # 2. option_universe — daily contract identity cache
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS archive.option_universe (
            universe_date    DATE NOT NULL,
            con_id           BIGINT NOT NULL,
            ticker           TEXT NOT NULL,
            trading_class    TEXT NOT NULL,
            exchange         TEXT NOT NULL,
            multiplier       INTEGER NOT NULL,
            local_symbol     TEXT NOT NULL,
            expiry           DATE NOT NULL,
            strike           NUMERIC(14,4) NOT NULL,
            "right"          CHAR(1) NOT NULL,
            status           TEXT NOT NULL DEFAULT 'active',
            failure_count    INTEGER NOT NULL DEFAULT 0,
            disabled_until   TIMESTAMPTZ,
            last_error_code  INTEGER,
            universe_date_committed BOOLEAN NOT NULL DEFAULT FALSE,
            discovered_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (universe_date, con_id)
        )
        """
    )
    op.execute(
        'CREATE INDEX IF NOT EXISTS idx_option_universe_ticker ON archive.option_universe (ticker, universe_date, expiry, strike, "right")'
    )

    # 3. snapshot_run — per-cycle run ledger
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS archive.snapshot_run (
            id                   BIGSERIAL PRIMARY KEY,
            ticker               TEXT NOT NULL,
            started_at           TIMESTAMPTZ NOT NULL,
            finished_at          TIMESTAMPTZ,
            contracts_attempted  INT,
            contracts_persisted  INT,
            duration_ms          INT,
            ib_lines_peak        INT,
            status               TEXT NOT NULL,
            error                TEXT
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_snapshot_run_ticker ON archive.snapshot_run (ticker, started_at DESC)")

    # 4. option_chain — full chain snapshots
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS archive.option_chain (
            snapshot_ts      TIMESTAMPTZ NOT NULL,
            con_id           BIGINT NOT NULL,
            ticker           TEXT NOT NULL,
            trading_class    TEXT NOT NULL,
            expiry           DATE NOT NULL,
            strike           NUMERIC(14,4) NOT NULL,
            "right"          CHAR(1) NOT NULL,
            request_ts       TIMESTAMPTZ NOT NULL,
            quote_ts         TIMESTAMPTZ,
            greeks_ts        TIMESTAMPTZ,
            bid              NUMERIC(12,4),
            ask              NUMERIC(12,4),
            bid_size         INTEGER,
            ask_size         INTEGER,
            last             NUMERIC(12,4),
            last_size        INTEGER,
            volume           BIGINT,
            open_interest    BIGINT,
            iv               REAL,
            delta            REAL,
            gamma            REAL,
            vega             REAL,
            theta            REAL,
            underlying_px    NUMERIC(12,4),
            run_id           BIGINT NOT NULL REFERENCES archive.snapshot_run(id),
            PRIMARY KEY (snapshot_ts, con_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_option_chain_ticker ON archive.option_chain (ticker, trading_class, expiry, snapshot_ts DESC)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_option_chain_conid ON archive.option_chain (con_id, snapshot_ts DESC)")

    # TimescaleDB hypertable + compression (skipped silently on plain PG)
    _try_timescaledb(
        "SELECT create_hypertable('archive.option_chain', 'snapshot_ts', "
        "chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE)"
    )
    _try_timescaledb("ALTER TABLE archive.option_chain SET (timescaledb.compress = true)")
    _try_timescaledb("SELECT add_compression_policy('archive.option_chain', INTERVAL '7 days')")

    # 5. underlying_ohlcv — 1-min bars
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS archive.underlying_ohlcv (
            bar_ts      TIMESTAMPTZ NOT NULL,
            ticker      TEXT NOT NULL,
            bar_size    TEXT NOT NULL DEFAULT '1 min',
            open        NUMERIC(14,4),
            high        NUMERIC(14,4),
            low         NUMERIC(14,4),
            close       NUMERIC(14,4),
            volume      BIGINT,
            PRIMARY KEY (bar_ts, ticker, bar_size)
        )
        """
    )
    _try_timescaledb(
        "SELECT create_hypertable('archive.underlying_ohlcv', 'bar_ts', "
        "chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE)"
    )
    _try_timescaledb("ALTER TABLE archive.underlying_ohlcv SET (timescaledb.compress = true)")
    _try_timescaledb("SELECT add_compression_policy('archive.underlying_ohlcv', INTERVAL '30 days')")

    # 6. v_staleness — operator dashboard view
    op.execute(
        """
        CREATE OR REPLACE VIEW archive.v_staleness AS
        SELECT
            c.ticker,
            c.cadence_seconds,
            EXTRACT(EPOCH FROM (now() - last_run.finished_at))::INT AS seconds_since_last,
            last_run.contracts_persisted,
            last_run.status,
            CASE
                WHEN last_run.finished_at IS NULL THEN 'stale'
                WHEN last_run.status NOT IN ('ok', 'partial') THEN 'stale'
                WHEN now() - last_run.finished_at > make_interval(secs => c.cadence_seconds * 4) THEN 'stale'
                ELSE 'fresh'
            END AS health
        FROM archive.snapshot_config c
        LEFT JOIN LATERAL (
            SELECT * FROM archive.snapshot_run r
            WHERE r.ticker = c.ticker
            ORDER BY r.started_at DESC LIMIT 1
        ) last_run ON true
        WHERE c.enabled
        """
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS archive CASCADE")
