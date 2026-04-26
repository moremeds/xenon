# Production Database Strategy

This document captures the target database workflow for Xenon production,
research, backtesting, and historical market data. It is a design reference, not
an implemented migration plan yet.

## Goal

Use one centralized production database for live trading and shared research
state, with clear writer ownership.

The database should not become a shared scratchpad. Xenon execution, signal
research, backtesting, and market-data ingestion can all use it, but they need
schema boundaries, separate credentials, explicit migration rules, and clear
synchronization direction.

## Service Model

```text
Mac mini / production DB
  operational truth:
    orders, fills, positions snapshots, account snapshots, risk decisions,
    UW analysis snapshots, signal decisions used by execution

Xenon UI/execution
  writes:
    orders, order events, trade audit log, portfolio snapshots,
    execution decisions, UW enrichment tightly coupled to workflow
  reads:
    market data, signal candidates, backtest summaries, execution state

Signal and backtesting system
  writes:
    signal candidates, model versions, backtest runs, metrics, decisions
  reads:
    canonical market data, UW-derived features, portfolio/risk state as needed

Historical market data service
  writes:
    canonical OHLCV/options/reference metadata to Postgres
    large parquet/object artifacts to Cloudflare R2
    manifest rows that point to R2 objects
```

For the first production version, the Mac mini can keep existing `data/` and
DuckDB files as the operational store, but laptop development must not mount or
write those files. Development should read production through APIs, read-only
credentials, or local snapshots.

## Postgres Schema Split

Medium-term, move durable shared state to Postgres with schema boundaries:

| Schema       | Owner                   | Purpose                                                                   |
| ------------ | ----------------------- | ------------------------------------------------------------------------- |
| `ops`        | Xenon production        | orders, fills, positions, account snapshots, risk decisions, audit events |
| `uw`         | Xenon/UW ingestion      | UW raw/enriched snapshots, quota stats, analysis cache history            |
| `signals`    | signal system           | signal candidates, feature snapshots, model versions, decision records    |
| `backtests`  | backtest system         | run metadata, parameters, metrics, artifact references                    |
| `marketdata` | historical data service | instruments, bars metadata, option chains metadata, R2 object manifests   |

Use append-only event tables for trading and decision history. Mutable tables can
hold latest state, but every live-trading decision should be reconstructable
from events.

## Schema Changes And Migrations

Schema changes should be versioned in the repo and deployed with the tagged
release that needs them. Do not change production schema manually from a SQL
console except for emergency repair, and record any emergency repair afterward.

Recommended workflow:

```text
developer proposes schema change
  -> add migration file
  -> update application code
  -> run migration against local dev database
  -> run tests
  -> merge
  -> cut version tag
  -> release workflow verifies
  -> Mac mini deploy backs up DB
  -> Mac mini deploy runs migration before restarting services
  -> health checks confirm app and DB compatibility
```

Migration rules:

- Prefer backward-compatible migrations: add columns/tables first, deploy readers/writers later, remove old columns only after one or more releases.
- For live trading tables, preserve append-only history. Never rewrite order/fill/risk event history casually.
- Every migration should be idempotent or have a clear migration ledger entry.
- Large backfills should run as separate jobs, not inside the service restart path.
- Deploy should backup the production database before applying migrations.
- Application startup should fail loudly if the required schema version is missing.
- Rollback plans must distinguish code rollback from schema rollback. Code rollback should be common; destructive schema rollback should be rare.

For Postgres, use a migration tool such as Alembic for Python-owned schemas. If
other systems own their own schema, each system can own its migrations, but all
migrations should write to one shared migration ledger or have a documented
release order.

## Dev And Production Separation

Production and development need separate write paths.

| Environment | Purpose                                                  | Write access                                                 |
| ----------- | -------------------------------------------------------- | ------------------------------------------------------------ |
| `prod`      | live trading, production UI, production signal decisions | production services only                                     |
| `paper`     | broker-connected paper execution tests                   | paper services only                                          |
| `dev`       | local development and tests                              | local DB or disposable DB                                    |
| `research`  | backtests, feature experiments, offline analysis         | scoped writes to `backtests`/`signals`, no live `ops` writes |

Production rules:

- Production database is the source of truth for live trading state.
- Only production services write execution-critical tables.
- Non-execution systems write through scoped credentials and schema boundaries.
- Laptop dev never writes production execution tables directly.
- Migrations run before service restart and are versioned in the repo.
- Backups run before deploy and on a schedule.

Development rules:

- Local dev uses a local DB, fixtures, or disposable DB by default.
- For realistic reads, dev can use read-only credentials or API endpoints.
- Paper trading writes to a separate paper schema or separate paper database.
- Backtests write to `backtests` with run IDs and artifact references, not to live execution tables.
- Notebooks and local scripts never receive the production app credential.

Recommended credentials:

- `xenon_prod_app`: read/write only required `ops`, `uw`, and selected read schemas.
- `xenon_readonly`: read-only production access for dashboards or dev inspection.
- `marketdata_writer`: writes `marketdata` manifests and ingestion logs only.
- `signal_writer`: writes `signals` and selected `backtests` rows only.
- `backtest_writer`: writes `backtests` only.

## Synchronization Strategy

Avoid bidirectional synchronization for execution state. Live execution state
should flow outward from production, not merge back from dev.

Recommended sync rules:

