# Postgres Migration Completion — Full-Pipeline Plan

> **Status:** Draft (planning only — no code yet). Created 2026-04-28.
> **Scope:** Close the file→Postgres migration end-to-end across order recording, fill capture, historical reading, and remaining JSON shims. Replaces the per-surface ad-hoc migration approach with one coherent pipeline plan.

---

## Goal

Make Postgres the single source of truth for the full order-and-trade pipeline:

```
place → submit (PG) → IB ack → fill (IB) → finalize (PG) → trades (PG) → reads (PG)
```

…and reduce `data/*.json` to either:

- **One-shot import buffers** processed once into PG (e.g. legacy `trade_log.json`), or
- **Adapter-boundary inputs** that never leave their adapter (e.g. `futu_portfolio.json`).

After this work, no Next.js or FastAPI runtime route reads or dual-writes file-backed JSON.

## Why now

Three independent symptoms share one root cause:

1. `Historical Trades (30 Days)` panel shows `Xenon API 502: Run with --setup for configuration guide.` — depends on optional IB Flex Query because PG `xenon.trades` is empty.
2. PG `xenon.order_submissions` rows from the past 3 days are stuck in `state=UNKNOWN` with `filled_qty=0` and `avg_fill_price=NULL` — fill outcomes are not being recorded.
3. Phase-2 file readers (`performance/portfolio/orders/journal/pi`) still hit `data/*.json` and silently go stale.

Root cause: **the submission→fill→trades transition is the broken handoff**. P&L, journal, blotter, performance, and Kelly all depend on it. Closing this gap is the keystone for completing migration.

## Current state — verified evidence

```
xenon.trades:                     1 row total (legacy_unknown scope)
xenon.order_submissions:          15 rows past 3 days
                                  states: UNKNOWN(7), PENDING(5), WORKING(2), REJECTED(1)
                                  no FILLED, no CANCELLED
xenon.order_events:               11 events
                                  kinds: REHYDRATE_UNCERTAIN, MODIFY, CANCEL, PREFLIGHT_ACK_LIMIT
                                  no FILL kind exists in code
ib_execute.py:345:                inserts to xenon.trades only on inline-watch path
orders_store.py:351 mark_finalized: updates state but does NOT insert to xenon.trades
single_leg_rehydrate.py:185:      writes UNKNOWN when execs snapshot empty + positions changed
```

## Target end state

```
                           ┌────────────────────────┐
   /orders/place ─────────►│ order_submissions (PG) │
                           └────────────┬───────────┘
                                        │ state: PENDING/WORKING
                                        ▼
                           ┌────────────────────────┐
   IB fill events ────────►│  order_events (PG)     │ kind: FILL/CANCEL/...
                           └────────────┬───────────┘
                                        │ state: FILLED/CANCELLED/REJECTED
                                        ▼
                           ┌────────────────────────┐
   On finalize ──────────► │     trades (PG)        │ realized P&L ledger
                           └────────────┬───────────┘
                                        │
        ┌───────────────────────────────┼───────────────────────────┐
        ▼                               ▼                           ▼
  /portfolio reads PG          /blotter reads PG            /journal reads PG
  (already shipped)            (W3)                         (W4)

  Files post-migration:
    trade_log.json         → backfill input only (one-shot)
    futu_portfolio.json    → Futu adapter boundary; OK
    orders.json            → DELETED
    gex.json/vcg.json      → DELETED (dual-write zombies)
    scanner/discover/cri/blotter/performance.json → either deleted or made TTL-cache-only
    Flex Query             → optional audit overlay only
```

---

## Workstreams

### W1 — Fill capture (KEYSTONE; blocks W3, W4-journal, W4-pi, W4-portfolio-entry)

**Goal:** Every IB fill lands in `xenon.trades` and `xenon.order_events.kind='FILL'`, regardless of whether the order took the inline or async-rehydrate path.

