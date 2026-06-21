# Option Chain Snapshotter — Operator Runbook

Bootstrap and operations guide for the `option_chain` archive database and snapshotter daemon.

## Prerequisites

- macmini Postgres reachable (`psql -h 100.66.147.98 -U postgres`)
- TimescaleDB extension installed on macmini (`timescaledb-2` package)
- IB Gateway running and reachable on macmini (`100.66.147.98:4001`)

---

## One-time: Provision the option_chain database

Run once on macmini as a Postgres superuser:

```sql
-- 1. Role
CREATE ROLE option_chain_writer WITH LOGIN PASSWORD '<strong-password>';

-- 2. Database
CREATE DATABASE option_chain OWNER option_chain_writer;

-- 3. TimescaleDB extension (superuser required)
\c option_chain
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- 4. Grant superuser to the writer role for TimescaleDB setup (narrow window)
--    TimescaleDB's create_hypertable and add_compression_policy require it.
ALTER ROLE option_chain_writer SUPERUSER;
-- Run the migration (step below), then revoke:
-- ALTER ROLE option_chain_writer NOSUPERUSER;
```

Set `OPTION_CHAIN_DATABASE_URL` in the snapshotter's env:

```
OPTION_CHAIN_DATABASE_URL=postgresql+psycopg://option_chain_writer:<pw>@100.66.147.98:5432/option_chain
```

---

## Run the migration

```bash
cd /Users/chenxi/projects/xenon
OPTION_CHAIN_DATABASE_URL=postgresql+psycopg://option_chain_writer:<pw>@100.66.147.98:5432/option_chain \
  uv run alembic -c scripts/migrations/option_chain/alembic.ini upgrade head
```

After success, revoke the temporary superuser grant:

```sql
\c option_chain
ALTER ROLE option_chain_writer NOSUPERUSER;
-- Grant back only what it needs:
GRANT ALL ON SCHEMA archive TO option_chain_writer;
GRANT ALL ON ALL TABLES IN SCHEMA archive TO option_chain_writer;
GRANT ALL ON ALL SEQUENCES IN SCHEMA archive TO option_chain_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA archive GRANT ALL ON TABLES TO option_chain_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA archive GRANT ALL ON SEQUENCES TO option_chain_writer;
```

---

## Verify provisioning

```bash
psql "$OPTION_CHAIN_DATABASE_URL" -c "\dt archive.*"
# Should show: snapshot_config, option_universe, snapshot_run, option_chain, underlying_ohlcv

psql "$OPTION_CHAIN_DATABASE_URL" -c "SELECT * FROM archive.v_staleness;"
# Should show 4 rows (SPX/NDX/RUT/VIX) with health='stale' (no runs yet)

psql "$OPTION_CHAIN_DATABASE_URL" -c "SELECT * FROM archive.snapshot_config;"
# Should show 4 rows, cadence_seconds=600, enabled=TRUE
```

---

## Operator dashboard queries

```sql
-- Staleness overview
SELECT ticker, health, status, seconds_since_last, contracts_persisted
FROM archive.v_staleness
ORDER BY ticker;

-- Last 10 runs
SELECT ticker, started_at, duration_ms, contracts_attempted, contracts_persisted, status, error
FROM archive.snapshot_run
ORDER BY started_at DESC LIMIT 10;

-- Universe size by ticker (today)
SELECT ticker, count(*) AS contracts
FROM archive.option_universe
WHERE universe_date = current_date
GROUP BY ticker ORDER BY ticker;
```

---

## Reset a disabled contract

```sql
UPDATE archive.option_universe
SET status='active', failure_count=0, disabled_until=NULL, last_error_code=NULL
WHERE con_id = <conId> AND universe_date = current_date;
```

---

## clientId allocation

The snapshotter uses IB clientIds **901** (pool A) and **902** (pool B), registered in
`src/xenon/clients/ib_client.py`. These are in the 900+ dedicated daemon range.
Note: clientId 900 is reserved for radon's relay — do not use it.
