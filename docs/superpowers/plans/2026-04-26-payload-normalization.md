# Payload Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Break down opaque JSONB payloads in xenon's analytics tables into queryable typed columns (via Postgres `GENERATED` columns) and child tables (via fanout triggers), capture the full UW analyze payload that's currently being dropped, add a new `vcg_series` table, and backfill all three on-disk JSON archives into Postgres.

**Architecture:** JSONB stays as the single source of truth on each parent table. Typed scalar columns are `GENERATED ALWAYS AS (...) STORED` from the JSONB — Postgres rewrites the table to backfill them automatically when the column is added. 1-to-many array data fans out to child tables via `AFTER INSERT OR UPDATE` triggers. Single Alembic revision; backfill scripts run after `alembic upgrade head`. No production rollout gating (system is test/QA mode).

**Tech Stack:** Python 3.13 + uv, SQLAlchemy 2.x Core (`Table`, `Computed`), Alembic, Postgres 15+, pytest. Schema namespace is `xenon`. Spec: `docs/superpowers/specs/2026-04-26-payload-normalization-design.md`.

---

## File map

**Create:**

- `src/xenon/db/migrations/versions/<rev>_normalize_payloads.py` — single Alembic revision (DDL + triggers + replay)
- `scripts/migrations/2026_04_26_backfill_uw_analyze_history.py`
- `scripts/migrations/2026_04_26_backfill_vcg_history.py`
- `scripts/migrations/2026_04_26_backfill_uw_api_stats.py`
- `scripts/migrations/2026_04_26_verify_normalize_payloads.py`
- `scripts/tests/test_db_schema_normalize_payloads.py`
- `scripts/tests/test_cri_generated_columns.py`
- `scripts/tests/test_uw_analyze_writer_full_payload.py`
- `scripts/tests/test_uw_analyze_flow_alerts_trigger.py`
- `scripts/tests/test_uw_analyze_gex_strikes_trigger.py`
- `scripts/tests/test_uw_analyze_short_volume_trend_trigger.py`
- `scripts/tests/test_uw_flow_event_ticks_trigger.py`
- `scripts/tests/test_vcg_writer_postgres.py`
- `scripts/tests/test_gex_writer_dual.py`
- `scripts/tests/test_backfill_uw_analyze_history.py`
- `scripts/tests/test_backfill_vcg_history.py`
- `scripts/tests/test_backfill_uw_api_stats.py`

**Modify:**

- `src/xenon/db/schema.py` — restructure `uw_analyze_snapshots`, add new tables and generated columns
- `src/xenon/db/queries/uw.py` — `save_snapshot()` signature change
- `src/xenon/db/queries/scans.py` — add `save_gex_snapshot()`, `save_vcg_scan()`
- `src/xenon/api/services/uw_analyze_cache.py` — `_archive_to_postgres` writes the full `current` dict
- `src/xenon/scanners/vcg.py` — add Postgres write block
- `src/xenon/scanners/gex.py` — dual-write to `gex_snapshots`
- `scripts/tests/conftest.py` — extend truncate list with new tables

---

## Task 0: Phase 0 reconnaissance

**Goal:** confirm exact JSONB key names so generated-column expressions and trigger DDL are correct on the first try.

**Files:**

- Output: append findings to this plan as a comment block at the bottom (or to a separate `recon-results.md` next to the spec)

- [ ] **Step 1: Run distinct scan_type query**

```bash
psql -h localhost -U xenon_app xenon_db -c "SELECT scan_type, COUNT(*) FROM xenon.scan_results GROUP BY scan_type ORDER BY 2 DESC;"
```

Record the output. The plan only details `gex_snapshots`; if other types exist, the implementation needs an analogous per-type table for each (same template — see Task 9 for the GEX pattern).

- [ ] **Step 2: Sample uw_flow_events.initial keys**

```bash
psql -h localhost -U xenon_app xenon_db -c "SELECT jsonb_object_keys(initial) AS k, COUNT(*) FROM xenon.uw_flow_events GROUP BY k ORDER BY 2 DESC;"
```

Record which keys actually exist. Task 11's generated columns assume `premium_usd`, `size`, `dte`, `iv`, `spot`, `aggressor`. If actual keys differ (e.g. `premium` not `premium_usd`), update Task 11 step 1 expressions before generating the migration.

- [ ] **Step 3: Sample uw_flow_events.daily_track shape**

```bash
psql -h localhost -U xenon_app xenon_db -c "SELECT id, jsonb_typeof(daily_track), daily_track FROM xenon.uw_flow_events WHERE daily_track IS NOT NULL LIMIT 3;"
```

Determine if `daily_track` is `'object'` (dict keyed by ISO timestamp) or `'array'`. The trigger DDL in Task 11 step 4 assumes object form (`jsonb_each`). If array, change to `jsonb_array_elements` and extract `observed_at` from inside each element.

- [ ] **Step 4: Sample uw_analyze_snapshots existing JSONBs**

```bash
psql -h localhost -U xenon_app xenon_db -c "SELECT vrp_state, regime FROM xenon.uw_analyze_snapshots WHERE vrp_state IS NOT NULL LIMIT 2;"
```

These are about to be dropped, but the contents inform what existing rows look like vs the on-disk archive — backfill must reconcile both sources.

- [ ] **Step 5: Commit recon findings**