| #   | Change                                                           | Site                                                     | Notes                                                                     |
| --- | ---------------------------------------------------------------- | -------------------------------------------------------- | ------------------------------------------------------------------------- |
| 1.1 | Add `FILL` event kind                                            | `orders_store.py:351`, `mark_finalized()`                | `kind` is text — no schema migration                                      |
| 1.2 | Insert into `xenon.trades` on finalize                           | `orders_store.py::mark_finalized()`                      | Currently only `ib_execute.py:345` inserts; rehydrate path skips          |
| 1.3 | Diagnose + fix rehydrate exec-snapshot lookup                    | `single_leg_rehydrate.py:60-100`, IB pool `executions()` | Today's orders all `UNKNOWN`; either pool role or gateway-restart issue   |
| 1.4 | Enrich trades writes with `structure` + `decision`               | new helper in `orders_store`                             | `xenon.trades.structure` currently empty; needed for blotter/journal UX   |
| 1.5 | Combo wizard fill propagation                                    | `combo_wizard/rehydrate.py:144-170`                      | All combo legs roll into one `trades` row, leg detail in `metadata` JSONB |
| 1.6 | Backfill `xenon.trades` from `trade_log.json` historical entries | new migration script                                     | One-shot recovery of pre-PG history                                       |

**Tests:** unit (rehydrate decisions), integration (IB paper place→fill→PG row), backfill idempotency, dual-source sanity check (PG vs Flex on a recent day).

**Risk:** State-transition logic is delicate; a regression silently misreports fills. Mandatory paper-smoke + dual-source validation before flipping.

**Effort:** 3–5 days.

### W2 — Historical Trades UX fix (immediate; independent of W1)

**Goal:** Stop the red `502: Run with --setup` error. Show empty state until Flex configured or until W3 ships.

| #   | Change                                                                                            | Site                                                            |
| --- | ------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| 2.1 | Detect missing `IB_FLEX_TOKEN`/`IB_FLEX_QUERY_ID` in `/blotter`                                   | `server.py:2835-2842`                                           |
| 2.2 | Return 200 + `{configured: false, closed_trades: [], open_trades: [], as_of: null, message: ...}` | same                                                            |
| 2.3 | Frontend renders friendly empty state on `configured: false`                                      | `web/components/WorkspaceSections.tsx::HistoricalTradesSection` |
| 2.4 | Document `IB_FLEX_TOKEN` + `IB_FLEX_QUERY_ID` in CLAUDE.md credentials table + `.env.example`     | docs                                                            |

**Risk:** Trivial.

**Effort:** 30 min — 1 hour. Ships independent of all other work.

### W3 — Blotter PG-first, Flex as optional overlay (depends on W1)

**Goal:** `/blotter` reads `xenon.trades` joined with `order_submissions` aggregation; Flex used only as optional audit overlay.

| #   | Change                                                                                                              | Site                      |
| --- | ------------------------------------------------------------------------------------------------------------------- | ------------------------- |
| 3.1 | New `xenon.db.queries.blotter` module: PG → `BlotterTrade` shape                                                    | new file                  |
| 3.2 | `/blotter` route reads PG-first                                                                                     | `server.py:2835`          |
| 3.3 | Flex fallback path: PG empty AND Flex configured → run Flex                                                         | same                      |
| 3.4 | Optional overlay: when both configured, merge Flex executions into matching PG trades by `perm_id`; flag divergence | new merge logic           |
| 3.5 | Frontend stays the same JSON shape (`closed_trades` / `open_trades`)                                                | no change                 |
| 3.6 | "Source: PG" / "Source: Flex" / "Source: PG+Flex" pill in panel header                                              | `HistoricalTradesSection` |

**Risk:** Trade-grouping mapping. Flex groups executions into trades; PG `trades` is already trade-grain so simpler. Dual-source merge needs a divergence-tolerant join on `perm_id`.

**Effort:** 1–2 days post-W1.

### W4 — Hot read-side migration (Phase 2A from earlier audit)

**Goal:** Kill all `data/*.json` reads in `web/app/api/**` runtime paths.

| #   | Route                            | File:line                     | Source after migration                                              | Depends |
| --- | -------------------------------- | ----------------------------- | ------------------------------------------------------------------- | ------- |
| 4.1 | `/api/performance`               | `performance/route.ts:11`     | `account_snapshots.payload`                                         | none    |
| 4.2 | `/api/portfolio` entry-date join | `portfolio/route.ts:23`       | `xenon.trades.opened_at`                                            | W1      |
| 4.3 | `/api/orders` list               | `orders/route.ts:18`          | `xenon.order_submissions` query                                     | none    |
| 4.4 | `/api/orders/cancel`             | `cancel/route.ts:42`          | same                                                                | none    |
| 4.5 | `/api/orders/modify` (3 sites)   | `modify/route.ts:163,185,240` | same                                                                | none    |
| 4.6 | `/api/journal`                   | `journal/route.ts:7`          | `xenon.trades` + new `xenon.journal_entries` (table decision below) | W1      |
| 4.7 | `/api/journal/sync`              | full file                     | same                                                                | W1      |
| 4.8 | `/api/pi` (private investment)   | `pi/route.ts:91,346,372`      | PG                                                                  | W1      |
| 4.9 | `/api/futu/portfolio`            | `futu/portfolio/route.ts:9`   | DEFERRED ~2026-05-03 per existing memory                            | —       |

