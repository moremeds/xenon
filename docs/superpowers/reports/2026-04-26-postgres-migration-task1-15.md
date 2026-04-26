# Postgres Migration Report — Tasks 1–15

**Date:** 2026-04-26
**Branch:** `feat/postgres-migration` (13 commits)
**Status:** Tasks 1–15 complete. DB layer built, data migrated, verified.

---

## Commits

| #   | Hash | Message                                                                             |
| --- | ---- | ----------------------------------------------------------------------------------- |
| 1   | —    | `feat(db): add sqlalchemy + asyncpg + alembic dependencies`                         |
| 2   | —    | `feat(db): add async engine module with connection pooling`                         |
| 3   | —    | `feat(db): define all SQLAlchemy table schemas for xenon + events`                  |
| 4   | —    | `feat(db): alembic setup with initial migration for all tables`                     |
| 5   | —    | `feat(db): add shared test fixtures with per-test table truncation`                 |
| 6   | —    | `feat(db): all query modules — portfolio, orders, wizard, trades, scans, UW, cache` |
| 7   | —    | `feat(db): event bus with LISTEN/NOTIFY + outbox trigger`                           |
| 8   | —    | `feat(db): wire Postgres engine into FastAPI lifespan`                              |
| 9   | —    | `feat(db): one-time data migration script (JSON/DuckDB → Postgres)`                 |
| 10  | —    | `feat(db): import uw_analyze_history archive (45K snapshots, 93 tickers)`           |
| 11  | —    | `fix(db): parse nested entries in uw_analyze_cache.json, import 82 cache snapshots` |
| 12  | —    | `refactor(db): rename uw_snapshots → uw_analyze_snapshots`                          |

## New Files

```
src/xenon/db/
├── __init__.py
├── engine.py              # Async engine + pool from DATABASE_URL
├── schema.py              # 16 xenon tables + 1 events.outbox
├── events.py              # LISTEN/NOTIFY + outbox helpers
├── queries/
│   ├── __init__.py
│   ├── portfolio.py       # positions, account_snapshots, nav_history
│   ├── orders.py          # order_submissions, order_events
│   ├── wizard.py          # wizard_sessions, wizard_events
│   ├── trades.py          # trades
│   ├── scans.py           # scan_results, cri_series
│   ├── uw.py              # uw_analyze_snapshots, uw_flow_events, uw_api_stats
│   └── cache.py           # ticker_cache
├── tests/
│   ├── __init__.py
│   ├── conftest.py        # engine/conn/clean_tables fixtures
│   ├── test_engine.py
│   ├── test_schema.py
│   ├── test_portfolio.py
│   ├── test_orders.py
│   ├── test_wizard.py
│   ├── test_trades.py
│   ├── test_scans.py
│   ├── test_uw.py
│   ├── test_cache.py
│   ├── test_events.py
│   └── test_lifespan.py
└── migrations/
    ├── env.py             # Async + multi-schema (xenon, events)
    ├── script.py.mako
    ├── README
    └── versions/
        ├── 0cf835b06d68_initial_schema.py
        ├── 9b645325b50d_add_outbox_notify_trigger.py
        └── 80e181cf1308_rename_uw_snapshots_to_uw_analyze_.py

alembic.ini
scripts/migrations/migrate_to_postgres.py
```

## Modified Files

```
pyproject.toml              # +sqlalchemy[asyncio], asyncpg, psycopg[binary], alembic
src/xenon/api/server.py     # Lifespan: init_engine on startup, dispose_engine on shutdown
.env                        # +DATABASE_URL, +DATABASE_URL_TEST (not committed)
```

## Databases Created

| Database     | Purpose                        |
| ------------ | ------------------------------ |
| `xenon_db`   | Production — all migrated data |
| `xenon_test` | Pytest — truncated per test    |

Schemas: `xenon`, `signals`, `marketdata`, `events`
Role: `xenon_app` (login, owns xenon + events)

## Reconciliation: Source vs Postgres

