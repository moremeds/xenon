# Postgres Migration Design Spec

**Date:** 2026-04-26
**Status:** Approved
**Scope:** Migrate Xenon's JSON/DuckDB persistence to PostgreSQL; establish shared data backbone for multi-service trading platform

---

## 1. Motivation

- **Query power** — join/aggregate across data currently siloed in 21+ JSON files
- **Multi-service access** — signal server, market data server, and Xenon share one database with role-based access
- **Durability** — WAL-backed persistence, point-in-time recovery for real-money trading data
- **Foundation** — enable dashboards, backtesting, historical analysis, and reactive event-driven architecture

## 2. Infrastructure

### Postgres Host

- **Day 1:** Local Mac (dev machine), Homebrew or Docker, `localhost:5432`
- **Day N:** Mac Mini (16GB RAM, extendable storage), all services colocated
- **Day N+M:** Liftable to managed Postgres (RDS, Neon, Supabase) — zero code changes, only `DATABASE_URL` changes

All portability lives in a single env var: `DATABASE_URL=postgresql://xenon_app:***@<host>:5432/xenon_db`

A runbook for the Mac Mini migration will be written separately.

### Tech Stack

| Component         | Choice                  | Rationale                                                         |
| ----------------- | ----------------------- | ----------------------------------------------------------------- |
| Driver (async)    | asyncpg                 | Fastest async Postgres driver; matches existing raw SQL style     |
| Driver (sync)     | psycopg[binary]         | Sync Postgres driver for CLI scripts, migrations, one-off queries |
| Schema management | SQLAlchemy Core (async) | Schema-as-Python, composable query builder, no ORM overhead       |
| Migrations        | Alembic                 | Autogenerate from table defs, versioned, reversible, portable     |
| Reactive events   | LISTEN/NOTIFY + outbox  | Sub-ms delivery, durable replay on restart, no Kafka/Redis        |

## 3. Database Architecture

### Schema Organization

```
xenon_db (database)
├── xenon        — Xenon trading app (portfolio, orders, trades, scans)
├── signals      — Signal server (future)
├── marketdata   — Market data server (future)
└── events       — Shared event bus (outbox, cross-service notifications)
```

### Roles & Access

| Role             | Own schema   | Read                    | Write    |
| ---------------- | ------------ | ----------------------- | -------- |
| `xenon_app`      | `xenon`      | `signals`, `marketdata` | `events` |
| `signal_svc`     | `signals`    | —                       | `events` |
| `marketdata_svc` | `marketdata` | —                       | `events` |

## 4. Table Schemas

### 4A. Portfolio & Trading (`xenon` schema)

#### `xenon.positions`

Live positions snapshot. Replaces `data/portfolio.json` positions array.

| Column         | Type                               | Notes            |
| -------------- | ---------------------------------- | ---------------- |
| id             | BIGSERIAL PK                       |                  |
| ticker         | TEXT NOT NULL                      |                  |
| security_type  | TEXT NOT NULL                      | STK, OPT, COMBO  |
| expiry         | DATE                               | nullable for STK |
| strike         | NUMERIC(12,2)                      | nullable for STK |
| "right"        | TEXT                               | CALL, PUT        |
| quantity       | INTEGER NOT NULL                   |                  |
| avg_cost       | NUMERIC(12,4) NOT NULL             |                  |
| current_price  | NUMERIC(12,4)                      |                  |
| unrealized_pnl | NUMERIC(12,2)                      |                  |
| account        | TEXT NOT NULL                      | IB, FUTU         |
| synced_at      | TIMESTAMPTZ NOT NULL DEFAULT now() |                  |

#### `xenon.account_snapshots`

Account-level summary. Replaces top-level fields from `data/portfolio.json`.

| Column          | Type                               | Notes    |
| --------------- | ---------------------------------- | -------- |
| id              | BIGSERIAL PK                       |          |
| account         | TEXT NOT NULL                      | IB, FUTU |
| bankroll        | NUMERIC(14,2) NOT NULL             |          |
| peak_value      | NUMERIC(14,2)                      |          |
| net_liquidation | NUMERIC(14,2)                      |          |
| snapshot_at     | TIMESTAMPTZ NOT NULL DEFAULT now() |          |

#### `xenon.trades`

Append-only trade journal. Replaces `data/trade_log.json`.