**Tests:** Vitest route-level + Playwright E2E per route group. CI guard tests pinning "stale JSON not read."

**Risk:** Hot user-facing routes; UI E2E mandatory per CLAUDE.md ⛔ rule 2.

**Effort:** ~2 days per route group → 3 PRs (orders trio | performance+portfolio | journal+pi).

### W5 — Dual-write removal (cleanup; post-W4)

**Goal:** Delete file writes that are now zombies.

| #   | Change                                                                        | Site                                                             |
| --- | ----------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| 5.1 | Drop `data/gex.json` write                                                    | `server.py:2734-2745` (PG `gex_snapshots` already authoritative) |
| 5.2 | Drop `data/vcg.json` write                                                    | `server.py:2552` (PG `vcg_series` already authoritative)         |
| 5.3 | Drop `data/orders.json` write                                                 | wherever; readers gone after W4                                  |
| 5.4 | Decision: keep or kill `scanner/discover/cri/blotter/performance.json` caches | `server.py:1289,1302,2456,2841,2856`                             |

**Risk:** Low — confirm no sub-CLI / script reads remain (W7 catches stragglers).

**Effort:** 1 day, single PR.

### W6 — Snapshot freshness + observability (cross-cutting)

**Goal:** Fail loudly when snapshots are stale; surface coverage metrics.

| #   | Change                                                                             | Site             |
| --- | ---------------------------------------------------------------------------------- | ---------------- |
| 6.1 | `_load_portfolio_view_sync` returns `(view, snapshot_at)`                          | `server.py:1466` |
| 6.2 | Add `PORTFOLIO_SNAPSHOT_STALE` reason if `now - snapshot_at > N` during open hours | preflight        |
| 6.3 | `/health` surfaces snapshotter heartbeat                                           | `/health` route  |
| 6.4 | Metric on `state=UNKNOWN` count in `xenon.order_submissions`; alarm if > threshold | observability    |
| 6.5 | "Source: PG / Flex / PG+Flex" pill                                                 | already in W3.6  |

**Risk:** Low.

**Effort:** 1 day; can fold into W1 or W3.

### W7 — CLI/script cleanup (Phase 3; opportunistic)

| #    | File                                                  | Action                                               |
| ---- | ----------------------------------------------------- | ---------------------------------------------------- |
| 7.1  | `src/xenon/scanners/trend/cli.py:850`                 | **Delete** — deprecated per CLAUDE.md                |
| 7.2  | `src/xenon/utils/incremental_sync.py`                 | Confirm dead → delete                                |
| 7.3  | `src/xenon/scanners/repair_cri_rvol_cache.py`         | One-shot script → delete after W5                    |
| 7.4  | `src/xenon/reports/portfolio_report.py`               | Migrate to PG read or accept as legacy               |
| 7.5  | `src/xenon/reports/free_trade_analyzer.py`            | Same                                                 |
| 7.6  | `src/xenon/reports/scenario_analysis.py:732`          | Same                                                 |
| 7.7  | `src/xenon/execution/naked_short_audit.py:300-302`    | Migrate to PG read                                   |
| 7.8  | `src/xenon/execution/ib_reconcile.py:280`             | Becomes append-only writer to `xenon.trades` post-W1 |
| 7.9  | `src/xenon/fetchers/fetch_analyst_ratings.py:55-57`   | Migrate to PG read                                   |
| 7.10 | `src/xenon/shares/generate_{gex,vcg,regime}_share.py` | Migrate to PG read                                   |

**Risk:** Low per file; many likely dead.

**Effort:** 0.5 day per file, opportunistic.

---

## Cross-cutting policy decisions (resolve once before W1 lands)