Append findings as `## Phase 0 Findings` at the end of this plan file (do not commit yet — combine with the recon comment in the next task's commit, or commit standalone if you prefer separate history). One commit:

```bash
git add docs/superpowers/plans/2026-04-26-payload-normalization.md
git commit -m "plan: add phase 0 reconnaissance findings for payload normalization"
```

---

## Task 1: Schema model — add new and restructured tables to `src/xenon/db/schema.py`

**Goal:** declare every new table and Computed column in SQLAlchemy. No migration yet; this is just the model that subsequent steps autogenerate from.

**Files:**

- Modify: `src/xenon/db/schema.py` (after line 343, before `# ---------- Caches ----------`)

- [ ] **Step 1: Add Computed import**

Modify the existing import block (`src/xenon/db/schema.py:3-18`) to add `Computed`:

```python
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Computed,           # NEW
    Date,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,       # NEW (used by vcg_series)
    Table,
    Text,
    UniqueConstraint,
    text,
)
```

- [ ] **Step 2: Restructure `uw_analyze_snapshots` (drop old JSONB cols, add new shape)**

Replace the existing definition at `src/xenon/db/schema.py:301-312` with:

```python
uw_analyze_snapshots = Table(
    "uw_analyze_snapshots",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("ticker", Text, nullable=False),
    Column("portfolio_score", Numeric(6, 2)),
    Column("snapshot_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    # New JSONB sub-trees from on-disk archive shape
    Column("report", JSONB),
    Column("display", JSONB),
    Column("derived", JSONB),
    Column("dark_pool_summary", JSONB),
    Column("options_flow_summary", JSONB),
    Column("flow_alerts", JSONB),
    Column("materialized_changes", JSONB),
    Column("report_fetched_at", TIMESTAMP(timezone=True)),
    Column("archived_at", TIMESTAMP(timezone=True)),
    # Generated columns from report.*
    Column("price", Numeric(12, 4), Computed("(report->>'price')::numeric", persisted=True)),
    Column("composite_score", Numeric(6, 2), Computed("((report->'scores')->>'composite')::numeric", persisted=True)),
    Column("flow_score", Numeric(6, 2), Computed("((report->'scores')->>'flow')::numeric", persisted=True)),
    Column("volatility_score", Numeric(6, 2), Computed("((report->'scores')->>'volatility')::numeric", persisted=True)),
    Column("market_structure_score", Numeric(6, 2), Computed("((report->'scores')->>'market_structure')::numeric", persisted=True)),
    Column("positioning_score", Numeric(6, 2), Computed("((report->'scores')->>'positioning')::numeric", persisted=True)),
    Column("grade", Text, Computed("(report->'scores')->>'grade'", persisted=True)),
    Column("bias", Text, Computed("(report->'scores')->>'bias'", persisted=True)),
    Column("mode", Text, Computed("(report->'scores')->>'mode'", persisted=True)),
    Column("reweighted", Boolean, Computed("((report->'scores')->>'reweighted')::boolean", persisted=True)),
    # vrp.*
    Column("vrp_raw", Numeric(8, 4), Computed("((report->'vrp')->>'vrp_raw')::numeric", persisted=True)),
    Column("vrp_zscore", Numeric(8, 4), Computed("((report->'vrp')->>'vrp_zscore')::numeric", persisted=True)),
    Column("iv_percentile", Numeric(6, 2), Computed("((report->'vrp')->>'iv_percentile')::numeric", persisted=True)),
    Column("ts_ratio", Numeric(8, 4), Computed("((report->'vrp')->>'ts_ratio')::numeric", persisted=True)),
    Column("ts_inverted", Boolean, Computed("((report->'vrp')->>'ts_inverted')::boolean", persisted=True)),
    Column("earnings_within_14d", Boolean, Computed("((report->'vrp')->>'earnings_within_14d')::boolean", persisted=True)),
    # regime.*
    Column("regime_label", Text, Computed("(report->'regime')->>'regime'", persisted=True)),
    Column("regime_reason", Text, Computed("(report->'regime')->>'reason'", persisted=True)),
    Column("gex_sign", Text, Computed("(report->'regime')->>'gex_sign'", persisted=True)),
    Column("gex_flip_relative", Text, Computed("(report->'regime')->>'gex_flip_relative'", persisted=True)),
    Column("flip_distance_pct", Numeric(8, 4), Computed("((report->'regime')->>'flip_distance_pct')::numeric", persisted=True)),
    # display.*
    Column("iv", Numeric(8, 4), Computed("(display->>'iv')::numeric", persisted=True)),
    Column("rv", Numeric(8, 4), Computed("(display->>'rv')::numeric", persisted=True)),
    Column("iv_rank", Numeric(6, 2), Computed("(display->>'iv_rank')::numeric", persisted=True)),
    Column("call_wall_strike", Numeric(12, 4), Computed("(display->>'call_wall_strike')::numeric", persisted=True)),
    Column("put_wall_strike", Numeric(12, 4), Computed("(display->>'put_wall_strike')::numeric", persisted=True)),
    Column("gamma_per_1pct", Numeric(18, 4), Computed("(display->>'gamma_per_1pct')::numeric", persisted=True)),
    Column("net_call_premium", Numeric(18, 2), Computed("(display->>'net_call_premium')::numeric", persisted=True)),
    Column("net_put_premium", Numeric(18, 2), Computed("(display->>'net_put_premium')::numeric", persisted=True)),
    Column("short_volume_ratio", Numeric(6, 4), Computed("(display->>'short_volume_ratio')::numeric", persisted=True)),
    Column("term_structure_label", Text, Computed("display->>'term_structure_label'", persisted=True)),
    Column("max_pain", Numeric(12, 4), Computed("(display->>'max_pain')::numeric", persisted=True)),
    # derived.*
    Column("derived_gex_sign", Text, Computed("derived->>'gex_sign'", persisted=True)),
    Column("derived_call_wall", Numeric(12, 4), Computed("(derived->>'call_wall')::numeric", persisted=True)),
    Column("derived_put_wall", Numeric(12, 4), Computed("(derived->>'put_wall')::numeric", persisted=True)),
    Column("derived_max_pain", Numeric(12, 4), Computed("(derived->>'max_pain')::numeric", persisted=True)),
    Column("derived_spot", Numeric(12, 4), Computed("(derived->>'spot')::numeric", persisted=True)),
    # dark_pool_summary.*
    Column("dp_score", Numeric(8, 4), Computed("(dark_pool_summary->>'score')::numeric", persisted=True)),
    Column("dp_signal", Text, Computed("dark_pool_summary->>'signal'", persisted=True)),
    Column("dp_direction", Text, Computed("dark_pool_summary->>'direction'", persisted=True)),
    Column("dp_strength", Integer, Computed("(dark_pool_summary->>'strength')::int", persisted=True)),
    Column("dp_buy_ratio", Numeric(6, 4), Computed("(dark_pool_summary->>'buy_ratio')::numeric", persisted=True)),
    Column("dp_options_conflict", Boolean, Computed("(dark_pool_summary->>'options_conflict')::boolean", persisted=True)),
    Column("dp_num_prints", Integer, Computed("(dark_pool_summary->>'num_prints')::int", persisted=True)),
    Column("dp_sustained_days", Integer, Computed("(dark_pool_summary->>'sustained_days')::int", persisted=True)),
    # options_flow_summary.*
    Column("of_total_alerts", Integer, Computed("(options_flow_summary->>'total_alerts')::int", persisted=True)),
    Column("of_total_premium", Numeric(18, 2), Computed("(options_flow_summary->>'total_premium')::numeric", persisted=True)),
    Column("of_call_premium", Numeric(18, 2), Computed("(options_flow_summary->>'call_premium')::numeric", persisted=True)),
    Column("of_put_premium", Numeric(18, 2), Computed("(options_flow_summary->>'put_premium')::numeric", persisted=True)),
    Column("of_call_put_ratio", Numeric(8, 4), Computed("(options_flow_summary->>'call_put_ratio')::numeric", persisted=True)),
    Column("of_bias", Text, Computed("options_flow_summary->>'bias'", persisted=True)),
    # benchmark.* (nested in report)
    Column("spy_iv_rank", Numeric(6, 2), Computed("((report->'benchmark'->'spy')->>'iv_rank')::numeric", persisted=True)),
    Column("spy_gex_regime", Text, Computed("(report->'benchmark'->'spy')->>'gex_regime'", persisted=True)),
    Column("sector_etf_ticker", Text, Computed("(report->'benchmark'->'sector_etf')->>'ticker'", persisted=True)),
    Column("sector_etf_iv_rank", Numeric(6, 2), Computed("((report->'benchmark'->'sector_etf')->>'iv_rank')::numeric", persisted=True)),
    Column("sector_etf_gex_regime", Text, Computed("(report->'benchmark'->'sector_etf')->>'gex_regime'", persisted=True)),
    Index("ix_uw_analyze_snap_ticker_time", "ticker", "snapshot_at"),
    Index("ix_uw_analyze_snap_time_gex", "snapshot_at", "gex_sign"),
    Index("ix_uw_analyze_snap_time_regime", "snapshot_at", "regime_label"),
    Index("ix_uw_analyze_snap_time_bias", "snapshot_at", "bias"),
)
```

- [ ] **Step 3: Add CRI generated columns by extending `cri_series`**

Replace `src/xenon/db/schema.py:289-297`:

```python
cri_series = Table(
    "cri_series",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("cri_level", Numeric(8, 4), nullable=False),
    Column("alert", Boolean, server_default=text("false")),
    Column("payload", JSONB),
    Column("recorded_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    # Generated columns from payload (src/xenon/scanners/cri.py:920-941)
    Column("recorded_date", Date, Computed("(payload->>'date')::date", persisted=True)),
    Column("vix", Numeric(8, 4), Computed("(payload->>'vix')::numeric", persisted=True)),
    Column("vvix", Numeric(8, 4), Computed("(payload->>'vvix')::numeric", persisted=True)),
    Column("spy", Numeric(10, 4), Computed("(payload->>'spy')::numeric", persisted=True)),
    Column("vix_5d_roc", Numeric(8, 4), Computed("(payload->>'vix_5d_roc')::numeric", persisted=True)),
    Column("vvix_vix_ratio", Numeric(8, 4), Computed("(payload->>'vvix_vix_ratio')::numeric", persisted=True)),
    Column("spx_100d_ma", Numeric(10, 4), Computed("(payload->>'spx_100d_ma')::numeric", persisted=True)),
    Column("spx_distance_pct", Numeric(8, 4), Computed("(payload->>'spx_distance_pct')::numeric", persisted=True)),
    Column("cor1m", Numeric(6, 4), Computed("(payload->>'cor1m')::numeric", persisted=True)),
    Column("cor1m_previous_close", Numeric(6, 4), Computed("(payload->>'cor1m_previous_close')::numeric", persisted=True)),
    Column("cor1m_5d_change", Numeric(6, 4), Computed("(payload->>'cor1m_5d_change')::numeric", persisted=True)),
    Column("realized_vol", Numeric(8, 4), Computed("(payload->>'realized_vol')::numeric", persisted=True)),
    Column("cri_score", Numeric(8, 4), Computed("((payload->'cri')->>'score')::numeric", persisted=True)),
    Column("cri_components", JSONB, Computed("payload->'cri'->'components'", persisted=True)),
    Column("cta_exposure_pct", Numeric(6, 2), Computed("((payload->'cta')->>'exposure_pct')::numeric", persisted=True)),
    Column("cta_forced_reduction", Boolean, Computed("((payload->'cta')->>'forced_reduction')::boolean", persisted=True)),
    Column("cta_selling_usd_b", Numeric(8, 2), Computed("((payload->'cta')->>'selling_usd_b')::numeric", persisted=True)),
    Column("menthorq_cta_score", Numeric(8, 4), Computed("((payload->'menthorq_cta')->>'score')::numeric", persisted=True)),
    Column("crash_trigger_fired", Boolean, Computed("((payload->'crash_trigger')->>'fired')::boolean", persisted=True)),
    Index("ix_cri_recorded_date", "recorded_date"),
    Index("ix_cri_crash_trigger", "crash_trigger_fired", postgresql_where=text("crash_trigger_fired")),
    Index("ix_cri_cta_forced", "cta_forced_reduction", postgresql_where=text("cta_forced_reduction")),
)
```

- [ ] **Step 4: Add `vcg_series` table**

Insert after `cri_series` (before `# ---------- UW Analysis ----------`):

```python
vcg_series = Table(
    "vcg_series",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("scanned_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Column("market_open", Boolean),
    Column("credit_proxy", Text),
    Column("payload", JSONB, nullable=False),
    # signal.*
    Column("vcg", Numeric(10, 6), Computed("((payload->'signal')->>'vcg')::numeric", persisted=True)),
    Column("vcg_adj", Numeric(10, 6), Computed("((payload->'signal')->>'vcg_adj')::numeric", persisted=True)),
    Column("residual", Numeric(12, 8), Computed("((payload->'signal')->>'residual')::numeric", persisted=True)),
    Column("beta1_vvix", Numeric(12, 8), Computed("((payload->'signal')->>'beta1_vvix')::numeric", persisted=True)),
    Column("beta2_vix", Numeric(12, 8), Computed("((payload->'signal')->>'beta2_vix')::numeric", persisted=True)),
    Column("alpha", Numeric(12, 8), Computed("((payload->'signal')->>'alpha')::numeric", persisted=True)),
    Column("vix", Numeric(8, 4), Computed("((payload->'signal')->>'vix')::numeric", persisted=True)),
    Column("vvix", Numeric(8, 4), Computed("((payload->'signal')->>'vvix')::numeric", persisted=True)),
    Column("credit_price", Numeric(10, 4), Computed("((payload->'signal')->>'credit_price')::numeric", persisted=True)),
    Column("credit_5d_return_pct", Numeric(8, 4), Computed("((payload->'signal')->>'credit_5d_return_pct')::numeric", persisted=True)),
    Column("ro", SmallInteger, Computed("((payload->'signal')->>'ro')::int", persisted=True)),
    Column("edr", SmallInteger, Computed("((payload->'signal')->>'edr')::int", persisted=True)),
    Column("tier", SmallInteger, Computed("((payload->'signal')->>'tier')::int", persisted=True)),
    Column("bounce", SmallInteger, Computed("((payload->'signal')->>'bounce')::int", persisted=True)),
    Column("vvix_severity", Text, Computed("(payload->'signal')->>'vvix_severity'", persisted=True)),
    Column("sign_ok", Boolean, Computed("((payload->'signal')->>'sign_ok')::boolean", persisted=True)),
    Column("sign_suppressed", Boolean, Computed("((payload->'signal')->>'sign_suppressed')::boolean", persisted=True)),
    Column("pi_panic", Numeric(8, 4), Computed("((payload->'signal')->>'pi_panic')::numeric", persisted=True)),
    Column("regime", Text, Computed("(payload->'signal')->>'regime'", persisted=True)),
    Column("interpretation", Text, Computed("(payload->'signal')->>'interpretation'", persisted=True)),
    # attribution
    Column("attr_vvix_pct", Numeric(6, 2), Computed("((payload->'signal'->'attribution')->>'vvix_pct')::numeric", persisted=True)),
    Column("attr_vix_pct", Numeric(6, 2), Computed("((payload->'signal'->'attribution')->>'vix_pct')::numeric", persisted=True)),
    Column("attr_vvix_component", Numeric(12, 8), Computed("((payload->'signal'->'attribution')->>'vvix_component')::numeric", persisted=True)),
    Column("attr_vix_component", Numeric(12, 8), Computed("((payload->'signal'->'attribution')->>'vix_component')::numeric", persisted=True)),
    Column("attr_model_implied", Numeric(12, 8), Computed("((payload->'signal'->'attribution')->>'model_implied')::numeric", persisted=True)),
    Index("ix_vcg_scanned_at", "scanned_at"),
    Index("ix_vcg_regime", "regime"),
    Index("ix_vcg_tier", "tier", postgresql_where=text("tier IS NOT NULL")),
)
```

- [ ] **Step 5: Add `gex_snapshots` table**

Insert next to `scan_results`:

```python
gex_snapshots = Table(
    "gex_snapshots",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("ticker", Text, nullable=False),
    Column("data_date", Date),
    Column("scanned_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Column("payload", JSONB, nullable=False),
    Column("spot", Numeric(12, 4), Computed("(payload->>'spot')::numeric", persisted=True)),
    Column("net_gex", Numeric(14, 2), Computed("(payload->>'net_gex')::numeric", persisted=True)),
    Column("net_dex", Numeric(14, 2), Computed("(payload->>'net_dex')::numeric", persisted=True)),
    Column("vol_pc", Numeric(8, 4), Computed("(payload->>'vol_pc')::numeric", persisted=True)),
    Column("iv_30d", Numeric(6, 4), Computed("((payload->'iv')->>'iv30d')::numeric", persisted=True)),
    Column("iv_rank", Numeric(6, 2), Computed("((payload->'iv')->>'iv_rank')::numeric", persisted=True)),
    Column("hv_30d", Numeric(6, 4), Computed("((payload->'iv')->>'hv30')::numeric", persisted=True)),
    Column("mq_iv_30d", Numeric(6, 4), Computed("((payload->'iv')->>'mq_iv30d')::numeric", persisted=True)),
    Column("level_max_magnet", Numeric(12, 4), Computed("((payload->'levels')->>'max_magnet')::numeric", persisted=True)),
    Column("level_second_magnet", Numeric(12, 4), Computed("((payload->'levels')->>'second_magnet')::numeric", persisted=True)),
    Column("level_max_accelerator", Numeric(12, 4), Computed("((payload->'levels')->>'max_accelerator')::numeric", persisted=True)),
    Column("level_put_wall", Numeric(12, 4), Computed("((payload->'levels')->>'put_wall')::numeric", persisted=True)),
    Index("ix_gex_ticker_time", "ticker", "scanned_at"),
    Index("ix_gex_scanned_at", "scanned_at"),
    Index("ix_gex_data_date", "data_date"),
)
```

- [ ] **Step 6: Add `uw_flow_events` generated columns**

Replace `src/xenon/db/schema.py:314-329`:

```python
uw_flow_events = Table(
    "uw_flow_events",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("flow_event_key", Text, nullable=False, unique=True),
    Column("ticker", Text, nullable=False),
    Column("side", Text),
    Column("strike", Numeric(12, 2)),
    Column("expiry", Date),
    Column("detected_at", TIMESTAMP(timezone=True), nullable=False),
    Column("initial", JSONB, nullable=False),
    Column("daily_track", JSONB),
    Column("status", Text, nullable=False),
    Column("anomaly_reason", Text),
    Column("closed_at", TIMESTAMP(timezone=True)),
    # Generated from initial.* — exact key names confirmed in Phase 0
    Column("initial_premium_usd", Numeric(14, 2), Computed("(initial->>'premium_usd')::numeric", persisted=True)),
    Column("initial_size", Integer, Computed("(initial->>'size')::int", persisted=True)),
    Column("initial_dte", Integer, Computed("(initial->>'dte')::int", persisted=True)),
    Column("initial_iv", Numeric(6, 4), Computed("(initial->>'iv')::numeric", persisted=True)),
    Column("initial_spot", Numeric(12, 4), Computed("(initial->>'spot')::numeric", persisted=True)),
    Column("initial_aggressor", Text, Computed("initial->>'aggressor'", persisted=True)),
)
```

- [ ] **Step 7: Add child tables (flow_alerts, gex_strikes, short_volume_trend, flow_event_ticks)**

Insert after `uw_flow_events`:

```python
uw_analyze_flow_alerts = Table(
    "uw_analyze_flow_alerts",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("snapshot_id", BigInteger, ForeignKey("xenon.uw_analyze_snapshots.id", ondelete="CASCADE"), nullable=False),
    Column("ticker", Text, nullable=False),
    Column("snapshot_at", TIMESTAMP(timezone=True), nullable=False),
    Column("alert_type", Text),
    Column("alert_severity", Text),
    Column("alert_payload", JSONB, nullable=False),
    Index("ix_uw_flow_alerts_snapshot", "snapshot_id"),
    Index("ix_uw_flow_alerts_ticker_time", "ticker", "snapshot_at"),
)

uw_analyze_gex_strikes = Table(
    "uw_analyze_gex_strikes",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("snapshot_id", BigInteger, ForeignKey("xenon.uw_analyze_snapshots.id", ondelete="CASCADE"), nullable=False),
    Column("ticker", Text, nullable=False),
    Column("snapshot_at", TIMESTAMP(timezone=True), nullable=False),
    Column("strike", Numeric(12, 4), nullable=False),
    Column("call_gamma", Numeric(14, 4)),
    Column("put_gamma", Numeric(14, 4)),
    Column("net_gamma", Numeric(14, 4)),
    Column("distance_pct", Numeric(10, 6)),
    Column("is_call_wall", Boolean),
    Column("is_put_wall", Boolean),
    Index("ix_uw_gex_strikes_snapshot", "snapshot_id"),
    Index("ix_uw_gex_strikes_ticker_time", "ticker", "snapshot_at"),
)

uw_analyze_short_volume_trend = Table(
    "uw_analyze_short_volume_trend",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("snapshot_id", BigInteger, ForeignKey("xenon.uw_analyze_snapshots.id", ondelete="CASCADE"), nullable=False),
    Column("ticker", Text, nullable=False),
    Column("snapshot_at", TIMESTAMP(timezone=True), nullable=False),
    Column("position_in_trend", Integer, nullable=False),
    Column("ratio", Numeric(8, 6)),
    Index("ix_uw_short_vol_snapshot", "snapshot_id"),
)

uw_flow_event_ticks = Table(
    "uw_flow_event_ticks",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("event_id", BigInteger, ForeignKey("xenon.uw_flow_events.id", ondelete="CASCADE"), nullable=False),
    Column("flow_event_key", Text, nullable=False),
    Column("observed_at", TIMESTAMP(timezone=True), nullable=False),
    Column("spot", Numeric(12, 4)),
    Column("bid", Numeric(10, 4)),
    Column("ask", Numeric(10, 4)),
    Column("mark", Numeric(10, 4)),
    Column("oi", Integer),
    Column("volume", Integer),
    Column("iv", Numeric(6, 4)),
    Column("tick_payload", JSONB, nullable=False),
    UniqueConstraint("event_id", "observed_at", name="uq_uw_flow_event_ticks"),
    Index("ix_uw_flow_event_ticks_event_time", "event_id", "observed_at"),
    Index("ix_uw_flow_event_ticks_observed_at", "observed_at"),
)
```

- [ ] **Step 8: Verify schema imports cleanly**

Run:

```bash
uv run python -c "from xenon.db.schema import vcg_series, gex_snapshots, uw_analyze_flow_alerts, uw_analyze_gex_strikes, uw_analyze_short_volume_trend, uw_flow_event_ticks, uw_analyze_snapshots; print('ok'); print('uw_analyze cols:', len(uw_analyze_snapshots.c))"
```

Expected: prints `ok` and `uw_analyze cols: 60+`.

- [ ] **Step 9: Commit schema model**

```bash
git add src/xenon/db/schema.py
git commit -m "schema: add normalized payload tables and generated columns"
```

---

## Task 2: Generate and edit Alembic migration

**Goal:** produce one migration file that creates everything Task 1 declared, plus the four PL/pgSQL fanout triggers, plus a backfill replay step that fires triggers over existing rows.

**Files:**

- Create: `src/xenon/db/migrations/versions/<rev>_normalize_payloads.py`

- [ ] **Step 1: Autogenerate the migration**

```bash
uv run alembic revision --autogenerate -m "normalize_payloads"
```

This creates a new file under `src/xenon/db/migrations/versions/`. Note its revision ID and confirm `down_revision = "eaec7f146df5"`. The autogenerated body will contain the `op.drop_column` for old uw_analyze cols, `op.add_column` for new ones with `Computed`, and `op.create_table` for new tables. **Inspect it carefully** — autogenerate sometimes misses `Computed` clauses or generates them with wrong syntax.

- [ ] **Step 2: Manually verify drop columns are present**

Open the new migration. The `upgrade()` function should contain:

```python
op.drop_column("uw_analyze_snapshots", "vrp_state", schema="xenon")
op.drop_column("uw_analyze_snapshots", "regime", schema="xenon")
op.drop_column("uw_analyze_snapshots", "flow_signals", schema="xenon")
```

If missing, add them at the top of `upgrade()` before any `add_column` calls.

- [ ] **Step 3: Append trigger DDL to upgrade()**

After the autogenerated body of `upgrade()`, append:

```python
    # ===== Trigger 1: uw_analyze_flow_alerts fanout =====
    op.execute("""
    CREATE OR REPLACE FUNCTION xenon.fanout_uw_analyze_flow_alerts() RETURNS TRIGGER AS $$
    DECLARE alert jsonb;
    BEGIN
      IF NEW.flow_alerts IS NULL OR jsonb_typeof(NEW.flow_alerts) <> 'array' THEN
        RETURN NEW;
      END IF;
      DELETE FROM xenon.uw_analyze_flow_alerts WHERE snapshot_id = NEW.id;
      FOR alert IN SELECT * FROM jsonb_array_elements(NEW.flow_alerts) LOOP
        INSERT INTO xenon.uw_analyze_flow_alerts (
          snapshot_id, ticker, snapshot_at, alert_type, alert_severity, alert_payload
        ) VALUES (
          NEW.id, NEW.ticker, NEW.snapshot_at,
          alert->>'type', alert->>'severity', alert
        );
      END LOOP;
      RETURN NEW;
    END $$ LANGUAGE plpgsql;
    """)
    op.execute("""
    CREATE TRIGGER trg_uw_analyze_flow_alerts_fanout
    AFTER INSERT OR UPDATE OF flow_alerts ON xenon.uw_analyze_snapshots
    FOR EACH ROW EXECUTE FUNCTION xenon.fanout_uw_analyze_flow_alerts();
    """)

    # ===== Trigger 2: uw_analyze_gex_strikes fanout =====
    op.execute("""
    CREATE OR REPLACE FUNCTION xenon.fanout_uw_analyze_gex_strikes() RETURNS TRIGGER AS $$
    DECLARE strike_row jsonb;
    BEGIN
      IF NEW.display IS NULL OR (NEW.display->'gex_by_strike') IS NULL THEN
        RETURN NEW;
      END IF;
      IF jsonb_typeof(NEW.display->'gex_by_strike') <> 'array' THEN
        RETURN NEW;
      END IF;
      DELETE FROM xenon.uw_analyze_gex_strikes WHERE snapshot_id = NEW.id;
      FOR strike_row IN SELECT * FROM jsonb_array_elements(NEW.display->'gex_by_strike') LOOP
        INSERT INTO xenon.uw_analyze_gex_strikes (
          snapshot_id, ticker, snapshot_at, strike,
          call_gamma, put_gamma, net_gamma, distance_pct,
          is_call_wall, is_put_wall
        ) VALUES (
          NEW.id, NEW.ticker, NEW.snapshot_at,
          (strike_row->>'strike')::numeric,
          (strike_row->>'call_gamma')::numeric,
          (strike_row->>'put_gamma')::numeric,
          (strike_row->>'net_gamma')::numeric,
          (strike_row->>'distance_pct')::numeric,
          (strike_row->>'is_call_wall')::boolean,
          (strike_row->>'is_put_wall')::boolean
        );
      END LOOP;
      RETURN NEW;
    END $$ LANGUAGE plpgsql;
    """)
    op.execute("""
    CREATE TRIGGER trg_uw_analyze_gex_strikes_fanout
    AFTER INSERT OR UPDATE OF display ON xenon.uw_analyze_snapshots
    FOR EACH ROW EXECUTE FUNCTION xenon.fanout_uw_analyze_gex_strikes();
    """)

    # ===== Trigger 3: uw_analyze_short_volume_trend fanout =====
    op.execute("""
    CREATE OR REPLACE FUNCTION xenon.fanout_uw_analyze_short_volume_trend() RETURNS TRIGGER AS $$
    DECLARE
      ratio_val jsonb;
      pos int := 0;
    BEGIN
      IF NEW.display IS NULL OR (NEW.display->'short_volume_trend') IS NULL THEN
        RETURN NEW;
      END IF;
      IF jsonb_typeof(NEW.display->'short_volume_trend') <> 'array' THEN
        RETURN NEW;
      END IF;
      DELETE FROM xenon.uw_analyze_short_volume_trend WHERE snapshot_id = NEW.id;
      FOR ratio_val IN SELECT * FROM jsonb_array_elements(NEW.display->'short_volume_trend') LOOP
        INSERT INTO xenon.uw_analyze_short_volume_trend (
          snapshot_id, ticker, snapshot_at, position_in_trend, ratio
        ) VALUES (
          NEW.id, NEW.ticker, NEW.snapshot_at, pos, (ratio_val#>>'{}')::numeric
        );
        pos := pos + 1;
      END LOOP;
      RETURN NEW;
    END $$ LANGUAGE plpgsql;
    """)
    op.execute("""
    CREATE TRIGGER trg_uw_analyze_short_volume_trend_fanout
    AFTER INSERT OR UPDATE OF display ON xenon.uw_analyze_snapshots
    FOR EACH ROW EXECUTE FUNCTION xenon.fanout_uw_analyze_short_volume_trend();
    """)

    # ===== Trigger 4: uw_flow_event_ticks fanout =====
    # Assumes daily_track is a dict keyed by ISO timestamp.
    # If Phase 0 step 3 found it's an array instead, swap jsonb_each for
    # jsonb_array_elements and pull observed_at from inside each element.
    op.execute("""
    CREATE OR REPLACE FUNCTION xenon.fanout_uw_flow_event_ticks() RETURNS TRIGGER AS $$
    DECLARE k text; v jsonb;
    BEGIN
      IF NEW.daily_track IS NULL OR jsonb_typeof(NEW.daily_track) <> 'object' THEN
        RETURN NEW;
      END IF;
      FOR k, v IN SELECT key, value FROM jsonb_each(NEW.daily_track) LOOP
        INSERT INTO xenon.uw_flow_event_ticks (
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
    """)
    op.execute("""
    CREATE TRIGGER trg_uw_flow_event_ticks_fanout
    AFTER INSERT OR UPDATE OF daily_track ON xenon.uw_flow_events
    FOR EACH ROW EXECUTE FUNCTION xenon.fanout_uw_flow_event_ticks();
    """)

    # ===== Backfill: replay triggers over existing rows =====
    op.execute("UPDATE xenon.uw_analyze_snapshots SET id = id;")
    op.execute("UPDATE xenon.uw_flow_events SET id = id;")
```

- [ ] **Step 4: Append trigger drops to downgrade()**

At the **top** of `downgrade()` (before the autogenerated drop_column / drop_table calls), prepend:

```python
    op.execute("DROP TRIGGER IF EXISTS trg_uw_flow_event_ticks_fanout ON xenon.uw_flow_events;")
    op.execute("DROP FUNCTION IF EXISTS xenon.fanout_uw_flow_event_ticks();")
    op.execute("DROP TRIGGER IF EXISTS trg_uw_analyze_short_volume_trend_fanout ON xenon.uw_analyze_snapshots;")
    op.execute("DROP FUNCTION IF EXISTS xenon.fanout_uw_analyze_short_volume_trend();")
    op.execute("DROP TRIGGER IF EXISTS trg_uw_analyze_gex_strikes_fanout ON xenon.uw_analyze_snapshots;")
    op.execute("DROP FUNCTION IF EXISTS xenon.fanout_uw_analyze_gex_strikes();")
    op.execute("DROP TRIGGER IF EXISTS trg_uw_analyze_flow_alerts_fanout ON xenon.uw_analyze_snapshots;")
    op.execute("DROP FUNCTION IF EXISTS xenon.fanout_uw_analyze_flow_alerts();")
```

The downgrade also needs to re-add the dropped uw_analyze columns. Confirm autogenerate produced these in `downgrade()`:

```python
op.add_column("uw_analyze_snapshots", sa.Column("vrp_state", postgresql.JSONB), schema="xenon")
op.add_column("uw_analyze_snapshots", sa.Column("regime", postgresql.JSONB), schema="xenon")
op.add_column("uw_analyze_snapshots", sa.Column("flow_signals", postgresql.JSONB), schema="xenon")
```

If missing, add them.

- [ ] **Step 5: Run upgrade against the test DB**

```bash
DATABASE_URL=postgresql+psycopg://xenon_app:xenon_dev@localhost:5432/xenon_test uv run alembic upgrade head
```

Expected: no errors. The migration may take a few seconds because adding STORED generated columns rewrites tables.

- [ ] **Step 6: Sanity check tables exist**

```bash
psql -h localhost -U xenon_app xenon_test -c "\dt xenon.*" | grep -E "vcg_series|gex_snapshots|uw_analyze_flow_alerts|uw_analyze_gex_strikes|uw_analyze_short_volume_trend|uw_flow_event_ticks"
```

Expected: all 6 new tables listed.

- [ ] **Step 7: Sanity check uw_analyze_snapshots column count**

```bash
psql -h localhost -U xenon_app xenon_test -c "SELECT count(*) FROM information_schema.columns WHERE table_schema='xenon' AND table_name='uw_analyze_snapshots';"
```

Expected: ≥ 60 (4 base + 9 new JSONB + ~50 generated).

- [ ] **Step 8: Test downgrade then re-upgrade**

```bash
DATABASE_URL=postgresql+psycopg://xenon_app:xenon_dev@localhost:5432/xenon_test uv run alembic downgrade -1
DATABASE_URL=postgresql+psycopg://xenon_app:xenon_dev@localhost:5432/xenon_test uv run alembic upgrade head
```

Both should succeed. This proves the downgrade is correct.

- [ ] **Step 9: Commit**

```bash
git add src/xenon/db/migrations/versions/
git commit -m "alembic: normalize_payloads — generated cols + child tables + triggers"
```

---

## Task 3: Update conftest truncate list

**Goal:** add the new tables to the test fixture's truncate list so tests start clean.

**Files:**

- Modify: `scripts/tests/conftest.py:35-53`

- [ ] **Step 1: Edit the table tuple**

Replace the table tuple in `_truncate_postgres_tables()` with:

```python
            for table in (
                "events.outbox",
                "xenon.order_events",
                "xenon.order_submissions",
                "xenon.wizard_protection",
                "xenon.wizard_events",
                "xenon.wizard_combo_attempts",
                "xenon.wizard_sessions",
                "xenon.uw_flow_event_ticks",
                "xenon.uw_flow_events",
                "xenon.uw_api_stats",
                "xenon.uw_analyze_flow_alerts",
                "xenon.uw_analyze_gex_strikes",
                "xenon.uw_analyze_short_volume_trend",
                "xenon.uw_analyze_snapshots",
                "xenon.positions",
                "xenon.account_snapshots",
                "xenon.trades",
                "xenon.nav_history",
                "xenon.gex_snapshots",
                "xenon.scan_results",
                "xenon.vcg_series",
                "xenon.cri_series",
                "xenon.ticker_cache",
            ):
```

(Order matters: child tables first, parents second. CASCADE handles FK refs but explicit order is clearer.)

- [ ] **Step 2: Smoke-test by running an existing test**

```bash
uv run pytest scripts/tests/test_combo_wizard_protect.py -x -q
```

Expected: passes (no regression).

- [ ] **Step 3: Commit**

```bash
git add scripts/tests/conftest.py
git commit -m "test(conftest): truncate new normalize_payloads tables"
```

---

## Task 4: Test — schema sanity

**Goal:** assert that all new columns/tables exist with correct types after migration.

**Files:**

- Create: `scripts/tests/test_db_schema_normalize_payloads.py`

- [ ] **Step 1: Write the test**

```python
"""Verify normalize_payloads migration produced the expected schema."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from scripts.tests.conftest import _sync_test_db_url


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


def _columns(engine, table: str) -> dict[str, dict]:
    insp = inspect(engine)
    return {c["name"]: c for c in insp.get_columns(table, schema="xenon")}


def test_uw_analyze_snapshots_has_new_jsonb_columns(engine):
    cols = _columns(engine, "uw_analyze_snapshots")
    for name in (
        "report", "display", "derived", "dark_pool_summary",
        "options_flow_summary", "flow_alerts", "materialized_changes",
        "report_fetched_at", "archived_at",
    ):
        assert name in cols, f"missing column {name}"


def test_uw_analyze_snapshots_dropped_old_jsonb_columns(engine):
    cols = _columns(engine, "uw_analyze_snapshots")
    for name in ("vrp_state", "regime", "flow_signals"):
        assert name not in cols, f"old column {name} should be dropped"


def test_uw_analyze_snapshots_has_generated_columns(engine):
    cols = _columns(engine, "uw_analyze_snapshots")
    for name in (
        "price", "composite_score", "grade", "bias",
        "vrp_raw", "regime_label", "gex_sign",
        "iv_rank", "call_wall_strike",
        "dp_score", "dp_signal",
        "of_total_alerts",
        "spy_iv_rank",
    ):
        assert name in cols, f"missing generated column {name}"


def test_cri_series_has_generated_columns(engine):
    cols = _columns(engine, "cri_series")
    for name in (
        "recorded_date", "vix", "vvix", "spy",
        "cri_score", "cri_components",
        "cta_exposure_pct", "cta_forced_reduction",
        "menthorq_cta_score", "crash_trigger_fired",
    ):
        assert name in cols


def test_vcg_series_table_exists(engine):
    insp = inspect(engine)
    assert "vcg_series" in insp.get_table_names(schema="xenon")
    cols = _columns(engine, "vcg_series")
    for name in (
        "scanned_at", "market_open", "credit_proxy", "payload",
        "vcg", "vcg_adj", "residual", "regime", "interpretation",
        "attr_vvix_pct", "attr_model_implied",
    ):
        assert name in cols


def test_gex_snapshots_table_exists(engine):
    insp = inspect(engine)
    assert "gex_snapshots" in insp.get_table_names(schema="xenon")
    cols = _columns(engine, "gex_snapshots")
    for name in (
        "ticker", "data_date", "scanned_at", "payload",
        "spot", "net_gex", "iv_30d", "level_max_magnet",
    ):
        assert name in cols


def test_child_tables_exist(engine):
    insp = inspect(engine)
    names = set(insp.get_table_names(schema="xenon"))
    for t in (
        "uw_analyze_flow_alerts",
        "uw_analyze_gex_strikes",
        "uw_analyze_short_volume_trend",
        "uw_flow_event_ticks",
    ):
        assert t in names


def test_uw_flow_events_has_initial_generated_columns(engine):
    cols = _columns(engine, "uw_flow_events")
    for name in (
        "initial_premium_usd", "initial_size", "initial_dte",
        "initial_iv", "initial_spot", "initial_aggressor",
    ):
        assert name in cols


def test_triggers_exist(engine):
    with engine.begin() as conn:
        result = conn.execute(text("""
            SELECT trigger_name FROM information_schema.triggers
            WHERE trigger_schema = 'xenon'
              AND trigger_name LIKE 'trg_%_fanout'
        """)).scalars().all()
    expected = {
        "trg_uw_analyze_flow_alerts_fanout",
        "trg_uw_analyze_gex_strikes_fanout",
        "trg_uw_analyze_short_volume_trend_fanout",
        "trg_uw_flow_event_ticks_fanout",
    }
    assert expected.issubset(set(result)), f"missing: {expected - set(result)}"
```

- [ ] **Step 2: Run the test**

```bash
uv run pytest scripts/tests/test_db_schema_normalize_payloads.py -xvs
```

Expected: all 8 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add scripts/tests/test_db_schema_normalize_payloads.py
git commit -m "test(schema): verify normalize_payloads migration shape"
```

---

## Task 5: Test — CRI generated columns derive correctly

**Goal:** prove that inserting a CRI payload causes the generated columns to populate.

**Files:**

- Create: `scripts/tests/test_cri_generated_columns.py`

- [ ] **Step 1: Write the test**

```python
"""CRI generated columns derive from payload."""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, insert, select, text

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.schema import cri_series


@pytest.fixture
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


def test_cri_payload_extracts_to_columns(engine):
    payload = {
        "date": "2026-04-08",
        "vix": 18.87,
        "vvix": 98.15,
        "spy": 520.0,
        "vix_5d_roc": 1.2,
        "vvix_vix_ratio": 5.20,
        "spx_100d_ma": 510.0,
        "spx_distance_pct": 1.96,
        "cor1m": 0.42,
        "cor1m_previous_close": 0.40,
        "cor1m_5d_change": 0.05,
        "realized_vol": 14.5,
        "cri": {"score": 12.3, "components": {"vix_z": 0.3, "spy_z": -0.1}},
        "cta": {"exposure_pct": 87.5, "forced_reduction": False, "selling_usd_b": 0.0},
        "menthorq_cta": {"score": 5.1},
        "crash_trigger": {"fired": False, "conditions": {}},
        "history": [],
        "spy_closes": [],
    }
    with engine.begin() as conn:
        conn.execute(insert(cri_series).values(
            cri_level=Decimal("12.3"),
            alert=False,
            payload=payload,
        ))
        row = conn.execute(select(cri_series)).first()
    assert row.recorded_date.isoformat() == "2026-04-08"
    assert float(row.vix) == 18.87
    assert float(row.vvix) == 98.15
    assert float(row.cri_score) == 12.3
    assert row.cri_components == {"vix_z": 0.3, "spy_z": -0.1}
    assert float(row.cta_exposure_pct) == 87.5
    assert row.cta_forced_reduction is False
    assert float(row.menthorq_cta_score) == 5.1
    assert row.crash_trigger_fired is False


def test_cri_partial_payload_gives_nulls(engine):
    payload = {"date": "2026-04-09", "vix": 20.0}
    with engine.begin() as conn:
        conn.execute(insert(cri_series).values(
            cri_level=Decimal("0"),
            alert=False,
            payload=payload,
        ))
        row = conn.execute(select(cri_series)).first()
    assert float(row.vix) == 20.0
    assert row.vvix is None
    assert row.cri_score is None
    assert row.crash_trigger_fired is None
```

- [ ] **Step 2: Run the test**

```bash
uv run pytest scripts/tests/test_cri_generated_columns.py -xvs
```

Expected: both tests PASS. If a generated column raises a cast error on the partial payload, adjust the column to use `nullif(payload->>'x','')::numeric` or a safer form — but Postgres typically returns NULL on missing keys, so this should just work.

- [ ] **Step 3: Commit**

```bash
git add scripts/tests/test_cri_generated_columns.py
git commit -m "test(cri): generated columns derive from payload"
```

---

## Task 6: Update `save_snapshot()` query

**Goal:** change the signature of `save_snapshot()` in `src/xenon/db/queries/uw.py` to match the new schema.

**Files:**

- Modify: `src/xenon/db/queries/uw.py:13-30`

- [ ] **Step 1: Replace `save_snapshot`**

Replace lines 13-30 with:

```python
async def save_snapshot(
    conn: AsyncConnection,
    *,
    ticker: str,
    report: dict | None = None,
    display: dict | None = None,
    derived: dict | None = None,
    dark_pool_summary: dict | None = None,
    options_flow_summary: dict | None = None,
    flow_alerts: list | None = None,
    materialized_changes: list | None = None,
    report_fetched_at: datetime | None = None,
    archived_at: datetime | None = None,
    portfolio_score: Decimal | None = None,
) -> int:
    result = await conn.execute(
        insert(uw_analyze_snapshots)
        .values(
            ticker=ticker,
            report=report,
            display=display,
            derived=derived,
            dark_pool_summary=dark_pool_summary,
            options_flow_summary=options_flow_summary,
            flow_alerts=flow_alerts,
            materialized_changes=materialized_changes,
            report_fetched_at=report_fetched_at,
            archived_at=archived_at,
            portfolio_score=portfolio_score,
        )
        .returning(uw_analyze_snapshots.c.id)
    )
    return result.scalar()
```

- [ ] **Step 2: Verify `get_latest_snapshot` and `get_snapshot_history` still work**

These select `uw_analyze_snapshots.*` and convert to dict. Since `dict(row._mapping)` includes generated columns automatically, they'll start returning the new shape. No change needed; just confirm no callers break.

```bash
grep -rn "get_latest_snapshot\|get_snapshot_history" src/xenon scripts/ web/ 2>/dev/null | grep -v test_
```

If callers reference the old `vrp_state`/`regime`/`flow_signals` keys explicitly, they'll need updating. Make notes for follow-up; do not fix in this task.

- [ ] **Step 3: Commit**

```bash
git add src/xenon/db/queries/uw.py
git commit -m "queries(uw): save_snapshot accepts full payload shape"
```

---

## Task 7: Rewrite `_archive_to_postgres` to write the full payload

**Goal:** stop dropping ~95% of the analyze payload.

**Files:**

- Modify: `src/xenon/api/services/uw_analyze_cache.py:418-459`

- [ ] **Step 1: Write the failing test first**

Create `scripts/tests/test_uw_analyze_writer_full_payload.py`:

```python
"""Verify uw_analyze archive writer persists the full payload."""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select

from scripts.tests.conftest import _sync_test_db_url
from xenon.api.services.uw_analyze_cache import UwAnalyzeCache
from xenon.db.schema import uw_analyze_snapshots


@pytest.fixture
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


SAMPLE_CURRENT = {
    "ticker": "AAPL",
    "ts": "2026-04-08T14:03:48.800154+00:00",
    "report": {
        "ticker": "AAPL",
        "price": 184.22,
        "fetched_at": "2026-04-08T14:02:11+00:00",
        "benchmark": {
            "spy": {"ticker": "SPY", "iv_rank": 22.0, "gex_regime": "positive"},
            "sector_etf": {"ticker": "XLK", "iv_rank": 31.0, "gex_regime": "mixed"},
        },
        "vrp": {"vrp_raw": 0.04, "vrp_zscore": 1.2, "iv_percentile": 38.0,
                "ts_ratio": 1.05, "ts_inverted": False, "earnings_within_14d": False},
        "regime": {"regime": "R1", "reason": "demo", "gex_sign": "positive",
                   "gex_flip_relative": "below_price", "flip_distance_pct": -1.1},
        "scores": {"market_structure": 24.0, "volatility": 19.0, "flow": 17.0,
                   "positioning": 0.0, "composite": 15.0, "grade": "B",
                   "bias": "MIXED", "mode": "full", "reweighted": True},
    },
    "display": {
        "iv_rank": 38.0, "iv": 22.0, "rv": 18.6,
        "call_wall_strike": 190.0, "put_wall_strike": 175.0,
        "gamma_per_1pct": 42000000.0,
        "net_call_premium": 12400000.0, "net_put_premium": -3100000.0,
        "short_volume_ratio": 0.41,
        "short_volume_trend": [0.4, 0.41, 0.42],
        "term_structure_label": "normal",
        "max_pain": None,
        "gex_by_strike": [
            {"strike": 190.0, "call_gamma": 44.8, "put_gamma": -2.7,
             "net_gamma": 42.1, "distance_pct": 0.0314,
             "is_call_wall": True, "is_put_wall": False},
        ],
    },
    "flow_alerts": [],
    "derived": {"gex_sign": "POSITIVE", "spot": 184.22},
    "dark_pool_summary": {"score": -20.0, "signal": "NONE", "direction": "NO_DATA",
                          "strength": 0, "buy_ratio": None, "options_conflict": False,
                          "num_prints": 0, "sustained_days": 0},
    "options_flow_summary": {"total_alerts": 0, "total_premium": 0,
                             "call_premium": 0, "put_premium": 0,
                             "call_put_ratio": None, "bias": "NO_DATA"},
}


def test_archive_to_postgres_writes_full_payload(engine, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", _sync_test_db_url())
    UwAnalyzeCache._archive_to_postgres(
        ticker="AAPL",
        current=SAMPLE_CURRENT,
        materialized_changes=[],
        archived_at_iso="2026-04-08T14:03:48.800469+00:00",
    )
    with engine.begin() as conn:
        row = conn.execute(select(uw_analyze_snapshots)).first()
    assert row is not None
    assert row.ticker == "AAPL"
    # JSONB columns
    assert row.report["scores"]["composite"] == 15.0
    assert row.display["call_wall_strike"] == 190.0
    assert row.derived["gex_sign"] == "POSITIVE"
    assert row.dark_pool_summary["signal"] == "NONE"
    assert row.options_flow_summary["bias"] == "NO_DATA"
    # Generated columns
    assert float(row.price) == 184.22
    assert float(row.composite_score) == 15.0
    assert row.grade == "B"
    assert row.bias == "MIXED"
    assert row.regime_label == "R1"
    assert row.gex_sign == "positive"
    assert float(row.iv) == 22.0
    assert float(row.iv_rank) == 38.0
    assert float(row.call_wall_strike) == 190.0
    assert row.dp_signal == "NONE"
    assert row.of_bias == "NO_DATA"
    assert float(row.spy_iv_rank) == 22.0
    assert row.sector_etf_ticker == "XLK"
    assert row.report_fetched_at is not None
    assert row.archived_at is not None
```

- [ ] **Step 2: Run test, expect failure**

```bash
uv run pytest scripts/tests/test_uw_analyze_writer_full_payload.py -xvs
```

Expected: FAIL — `_archive_to_postgres()` signature mismatch (current writer takes `current: dict` only, no `materialized_changes` / `archived_at_iso`).

- [ ] **Step 3: Rewrite `_archive_to_postgres`**

Replace `src/xenon/api/services/uw_analyze_cache.py:426-459` with:

```python
    @staticmethod
    def _archive_to_postgres(
        ticker: str,
        current: dict,
        materialized_changes: list | None = None,
        archived_at_iso: str | None = None,
    ) -> None:
        """Write snapshot to Postgres uw_analyze_snapshots (sync, for to_thread)."""
        try:
            url = os.environ.get("DATABASE_URL")
            if not url:
                return
            from datetime import datetime
            from decimal import Decimal

            from sqlalchemy import create_engine as _cse
            from sqlalchemy import insert

            from xenon.db.schema import uw_analyze_snapshots

            sync_url = url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
            engine = _cse(sync_url)
            report = current.get("report") or {}
            scores = report.get("scores") if isinstance(report, dict) else None
            score_val = None
            if isinstance(scores, dict):
                score_val = scores.get("flow") or scores.get("composite") or scores.get("total")

            def _ts(value: str | None):
                if not value:
                    return None
                try:
                    return datetime.fromisoformat(value.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    return None

            with engine.begin() as conn:
                conn.execute(
                    insert(uw_analyze_snapshots).values(
                        ticker=ticker,
                        report=_coerce_jsonable(report) if isinstance(report, dict) else None,
                        display=_coerce_jsonable(current.get("display")),
                        derived=_coerce_jsonable(current.get("derived")),
                        dark_pool_summary=_coerce_jsonable(current.get("dark_pool_summary")),
                        options_flow_summary=_coerce_jsonable(current.get("options_flow_summary")),
                        flow_alerts=_coerce_jsonable(current.get("flow_alerts")),
                        materialized_changes=_coerce_jsonable(materialized_changes),
                        report_fetched_at=_ts(report.get("fetched_at")) if isinstance(report, dict) else None,
                        archived_at=_ts(archived_at_iso),
                        portfolio_score=Decimal(str(score_val)) if score_val is not None else None,
                    )
                )
            engine.dispose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("uw_analyze_cache Postgres archive failed for %s: %s", ticker, exc)
```

- [ ] **Step 4: Update the caller (line 422) to pass new args**

In `_write_archive_async()` (around line 418-422), find the call site:

```python
            await asyncio.to_thread(self._archive_to_postgres, ticker, current)
```

Replace with:

```python
            await asyncio.to_thread(
                self._archive_to_postgres,
                ticker,
                current,
                payload.get("materialized_changes") if isinstance(payload, dict) else None,
                payload.get("archived_at") if isinstance(payload, dict) else None,
            )
```

(`payload` here is the dict that gets atomic-saved to disk and contains `current`, `materialized_changes`, `archived_at` — confirm by reading `_write_archive_sync` to see what it expects. If the variable is named differently, adapt.)

- [ ] **Step 5: Re-run the test**

```bash
uv run pytest scripts/tests/test_uw_analyze_writer_full_payload.py -xvs
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/tests/test_uw_analyze_writer_full_payload.py src/xenon/api/services/uw_analyze_cache.py
git commit -m "uw_analyze: archive full payload to postgres (was dropping 95%)"
```

---

## Task 8: Test the three uw_analyze fanout triggers

**Goal:** prove that inserting a snapshot with arrays causes child rows to appear.

**Files:**

- Create: `scripts/tests/test_uw_analyze_flow_alerts_trigger.py`
- Create: `scripts/tests/test_uw_analyze_gex_strikes_trigger.py`
- Create: `scripts/tests/test_uw_analyze_short_volume_trend_trigger.py`

- [ ] **Step 1: Write `test_uw_analyze_flow_alerts_trigger.py`**

```python
"""flow_alerts JSONB array fans out to uw_analyze_flow_alerts child table."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, insert, select, text

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.schema import uw_analyze_flow_alerts, uw_analyze_snapshots


@pytest.fixture
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


def test_flow_alerts_array_fans_out(engine):
    alerts = [
        {"type": "dark_pool_accumulation", "severity": "high", "size": 5_000_000},
        {"type": "deep_conviction_flow", "severity": "medium", "premium": 1_200_000},
    ]
    with engine.begin() as conn:
        result = conn.execute(
            insert(uw_analyze_snapshots).values(
                ticker="TSLA",
                flow_alerts=alerts,
            ).returning(uw_analyze_snapshots.c.id)
        )
        snap_id = result.scalar()
        rows = conn.execute(
            select(uw_analyze_flow_alerts).where(uw_analyze_flow_alerts.c.snapshot_id == snap_id)
        ).all()
    assert len(rows) == 2
    by_type = {r.alert_type: r for r in rows}
    assert by_type["dark_pool_accumulation"].alert_severity == "high"
    assert by_type["dark_pool_accumulation"].alert_payload["size"] == 5_000_000
    assert by_type["deep_conviction_flow"].alert_severity == "medium"


def test_flow_alerts_null_does_not_fan_out(engine):
    with engine.begin() as conn:
        result = conn.execute(
            insert(uw_analyze_snapshots).values(ticker="TSLA").returning(uw_analyze_snapshots.c.id)
        )
        snap_id = result.scalar()
        count = conn.execute(text(
            "SELECT count(*) FROM xenon.uw_analyze_flow_alerts WHERE snapshot_id=:i"
        ), {"i": snap_id}).scalar()
    assert count == 0


def test_flow_alerts_update_replaces_children(engine):
    """Updating flow_alerts should refresh the child rows (no duplicates)."""
    with engine.begin() as conn:
        result = conn.execute(
            insert(uw_analyze_snapshots).values(
                ticker="TSLA",
                flow_alerts=[{"type": "a", "severity": "low"}],
            ).returning(uw_analyze_snapshots.c.id)
        )
        snap_id = result.scalar()
        conn.execute(text(
            "UPDATE xenon.uw_analyze_snapshots SET flow_alerts = :a::jsonb WHERE id = :i"
        ), {"a": '[{"type":"b","severity":"high"},{"type":"c","severity":"medium"}]', "i": snap_id})
        rows = conn.execute(
            select(uw_analyze_flow_alerts).where(uw_analyze_flow_alerts.c.snapshot_id == snap_id)
        ).all()
    types = sorted(r.alert_type for r in rows)
    assert types == ["b", "c"], f"expected refreshed [b,c], got {types}"
```

- [ ] **Step 2: Write `test_uw_analyze_gex_strikes_trigger.py`**

```python
"""display.gex_by_strike JSONB array fans out to uw_analyze_gex_strikes."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, insert, select

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.schema import uw_analyze_gex_strikes, uw_analyze_snapshots


@pytest.fixture
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


def test_gex_by_strike_fans_out(engine):
    display = {
        "gex_by_strike": [
            {"strike": 190.0, "call_gamma": 44.8, "put_gamma": -2.7,
             "net_gamma": 42.1, "distance_pct": 0.0314,
             "is_call_wall": True, "is_put_wall": False},
            {"strike": 185.0, "call_gamma": 14.2, "put_gamma": -4.5,
             "net_gamma": 9.7, "distance_pct": 0.0042,
             "is_call_wall": False, "is_put_wall": False},
            {"strike": 175.0, "call_gamma": 3.1, "put_gamma": -9.4,
             "net_gamma": -6.3, "distance_pct": -0.0500,
             "is_call_wall": False, "is_put_wall": True},
        ],
    }
    with engine.begin() as conn:
        result = conn.execute(
            insert(uw_analyze_snapshots).values(
                ticker="AAPL",
                display=display,
            ).returning(uw_analyze_snapshots.c.id)
        )
        snap_id = result.scalar()
        rows = conn.execute(
            select(uw_analyze_gex_strikes)
            .where(uw_analyze_gex_strikes.c.snapshot_id == snap_id)
            .order_by(uw_analyze_gex_strikes.c.strike.desc())
        ).all()
    assert len(rows) == 3
    assert [float(r.strike) for r in rows] == [190.0, 185.0, 175.0]
    assert rows[0].is_call_wall is True
    assert rows[2].is_put_wall is True
    assert float(rows[0].net_gamma) == 42.1


def test_no_gex_by_strike_no_children(engine):
    with engine.begin() as conn:
        result = conn.execute(
            insert(uw_analyze_snapshots).values(
                ticker="AAPL",
                display={"iv_rank": 50.0},  # no gex_by_strike key
            ).returning(uw_analyze_snapshots.c.id)
        )
        snap_id = result.scalar()
        rows = conn.execute(
            select(uw_analyze_gex_strikes).where(uw_analyze_gex_strikes.c.snapshot_id == snap_id)
        ).all()
    assert rows == []
```

- [ ] **Step 3: Write `test_uw_analyze_short_volume_trend_trigger.py`**

```python
"""display.short_volume_trend array fans out to uw_analyze_short_volume_trend."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, insert, select

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.schema import uw_analyze_short_volume_trend, uw_analyze_snapshots


@pytest.fixture
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


def test_short_volume_trend_fans_out(engine):
    with engine.begin() as conn:
        result = conn.execute(
            insert(uw_analyze_snapshots).values(
                ticker="NVDA",
                display={"short_volume_trend": [0.40, 0.41, 0.42]},
            ).returning(uw_analyze_snapshots.c.id)
        )
        snap_id = result.scalar()
        rows = conn.execute(
            select(uw_analyze_short_volume_trend)
            .where(uw_analyze_short_volume_trend.c.snapshot_id == snap_id)
            .order_by(uw_analyze_short_volume_trend.c.position_in_trend)
        ).all()
    assert [r.position_in_trend for r in rows] == [0, 1, 2]
    assert [float(r.ratio) for r in rows] == [0.40, 0.41, 0.42]
```

- [ ] **Step 4: Run the three tests**

```bash
uv run pytest scripts/tests/test_uw_analyze_flow_alerts_trigger.py scripts/tests/test_uw_analyze_gex_strikes_trigger.py scripts/tests/test_uw_analyze_short_volume_trend_trigger.py -xvs
```

Expected: all PASS. If a trigger has a bug, the SQL error appears in the test output — fix the trigger DDL in the migration, downgrade+re-upgrade the test DB, retry.

- [ ] **Step 5: Commit**

```bash
git add scripts/tests/test_uw_analyze_flow_alerts_trigger.py scripts/tests/test_uw_analyze_gex_strikes_trigger.py scripts/tests/test_uw_analyze_short_volume_trend_trigger.py
git commit -m "test(uw_analyze): array fanout triggers for flow_alerts, gex_strikes, short_volume_trend"
```

---

## Task 9: Test uw_flow_event_ticks trigger

**Goal:** prove `daily_track` fans out to ticks, including UPDATE behavior (no duplicates).

**Files:**

- Create: `scripts/tests/test_uw_flow_event_ticks_trigger.py`

- [ ] **Step 1: Write the test**

```python
"""uw_flow_events.daily_track fans out to uw_flow_event_ticks; updates dedupe."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, insert, select, text

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.schema import uw_flow_event_ticks, uw_flow_events


@pytest.fixture
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


def _insert_event(conn, key: str, daily_track: dict | None) -> int:
    return conn.execute(
        insert(uw_flow_events).values(
            flow_event_key=key,
            ticker="SPY",
            detected_at=datetime(2026, 4, 26, 14, 0, tzinfo=timezone.utc),
            initial={"premium_usd": 100000, "size": 50},
            daily_track=daily_track,
            status="open",
        ).returning(uw_flow_events.c.id)
    ).scalar()


def test_daily_track_initial_insert_fans_out(engine):
    daily_track = {
        "2026-04-26T14:30:00+00:00": {"spot": 520.0, "bid": 1.10, "ask": 1.12, "mark": 1.11, "oi": 1000, "volume": 200, "iv": 0.18},
        "2026-04-26T15:00:00+00:00": {"spot": 521.0, "bid": 1.20, "ask": 1.22, "mark": 1.21, "oi": 1050, "volume": 250, "iv": 0.19},
    }
    with engine.begin() as conn:
        ev_id = _insert_event(conn, "evt-001", daily_track)
        rows = conn.execute(
            select(uw_flow_event_ticks)
            .where(uw_flow_event_ticks.c.event_id == ev_id)
            .order_by(uw_flow_event_ticks.c.observed_at)
        ).all()
    assert len(rows) == 2
    assert float(rows[0].spot) == 520.0
    assert float(rows[1].spot) == 521.0
    assert rows[0].flow_event_key == "evt-001"


def test_daily_track_update_adds_new_ticks_no_dupes(engine):
    daily_track = {
        "2026-04-26T14:30:00+00:00": {"spot": 520.0, "mark": 1.11},
        "2026-04-26T15:00:00+00:00": {"spot": 521.0, "mark": 1.21},
    }
    with engine.begin() as conn:
        ev_id = _insert_event(conn, "evt-002", daily_track)
        # Update with a third tick (existing two preserved + new)
        new_track = dict(daily_track)
        new_track["2026-04-26T15:30:00+00:00"] = {"spot": 522.0, "mark": 1.31}
        conn.execute(
            uw_flow_events.update()
            .where(uw_flow_events.c.id == ev_id)
            .values(daily_track=new_track)
        )
        rows = conn.execute(
            select(uw_flow_event_ticks)
            .where(uw_flow_event_ticks.c.event_id == ev_id)
            .order_by(uw_flow_event_ticks.c.observed_at)
        ).all()
    assert len(rows) == 3, f"expected 3 unique ticks, got {len(rows)}"
    assert [float(r.spot) for r in rows] == [520.0, 521.0, 522.0]


def test_null_daily_track_no_ticks(engine):
    with engine.begin() as conn:
        ev_id = _insert_event(conn, "evt-003", None)
        cnt = conn.execute(text(
            "SELECT count(*) FROM xenon.uw_flow_event_ticks WHERE event_id = :i"
        ), {"i": ev_id}).scalar()
    assert cnt == 0
```

- [ ] **Step 2: Run the test**

```bash
uv run pytest scripts/tests/test_uw_flow_event_ticks_trigger.py -xvs
```

Expected: PASS. **If `jsonb_each` raises on the daily_track shape**, the Phase 0 finding (Task 0 step 3) was that `daily_track` is an array, not an object. In that case, edit the trigger function in the migration: replace `jsonb_each(NEW.daily_track)` with iteration over array elements where each element has its own `observed_at` field. Then downgrade+re-upgrade and retry.

- [ ] **Step 3: Commit**

```bash
git add scripts/tests/test_uw_flow_event_ticks_trigger.py
git commit -m "test(uw_flow_events): daily_track fans out to ticks with dedupe"
```

---

## Task 10: GEX dual-write to gex_snapshots

**Goal:** every `xenon-gex-scan` invocation writes one row to BOTH `scan_results` (legacy) and `gex_snapshots` (new typed).

**Files:**

- Modify: `src/xenon/db/queries/scans.py`
- Modify: `src/xenon/scanners/gex.py:935-946`
- Create: `scripts/tests/test_gex_writer_dual.py`

- [ ] **Step 1: Add `save_gex_snapshot` query**

In `src/xenon/db/queries/scans.py`, add after the existing `save_scan` function:

```python
def save_gex_snapshot(conn, *, payload: dict) -> int:
    """Insert a row into gex_snapshots. Conn is a sync SA connection."""
    from datetime import date as _date

    from sqlalchemy import insert
    from xenon.db.schema import gex_snapshots

    data_date = payload.get("data_date")
    if isinstance(data_date, str):
        try:
            data_date = _date.fromisoformat(data_date)
        except ValueError:
            data_date = None
    return conn.execute(
        insert(gex_snapshots)
        .values(
            ticker=payload.get("ticker"),
            data_date=data_date,
            payload=payload,
        )
        .returning(gex_snapshots.c.id)
    ).scalar()
```

- [ ] **Step 2: Write the failing test**

Create `scripts/tests/test_gex_writer_dual.py`:

```python
"""GEX scanner dual-writes to scan_results and gex_snapshots."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select, text

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.queries.scans import save_gex_snapshot
from xenon.db.schema import gex_snapshots, scan_results


@pytest.fixture
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


SAMPLE_GEX = {
    "ticker": "AAPL",
    "spot": 184.22,
    "net_gex": 12345.67,
    "net_dex": -890.12,
    "vol_pc": 0.85,
    "iv": {"iv30d": 0.22, "iv_rank": 38.0, "hv30": 0.186, "mq_iv30d": 0.21},
    "levels": {"max_magnet": 185.0, "second_magnet": 180.0,
               "max_accelerator": 195.0, "put_wall": 175.0},
    "data_date": "2026-04-26",
}


def test_save_gex_snapshot_writes_row(engine):
    with engine.begin() as conn:
        new_id = save_gex_snapshot(conn, payload=SAMPLE_GEX)
        row = conn.execute(
            select(gex_snapshots).where(gex_snapshots.c.id == new_id)
        ).first()
    assert row.ticker == "AAPL"
    assert float(row.spot) == 184.22
    assert float(row.net_gex) == 12345.67
    assert float(row.iv_30d) == 0.22
    assert float(row.iv_rank) == 38.0
    assert float(row.level_max_magnet) == 185.0
    assert row.data_date.isoformat() == "2026-04-26"


def test_save_gex_snapshot_preserves_full_payload(engine):
    with engine.begin() as conn:
        new_id = save_gex_snapshot(conn, payload=SAMPLE_GEX)
        row = conn.execute(
            select(gex_snapshots).where(gex_snapshots.c.id == new_id)
        ).first()
    assert row.payload == SAMPLE_GEX
```

- [ ] **Step 3: Run, expect failure**

```bash
uv run pytest scripts/tests/test_gex_writer_dual.py -xvs
```

Expected: FAIL — `save_gex_snapshot` import works but you may see issues; if so iterate.

If it actually PASSES already (because Step 1 created the function), that's fine — proceed.

- [ ] **Step 4: Update GEX scanner to dual-write**

Modify `src/xenon/scanners/gex.py:935-946`. Replace:

```python
    # Also write to Postgres
    try:
        from sqlalchemy import insert

        from xenon.db.engine import get_sync_engine
        from xenon.db.schema import scan_results

        engine = get_sync_engine()
        with engine.begin() as conn:
            conn.execute(insert(scan_results).values(scan_type="gex", payload=result))
    except Exception as exc:
        print(f"  Warning: Postgres scan write failed: {exc}", file=sys.stderr)
```

With:

```python
    # Also write to Postgres (dual-write: legacy scan_results + new gex_snapshots)
    try:
        from sqlalchemy import insert

        from xenon.db.engine import get_sync_engine
        from xenon.db.queries.scans import save_gex_snapshot
        from xenon.db.schema import scan_results

        engine = get_sync_engine()
        with engine.begin() as conn:
            conn.execute(insert(scan_results).values(scan_type="gex", payload=result))
            save_gex_snapshot(conn, payload=result)
    except Exception as exc:
        print(f"  Warning: Postgres scan write failed: {exc}", file=sys.stderr)
```

- [ ] **Step 5: Re-run tests**

```bash
uv run pytest scripts/tests/test_gex_writer_dual.py -xvs
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/xenon/db/queries/scans.py src/xenon/scanners/gex.py scripts/tests/test_gex_writer_dual.py
git commit -m "gex: dual-write scan_results + gex_snapshots"
```

---

## Task 11: VCG scanner Postgres write + new query

**Goal:** every `xenon-vcg-scan` invocation writes a row to `vcg_series`.

**Files:**

- Modify: `src/xenon/db/queries/scans.py` (add `save_vcg_scan`)
- Modify: `src/xenon/scanners/vcg.py` (add Postgres write block)
- Create: `scripts/tests/test_vcg_writer_postgres.py`

- [ ] **Step 1: Add `save_vcg_scan` query**

Append to `src/xenon/db/queries/scans.py`:

```python
def save_vcg_scan(conn, *, payload: dict, market_open: bool | None = None,
                  credit_proxy: str | None = None) -> int:
    """Insert a row into vcg_series. Conn is a sync SA connection."""
    from sqlalchemy import insert
    from xenon.db.schema import vcg_series

    return conn.execute(
        insert(vcg_series)
        .values(
            market_open=market_open,
            credit_proxy=credit_proxy,
            payload=payload,
        )
        .returning(vcg_series.c.id)
    ).scalar()
```

- [ ] **Step 2: Write the failing test**

Create `scripts/tests/test_vcg_writer_postgres.py`:

```python
"""VCG scanner persists to vcg_series with generated columns."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.queries.scans import save_vcg_scan
from xenon.db.schema import vcg_series


@pytest.fixture
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


SAMPLE_VCG = {
    "scan_time": "2026-04-21T14:11:08.383805",
    "market_open": False,
    "credit_proxy": "HYG",
    "signal": {
        "vcg": 1.0416, "vcg_adj": 1.0416, "residual": 0.002108,
        "beta1_vvix": -0.061933, "beta2_vix": -0.011826, "alpha": -0.000135,
        "vix": 18.87, "vvix": 98.15,
        "credit_price": 80.58, "credit_5d_return_pct": 0.399,
        "ro": 0, "edr": 0, "tier": None, "bounce": 0,
        "vvix_severity": "moderate",
        "sign_ok": True, "sign_suppressed": False,
        "pi_panic": 0.0,
        "regime": "DIVERGENCE", "interpretation": "NORMAL",
        "attribution": {
            "vvix_pct": 68.1, "vix_pct": 31.9,
            "vvix_component": -0.001936, "vix_component": -0.000905,
            "model_implied": -0.002976,
        },
    },
    "history": [],
}


def test_save_vcg_scan_extracts_signal(engine):
    with engine.begin() as conn:
        new_id = save_vcg_scan(conn, payload=SAMPLE_VCG, market_open=False, credit_proxy="HYG")
        row = conn.execute(select(vcg_series).where(vcg_series.c.id == new_id)).first()
    assert row.market_open is False
    assert row.credit_proxy == "HYG"
    assert float(row.vcg) == 1.0416
    assert float(row.vix) == 18.87
    assert float(row.vvix) == 98.15
    assert row.regime == "DIVERGENCE"
    assert row.interpretation == "NORMAL"
    assert row.tier is None
    assert row.sign_ok is True
    assert float(row.attr_vvix_pct) == 68.1
    assert float(row.attr_model_implied) == -0.002976
```

- [ ] **Step 3: Run test, expect PASS (or import-failure)**

```bash
uv run pytest scripts/tests/test_vcg_writer_postgres.py -xvs
```

Expected: PASS (Step 1 added the query).

- [ ] **Step 4: Add Postgres write block to VCG scanner**

Locate the spot in `src/xenon/scanners/vcg.py` where the file write happens (similar to `gex.py:929-933` pattern: after `atomic_save(...)` to `data/vcg.json`). Append:

```python
    # Also write to Postgres
    try:
        from xenon.db.engine import get_sync_engine
        from xenon.db.queries.scans import save_vcg_scan

        engine = get_sync_engine()
        with engine.begin() as conn:
            save_vcg_scan(
                conn,
                payload=result,
                market_open=result.get("market_open"),
                credit_proxy=result.get("credit_proxy"),
            )
    except Exception as exc:
        print(f"  Warning: Postgres VCG write failed: {exc}", file=sys.stderr)
```

(`result` here is the dict that gets passed to `atomic_save`. Adapt the variable name to whatever the VCG scanner uses.)

- [ ] **Step 5: Smoke test**

```bash
uv run pytest scripts/tests/test_vcg_writer_postgres.py -xvs
```

Expected: still PASS (the scanner integration isn't unit-tested here — just the query function).

- [ ] **Step 6: Commit**

```bash
git add src/xenon/db/queries/scans.py src/xenon/scanners/vcg.py scripts/tests/test_vcg_writer_postgres.py
git commit -m "vcg: persist scan results to vcg_series"
```

---

## Task 12: Backfill `uw_api_stats` from JSON

**Goal:** load `data/uw_api_stats_history.json` into `xenon.uw_api_stats`.

**Files:**

- Create: `scripts/migrations/2026_04_26_backfill_uw_api_stats.py`
- Create: `scripts/tests/test_backfill_uw_api_stats.py`

- [ ] **Step 1: Write the failing test**

```python
"""Backfill uw_api_stats from history JSON."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.schema import uw_api_stats


@pytest.fixture
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


def test_backfill_loads_buckets(engine, tmp_path):
    history = {
        "updated_at": "2026-04-26T09:59:20Z",
        "schema_version": 1,
        "buckets": {
            "2026-04-21T19:00:00Z": {
                "requests_2xx": 81, "requests_4xx": 0, "requests_5xx": 0,
                "cached": 0,
                "sum_latency_ms": 21525.21, "latency_count": 81,
            },
            "2026-04-22T14:00:00Z": {
                "requests_2xx": 1511, "requests_4xx": 983, "requests_5xx": 0,
                "cached": 296,
                "sum_latency_ms": 436723.98, "latency_count": 1511,
            },
        },
    }
    src = tmp_path / "uw_api_stats_history.json"
    src.write_text(json.dumps(history))

    from scripts.migrations import _2026_04_26_backfill_uw_api_stats as backfill  # see Step 2 for module name

    backfill.run(json_path=src, db_url=_sync_test_db_url())

    with engine.begin() as conn:
        rows = conn.execute(select(uw_api_stats).order_by(uw_api_stats.c.bucket_hour)).all()
    assert len(rows) == 2
    assert rows[0].bucket_hour == datetime(2026, 4, 21, 19, 0, tzinfo=timezone.utc)
    assert rows[0].status_2xx == 81
    assert rows[0].cache_hits == 0
    assert float(rows[0].latency_sum) == 21525.21
    assert rows[1].status_4xx == 983
    assert rows[1].cache_hits == 296


def test_backfill_idempotent(engine, tmp_path):
    history = {"buckets": {
        "2026-04-21T19:00:00Z": {
            "requests_2xx": 50, "requests_4xx": 1, "requests_5xx": 0,
            "cached": 5, "sum_latency_ms": 1000.0, "latency_count": 50,
        },
    }}
    src = tmp_path / "h.json"
    src.write_text(json.dumps(history))
    from scripts.migrations import _2026_04_26_backfill_uw_api_stats as backfill
    backfill.run(json_path=src, db_url=_sync_test_db_url())
    backfill.run(json_path=src, db_url=_sync_test_db_url())  # second run
    with engine.begin() as conn:
        cnt = conn.execute(select(uw_api_stats)).all()
    assert len(cnt) == 1, "second run should upsert, not duplicate"
```

- [ ] **Step 2: Run, expect ImportError (module doesn't exist)**

```bash
uv run pytest scripts/tests/test_backfill_uw_api_stats.py -xvs
```

Expected: FAIL with import error.

- [ ] **Step 3: Create the backfill script**

The test imports `from scripts.migrations import _2026_04_26_backfill_uw_api_stats`. Python identifiers can't start with a digit, so the module file must be importable via this exact name. **Use a leading underscore prefix in both the filename and the import.** Create `scripts/migrations/_2026_04_26_backfill_uw_api_stats.py`:

```python
"""Backfill xenon.uw_api_stats from data/uw_api_stats_history.json.

JSON shape:
  {"updated_at": ..., "schema_version": 1, "buckets":
    {"<iso-ts>": {"requests_2xx": N, "requests_4xx": N, "requests_5xx": N,
                  "cached": N, "sum_latency_ms": F, "latency_count": N}, ...}}

Idempotent: each bucket upserts on bucket_hour PK.
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import insert as pg_insert

from xenon.db.schema import uw_api_stats


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def run(*, json_path: Path | str, db_url: str) -> int:
    """Returns the number of buckets processed."""
    data = json.loads(Path(json_path).read_text())
    buckets = data.get("buckets", {})
    engine = create_engine(db_url, pool_pre_ping=True)
    try:
        with engine.begin() as conn:
            for ts_str, b in buckets.items():
                bucket_hour = _parse_iso(ts_str)
                s2 = int(b.get("requests_2xx", 0))
                s4 = int(b.get("requests_4xx", 0))
                s5 = int(b.get("requests_5xx", 0))
                values = dict(
                    bucket_hour=bucket_hour,
                    requests=s2 + s4 + s5,
                    cache_hits=int(b.get("cached", 0)),
                    latency_sum=Decimal(str(b.get("sum_latency_ms", 0))),
                    latency_count=int(b.get("latency_count", 0)),
                    status_2xx=s2,
                    status_4xx=s4,
                    status_5xx=s5,
                )
                stmt = pg_insert(uw_api_stats).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[uw_api_stats.c.bucket_hour],
                    set_={k: stmt.excluded[k] for k in values if k != "bucket_hour"},
                )
                conn.execute(stmt)
    finally:
        engine.dispose()
    return len(buckets)


if __name__ == "__main__":
    import argparse
    import os

    parser = argparse.ArgumentParser()
    parser.add_argument("--json", default="data/uw_api_stats_history.json")
    parser.add_argument("--db-url", default=os.environ.get("DATABASE_URL"))
    args = parser.parse_args()
    if not args.db_url:
        raise SystemExit("DATABASE_URL not set; pass --db-url")
    sync_url = args.db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    n = run(json_path=args.json, db_url=sync_url)
    print(f"backfilled {n} buckets")
```

Also add `__init__.py` if not present:

```bash
test -f scripts/migrations/__init__.py || touch scripts/migrations/__init__.py
```

- [ ] **Step 4: Re-run test**

```bash
uv run pytest scripts/tests/test_backfill_uw_api_stats.py -xvs
```

Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/migrations/_2026_04_26_backfill_uw_api_stats.py scripts/migrations/__init__.py scripts/tests/test_backfill_uw_api_stats.py
git commit -m "backfill: load uw_api_stats from history json"
```

---

## Task 13: Backfill VCG history

**Goal:** populate `vcg_series` from `data/vcg.json` — current snapshot + every history entry.

**Files:**

- Create: `scripts/migrations/_2026_04_26_backfill_vcg_history.py`
- Create: `scripts/tests/test_backfill_vcg_history.py`

- [ ] **Step 1: Write the failing test**

```python
"""Backfill vcg_series from data/vcg.json."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, select

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.schema import vcg_series


@pytest.fixture
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


def test_backfill_inserts_current_plus_history(engine, tmp_path):
    src_data = {
        "scan_time": "2026-04-21T14:11:08.383805",
        "market_open": False,
        "credit_proxy": "HYG",
        "signal": {
            "vcg": 1.0416, "vcg_adj": 1.0416, "residual": 0.002108,
            "beta1_vvix": -0.06, "beta2_vix": -0.01, "alpha": -0.0001,
            "vix": 18.87, "vvix": 98.15,
            "credit_price": 80.58, "credit_5d_return_pct": 0.4,
            "ro": 0, "edr": 0, "tier": None, "bounce": 0,
            "vvix_severity": "moderate",
            "sign_ok": True, "sign_suppressed": False,
            "pi_panic": 0.0, "regime": "DIVERGENCE", "interpretation": "NORMAL",
            "attribution": {"vvix_pct": 68.1, "vix_pct": 31.9,
                            "vvix_component": -0.0019, "vix_component": -0.0009,
                            "model_implied": -0.0029},
        },
        "history": [
            {"date": "2026-03-23", "residual": 0.006, "vcg": 3.11, "vcg_adj": 3.11,
             "beta1": -0.013, "beta2": -0.023, "vix": 26.15, "vvix": 122.82,
             "credit": 79.44, "ro": 0, "edr": 1, "tier": 3, "bounce": 0},
            {"date": "2026-03-24", "residual": -0.001, "vcg": -0.93, "vcg_adj": -0.93,
             "beta1": -0.011, "beta2": -0.025, "vix": 26.95, "vvix": 124.14,
             "credit": 79.17, "ro": 0, "edr": 0, "tier": None, "bounce": 0},
        ],
    }
    src = tmp_path / "vcg.json"
    src.write_text(json.dumps(src_data))

    from scripts.migrations import _2026_04_26_backfill_vcg_history as bf
    n = bf.run(json_path=src, db_url=_sync_test_db_url())
    assert n == 3  # 2 history + 1 current

    with engine.begin() as conn:
        rows = conn.execute(select(vcg_series).order_by(vcg_series.c.scanned_at)).all()
    assert len(rows) == 3
    # historical rows have signal-shaped payloads built from history fields
    assert float(rows[0].vcg) == 3.11
    assert rows[0].tier == 3
    # the current snapshot has the full attribution
    current_row = next(r for r in rows if r.regime == "DIVERGENCE")
    assert float(current_row.attr_model_implied) == -0.0029
```

- [ ] **Step 2: Run, expect ImportError**

```bash
uv run pytest scripts/tests/test_backfill_vcg_history.py -xvs
```

- [ ] **Step 3: Create the backfill script**

`scripts/migrations/_2026_04_26_backfill_vcg_history.py`:

```python
"""Backfill xenon.vcg_series from data/vcg.json.

Current snapshot becomes one row with full signal+attribution payload.
Each `history[i]` entry becomes one row with a synthesized signal-shaped
payload built from the history fields (fields not present in history items
stay NULL via missing keys).
"""
from __future__ import annotations

import json
from datetime import datetime, time, timezone
from pathlib import Path

from sqlalchemy import create_engine, insert

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
    """Trading-day date ('YYYY-MM-DD') → 16:00 ET timestamp (close), as UTC."""
    return datetime.combine(datetime.fromisoformat(d).date(), time(20, 0), tzinfo=timezone.utc)


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def run(*, json_path: Path | str, db_url: str) -> int:
    data = json.loads(Path(json_path).read_text())
    history = data.get("history", []) or []
    market_open = data.get("market_open")
    credit_proxy = data.get("credit_proxy")
    rows_to_insert = []
    for item in history:
        ts = _date_to_ts(item["date"])
        rows_to_insert.append(dict(
            scanned_at=ts,
            market_open=False,  # history items are EOD
            credit_proxy=credit_proxy,
            payload=_history_row_payload(item),
        ))
    # Current snapshot last so its scanned_at sorts after history
    current_ts = _parse_iso(data.get("scan_time")) or datetime.now(tz=timezone.utc)
    rows_to_insert.append(dict(
        scanned_at=current_ts,
        market_open=market_open,
        credit_proxy=credit_proxy,
        payload=data,
    ))
    engine = create_engine(db_url, pool_pre_ping=True)
    try:
        with engine.begin() as conn:
            for row in rows_to_insert:
                conn.execute(insert(vcg_series).values(**row))
    finally:
        engine.dispose()
    return len(rows_to_insert)


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
```

- [ ] **Step 4: Run test**

```bash
uv run pytest scripts/tests/test_backfill_vcg_history.py -xvs
```

Expected: PASS.

**Note: backfill is NOT idempotent** — re-running inserts duplicate history rows. That's acceptable for a one-shot file-based source; if it matters, truncate `vcg_series` first.

- [ ] **Step 5: Commit**

```bash
git add scripts/migrations/_2026_04_26_backfill_vcg_history.py scripts/tests/test_backfill_vcg_history.py
git commit -m "backfill: load vcg_series from data/vcg.json (current + history)"
```

---

## Task 14: Backfill `uw_analyze_snapshots` from on-disk archives

**Goal:** load every `data/uw_analyze_history/<TICKER>/*.json` into the now-rich `uw_analyze_snapshots` table. Triggers populate child tables automatically.

**Files:**

- Create: `scripts/migrations/_2026_04_26_backfill_uw_analyze_history.py`
- Create: `scripts/tests/test_backfill_uw_analyze_history.py`

- [ ] **Step 1: Write the failing test**

```python
"""Backfill uw_analyze_snapshots from on-disk uw_analyze_history archives."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, select, text

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.schema import uw_analyze_gex_strikes, uw_analyze_snapshots


@pytest.fixture
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


SAMPLE_ARCHIVE = {
    "current": {
        "ticker": "AAPL",
        "ts": "2026-04-08T14:03:48.800154+00:00",
        "report": {
            "ticker": "AAPL", "price": 184.22,
            "fetched_at": "2026-04-08T14:02:11+00:00",
            "scores": {"composite": 15.0, "flow": 17.0, "grade": "B", "bias": "MIXED"},
            "regime": {"regime": "R1", "gex_sign": "positive"},
            "vrp": {"vrp_zscore": 1.2, "iv_percentile": 38.0},
        },
        "display": {
            "iv": 22.0, "iv_rank": 38.0, "call_wall_strike": 190.0,
            "gex_by_strike": [
                {"strike": 190.0, "call_gamma": 44.8, "put_gamma": -2.7,
                 "net_gamma": 42.1, "distance_pct": 0.03,
                 "is_call_wall": True, "is_put_wall": False},
            ],
        },
        "derived": {"gex_sign": "POSITIVE", "spot": 184.22},
        "dark_pool_summary": {"signal": "NONE", "score": -20.0},
        "options_flow_summary": {"bias": "NO_DATA", "total_alerts": 0},
        "flow_alerts": [],
    },
    "materialized_changes": [],
    "archived_at": "2026-04-08T14:03:48.800469+00:00",
}


def test_backfill_one_file_creates_snapshot_and_strikes(engine, tmp_path):
    aapl_dir = tmp_path / "AAPL"
    aapl_dir.mkdir()
    (aapl_dir / "20260408-140348-800496.json").write_text(json.dumps(SAMPLE_ARCHIVE))

    from scripts.migrations import _2026_04_26_backfill_uw_analyze_history as bf
    n = bf.run(history_root=tmp_path, db_url=_sync_test_db_url())
    assert n == 1

    with engine.begin() as conn:
        snap = conn.execute(select(uw_analyze_snapshots)).first()
        strikes = conn.execute(select(uw_analyze_gex_strikes)).all()
    assert snap.ticker == "AAPL"
    assert snap.report["scores"]["grade"] == "B"
    assert float(snap.price) == 184.22
    assert snap.regime_label == "R1"
    assert float(snap.iv_rank) == 38.0
    assert len(strikes) == 1
    assert float(strikes[0].strike) == 190.0
    assert strikes[0].is_call_wall is True


def test_backfill_idempotent(engine, tmp_path):
    aapl_dir = tmp_path / "AAPL"
    aapl_dir.mkdir()
    (aapl_dir / "f.json").write_text(json.dumps(SAMPLE_ARCHIVE))
    from scripts.migrations import _2026_04_26_backfill_uw_analyze_history as bf
    bf.run(history_root=tmp_path, db_url=_sync_test_db_url())
    bf.run(history_root=tmp_path, db_url=_sync_test_db_url())
    with engine.begin() as conn:
        cnt = conn.execute(text("SELECT count(*) FROM xenon.uw_analyze_snapshots")).scalar()
    assert cnt == 1, "second run should not duplicate"
```

- [ ] **Step 2: Run, expect ImportError**

```bash
uv run pytest scripts/tests/test_backfill_uw_analyze_history.py -xvs
```

- [ ] **Step 3: Create the backfill script**

`scripts/migrations/_2026_04_26_backfill_uw_analyze_history.py`:

```python
"""Backfill xenon.uw_analyze_snapshots from data/uw_analyze_history/<TICKER>/*.json.

Each on-disk JSON file contains a `current` dict (ticker, report, display, derived,
dark_pool_summary, options_flow_summary, flow_alerts) plus `materialized_changes`
and `archived_at`. Idempotent on (ticker, archived_at) — second run UPDATEs
matching rows rather than inserting duplicates.
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from xenon.db.schema import uw_analyze_snapshots


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _row_values(payload: dict) -> dict:
    current = payload.get("current") or {}
    report = current.get("report") or {}
    scores = report.get("scores") if isinstance(report, dict) else None
    score_val = None
    if isinstance(scores, dict):
        score_val = scores.get("flow") or scores.get("composite") or scores.get("total")
    return dict(
        ticker=current.get("ticker"),
        report=report or None,
        display=current.get("display"),
        derived=current.get("derived"),
        dark_pool_summary=current.get("dark_pool_summary"),
        options_flow_summary=current.get("options_flow_summary"),
        flow_alerts=current.get("flow_alerts"),
        materialized_changes=payload.get("materialized_changes"),
        report_fetched_at=_parse_iso(report.get("fetched_at") if isinstance(report, dict) else None),
        archived_at=_parse_iso(payload.get("archived_at")),
        portfolio_score=Decimal(str(score_val)) if score_val is not None else None,
    )


def run(*, history_root: Path | str, db_url: str) -> int:
    """Walk history_root for *.json files; insert/update one row per file.

    Returns count of files processed.
    """
    root = Path(history_root)
    files = sorted(root.rglob("*.json"))
    engine = create_engine(db_url, pool_pre_ping=True)
    processed = 0
    try:
        with engine.begin() as conn:
            for f in files:
                try:
                    payload = json.loads(f.read_text())
                except Exception as exc:  # noqa: BLE001
                    print(f"  skip {f}: parse error {exc}")
                    continue
                values = _row_values(payload)
                if not values["ticker"] or not values["archived_at"]:
                    print(f"  skip {f}: missing ticker or archived_at")
                    continue
                # Idempotency: match on (ticker, archived_at)
                existing = conn.execute(
                    select(uw_analyze_snapshots.c.id)
                    .where(uw_analyze_snapshots.c.ticker == values["ticker"])
                    .where(uw_analyze_snapshots.c.archived_at == values["archived_at"])
                ).scalar()
                if existing:
                    conn.execute(
                        update(uw_analyze_snapshots)
                        .where(uw_analyze_snapshots.c.id == existing)
                        .values(**values, snapshot_at=values["archived_at"])
                    )
                else:
                    conn.execute(
                        pg_insert(uw_analyze_snapshots)
                        .values(**values, snapshot_at=values["archived_at"])
                    )
                processed += 1
    finally:
        engine.dispose()
    return processed


if __name__ == "__main__":
    import argparse
    import os

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="data/uw_analyze_history")
    parser.add_argument("--db-url", default=os.environ.get("DATABASE_URL"))
    args = parser.parse_args()
    if not args.db_url:
        raise SystemExit("DATABASE_URL not set; pass --db-url")
    sync_url = args.db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    n = run(history_root=args.root, db_url=sync_url)
    print(f"backfilled {n} uw_analyze_snapshots rows")
```

- [ ] **Step 4: Run test**

```bash
uv run pytest scripts/tests/test_backfill_uw_analyze_history.py -xvs
```

Expected: PASS. The trigger fires on UPDATE/INSERT of the snapshot, so child tables (`uw_analyze_gex_strikes` etc.) populate without separate calls.

- [ ] **Step 5: Commit**

```bash
git add scripts/migrations/_2026_04_26_backfill_uw_analyze_history.py scripts/tests/test_backfill_uw_analyze_history.py
git commit -m "backfill: load uw_analyze_snapshots from on-disk history archives"
```

---

## Task 15: Verification report script

**Goal:** one operator-run script that prints row counts + non-NULL coverage so you can eyeball the backfill outcome.

**Files:**

- Create: `scripts/migrations/2026_04_26_verify_normalize_payloads.py`

(This script doesn't get a unit test — it's a reporting tool. Smoke-test it manually.)

- [ ] **Step 1: Write the script**

`scripts/migrations/2026_04_26_verify_normalize_payloads.py`:

```python
"""Print a sanity report after running normalize_payloads + backfills.

Usage:
  uv run python scripts/migrations/2026_04_26_verify_normalize_payloads.py
"""
from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine, text


SUMMARY_QUERIES = {
    "uw_analyze_snapshots": """
        SELECT count(*) AS rows,
               count(report) AS report_non_null,
               count(display) AS display_non_null,
               count(derived) AS derived_non_null,
               count(dark_pool_summary) AS dp_non_null,
               count(options_flow_summary) AS of_non_null,
               count(price) AS price_extracted,
               count(grade) AS grade_extracted,
               count(regime_label) AS regime_extracted
        FROM xenon.uw_analyze_snapshots;
    """,
    "uw_analyze_flow_alerts": "SELECT count(*) AS rows, count(DISTINCT snapshot_id) AS snapshots FROM xenon.uw_analyze_flow_alerts;",
    "uw_analyze_gex_strikes": "SELECT count(*) AS rows, count(DISTINCT snapshot_id) AS snapshots FROM xenon.uw_analyze_gex_strikes;",
    "uw_analyze_short_volume_trend": "SELECT count(*) AS rows, count(DISTINCT snapshot_id) AS snapshots FROM xenon.uw_analyze_short_volume_trend;",
    "cri_series": """
        SELECT count(*) AS rows,
               count(vix) AS vix_extracted,
               count(cri_score) AS cri_score_extracted,
               count(*) FILTER (WHERE crash_trigger_fired) AS crash_triggered
        FROM xenon.cri_series;
    """,
    "vcg_series": """
        SELECT count(*) AS rows,
               count(vcg) AS vcg_extracted,
               count(*) FILTER (WHERE regime IS NOT NULL) AS with_regime
        FROM xenon.vcg_series;
    """,
    "gex_snapshots": "SELECT count(*) AS rows, count(DISTINCT ticker) AS tickers FROM xenon.gex_snapshots;",
    "scan_results": "SELECT count(*) AS rows, count(DISTINCT scan_type) AS scan_types FROM xenon.scan_results;",
    "uw_flow_events": """
        SELECT count(*) AS rows,
               count(*) FILTER (WHERE daily_track IS NOT NULL) AS with_daily_track,
               count(initial_premium_usd) AS premium_extracted
        FROM xenon.uw_flow_events;
    """,
    "uw_flow_event_ticks": "SELECT count(*) AS rows, count(DISTINCT event_id) AS events FROM xenon.uw_flow_event_ticks;",
    "uw_api_stats": "SELECT count(*) AS buckets, min(bucket_hour) AS earliest, max(bucket_hour) AS latest FROM xenon.uw_api_stats;",
}


def main() -> None:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)
    sync_url = url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    engine = create_engine(sync_url, pool_pre_ping=True)
    try:
        with engine.begin() as conn:
            for table, query in SUMMARY_QUERIES.items():
                print(f"\n=== {table} ===")
                row = conn.execute(text(query)).first()
                if row is None:
                    print("  (no rows)")
                    continue
                for key, value in row._mapping.items():
                    print(f"  {key}: {value}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-test**

```bash
DATABASE_URL=postgresql+psycopg://xenon_app:xenon_dev@localhost:5432/xenon_test \
  uv run python scripts/migrations/2026_04_26_verify_normalize_payloads.py
```

Expected: prints all 11 sections without errors. Most counts will be 0 in the test DB; the dev DB will show real numbers.

- [ ] **Step 3: Commit**

```bash
git add scripts/migrations/2026_04_26_verify_normalize_payloads.py
git commit -m "verify: report row counts + non-NULL coverage for normalize_payloads"
```

---

## Task 16: Run full backfill against the dev DB and report

**Goal:** turn the dev DB into the new shape.

(This is operator work, not test work — no commit at the end of each step.)

- [ ] **Step 1: Run the migration on the dev DB**

```bash
uv run alembic upgrade head
```

Expected: no errors. Table rewrites for uw_analyze_snapshots and uw_flow_events take a few seconds.

- [ ] **Step 2: Backfill uw_api_stats**

```bash
uv run python scripts/migrations/_2026_04_26_backfill_uw_api_stats.py
```

Expected: prints `backfilled N buckets`.

- [ ] **Step 3: Backfill VCG**

```bash
uv run python scripts/migrations/_2026_04_26_backfill_vcg_history.py
```

Expected: prints `backfilled 21 vcg rows` (20 history + 1 current, given the current `data/vcg.json`).

- [ ] **Step 4: Backfill UW analyze history**

```bash
uv run python scripts/migrations/_2026_04_26_backfill_uw_analyze_history.py
```

Expected: prints `backfilled N uw_analyze_snapshots rows`. Triggers fan out child tables automatically.

- [ ] **Step 5: Run verification**

```bash
uv run python scripts/migrations/2026_04_26_verify_normalize_payloads.py
```

Eyeball the report. Things to look for:

- `uw_analyze_snapshots`: `report_non_null` should equal `rows` (every row has a `report`).
- `uw_analyze_gex_strikes` and `uw_analyze_flow_alerts`: `snapshots` should be ≤ `uw_analyze_snapshots.rows`.
- `vcg_series`: `vcg_extracted` close to `rows`.
- `gex_snapshots`: 0 unless GEX has been re-run since the writer change.
- `uw_api_stats`: `buckets` matches the JSON file's bucket count.

- [ ] **Step 6: Spot-check a real query**

```bash
psql -h localhost -U xenon_app xenon_db -c "
SELECT ticker, snapshot_at, grade, bias, gex_sign, iv_rank, dp_signal
FROM xenon.uw_analyze_snapshots
ORDER BY snapshot_at DESC LIMIT 10;
"
```

Expected: rows return with all columns populated (where source data exists).

---

## Task 17: Run the full test suite

**Goal:** ensure nothing in the broader codebase regressed.

- [ ] **Step 1: Affected-only**

```bash
uv run python scripts/infra/dev/run_pytest_affected.py
```

Expected: PASS. Likely affected: anything touching `uw_analyze_snapshots`, `cri_series`, scanners, db/queries.

- [ ] **Step 2: Full Python suite**

```bash
uv run pytest -q
```

Expected: PASS. If anything fails because old `vrp_state`/`regime`/`flow_signals` keys are still referenced, fix the caller (typically a test fixture or a `dict(row._mapping)["vrp_state"]` access).

- [ ] **Step 3: Web suite (Vitest)**

```bash
cd web && npm test
```

Expected: PASS. Web typically only reads via FastAPI; if a route returns the old shape it will need updating, but that's a follow-up not in this plan.

- [ ] **Step 4: Final commit if any test fixes were needed**

```bash
git add -p
git commit -m "test: align fixtures with new uw_analyze_snapshots shape"
```

---

## Self-review notes

Spec coverage check (each section of the spec → task that implements it):

- Spec §"`cri_series`" → Task 1 step 3 + Task 5
- Spec §"`uw_analyze_snapshots`" → Task 1 step 2, Task 6, Task 7, Task 8
- Spec §"`scan_results` — split per scan_type" → Task 1 step 5, Task 10. Plan only ships GEX; other scan_types deferred to a follow-up driven by Task 0 step 1 findings (called out in the plan).
- Spec §"`uw_flow_events`" → Task 1 step 6 + step 7, Task 9
- Spec §"NEW `vcg_series`" → Task 1 step 4, Task 11, Task 13
- Spec §"`uw_api_stats` — backfill" → Task 12
- Spec §"Migration path" → Tasks 0–2, Task 16
- Spec §"Trigger DDL" → Task 2 step 3
- Spec §"Writer code changes" → Tasks 6, 7, 10, 11
- Spec §"Backfill scripts" → Tasks 12–14
- Spec §"Testing" → Tasks 4, 5, 7, 8, 9, 10, 11, 12, 13, 14
- Spec §"Sanity check" → Task 15

Open spec items deferred to Phase 0 reconnaissance (Task 0): exact `initial.*` keys for uw_flow_events generated columns; exact `daily_track` shape (object vs array) for the trigger DDL; non-GEX scan_types for sibling tables.
