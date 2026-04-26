# Payload Normalization for Backtesting Analytics

**Date:** 2026-04-26
**Status:** Draft — pending implementation plan
**Author:** Brainstorm with chenxi

## Problem

Several Postgres tables store substantive data inside opaque `payload` / `vrp_state` / `regime` / `flow_signals` JSONB columns, which makes them impossible to query for analytics. The motivating use case is **backtesting**: discovering patterns in market-structure signals (CRI, VCG, UW analyze, GEX, flow events) to find alpha or enhanced beta. We can't run feature-matrix queries when every metric is nested inside a JSONB blob.

Two specific problems on top of the general one:

1. **`uw_analyze_snapshots` is lossy.** The on-disk archives at `data/uw_analyze_history/<TICKER>/*.json` contain ~80 fields per snapshot (full `report`, `display`, `derived`, `dark_pool_summary`, `options_flow_summary`, `flow_alerts`, `gex_by_strike`, etc.). The Postgres writer at `src/xenon/api/services/uw_analyze_cache.py:_archive_to_postgres` persists only 4. ~95% of the analyze output is dropped at archive time.
2. **VCG is not persisted to Postgres at all** — only to `data/vcg.json`. The UW API stats history at `data/uw_api_stats_history.json` exists but has never been backfilled into the `uw_api_stats` table.

## Goals

- Every signal metric becomes a queryable scalar column (no JSONB extraction at query time for common analytics).
- Single source of truth: JSONB stays as the authoritative payload; typed columns derive from it.
- Native event-time storage — preserve intraday granularity. No daily roll-up at write time.
- Backtesting framework agnostic. Apex (under overhaul) or any pandas/polars consumer can pull a feature matrix without bespoke unwrap code.
- Backfill existing data wherever an authoritative source exists (on-disk JSON archives, `data/vcg.json`, `data/uw_api_stats_history.json`).

## Non-goals

- Forward-returns / market-data table for backtest dependent variables. Out of scope; needs its own data source (IB historical bars or similar) and its own spec.
- Replacing the file-based archives. They stay. They're the disaster-recovery source.
- Apex itself or any backtest framework code.
- Production-grade rollout (zero-downtime, dual-writes, burn-in). System is currently test/QA mode with real-life data — single-PR rollout is acceptable; rollback is "drop tables and re-run."

## Decisions locked in

| #   | Decision            | Choice                                                                                                            | Reasoning                                                                                  |
| --- | ------------------- | ----------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| 1   | Scope               | `uw_analyze_snapshots`, `cri_series`, `scan_results`, `uw_flow_events`, NEW `vcg_series`, backfill `uw_api_stats` | Covers signal sources used for backtesting. Wizard tables and `ticker_cache` are excluded. |
| 2   | Use case            | Backtesting / pattern discovery for alpha + enhanced beta                                                         | Drives feature-matrix shape.                                                               |
| 3   | Time alignment      | Native event-time                                                                                                 | Intraday signals matter. No information loss.                                              |
| 4   | Existing data       | Backfill                                                                                                          | Lose nothing already captured (esp. on-disk uw_analyze archives).                          |
| 5   | JSONB lifecycle     | Keep both JSONB + typed columns                                                                                   | Forward-compat fallback if extraction misses a field.                                      |
| 6   | Extraction location | Postgres `GENERATED ALWAYS AS … STORED`                                                                           | Single source of truth (payload), Postgres derives. Quality verifiable later.              |
| 7   | Table layout        | Approach 2 — domain-grouped sibling tables; views over wide tables                                                | Each consumption surface is independently joinable.                                        |
| 8   | Rollout             | Single PR, single Alembic revision, post-upgrade backfill scripts                                                 | No production = no need for phased dual-writes.                                            |

## Architecture

**Storage model:**

- JSONB payload remains the single source of truth on every parent table.
- Typed scalar columns are `GENERATED ALWAYS AS (…) STORED` from the JSONB. Postgres backfills automatically when the column is added (table rewrite). Indexable.
- Array data fans out to child tables via `AFTER INSERT OR UPDATE` triggers on the parent. The same trigger logic handles backfill (replay the trigger over existing rows once during migration).
- Views provide consumption-friendly column groupings for the wider parents.