| #   | Decision                 | Recommendation                                                                                            |
| --- | ------------------------ | --------------------------------------------------------------------------------------------------------- |
| P1  | `xenon.trades` row grain | Keep current "logical position open→close" — 1 row per round-trip (preserves blotter UX)                  |
| P2  | Combo trade rolling      | One `trades` row per combo, leg detail in `metadata` JSONB                                                |
| P3  | Journal table            | New `xenon.journal_entries` (separate write cadence + audit trail), not extending `trades.metadata`       |
| P4  | Flex Query role          | Optional audit overlay only; nightly job flags PG↔Flex divergence in `/health`; never user-facing primary |
| P5  | Snapshot staleness       | `XENON_PORTFOLIO_SNAPSHOT_STALE_S=300` open / `1800` closed                                               |
| P6  | `state=UNKNOWN` policy   | Alarmable, never silently ignored. W6.4 surfaces count.                                                   |

---

## Dependencies

```
W2 (UX fix) ─── independent ────────────────────────────────────┐
                                                                │
W1 (fill capture) ──┬──► W3 (blotter PG-first)                  │
                    │                                           │
                    ├──► W4.2 (portfolio entry-date)            │
                    ├──► W4.6 (journal)                         │
                    ├──► W4.7 (journal sync)                    │
                    ├──► W4.8 (pi)                              │
                    └──► W7.8 (ib_reconcile rewrite)            │
                                                                │
W4.1 (performance), W4.3-4.5 (orders trio) ─── independent ─────┤
                                                                │
W5 (dual-write removal) ──► requires W4 routes done             │
                                                                │
W6 (observability) ──► folds into W1 or W3                      │
                                                                │
W7 (script cleanup) ──► opportunistic, post-W4 mostly           │
```

## Suggested execution order

```
Day 0     W2                                          ← unblock UX immediately
Week 1    W1                                          ← keystone
Week 2    W3 + W4.3-4.5 (orders trio)
Week 3    W4.1 (performance) + W4.2 (portfolio entry) + W4.6/4.7 (journal)
Week 4    W5 (cleanup) + W6 (observability)
Ongoing   W7 (opportunistic)
Deferred  W4.9 (Futu, ~2026-05-03 per existing memory)
```

**Minimum cut to fix the user-visible historical-trades issue:** W2 + W1 + W3 (~1 week). Resolves the 502, fixes the data integrity bug, makes blotter PG-native.

**Full migration completion:** ~3–4 weeks of focused work.

## Acceptance criteria

- [ ] `xenon.trades` populated for every IB fill (paper + live), regardless of inline vs async path.
- [ ] Zero `state=UNKNOWN` rows older than 1 hour in `xenon.order_submissions` during normal operation.
- [ ] `/blotter` returns 200 + valid `closed_trades`/`open_trades` from PG even with `IB_FLEX_TOKEN` unset.
- [ ] No `web/app/api/**` runtime route reads `data/*.json`.
- [ ] `data/gex.json`, `data/vcg.json`, `data/orders.json` not written.
- [ ] CI guard tests prove stale-JSON-not-read for migrated routes (mirror existing `test_vcg_json_not_read.py` pattern).
- [ ] Dual-source PG↔Flex divergence check runs nightly; result surfaced in `/health`.
- [ ] CLAUDE.md credentials table includes `IB_FLEX_TOKEN` + `IB_FLEX_QUERY_ID` (now optional).
- [ ] All paper-smoke + browser E2E gates pass per CLAUDE.md ⛔ rule 2.

## Non-goals

- Migrating Futu portfolio cache (deferred ~2026-05-03 per existing memory + plan).
- Deleting Flex Query support entirely — kept as optional audit overlay.
- Migrating `data/watchlist.json` (config file, not runtime DB).
- Migrating `data/trend_scan.json` (deprecated scanner; W7.1 deletes the producer).
- Refactoring report scripts that aren't on hot paths (W7 is opportunistic).

## Open questions for review

1. **Q1**: Is the trade-grain decision (P1) correct for combo + roll scenarios, or should we model legs as separate `trades` rows joined by `position_group_id`?
2. **Q2**: For W1.3 — is the `UNKNOWN` cluster from gateway-restart-loses-executions, or from rehydrate using the wrong pool client role? Need a one-time diagnostic before designing the fix.
3. **Q3**: For W4.6/4.7 — does the journal need to retain the `IB_AUTO_IMPORT` flow exactly, or can journal-sync be replaced by a PG-native event listener on `xenon.trades` writes?
4. **Q4**: For W5.4 — keep scan caches as TTL-bounded files or delete them entirely and rely on PG `scan_results` for freshness? Memory cost vs read latency trade-off.
5. **Q5**: For Flex divergence checks (P4 / W6) — what divergence tolerance is acceptable? Exact perm_id match? Or fuzzy on price/qty within ε?

---

## Provenance