| Table                         | Postgres   | Source     | Status   |
| ----------------------------- | ---------- | ---------- | -------- |
| `xenon.account_snapshots`     | 1          | 1          | ✅ exact |
| `xenon.cri_series`            | 380        | 380        | ✅ exact |
| `xenon.nav_history`           | 18         | 18         | ✅ exact |
| `xenon.order_events`          | 0          | 0          | ✅ exact |
| `xenon.order_submissions`     | 0          | 0          | ✅ exact |
| `xenon.positions`             | 0          | 0          | ✅ exact |
| `xenon.scan_results`          | 5          | 5          | ✅ exact |
| `xenon.ticker_cache`          | 12         | 12         | ✅ exact |
| `xenon.trades`                | 1          | 1          | ✅ exact |
| `xenon.uw_analyze_snapshots`  | 45,280     | 45,280     | ✅ exact |
| `xenon.uw_api_stats`          | 0          | 0          | ✅ exact |
| `xenon.uw_flow_events`        | 0          | 0          | ✅ exact |
| `xenon.wizard_combo_attempts` | 0          | 0          | ✅ exact |
| `xenon.wizard_events`         | 0          | 0          | ✅ exact |
| `xenon.wizard_protection`     | 0          | 0          | ✅ exact |
| `xenon.wizard_sessions`       | 0          | 0          | ✅ exact |
| `events.outbox`               | 0          | 0          | ✅ exact |
| **TOTAL**                     | **45,697** | **45,697** | ✅       |

### Source → Table mapping

| Source File                                           | → Postgres Table                                            |
| ----------------------------------------------------- | ----------------------------------------------------------- |
| `data/portfolio.json` (positions array)               | `xenon.positions` (0 — no open positions at migration time) |
| `data/portfolio.json` (bankroll/nav fields)           | `xenon.account_snapshots` (1 row)                           |
| `data/nav_history.jsonl` (19 lines, 1 dupe date)      | `xenon.nav_history` (18 rows — ON CONFLICT deduped)         |
| `data/trade_log.json`                                 | `xenon.trades` (1 row)                                      |
| `data/orders.duckdb` → `orders_submissions`           | `xenon.order_submissions` (0 — table was empty)             |
| `data/orders.duckdb` → `orders_events`                | `xenon.order_events` (0)                                    |
| `data/orders.duckdb` → `wizard_sessions`              | `xenon.wizard_sessions` (0)                                 |
| `data/orders.duckdb` → `wizard_session_events`        | `xenon.wizard_events` (0)                                   |
| `data/orders.duckdb` → `wizard_combo_attempts`        | `xenon.wizard_combo_attempts` (0)                           |
| `data/orders.duckdb` → `wizard_protection`            | `xenon.wizard_protection` (0)                               |
| `data/scanner.json`                                   | `xenon.scan_results` (scan_type=watchlist)                  |
| `data/discover.json`                                  | `xenon.scan_results` (scan_type=discover)                   |
| `data/gex.json`                                       | `xenon.scan_results` (scan_type=gex)                        |
| `data/vcg.json`                                       | `xenon.scan_results` (scan_type=vcg)                        |
| `data/cri.json`                                       | `xenon.scan_results` (scan_type=cri)                        |
| `data/cri_scheduled/*.json` (380 files)               | `xenon.cri_series` (380 rows)                               |
| `data/uw_analyze_cache.json` (82 tickers)             | `xenon.uw_analyze_snapshots` (82 rows)                      |
| `data/uw_analyze_history/` (45,198 files, 93 tickers) | `xenon.uw_analyze_snapshots` (45,198 rows)                  |
| `data/uw_unusual_flow_log.json`                       | `xenon.uw_flow_events` (0 — empty/no events)                |
| `data/uw_api_stats_history.json`                      | `xenon.uw_api_stats` (0 — no hourly buckets)                |
| `data/analyst_ratings_cache.json` (2 tickers)         | `xenon.ticker_cache` (2 rows, type=analyst_ratings)         |
| `data/company_info_cache/*.json` (6 files)            | `xenon.ticker_cache` (6 rows, type=company_info)            |
| `data/seasonality_cache/*.json` (4 files)             | `xenon.ticker_cache` (4 rows, type=seasonality)             |

## Files Staying as Files (per spec)

| File                                  | Reason                                          |
| ------------------------------------- | ----------------------------------------------- |
| `data/watchlist.json`                 | Manual config, referenced by 8+ scanner modules |
| `data/strategies.json`                | Manual config until strategy editor UI exists   |
| `data/flex_token_config.json`         | Trivial UI reminder state                       |
| `data/orders.json`                    | Read-only IB snapshot, regenerated every sync   |
| `data/futu_portfolio.json`            | Futu positions, regenerated by /futu/sync       |
| `data/performance.json`               | Computed from portfolio data on demand          |
| `data/flow_analysis.json`             | Computed portfolio bias, regenerated on demand  |
| `data/option_close_cache.json`        | Option close price cache                        |
| `data/ta_premarket_universe.json`     | Premarket universe config                       |
| `data/ta_premarket_status.json`       | Premarket scan status                           |
| `data/price_history_cache/` (2 files) | Parquet, bulk market data                       |
| `data/menthorq_cache/` (12 files)     | MenthorQ scrape cache, ephemeral                |