**Constraint to plan around:** generated expressions cannot reference subqueries or other generated columns; type casts that fail at INSERT raise. Wrap every extraction in safe form (`(payload->>'x')::numeric` against rows where `x` is always a number; for nullable/typed fields confirm via Phase-0 reconnaissance).

## Per-table schema

### `cri_series` — market crash risk index

Existing: `id`, `cri_level`, `alert`, `payload` (JSONB), `recorded_at`.

Add 19 generated columns extracted from `payload` (source: `src/xenon/scanners/cri.py:920-941`):

| Column                 | Type          | Expression                                         |
| ---------------------- | ------------- | -------------------------------------------------- |
| `recorded_date`        | date          | `(payload->>'date')::date`                         |
| `vix`                  | numeric(8,4)  | `(payload->>'vix')::numeric`                       |
| `vvix`                 | numeric(8,4)  | `(payload->>'vvix')::numeric`                      |
| `spy`                  | numeric(10,4) | `(payload->>'spy')::numeric`                       |
| `vix_5d_roc`           | numeric(8,4)  | `(payload->>'vix_5d_roc')::numeric`                |
| `vvix_vix_ratio`       | numeric(8,4)  | `(payload->>'vvix_vix_ratio')::numeric`            |
| `spx_100d_ma`          | numeric(10,4) | `(payload->>'spx_100d_ma')::numeric`               |
| `spx_distance_pct`     | numeric(8,4)  | `(payload->>'spx_distance_pct')::numeric`          |
| `cor1m`                | numeric(6,4)  | `(payload->>'cor1m')::numeric`                     |
| `cor1m_previous_close` | numeric(6,4)  | `(payload->>'cor1m_previous_close')::numeric`      |
| `cor1m_5d_change`      | numeric(6,4)  | `(payload->>'cor1m_5d_change')::numeric`           |
| `realized_vol`         | numeric(8,4)  | `(payload->>'realized_vol')::numeric`              |
| `cri_score`            | numeric(8,4)  | `((payload->'cri')->>'score')::numeric`            |
| `cri_components`       | jsonb         | `payload->'cri'->'components'`                     |
| `cta_exposure_pct`     | numeric(6,2)  | `((payload->'cta')->>'exposure_pct')::numeric`     |
| `cta_forced_reduction` | boolean       | `((payload->'cta')->>'forced_reduction')::boolean` |
| `cta_selling_usd_b`    | numeric(8,2)  | `((payload->'cta')->>'selling_usd_b')::numeric`    |
| `menthorq_cta_score`   | numeric(8,4)  | `((payload->'menthorq_cta')->>'score')::numeric`   |
| `crash_trigger_fired`  | boolean       | `((payload->'crash_trigger')->>'fired')::boolean`  |

**Indexes:** `(recorded_date)`, partial `(crash_trigger_fired) WHERE crash_trigger_fired`, partial `(cta_forced_reduction) WHERE cta_forced_reduction`.

**Views:**

- `cri_market_features(recorded_at, recorded_date, vix, vvix, spy, vix_5d_roc, vvix_vix_ratio, spx_100d_ma, spx_distance_pct, cor1m, cor1m_5d_change, realized_vol)`
- `cri_cta_positioning(recorded_at, cta_exposure_pct, cta_forced_reduction, cta_selling_usd_b, menthorq_cta_score)`
- `cri_crash_signals(recorded_at, cri_level, cri_score, alert, crash_trigger_fired)`

`history[]` and `spy_closes[]` arrays stay in JSONB; truth lives in row-by-row series.

### `uw_analyze_snapshots` — restructured to capture full payload

Source: `data/uw_analyze_history/<TICKER>/*.json` (the on-disk archive, which is the lossless source).

**Drop existing columns** `vrp_state`, `regime`, `flow_signals`. **Add** these JSONB columns (each holds a full sub-tree):

| Column                 | Source path                                                                  |
| ---------------------- | ---------------------------------------------------------------------------- |
| `report`               | `current.report` (incl. benchmark, vrp, regime, scores, notes, setup_thesis) |
| `display`              | `current.display` (incl. gex_by_strike[])                                    |
| `derived`              | `current.derived`                                                            |
| `dark_pool_summary`    | `current.dark_pool_summary`                                                  |
| `options_flow_summary` | `current.options_flow_summary`                                               |
| `flow_alerts`          | `current.flow_alerts` (array)                                                |
| `materialized_changes` | top-level `materialized_changes`                                             |
| `report_fetched_at`    | timestamptz from `report.fetched_at`                                         |
| `archived_at`          | timestamptz from top-level `archived_at`                                     |