| Column       | Type             | Notes                       |
| ------------ | ---------------- | --------------------------- |
| id           | BIGSERIAL PK     |                             |
| ticker       | TEXT NOT NULL    |                             |
| structure    | TEXT             | vertical, straddle, etc.    |
| action       | TEXT NOT NULL    | BUY, SELL                   |
| quantity     | INTEGER NOT NULL |                             |
| entry_cost   | NUMERIC(12,4)    |                             |
| exit_cost    | NUMERIC(12,4)    |                             |
| realized_pnl | NUMERIC(12,2)    |                             |
| edge         | TEXT             | signal that drove the trade |
| decision     | TEXT             | gate outcome                |
| opened_at    | TIMESTAMPTZ      |                             |
| closed_at    | TIMESTAMPTZ      |                             |
| metadata     | JSONB            | flexible overflow           |

#### `xenon.nav_history`

Daily NAV. Replaces `data/nav_history.jsonl`.

| Column    | Type                   | Notes           |
| --------- | ---------------------- | --------------- |
| date      | DATE PK                | one row per day |
| nav       | NUMERIC(14,2) NOT NULL |                 |
| daily_pnl | NUMERIC(12,2)          |                 |

### 4B. Order Lifecycle (`xenon` schema)

Direct port from `data/orders.duckdb`.

#### `xenon.order_submissions`

| Column            | Type                               | Notes |
| ----------------- | ---------------------------------- | ----- |
| submission_id     | TEXT PK                            |       |
| user_id           | TEXT                               |       |
| client_attempt_id | TEXT                               |       |
| ticker            | TEXT NOT NULL                      |       |
| security_type     | TEXT NOT NULL                      |       |
| action            | TEXT NOT NULL                      |       |
| quantity          | INTEGER NOT NULL                   |       |
| expiry            | DATE                               |       |
| strike            | NUMERIC(12,2)                      |       |
| "right"           | TEXT                               |       |
| multiplier        | INTEGER DEFAULT 100                |       |
| con_id            | BIGINT                             |       |
| placing_client_id | INTEGER                            |       |
| ib_order_id       | TEXT                               |       |
| perm_id           | TEXT                               |       |
| limit_price       | NUMERIC(12,4)                      |       |
| state             | TEXT NOT NULL                      |       |
| reason_code       | TEXT                               |       |
| filled_qty        | INTEGER DEFAULT 0                  |       |
| avg_fill_price    | NUMERIC(12,4)                      |       |
| modify_sequence   | INTEGER DEFAULT 0                  |       |
| submitted_at      | TIMESTAMPTZ NOT NULL               |       |
| updated_at        | TIMESTAMPTZ NOT NULL DEFAULT now() |       |

Indexes: `(state, ticker)`, `(perm_id)`, `(ib_order_id)`

Constraint: `UNIQUE(user_id, client_attempt_id)` — idempotency key for reserve_attempt

#### `xenon.order_events`

| Column        | Type                               | Notes |
| ------------- | ---------------------------------- | ----- |
| event_id      | BIGSERIAL PK                       |       |
| submission_id | UUID FK → order_submissions        |       |
| kind          | TEXT NOT NULL                      |       |
| detail        | JSONB                              |       |
| at            | TIMESTAMPTZ NOT NULL DEFAULT now() |       |

#### `xenon.wizard_sessions`

| Column             | Type                               | Notes                                               |
| ------------------ | ---------------------------------- | --------------------------------------------------- |
| session_id         | UUID PK                            |                                                     |
| ticker             | TEXT NOT NULL                      |                                                     |
| state              | TEXT NOT NULL                      | planned, leg_pricing, pricing, submitted, confirmed |
| structure_name     | TEXT                               |                                                     |
| intent             | TEXT                               | OPEN, CLOSE                                         |
| payload            | JSONB                              |                                                     |
| current_attempt_id | TEXT                               |                                                     |
| created_at         | TIMESTAMPTZ NOT NULL DEFAULT now() |                                                     |
| updated_at         | TIMESTAMPTZ NOT NULL DEFAULT now() |                                                     |

#### `xenon.wizard_events`

