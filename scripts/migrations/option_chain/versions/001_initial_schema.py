"""Initial schema for option_chain archive.

Revision ID: 001
Revises:
Create Date: 2026-06-02

Creates the five archive tables (snapshot_config, option_universe, snapshot_run,
option_chain, underlying_ohlcv) plus the v_staleness operator view and READ
grants for xenon_prod / xenon_dev / argon_app.

C-7 fix: ALTER TABLE ... SET (timescaledb.compress = true) before
add_compression_policy — required since TimescaleDB 2.18+. Verified
empirically against 2.24 on 2026-06-02; bare add_compression_policy
errors with: "columnstore not enabled on hypertable".
"""

from __future__ import annotations

from alembic import op

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS archive AUTHORIZATION option_chain_writer")

    # 1. snapshot_config — per-ticker poller config (seeded with 4 indexes)
    op.execute(
        """
        CREATE TABLE archive.snapshot_config (
            ticker          TEXT PRIMARY KEY,
            cadence_seconds INT NOT NULL DEFAULT 1800,
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
        """
    )

    # 2. option_universe — daily contract identity cache (Pass-2 C-2 + C-9, Pass-3 A-1)
    op.execute(
        """
        CREATE TABLE archive.option_universe (
            universe_date    DATE NOT NULL,
            con_id           BIGINT NOT NULL,
            ticker           TEXT NOT NULL,
            trading_class    TEXT NOT NULL,
            exchange         TEXT NOT NULL,
            multiplier       INTEGER NOT NULL,
            local_symbol     TEXT NOT NULL,
            expiry           DATE NOT NULL,
            strike           NUMERIC(14,4) NOT NULL,
            "right"          CHAR(1) NOT NULL,  -- quoted: RIGHT is reserved in SQL
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
    op.execute('CREATE INDEX ON archive.option_universe (ticker, universe_date, expiry, strike, "right")')
    op.execute("CREATE INDEX ON archive.option_universe (status, disabled_until) WHERE status <> 'active'")
    op.execute(
        "CREATE INDEX ON archive.option_universe (universe_date_committed, universe_date DESC) "
        "WHERE universe_date_committed"
    )

    # 3. snapshot_run — per-cycle run ledger
    op.execute(
        """
        CREATE TABLE archive.snapshot_run (
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
    op.execute("CREATE INDEX ON archive.snapshot_run (ticker, started_at DESC)")

    # 4. option_chain — full chain snapshots (hypertable, per C-2 + C-12 + C-13)
    op.execute(
        """
        CREATE TABLE archive.option_chain (
            snapshot_ts      TIMESTAMPTZ NOT NULL,
            con_id           BIGINT NOT NULL,
            ticker           TEXT NOT NULL,
            trading_class    TEXT NOT NULL,
            expiry           DATE NOT NULL,
            strike           NUMERIC(14,4) NOT NULL,
            "right"          CHAR(1) NOT NULL,  -- quoted: RIGHT is reserved in SQL
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
        "SELECT create_hypertable('archive.option_chain', 'snapshot_ts', chunk_time_interval => INTERVAL '1 day')"
    )
    # C-7: enable columnstore before adding the compression policy.
    op.execute("ALTER TABLE archive.option_chain SET (timescaledb.compress = true)")
    op.execute("SELECT add_compression_policy('archive.option_chain', INTERVAL '7 days')")
    op.execute("CREATE INDEX ON archive.option_chain (ticker, trading_class, expiry, snapshot_ts DESC)")
    op.execute("CREATE INDEX ON archive.option_chain (con_id, snapshot_ts DESC)")

    # 5. underlying_ohlcv — 1-min bars (hypertable)
    op.execute(
        """
        CREATE TABLE archive.underlying_ohlcv (
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
    op.execute(
        "SELECT create_hypertable('archive.underlying_ohlcv', 'bar_ts', chunk_time_interval => INTERVAL '7 days')"
    )
    op.execute("ALTER TABLE archive.underlying_ohlcv SET (timescaledb.compress = true)")  # C-7
    op.execute("SELECT add_compression_policy('archive.underlying_ohlcv', INTERVAL '30 days')")

    # 6. v_staleness — operator dashboard view
    op.execute(
        """
        CREATE VIEW archive.v_staleness AS
        SELECT
            c.ticker,
            c.cadence_seconds,
            EXTRACT(EPOCH FROM (now() - last_run.finished_at))::INT AS seconds_since_last,
            last_run.contracts_persisted,
            last_run.status,
            CASE WHEN now() - last_run.finished_at > make_interval(secs => c.cadence_seconds * 4)
                 THEN 'stale' ELSE 'fresh' END AS health
        FROM archive.snapshot_config c
        LEFT JOIN LATERAL (
            SELECT * FROM archive.snapshot_run r
            WHERE r.ticker = c.ticker AND r.status IN ('ok','partial')
            ORDER BY r.finished_at DESC LIMIT 1
        ) last_run ON true
        WHERE c.enabled
        """
    )

    # 7. Read grants — for the production cluster. Skipped silently if the roles
    # don't exist (local-dev DB won't have xenon_prod/xenon_dev/argon_app).
    for role in ("xenon_prod", "xenon_dev", "argon_app"):
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    GRANT CONNECT ON DATABASE option_chain TO {role};
                    GRANT USAGE ON SCHEMA archive TO {role};
                    GRANT SELECT ON ALL TABLES IN SCHEMA archive TO {role};
                    ALTER DEFAULT PRIVILEGES IN SCHEMA archive
                        GRANT SELECT ON TABLES TO {role};
                END IF;
            END
            $$;
            """
        )


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS archive CASCADE")