**Generated columns** (~30 total):

`report.*`: `price`, `composite_score`, `flow_score`, `volatility_score`, `market_structure_score`, `positioning_score`, `grade`, `bias`, `mode`, `reweighted`
`vrp.*`: `vrp_raw`, `vrp_zscore`, `iv_percentile`, `ts_ratio`, `ts_inverted`, `earnings_within_14d`
`regime.*`: `regime_label`, `regime_reason`, `gex_sign`, `gex_flip_relative`, `flip_distance_pct`
`display.*`: `iv`, `rv`, `iv_rank`, `call_wall_strike`, `put_wall_strike`, `gamma_per_1pct`, `net_call_premium`, `net_put_premium`, `short_volume_ratio`, `term_structure_label`, `max_pain`
`derived.*`: `derived_gex_sign`, `derived_call_wall`, `derived_put_wall`, `derived_max_pain`, `derived_spot`
`dark_pool_summary.*`: `dp_score`, `dp_signal`, `dp_direction`, `dp_strength`, `dp_buy_ratio`, `dp_options_conflict`, `dp_num_prints`, `dp_sustained_days`
`options_flow_summary.*`: `of_total_alerts`, `of_total_premium`, `of_call_premium`, `of_put_premium`, `of_call_put_ratio`, `of_bias`
`benchmark.*` (nested in report): `spy_iv_rank`, `spy_gex_regime`, `sector_etf_ticker`, `sector_etf_iv_rank`, `sector_etf_gex_regime`

**Child tables** (1-to-many fanouts, populated by trigger):

- `uw_analyze_flow_alerts(snapshot_id, ticker, snapshot_at, alert_type, alert_severity, alert_payload jsonb)`
- `uw_analyze_gex_strikes(snapshot_id, ticker, snapshot_at, strike, call_gamma, put_gamma, net_gamma, distance_pct, is_call_wall, is_put_wall)`
- `uw_analyze_short_volume_trend(snapshot_id, ticker, snapshot_at, position_in_trend, ratio)`

**Indexes:** `(ticker, snapshot_at DESC)`, `(snapshot_at, gex_sign)`, `(snapshot_at, regime_label)`, `(snapshot_at, bias)`.

### `scan_results` — split per scan_type

Today: one table with `scan_type` discriminator and type-specific JSONB. The mixed-type shape is wrong for analytics.

**Strategy:** create per-type sibling tables. Writers dual-write to both `scan_results` (legacy) and the per-type table. `scan_results` will be dropped in a future change once readers migrate.

**`gex_snapshots`** (from `src/xenon/scanners/gex.py:850-930`):

| Column                  | Type              | Source                                               |
| ----------------------- | ----------------- | ---------------------------------------------------- |
| `id`                    | bigserial PK      |                                                      |
| `ticker`                | text NOT NULL     | from payload, also explicit                          |
| `data_date`             | date              | from payload                                         |
| `scanned_at`            | timestamptz       | inherited                                            |
| `payload`               | jsonb NOT NULL    | full payload                                         |
| `spot`                  | numeric(12,4) GEN | `(payload->>'spot')::numeric`                        |
| `net_gex`               | numeric(14,2) GEN | `(payload->>'net_gex')::numeric`                     |
| `net_dex`               | numeric(14,2) GEN | `(payload->>'net_dex')::numeric`                     |
| `vol_pc`                | numeric(8,4) GEN  | `(payload->>'vol_pc')::numeric`                      |
| `iv_30d`                | numeric(6,4) GEN  | `((payload->'iv')->>'iv30d')::numeric`               |
| `iv_rank`               | numeric(6,2) GEN  | `((payload->'iv')->>'iv_rank')::numeric`             |
| `hv_30d`                | numeric(6,4) GEN  | `((payload->'iv')->>'hv30')::numeric`                |
| `mq_iv_30d`             | numeric(6,4) GEN  | `((payload->'iv')->>'mq_iv30d')::numeric`            |
| `level_max_magnet`      | numeric(12,4) GEN | `((payload->'levels')->>'max_magnet')::numeric`      |
| `level_second_magnet`   | numeric(12,4) GEN | `((payload->'levels')->>'second_magnet')::numeric`   |
| `level_max_accelerator` | numeric(12,4) GEN | `((payload->'levels')->>'max_accelerator')::numeric` |
| `level_put_wall`        | numeric(12,4) GEN | `((payload->'levels')->>'put_wall')::numeric`        |