| Column     | Type                               | Notes |
| ---------- | ---------------------------------- | ----- |
| event_id   | BIGSERIAL PK                       |       |
| session_id | UUID FK → wizard_sessions          |       |
| kind       | TEXT NOT NULL                      |       |
| detail     | JSONB                              |       |
| at         | TIMESTAMPTZ NOT NULL DEFAULT now() |       |

#### `xenon.wizard_combo_attempts`

| Column            | Type                               | Notes |
| ----------------- | ---------------------------------- | ----- |
| attempt_id        | TEXT PK                            |       |
| session_id        | TEXT FK → wizard_sessions NOT NULL |       |
| ticker            | TEXT NOT NULL                      |       |
| structure_name    | TEXT                               |       |
| legs              | JSONB                              |       |
| combo_contract    | JSONB                              |       |
| ib_order_id       | TEXT                               |       |
| perm_id           | TEXT                               |       |
| placing_client_id | INTEGER                            |       |
| limit_price       | NUMERIC(12,4)                      |       |
| state             | TEXT NOT NULL                      |       |
| reason_code       | TEXT                               |       |
| filled_qty        | INTEGER DEFAULT 0                  |       |
| avg_fill_price    | NUMERIC(12,4)                      |       |
| modify_sequence   | INTEGER DEFAULT 0                  |       |
| submitted_at      | TIMESTAMPTZ                        |       |
| updated_at        | TIMESTAMPTZ NOT NULL DEFAULT now() |       |

#### `xenon.wizard_protection`

| Column          | Type                               | Notes                                    |
| --------------- | ---------------------------------- | ---------------------------------------- |
| protection_id   | BIGSERIAL PK                       |                                          |
| session_id      | TEXT FK → wizard_sessions NOT NULL |                                          |
| attempt_id      | TEXT FK → wizard_combo_attempts    |                                          |
| protection_type | TEXT NOT NULL                      | take_profit, stop_loss, time_stop, alert |
| config          | JSONB NOT NULL                     |                                          |
| state           | TEXT NOT NULL DEFAULT 'active'     |                                          |
| triggered_at    | TIMESTAMPTZ                        |                                          |
| created_at      | TIMESTAMPTZ NOT NULL DEFAULT now() |                                          |

### 4C. Scanner Results & Signals (`xenon` schema)

#### `xenon.scan_results`

Replaces `scanner.json`, `discover.json`, `gex.json`, `vcg.json`, `cri.json`.

| Column     | Type                               | Notes                              |
| ---------- | ---------------------------------- | ---------------------------------- |
| id         | BIGSERIAL PK                       |                                    |
| scan_type  | TEXT NOT NULL                      | watchlist, discover, gex, vcg, cri |
| payload    | JSONB NOT NULL                     | full scan output                   |
| scanned_at | TIMESTAMPTZ NOT NULL DEFAULT now() |                                    |

Latest result per type: `ORDER BY scanned_at DESC LIMIT 1`

#### `xenon.cri_series`

Replaces `data/cri_scheduled/` directory of per-bucket JSON files.

| Column      | Type                               | Notes |
| ----------- | ---------------------------------- | ----- |
| id          | BIGSERIAL PK                       |       |
| cri_level   | NUMERIC(8,4) NOT NULL              |       |
| alert       | BOOLEAN DEFAULT FALSE              |       |
| payload     | JSONB                              |       |
| recorded_at | TIMESTAMPTZ NOT NULL DEFAULT now() |       |

### 4D. UW Analysis & Flow (`xenon` schema)

#### `xenon.uw_analyze_snapshots`

Replaces `data/uw_analyze_cache.json`. Each row is a point-in-time snapshot per ticker.

| Column          | Type                               | Notes |
| --------------- | ---------------------------------- | ----- |
| id              | BIGSERIAL PK                       |       |
| ticker          | TEXT NOT NULL                      |       |
| vrp_state       | JSONB                              |       |
| regime          | JSONB                              |       |
| flow_signals    | JSONB                              |       |
| portfolio_score | NUMERIC(6,2)                       |       |
| snapshot_at     | TIMESTAMPTZ NOT NULL DEFAULT now() |       |

Index: `(ticker, snapshot_at DESC)`

#### `xenon.uw_flow_events`

Replaces `data/uw_unusual_flow_log.json`.