This plan supersedes the per-surface migration approach in:

- `docs/plans/2026-04-27-portfolio-postgres-read-path.md` (Phase 1 — shipped in PR #56)
- `docs/plans/2026-04-27-order-placement-reliability.md` (W1 prerequisites — shipped in PR #61)
- `docs/plans/2026-04-27-futu-postgres-migration-followup.md` (W4.9 — deferred per existing scope)

Existing memory references that informed this plan:

- `project_postgres_migration_read_side_gap` — original Phase-1/2 split.
- `feedback_in_process_route_bypass` — relevant for W4 route guards.
- `feedback_testclient_skips_lifespan` — relevant for W4 test harness.

---

# Revision 1 (post-Codex review 2026-04-28)

> **Status:** Adopted. Codex returned `PASS_WITH_CHANGES` against revision 0; this section captures the architectural pivot and supersedes any conflicting content above. Implementation tasks moved to companion `-IMPL.md` Revision 1.

## What changed

Codex correctly identified that revision 0's keystone (a single `mark_finalized` call writing both lifecycle events and `xenon.trades` rows as deltas) cannot be idempotent under reconnects, partial fills, or combo legs because:

- `xenon.trades` has no `submission_id` / `perm_id` / `exec_id` linkage to the source fill (`src/xenon/db/schema.py:78-101`).
- `order_events` has no idempotency key (`src/xenon/db/schema.py:169-183`); rehydrate already aggregates by `perm_id` and discards execution identity (`src/xenon/execution/single_leg_rehydrate.py:235-263`).
- Combo wizard fills key off `wizard_combo_attempts.attempt_id`, not `order_submissions.submission_id` (`src/xenon/db/schema.py:222-244`), so a `submission_id`-keyed finalize call is wrong for combos.

Plus three name slips that would have broken first-day execution:

- Real symbol is `mark_terminal`, not `mark_finalized` (`src/xenon/execution/orders_store.py:348`).
- Real outbox is `events.outbox` (in the `events` schema), not `xenon.outbox` (`src/xenon/db/schema.py:720-732`).
- Pool roles are `sync` / `data` (and `orders`), not `exec` (`src/xenon/api/server.py:793,1733; src/xenon/api/ib_pool.py:98`).

## Locked architectural decisions (Q1–Q4)

| #   | Decision                                                                                                                                                                                                                                                                                              |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Q1  | **`xenon.trades` is a derived logical-position view** maintained on top of an immutable execution-grain ledger `xenon.order_fills`. Trades shape stays compatible with current UI; correctness flows from fills.                                                                                      |
| Q2  | **External IB trades are in scope.** `ib_reconcile` (W7.8) is promoted to before W4.7 and becomes a writer to `xenon.order_fills`. Journal sync removal happens after that.                                                                                                                           |
| Q3  | **`/api/performance` recomputes from PG on demand** with route-layer TTL cache (5min open / 30min closed). `portfolio_performance.py` migrates to PG read; no JSON intermediate.                                                                                                                      |
| Q4  | **Outbox uses two channels**: `fill.recorded` (every `order_fills` insert) and `trade.closed` (every `trades.closed_at` set). Journal listens to both; performance/analytics listens to `trade.closed` only; blotter reads on demand. Channel names validated against `src/xenon/db/events.py:16-29`. |

## Revised target end state

```
                           ┌────────────────────────┐
   /orders/place ─────────►│ order_submissions (PG) │  state machine
                           └────────────┬───────────┘
                                        │
                                        ▼
                           ┌────────────────────────┐
   IB exec events ────────►│  order_fills (PG, NEW) │  immutable execution ledger
                           └────────────┬───────────┘  PK: exec_id
                                        │              FK: submission_id
                                        │              keys: perm_id, con_id
                                        ▼
                           ┌────────────────────────┐
   On fill aggregate ─────►│     trades (PG)        │  derived logical-position view
                           │     (now FK-linked)    │  +submission_id, +combo_attempt_id
                           └────────────┬───────────┘
                                        │
                                        ▼
                           ┌────────────────────────┐
                           │  events.outbox (PG)    │  channels: fill.recorded, trade.closed
                           └────────────┬───────────┘
                                        │
        ┌───────────────────────────────┼──────────────────────────┐
        ▼                               ▼                          ▼
  /portfolio (done)            /blotter (W3, PG-first)     /journal (W4.6, PG-native)
                               /performance (W4.1, PG)     ib_reconcile (W7.8) ──► fills writer

  Files post-migration:
    trade_log.json     → backfill input only (one-shot into order_fills + journal_entries)
    futu_portfolio.json→ Futu adapter input (deferred per existing plan)
    orders.json        → DELETED
    gex.json/vcg.json  → DELETED
    Flex Query         → optional audit overlay only
```

## New workstream W0 — Schema + Naming Foundation

Inserted before W1. Contents:

- Add `xenon.order_fills` table (PK `exec_id`, FK `submission_id`, columns: `perm_id`, `con_id`, `side`, `qty`, `price`, `commission`, `filled_at`, broker scope).
- Add `submission_id` + `combo_attempt_id` nullable FKs to `xenon.trades`.
- Alembic migrations + idempotent backfill from existing `order_events.kind=FILL` (when present) and `trade_log.json` legacy entries.
- Symbol-name correction sweep across all subsequent task descriptions.
- Lock outbox channel names (`fill.recorded`, `trade.closed`) and emit helper.

Effort: **+1.5–2 days**, single PR, blocks W1 entry.

## Revised workstream sequence

```
Day 0       W2 (UX fix) — independent, ships immediately
Week 1A     W0 (schema + naming) — NEW; blocks W1
Week 1B     W1 (fill capture, now writing order_fills first) — keystone
Week 2A     W7.8 (ib_reconcile → order_fills writer) — PROMOTED before W4.7
Week 2B     W3 (blotter PG-first, can begin after W1.1+W1.2)
Week 2C     W4.3-4.5 (orders trio) — independent
Week 3A     W4.1 (performance) — now includes portfolio_performance.py PG migration
Week 3B     W4.2 (portfolio entry-date), W4.6+W4.7 (journal + sync)
Week 3C     W4.8 (pi)
Week 4      W5 (dual-write removal) + W6 (observability)
Ongoing     W7 (CLI cleanup, except W7.8 which moved up)
Deferred    W4.9 (Futu)
```

## Revised effort

| Workstream | Original      | Revised        | Delta                                       |
| ---------- | ------------- | -------------- | ------------------------------------------- |
| W0 (new)   | —             | 1.5–2 d        | +2 d                                        |
| W1         | 3–5 d         | 4–6 d          | +1 d (now writes fills before trades)       |
| W4.1       | 1 d           | 2–3 d          | +1.5 d (portfolio_performance.py migration) |
| W7.8       | 1 d (ongoing) | 1 d (promoted) | 0                                           |
| **Total**  | 3–4 wk        | **3.5–4.5 wk** | **+3–4 d**                                  |

## Revised acceptance criteria (additions)

- [ ] Every IB fill produces exactly one `xenon.order_fills` row (idempotent under replay).
- [ ] `xenon.trades` is reproducible from `order_fills` alone — re-aggregation is a no-op when source is unchanged.
- [ ] `events.outbox` emits `fill.recorded` on every fill insert and `trade.closed` on every close transition, in the same transaction as the source write (never racy thanks to post-commit `pg_notify`).
- [ ] `ib_reconcile` writes to `order_fills` for IB-external fills before W4.7 removes journal sync.
- [ ] `portfolio_performance.py` reads PG only — no `data/portfolio.json` or `data/blotter.json` reads.
- [ ] All references to `mark_finalized`, `xenon.outbox`, and pool role `'exec'` are eradicated from code, plans, tests, and docs.

## Revised non-goals (additions)

- Migrating `xenon.trades` to per-execution grain (Codex alternative B). The derived-view approach (Q1=A) preserves UI contracts while gaining correctness.
- Backwards-compatible double-writes from `ib_execute.py` — Codex alternative #3 rejected. The legacy manual CLI does not produce `order_submissions` rows, so it cannot route through `mark_terminal`. Instead, the CLI is migrated to write `order_fills` directly with a synthetic `submission_id="legacy_cli:<exec_id>"` marker.

## Codex review provenance

- Review run: 2026-04-28, verdict `PASS_WITH_CHANGES`.
- Critical issues 1–4 all addressed by W0 + Q1=A + Q2=A + Q4=A locks.
- Major concerns 1–9 addressed in revised IMPL plan task descriptions.
- Minor findings (pool role, BlotterData type, scope defaults) folded into respective tasks.
- Alternatives 1 (execution ledger) accepted; 2 (journal as own PR) accepted via dependency promotion; 3 (shadow-write) rejected with reasoning above; 4 (Flex daily replay) accepted as supplemental cleanup, not primary capture.