**Indexes:** `(ticker, scanned_at DESC)`, `(scanned_at)`, `(data_date)`.

**Other scan_types** discovered in Phase 0 reconnaissance get the same per-type-table treatment.

### `uw_flow_events` — initial + daily ticks

Existing: `id`, `flow_event_key`, `ticker`, `side`, `strike`, `expiry`, `detected_at`, `initial` (JSONB), `daily_track` (JSONB), `status`, `anomaly_reason`, `closed_at`.

**Add generated columns** from `initial.*`: `initial_premium_usd`, `initial_size`, `initial_dte`, `initial_iv`, `initial_spot`, `initial_aggressor`. (Exact key names to be confirmed in Phase 0.)

**Child table — `uw_flow_event_ticks`** (1-to-many fanout from mutating `daily_track`):

```sql
uw_flow_event_ticks (
  id             bigserial PK,
  event_id       bigint NOT NULL REFERENCES uw_flow_events(id) ON DELETE CASCADE,
  flow_event_key text NOT NULL,        -- denormalized for easier joins
  observed_at    timestamptz NOT NULL,
  spot           numeric(12,4),
  bid            numeric(10,4),
  ask            numeric(10,4),
  mark           numeric(10,4),
  oi             int,
  volume         int,
  iv             numeric(6,4),
  tick_payload   jsonb NOT NULL,
  UNIQUE (event_id, observed_at)
)
```

**This is the highest-value backtesting table** — gives markout curves per signal.

**Indexes:** `(event_id, observed_at)`, `(observed_at)`.

### NEW `vcg_series` — vol-credit gap

Mirrors `cri_series`. Source: `data/vcg.json` shape, written by `src/xenon/scanners/vcg.py` (writer must be added).

```sql
vcg_series (
  id            bigserial PK,
  scanned_at    timestamptz NOT NULL DEFAULT now(),
  market_open   boolean,
  credit_proxy  text,
  payload       jsonb NOT NULL,
  -- generated columns from signal.*:
  vcg, vcg_adj, residual,
  beta1_vvix, beta2_vix, alpha,
  vix, vvix,
  credit_price, credit_5d_return_pct,
  ro, edr, tier, bounce,
  vvix_severity, sign_ok, sign_suppressed,
  pi_panic, regime, interpretation,
  -- attribution
  attr_vvix_pct, attr_vix_pct,
  attr_vvix_component, attr_vix_component, attr_model_implied
)
```

(Type details in implementation plan.)

**Indexes:** `(scanned_at)`, `(regime)`, partial `(tier) WHERE tier IS NOT NULL`.

History array stays in JSONB. Going-forward truth = row-by-row appends.

### `uw_api_stats` — backfill only, no schema change

Schema mapping (column rename gotchas marked **!**):

| `data/uw_api_stats_history.json` key | Table column        |
| ------------------------------------ | ------------------- |
| `<timestamp>` (dict key)             | `bucket_hour`       |
| `requests_2xx`                       | `status_2xx` **!**  |
| `requests_4xx`                       | `status_4xx` **!**  |
| `requests_5xx`                       | `status_5xx` **!**  |
| `cached`                             | `cache_hits` **!**  |
| `sum_latency_ms`                     | `latency_sum` **!** |
| `latency_count`                      | `latency_count`     |
| derived sum of statuses              | `requests`          |

## Migration path (single PR)

| Step | What                                                                                                                            | Where                                                                                                                                                                                           |
| ---- | ------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0    | Reconnaissance SELECTs (3 queries): distinct `scan_type`, sample `initial`, sample `daily_track` shape                          | manual, results recorded in plan                                                                                                                                                                |
| 1    | One Alembic revision: all schema changes (drop old uw_analyze cols, add all new tables, generated cols, child tables, triggers) | `src/xenon/db/migrations/versions/<date>_normalize_payloads.py`                                                                                                                                 |
| 2    | Writer code changes (see "Writer code changes" below)                                                                           | `src/xenon/api/services/uw_analyze_cache.py`, `src/xenon/scanners/vcg.py`, `src/xenon/scanners/gex.py`, `src/xenon/db/queries/uw.py`, `src/xenon/db/queries/scans.py`, `src/xenon/db/schema.py` |
| 3    | Backfill scripts run after `alembic upgrade head`                                                                               | `scripts/migrations/2026_xx_xx_backfill_uw_analyze_history.py`, `..._backfill_vcg_history.py`, `..._backfill_uw_api_stats.py`                                                                   |
| 4    | Sanity check report: row counts + non-NULL coverage                                                                             | `scripts/migrations/<date>_verify_normalize_payloads.py`                                                                                                                                        |