| Column         | Type                 | Notes                                |
| -------------- | -------------------- | ------------------------------------ |
| id             | BIGSERIAL PK         |                                      |
| ticker         | TEXT NOT NULL        |                                      |
| side           | TEXT                 | call, put                            |
| strike         | NUMERIC(12,2)        |                                      |
| expiry         | DATE                 |                                      |
| detected_at    | TIMESTAMPTZ NOT NULL |                                      |
| initial        | JSONB NOT NULL       | premium, oi, volume, mid, underlying |
| daily_track    | JSONB                | array of daily observations          |
| status         | TEXT NOT NULL        | open, closed, anomaly, expired       |
| anomaly_reason | TEXT                 |                                      |
| closed_at      | TIMESTAMPTZ          |                                      |

#### `xenon.uw_api_stats`

Replaces `data/uw_api_stats_history.json`.

| Column        | Type                        | Notes |
| ------------- | --------------------------- | ----- |
| id            | BIGSERIAL PK                |       |
| bucket_hour   | TIMESTAMPTZ NOT NULL UNIQUE |       |
| requests      | INTEGER DEFAULT 0           |       |
| cache_hits    | INTEGER DEFAULT 0           |       |
| latency_sum   | NUMERIC(10,2) DEFAULT 0     |       |
| latency_count | INTEGER DEFAULT 0           |       |
| status_2xx    | INTEGER DEFAULT 0           |       |
| status_4xx    | INTEGER DEFAULT 0           |       |
| status_5xx    | INTEGER DEFAULT 0           |       |

### 4E. Caches (`xenon` schema)

#### `xenon.ticker_cache`

Replaces `analyst_ratings_cache.json`, `company_info_cache/`, `seasonality_cache/`, `option_close_cache.json`.

| Column     | Type                               | Notes                                                                    |
| ---------- | ---------------------------------- | ------------------------------------------------------------------------ |
| ticker     | TEXT NOT NULL                      | composite PK                                                             |
| cache_type | TEXT NOT NULL                      | analyst_ratings, company_info, seasonality, price_history — composite PK |
| data       | JSONB NOT NULL                     |                                                                          |
| expires_at | TIMESTAMPTZ                        |                                                                          |
| updated_at | TIMESTAMPTZ NOT NULL DEFAULT now() |                                                                          |

PRIMARY KEY: `(ticker, cache_type)`

### 4F. Shared Event Bus (`events` schema)

#### `events.outbox`

Cross-service reactive event delivery via LISTEN/NOTIFY.

| Column      | Type                               | Notes                                            |
| ----------- | ---------------------------------- | ------------------------------------------------ |
| id          | BIGSERIAL PK                       |                                                  |
| channel     | TEXT NOT NULL                      | e.g. signal.new, position.synced, scan.completed |
| source      | TEXT NOT NULL                      | service name: xenon, signals, marketdata         |
| payload     | JSONB NOT NULL                     |                                                  |
| emitted_at  | TIMESTAMPTZ NOT NULL DEFAULT now() |                                                  |
| consumed_by | JSONB DEFAULT '[]'                 | track which services consumed                    |

Index: `(channel, emitted_at DESC)`

Postgres trigger: `AFTER INSERT → pg_notify(channel, id::text)`. The trigger handles pg_notify; application code should NOT call pg_notify directly.

Cleanup: nightly job truncates rows older than 7 days.

### Event Channels

| Channel           | Emitter                    | Consumers            | Payload                               |
| ----------------- | -------------------------- | -------------------- | ------------------------------------- |
| `position.synced` | Xenon (ib_sync)            | Signal server, UI    | `{account, position_count}`           |
| `order.filled`    | Xenon (order lifecycle)    | Trade log, UI        | `{submission_id, ticker, fill_price}` |
| `scan.completed`  | Xenon (scanner)            | UI                   | `{scan_type, candidate_count}`        |
| `signal.new`      | Signal server (future)     | Xenon                | `{ticker, signal_type, score}`        |
| `quote.updated`   | Marketdata server (future) | Xenon, Signal server | `{ticker, bid, ask}`                  |

## 5. Python Database Layer

### Directory Structure