- `prod -> dev`: allowed as sanitized snapshots, read-only API access, or explicit database dump/restore into a local dev DB.
- `dev -> prod`: code and migrations only, through tagged releases. No ad hoc data writes to live execution tables.
- `paper -> prod`: no direct sync. Paper validates behavior, but production starts from live broker/account state.
- `historical data service -> prod DB/R2`: allowed through scoped writer credentials and immutable manifests.
- `signals/backtests -> prod DB`: allowed only into `signals`/`backtests` schemas. Execution tables consume signals through an explicit decision workflow.

For local development against realistic data, prefer one of these:

1. Read-only production connection for inspection and UI reads.
2. Sanitized snapshot restore into local Postgres for offline work.
3. API-based reads from production services when the UI needs production-like behavior.

Do not point local dev at production with write credentials.

## R2 And Postgres Split

Use Postgres for indexed metadata, lineage, and queryable facts. Use R2 for
large immutable blobs such as parquet datasets, raw vendor payload archives, and
backtest artifacts.

Postgres should store:

- instrument/reference rows
- dataset manifests
- coverage windows
- checksums and row counts
- source/vendor/version metadata
- R2 object keys
- ingestion job status
- small derived facts needed for UI or signal queries

R2 should store:

- raw vendor payload archives
- historical OHLCV parquet
- option chain or option quote parquet
- large feature matrices
- backtest artifacts
- immutable dataset versions

Suggested R2 layout:

```text
r2://apex-data/
  raw/{source}/{asset_class}/{date}/...
  lake/marketdata/{asset_class}/{timeframe}/{symbol}/{version}/part-*.parquet
  lake/options/{symbol}/{date}/{version}/part-*.parquet
  artifacts/backtests/{run_id}/...
  manifests/{dataset}/{version}.json
```

Production consumers should not scan arbitrary R2 prefixes and infer truth from
whatever files exist. They should query Postgres manifests first, then load the
specific R2 objects listed by the selected manifest version.

## Current R2 Parquet State

Current historical parquet in R2 is useful staging material, but not yet
production-ready. Treat it as a staging dataset until it has:

- manifest rows in Postgres
- source and ingestion timestamp
- coverage windows per symbol/timeframe
- row counts and checksums
- completeness checks
- rebuild instructions
- clear versioning so production consumers know exactly which dataset they are using

The minimum productionization step is a manifest table, not rewriting all
parquet immediately. Once a manifest exists, consumers can ask:

```text
Which dataset version is approved for SPY 1d bars?
What R2 objects belong to that version?
What date range is covered?
What source produced it?
What checksum/row count should I expect?
```

## Practical First Step

Do not attempt a full data-platform migration before productionizing the Mac
mini. The first safe sequence is:

1. Keep Mac mini production state local and single-writer.
2. Add tagged-release deploy automation.
3. Add backups for existing `data/` and DuckDB state.
4. Introduce Postgres for new durable shared tables.
5. Move one domain at a time from files/DuckDB into Postgres.
6. Put existing R2 parquet behind explicit manifests before relying on it for production signals.

## Open Questions

- Should Postgres run on the Mac mini or on a dedicated database host?
- Should paper trading use a separate database or a separate schema?
- Which domain moves first: orders, UW cache/history, signals, or marketdata manifests?
- Which system owns Alembic migrations for shared schemas?
- What is the retention policy for raw vendor payloads and large backtest artifacts?

## Broker Account Scope

Every execution and portfolio row carries three scope columns so paper and
live data never blend in a shared Postgres, and every row is auditable to a
specific broker account.

| Column           | Values                                   | Meaning                           |
| ---------------- | ---------------------------------------- | --------------------------------- |
| `broker`         | `IB`, `FUTU`                             | Originating broker                |
| `account_env`    | `paper`, `live`, `sim`, `legacy_unknown` | Runtime environment at write time |
| `broker_account` | e.g. `DU1234567`, `U9876543`             | External account ID               |

**Rules:**

- Execution tables (`order_submissions`, `trades`, `wizard_sessions`,
  `wizard_combo_attempts`) enforce `broker = 'IB'` via CHECK constraint.
  Futu execution is not permitted.
- Portfolio tables (`positions`, `account_snapshots`, `nav_history`) allow
  both `IB` and `FUTU`.
- The order idempotency key is
  `(broker, account_env, broker_account, user_id, client_attempt_id)` —
  same `client_attempt_id` in paper and live creates two distinct rows.
- The `nav_history` PK is
  `(broker, account_env, broker_account, date)`.
- `legacy_unknown` rows are pre-scope historical data. They are excluded
  from active execution workflows when scope filters are active (rehydrate,
  monitor, working-orders queries). Operators may manually classify legacy
  rows later; automated backfill is not planned.
- Futu is read-only — no Futu rows in execution tables until a future
  migration explicitly enables it.

**Resolution:**

- **FastAPI routes:** `AccountScope.resolve_from_app_state(app.state)`
  reads `app.state.{trading_mode, account}` populated by the lifespan guard.
  Helper: `xenon.api.guards.get_account_scope` (FastAPI dependency).
- **Sync subprocesses (`ib_sync`, `ib_execute`, monitor daemons):**
  `resolve_from_env()` reads `XENON_TRADING_MODE` + `XENON_BROKER_ACCOUNT`.
  The IB sync path should set `XENON_BROKER_ACCOUNT` from
  `managedAccounts()[0]` at connect time.

**Environment variables:**

- `XENON_TRADING_MODE` — `paper` or `live` (drives `account_env`)
- `XENON_BROKER_ACCOUNT` — the actual IB account ID
- `XENON_BROKER` — defaults to `IB`; only override for non-IB writers

**Implementation:** `src/xenon/execution/account_scope.py`