Rollback = `alembic downgrade -1` + drop new tables. No data loss because JSONB / on-disk archives are intact.

## Trigger DDL — example sketch

`uw_flow_event_ticks` (assumes `daily_track` is a dict keyed by ISO timestamp; confirm in Phase 0):

```sql
CREATE OR REPLACE FUNCTION fanout_uw_flow_event_ticks() RETURNS TRIGGER AS $$
DECLARE
  k text; v jsonb;
BEGIN
  IF NEW.daily_track IS NULL THEN RETURN NEW; END IF;
  FOR k, v IN SELECT key, value FROM jsonb_each(NEW.daily_track) LOOP
    INSERT INTO uw_flow_event_ticks (
      event_id, flow_event_key, observed_at,
      spot, bid, ask, mark, oi, volume, iv, tick_payload
    ) VALUES (
      NEW.id, NEW.flow_event_key, k::timestamptz,
      (v->>'spot')::numeric, (v->>'bid')::numeric, (v->>'ask')::numeric,
      (v->>'mark')::numeric, (v->>'oi')::int, (v->>'volume')::int,
      (v->>'iv')::numeric, v
    )
    ON CONFLICT (event_id, observed_at) DO NOTHING;
  END LOOP;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_uw_flow_event_ticks_fanout
AFTER INSERT OR UPDATE OF daily_track ON uw_flow_events
FOR EACH ROW EXECUTE FUNCTION fanout_uw_flow_event_ticks();
```

Same pattern for `uw_analyze_flow_alerts`, `uw_analyze_gex_strikes`, `uw_analyze_short_volume_trend` — each iterates over a JSONB array on the parent and inserts child rows. Backfill = `UPDATE <parent> SET id = id` to fire the trigger over existing rows.

## Writer code changes

### `src/xenon/api/services/uw_analyze_cache.py:_archive_to_postgres`

Today (lines 426-459): writes 4 trimmed values. After: writes the full `current` dict + `materialized_changes` + `archived_at` into the new JSONB columns. Caller passes through `materialized_changes` and `archived_at` from the file-archive write that runs above.

### `src/xenon/scanners/vcg.py`

Add a Postgres write block mirroring `src/xenon/scanners/gex.py:935-946`. Single `vcg_series.insert()` after the file write, wrapped in try/except (failure should not abort the scan).

### `src/xenon/scanners/gex.py:935-946`

Dual-write: existing `scan_results(scan_type='gex', payload=result)` plus new `gex_snapshots(payload=result, ticker=..., data_date=...)`. Same transaction.

### `src/xenon/db/queries/`

- `scans.py` — add `save_gex_snapshot(payload)`, `save_vcg_scan(payload, market_open, credit_proxy)`.
- `uw.py` — `save_snapshot()` signature changes to `(report, display, derived, dark_pool_summary, options_flow_summary, flow_alerts, materialized_changes, report_fetched_at, archived_at)` matching new schema.

### `src/xenon/db/schema.py`

Restructure `uw_analyze_snapshots`. Add new tables. Generated columns expressed via SQLAlchemy `Computed("…", persisted=True)`.

## Backfill scripts

### `scripts/migrations/<date>_backfill_uw_analyze_history.py`

Walk `data/uw_analyze_history/<TICKER>/*.json`. For each file:

- Parse `current`, `materialized_changes`, `archived_at`.
- Locate matching `uw_analyze_snapshots` row by `(ticker, snapshot_at ≈ archived_at)` within ±1 second tolerance.
- If found, UPDATE new columns. If not, INSERT (these are pre-table-creation snapshots).
- Triggers fire on UPDATE/INSERT → child tables backfill automatically.