## Uncategorized Files (not in migration spec)

| Path                      | Files | Notes                            |
| ------------------------- | ----- | -------------------------------- |
| `data/analysis/`          | 5     | Investigation/analysis artifacts |
| `data/evidence/`          | 5     | Evidence artifacts               |
| `data/locks/`             | 0     | Empty lock dir                   |
| `data/presets/`           | 152   | Preset configurations            |
| `data/scanner/`           | 0     | Empty                            |
| `data/service_health/`    | 2     | Service health state             |
| `data/universe/`          | 2     | Universe definition files        |
| `data/uw_scan/`           | 7     | UW scan artifacts                |
| `data/orders.json.bak`    | 1     | Backup                           |
| `data/portfolio.json.bak` | 1     | Backup                           |
| `data/ta.duckdb`          | 1     | Deprecated, dead code            |

## Known Issues

### Test pollution in uw_analyze_snapshots

Tickers `AAA`, `BBB`, `CCC`, `DDD`, `EEE`, `T0`–`T4`, `ZZZZ` (2,224 rows total) are test/dev pollution from `_archive_snapshot` in `uw_analyze_cache.py` running against the real data directory. Source: dev experimentation, not a code bug. Can be cleaned with:

```sql
DELETE FROM xenon.uw_analyze_snapshots
WHERE ticker IN ('AAA','BBB','CCC','DDD','EEE','T0','T1','T2','T3','T4','ZZZZ');
```

### Table rename

`uw_snapshots` was renamed to `uw_analyze_snapshots` (Alembic migration `80e181cf1308`). The spec still references the old name — update in Task 23 (docs).

## Fixes Applied During Execution

| Issue                                                                     | Fix                                                             |
| ------------------------------------------------------------------------- | --------------------------------------------------------------- |
| `ib_order_id`/`perm_id` are TEXT columns but callers pass int             | Added `str()` coercion in `mark_submitted` and lookup functions |
| `save_positions` batch INSERT fails with heterogeneous dict keys          | Changed to per-row INSERT                                       |
| Alembic trigger migration: asyncpg can't execute multi-statement          | Split into two `op.execute()` calls                             |
| Migration script: `:param::jsonb` conflicts with psycopg parameter syntax | Replaced with `CAST(:param AS jsonb)`                           |
| CRI `cri_level` field is sometimes a nested dict                          | Added `_extract_cri_level()` helper                             |
| `uw_analyze_cache.json` has nested `{entries: {tickers}}` not flat        | Fixed to iterate `data.get("entries", data)`                    |

## Tests

36 tests, all passing:

- `test_engine.py` (2) — engine creation, connectivity
- `test_schema.py` (4) — table introspection
- `test_portfolio.py` (4) — positions, snapshots, NAV upsert
- `test_orders.py` (8) — reserve, submit, terminal, events, lookups, modify
- `test_wizard.py` (3) — sessions, state updates, events
- `test_trades.py` (2) — append, filter
- `test_scans.py` (3) — save/get scan, CRI series
- `test_uw.py` (4) — snapshots, history, flow events, API stats upsert
- `test_cache.py` (3) — set/get, upsert, TTL expiry
- `test_events.py` (2) — outbox emit, get_events_since
- `test_lifespan.py` (1) — engine init/get/dispose lifecycle

## What's Next (Tasks 16–23)

| Task | What                                                 | Risk                                     |
| ---- | ---------------------------------------------------- | ---------------------------------------- |
| 16   | Migrate `ib_sync.py` (portfolio + NAV writes)        | Low — sync subprocess, needs own engine  |
| 17   | Migrate `orders_store.py` (DuckDB → Postgres)        | **High** — order lifecycle state machine |
| 18   | Migrate `ib_execute.py` (trade log)                  | Low                                      |
| 19   | Migrate UW services (cache, flow tracker, API stats) | Medium                                   |
| 20   | Migrate scanner cache writes                         | Medium                                   |
| 21   | Migrate combo wizard (DuckDB → Postgres)             | High — 10 files touch DuckDB             |
| 22   | Cleanup — remove DuckDB dep, dead JSON code          | Low                                      |
| 23   | Update CLAUDE.md + documentation                     | Low                                      |