```
src/xenon/db/
├── engine.py          # async engine + connection pool (from DATABASE_URL)
├── schema.py          # all SQLAlchemy Table definitions (xenon.* + events.*)
├── queries/           # query functions grouped by domain
│   ├── portfolio.py   # get_positions(), save_positions(), save_nav()
│   ├── orders.py      # insert_submission(), update_state(), get_by_perm_id()
│   ├── trades.py      # append_trade(), get_journal()
│   ├── scans.py       # save_scan(), get_latest_scan()
│   ├── uw.py          # save_snapshot(), get_latest_snapshot()
│   └── cache.py       # get_cached(), set_cached()
├── events.py          # LISTEN/NOTIFY helpers (emit_event, subscribe)
└── migrations/        # Alembic env + versions/
```

### FastAPI Integration

- Engine created from `DATABASE_URL` env var, pool_size=10
- Pool starts/stops with app lifespan (existing pattern in `server.py`)
- Each JSON read/write call swaps to a query function (e.g., `atomic_save("data/portfolio.json", data)` → `await save_positions(conn, positions)`)

### Dependencies

New packages: `sqlalchemy[asyncio]`, `asyncpg`, `psycopg[binary]`, `alembic`

Removed packages: `duckdb` (after migration)

orders_store.py is kept as the public facade -- its existing function signatures, dataclasses (RequestRow, ReservationOutcome, SubmissionRow, WorkingReservations), and return types are preserved. Only the DuckDB internals are replaced with calls to db.queries.orders.

## 6. Data Migration

One-time script: `scripts/migrations/migrate_to_postgres.py`

### Phases (sequential, FK-order-aware)

1. **Bootstrap schema** — `alembic upgrade head`
2. **Import critical data** — portfolio.json → positions + account_snapshots; nav_history.jsonl → nav_history; trade_log.json → trades; orders.duckdb → order_submissions + order_events + wizard_sessions + wizard_events
3. **Import scanner data** — scanner.json, discover.json, gex.json, vcg.json, cri.json → scan_results; cri_scheduled/ → cri_series
4. **Import UW data** — uw_analyze_cache.json → uw_analyze_snapshots; uw_unusual_flow_log.json → uw_flow_events; uw_api_stats_history.json → uw_api_stats
5. **Import caches** — analyst_ratings_cache.json, company_info_cache/, seasonality_cache/ → ticker_cache
6. **Verify** — row counts vs source, spot-check random records, print summary

### Post-Migration

- JSON files left in place as backup, no longer read by app
- Old JSON read/write code deleted (no shims)
- DuckDB dependency removed from `pyproject.toml`
- Fresh installs: `alembic upgrade head` creates empty tables; IB sync populates on first run

## 7. Testing

- Tests use a real Postgres instance (via `testcontainers-python` or dedicated test database)
- `alembic upgrade head` in test setup
- No mocking the database

## 8. What Stays as Files

| File/Dir                             | Reason                                                                            |
| ------------------------------------ | --------------------------------------------------------------------------------- |
| `data/apex_mirror/`                  | Parquet, bulk market data, read-only                                              |
| `data/price_history_cache/*.parquet` | Same — bulk data, file-native format                                              |
| `data/flex_token_config.json`        | Trivial UI reminder state                                                         |
| `data/strategies.json`               | Manual config until strategy editor UI exists                                     |
| `data/ta.duckdb`                     | Deprecated, dead code                                                             |
| `data/orders.json`                   | Read-only IB snapshot, regenerated every sync                                     |
| `data/futu_portfolio.json`           | Futu positions, regenerated by /futu/sync                                         |
| `data/performance.json`              | Computed from portfolio data on demand                                            |
| `data/watchlist.json`                | Manual config, referenced by 8+ scanner modules                                   |
| `data/reconciliation.json`           | Computed reconciliation output                                                    |
| `data/menthorq_cache/`               | MenthorQ scrape cache, ephemeral                                                  |
| `data/uw_analyze_history/`           | Per-ticker append-only archive (subsumed by uw_analyze_snapshots table over time) |
| `data/flow_analysis.json`            | Computed portfolio bias, regenerated on demand                                    |

## 9. Future Considerations

- **Mac Mini runbook** — document the `pg_dump | psql` + env var flip procedure
- **Connection pooling** — PgBouncer if connection count grows beyond pool_size=10
- **Partitioning** — `uw_analyze_snapshots` and `scan_results` by month if volume grows
- **Managed migration** — when spinning off to RDS/Neon, same `pg_dump | psql` + update `DATABASE_URL`
- **Signals/Marketdata schemas** — stub schemas created but populated when those services are built