Idempotent (UPDATE then INSERT-on-miss). Reports per-file outcome.

### `scripts/migrations/<date>_backfill_vcg_history.py`

Read `data/vcg.json`:

- Insert one current-snapshot row with full `payload`, `signal`, attribution.
- For each `history[i]` insert one row with `scanned_at = history[i].date::timestamptz`, `payload = {signal: {derived from history item}, history: [history[i]]}`. Fields not in history items stay NULL.

### `scripts/migrations/<date>_backfill_uw_api_stats.py`

Read `data/uw_api_stats_history.json`. For each `<timestamp> -> bucket` entry, call existing `upsert_api_stats(bucket_hour=ts, status_2xx=bucket['requests_2xx'], status_4xx=..., status_5xx=..., cache_hits=bucket['cached'], latency_sum=bucket['sum_latency_ms'], latency_count=bucket['latency_count'], requests=sum_of_statuses)`. Idempotent (existing `ON CONFLICT DO UPDATE`).

### `scripts/migrations/<date>_verify_normalize_payloads.py`

Reports:

```
uw_analyze_snapshots: backfilled=N rows, source files=M, missing=M-N
  per-column non-NULL counts
uw_analyze_flow_alerts: total=X rows, distinct snapshot_ids=Y
uw_analyze_gex_strikes: total=X rows
uw_flow_event_ticks: total=X rows, parent events with daily_track=Y
vcg_series: backfilled=N rows from history + 1 current
uw_api_stats: backfilled=N buckets, JSON had M keys
```

## Testing

Following codebase rules (TDD, 95% coverage target, scoped via `scripts/infra/dev/run_pytest_affected.py`).

### Unit tests (`scripts/tests/`)

| Test                                     | Asserts                                                                                                                                                             |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_db_schema_normalize_payloads.py`   | After Alembic upgrade: every new column exists with correct type + nullability; every generated column has correct expression                                       |
| `test_uw_analyze_writer_full_payload.py` | Given a sample `current` dict from a real `data/uw_analyze_history/AAPL/<file>.json`, `_archive_to_postgres` writes all sub-trees; generated columns reflect values |
| `test_uw_analyze_flow_alerts_trigger.py` | Insert with `flow_alerts=[{...}, {...}]` → 2 child rows                                                                                                             |
| `test_uw_analyze_gex_strikes_trigger.py` | Insert with `display.gex_by_strike=[{...}×3]` → 3 child rows                                                                                                        |
| `test_uw_flow_event_ticks_trigger.py`    | Insert with `daily_track={ts1, ts2}` → 2 ticks; UPDATE adding ts3 → 3 ticks (no dupes)                                                                              |
| `test_vcg_writer_postgres.py`            | Run VCG scanner against fixture → row in `vcg_series`, all generated cols populated                                                                                 |
| `test_gex_writer_dual.py`                | Run GEX scanner → both `scan_results` and `gex_snapshots` get rows                                                                                                  |
| `test_cri_generated_columns.py`          | Insert sample CRI payload → all 19 generated columns derive correct values; views return expected rows                                                              |

### Backfill tests

| Test                                  | Asserts                                                                                         |
| ------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `test_backfill_uw_analyze_history.py` | Given `tmp_path` with 3 fixture JSON files, script populates 3 rows; idempotent on re-run       |
| `test_backfill_vcg_history.py`        | Given fixture `vcg.json` with 5 history items, 6 total rows inserted (5 historical + 1 current) |
| `test_backfill_uw_api_stats.py`       | Given fixture history JSON, all buckets land with correct column-name remap                     |

### Out of scope for tests

End-to-end FastAPI tests (HTTP layer unchanged). Performance benchmarks (deal with it if/when it bites).

## Open items for the implementation plan

1. Phase 0 reconnaissance results: distinct `scan_results.scan_type` values, `uw_flow_events.initial` keys, `uw_flow_events.daily_track` shape (dict-keyed-by-ts vs array).
2. Exact key names inside `uw_analyze_snapshots.report.vrp` and `report.regime` confirmed via sample `SELECT`.
3. Whether any `scan_results` row count is large enough that an `ALTER TABLE ADD COLUMN GENERATED` lock could matter — if so, switch that table to `pg_repack` or batched migration.
4. Decision on per-scan-type tables for non-GEX scan_types — same template, but exact columns depend on Phase 0 enumeration.
