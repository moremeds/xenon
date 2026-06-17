# Futu Order Querying — Design Spec

**Date:** 2026-06-17
**Branch:** `feat/futu-orders-query`
**Status:** Approved design → ready for implementation plan

## Goal

Surface **Futu account orders** in the terminal the same way IB orders are surfaced today —
read-only (no order placement), unified onto the IB data structure, and exposed via the existing
RESTful API. Four UI surfaces must populate for the Futu account:

1. **OPEN ORDERS** — live working orders (e.g. resting GTC limits).
2. **TODAY'S EXECUTED ORDERS** — today's fills.
3. **HISTORICAL TRADES (30 DAYS)** — closed trades with realized P&L / cost basis / proceeds.
4. **TRADE JOURNAL** — auto-imported closed-trade rows (Futu analogue of `IB_AUTO_IMPORT`).

**Explicitly out of scope:** placing / modifying / cancelling Futu orders; touching IB's order
path or the `xenon.trades` table; changing the existing live Futu *positions* path.

## Non-negotiable principle: DB-first

Everything is **written to Postgres by a sync writer, then read by the API, then rendered by the UI.**
The HTTP request path never calls Futu OpenD. This matches the repo invariant
("Everything writes to Postgres first; the website only reads") and the CI write-side guard
`scripts/checks/no_json_write_on_order_path.py`.

> Note: the *current* Futu **positions** path is live-fetch-on-request
> (`POST /futu/portfolio` → live OpenD, cached 10s) — **not** DB-first. This new orders work does
> not follow that pattern; it is DB-first. The positions path is left untouched.

All new persistence honors `XENON_READ_ONLY=1` (no-op writes under read-only live sessions).

## Decisions locked in brainstorming

| Question | Decision |
| --- | --- |
| Unmerged prior art `9e8a2a90` (`feat/futu-orders-fees-ingestion`) | **Start fresh** from master; use the commit as a *design reference* (it already built `futu_orders` + `futu_order_fees` tables, `fetch_history_orders`/`fetch_order_fees`, query helpers, an Alembic migration, and 32 tests — but is ~44k lines behind master and history-only). |
| Live open/working orders | **Full parity** — pull via `order_list_query`, persist, poll during market hours so OPEN ORDERS stays fresh. |
| Journal population | **FIFO closed-trade reconstruction** → `FUTU_AUTO_IMPORT` rows with realized P&L. |
| Closed-trade storage | **Dedicated `xenon.futu_closed_trades` table** (the `trades` table is locked to `broker='IB'` by `ck_trades_broker_ib_only` and is IB-shaped). |
| Open-orders poll cadence | Short market-hours poll into the DB (acceptable freshness on the order of the sync cadence). |
| First slice | Ship the full feature (no thinner slice requested). |

## Data flow

```
Futu OpenD ──(sync writer: poll loop + xenon-futu-history-sync CLI + POST /futu/sync)──▶ Postgres
                                                                                            │ read-only
   GET /orders?broker=FUTU · GET /blotter?broker=FUTU · GET /journal?broker=FUTU
                                                                                            │
                          Next API routes (thread activeAccount → ?broker=) ──▶ UI sections
```

## Component design

### 1. New Postgres tables (mirror the existing `xenon.futu_trades` pattern)

All keyed/scoped by `(broker, account_env, broker_account, …)` per the Broker Account Scope policy.
UPSERT semantics so re-pulls re-stamp mutable fields without duplicating rows. `raw` JSONB preserves
the original OpenD payload on every table.

- **`xenon.futu_orders`** — open/working **and** historical orders. PK
  `(broker, account_env, broker_account, futu_order_id)`. Columns map to the unified `OpenOrder`
  shape: `ticker`, `futu_code`, `market`, `action`, `order_type`, `quantity`, `limit_price`,
  `aux_price` (stop/trigger), `status`, `tif`, `created_at`, `updated_at`, `filled_qty`,
  `avg_fill_price`, `raw`.
- **`xenon.futu_order_fees`** — per-order fee snapshot. PK
  `(broker, account_env, broker_account, futu_order_id)`.
- **`xenon.futu_closed_trades`** — FIFO-reconstructed closed lots. Columns:
  `ticker`, `structure`, `action`, `quantity`, `entry_cost`, `exit_cost`,
  **`realized_pnl`, `cost_basis`, `proceeds`**, `opened_at`, `closed_at`, `metadata`.
  A stable synthetic close id (`futu_close_id`) provides idempotency and the journal dedup key.
  Feeds **both** the 30-day HISTORICAL TRADES table and the journal.

Schema lives in `src/xenon/db/schema.py`; one Alembic migration adds all three tables
(autogenerate, then `uv run alembic upgrade head` against the **dev** DB only — the macmini Docker
`migrator` applies to `core_dev` on deploy). Per-table batch chunking for the wide `futu_orders`
insert (asyncpg 32767-bind ceiling) — same lesson as the reference commit.

### 2. Sync writers (ingestion → DB)

Extend `FutuClient` (`src/xenon/clients/futu_client.py`) and the sync service
(`src/xenon/api/services/futu_history_sync.py`); add query helpers in
`src/xenon/db/queries/futu_history.py`:

- `fetch_open_orders()` → `order_list_query` (live working orders) → UPSERT `futu_orders`.
- `fetch_history_orders()` → `history_order_list_query`; `fetch_order_fees()` → per-order fees
  (with a fee throttle so the call isn't rate-limited). Rate limits respected (10 calls / 30s).
- **Shared FIFO lot-matcher:** extract the matching logic from
  `src/xenon/api/services/futu_nav_backfill.py::_compute_daily_realized_pnl` so it emits closed-trade
  **lots** (entry/exit, cost basis, proceeds, realized P&L incl. the 100× OCC options multiplier),
  not just daily sums. NAV's daily P&L then derives from the same lots — **one FIFO source of truth,
  no drift.** Lots persist to `futu_closed_trades`.
- After persisting closed trades, **directly upsert `FUTU_AUTO_IMPORT` journal rows** (one per closed
  lot, realized P&L in metadata) within the sync's transaction. No outbox wiring (the batch sync owns
  the transaction, unlike IB's event-driven fills).
- Triggers (all honor `XENON_READ_ONLY=1`):
  - existing 16:30 ET history loop (`_maybe_start_futu_history_loop`) — now also pulls orders/fees/closed-trades,
  - `xenon-futu-history-sync` CLI,
  - a **market-hours open-orders poll** (default 60s, env-configurable, gated off outside RTH) so
    OPEN ORDERS stays fresh,
  - `POST /futu/sync` also refreshes orders.

### 3. API unification (read path — the "unify with IB" requirement)

The unification is a **response-shape** concern, not storage. No new response contract.

- `orders_payload_for_scope(scope)` in `src/xenon/api/routes/orders.py` gains a
  **`scope.broker == "FUTU"` branch**: reads `futu_orders` (active statuses) + `futu_trades`
  (today's fills) and shapes them to the *same* `OpenOrder` / `ExecutedOrder` types IB uses.
- `get_account_scope` (`src/xenon/api/guards.py`) accepts `?broker=FUTU` and resolves the Futu scope
  via the existing matched-account `_scope_factory` (account id + env from `_matched_trd_env`) — the
  **same mechanism `get_performance_scope` already uses** for `?broker=FUTU`. Default stays IB.
- `/blotter?broker=FUTU` → reads `futu_closed_trades`, shaped to the existing blotter response.
- `/journal?broker=FUTU` → works once scope resolves (the `journal_entries` table already allows
  `broker IN ('IB','FUTU')`).

### 4. Journal auto-import (Futu)

- `journal_entries` already permits `broker='FUTU'`. The existing partial-unique index
  `uq_journal_auto_import` is `WHERE decision='IB_AUTO_IMPORT' AND trade_id IS NOT NULL` and does
  **not** cover Futu (Futu rows use `trade_id IS NULL`, since `trade_id` FKs the IB-only `trades`
  table). Add a **new partial-unique index** for Futu dedup, e.g. on
  `(broker, account_env, broker_account, (metadata->>'futu_close_id'))
  WHERE decision='FUTU_AUTO_IMPORT'`.
- Add `upsert_futu_auto_import_entry(...)` in `src/xenon/db/queries/journal.py` (sibling of
  `upsert_auto_import_entry`); store realized P&L / cost basis / proceeds / open+close dates in
  `metadata`. `trade_id` stays NULL.

### 5. Frontend wiring

`activeAccount` (`"ib" | "futu"`, `web/lib/accountContext.ts`) already exists and already switches the
portfolio. Today `OrdersSections` / `JournalSections` (`web/components/WorkspaceSections.tsx`) receive
`activeAccount` but ignore it (hardcoded IB).

- Thread `activeAccount` into `useOrders` / `useBlotter` / `useJournal` → those hooks append
  `?broker=IB|FUTU` to the Next API routes (`web/app/api/orders/route.ts`,
  `web/app/api/blotter/route.ts`, `web/app/api/journal/route.ts`) → `xenonFetch` forwards the param to
  FastAPI.
- The OPEN ORDERS / EXECUTED / HISTORICAL / JOURNAL sections then render Futu data with the
  **identical components** (no new tables). Order-entry / modify / cancel buttons stay suppressed on
  Futu rows (read-only, as positions already are).

### 6. Futu order types (exploration requirement) — display-faithful, no placement

Futu's SDK exposes **18 `OrderType` values** vs IB's effective LMT/MKT. We do not *place* them; we
store and display what OpenD reports:

- `NORMAL → LMT`, `MARKET → MKT` (parity with IB display).
- `ABSOLUTE_LIMIT`, `AUCTION`, `AUCTION_LIMIT`, `SPECIAL_LIMIT`, `SPECIAL_LIMIT_ALL`, `STOP`,
  `STOP_LIMIT`, `MARKET_IF_TOUCHED`, `LIMIT_IF_TOUCHED`, `TRAILING_STOP`, `TRAILING_STOP_LIMIT`,
  `TWAP`, `TWAP_LIMIT`, `VWAP`, `VWAP_LIMIT` → shown as their Futu label in the TYPE column.
- **TIF:** `DAY`, `GTC` (same two as IB; this SDK version has no GTD).
- **Status:** the 17-value `OrderStatus` enum
  (`UNSUBMITTED/WAITING_SUBMIT/SUBMITTING/SUBMIT_FAILED/TIMEOUT/SUBMITTED/FILLED_PART/FILLED_ALL/
  CANCELLING_*/CANCELLED_*/FAILED/DISABLED/DELETED/FILL_CANCELLED`) maps onto the existing display
  states (`PendingSubmit/Submitted/PartiallyFilled/Filled/Cancelled`).
- **Side:** `BUY/SELL/SELL_SHORT/BUY_BACK` — normalized `SELL_SHORT→SELL`, `BUY_BACK→BUY` (the
  existing `futu_trades` normalization).

## Testing (red/green TDD)

- **Client:** OpenD frame parsing for orders/fees + throttle behavior (mirror the 14 tests in the
  reference commit).
- **FIFO lot-matcher:** closed-lot round-trip incl. options multiplier; NAV daily-sum equals
  sum-of-lots (single source of truth).
- **Queries:** idempotent UPSERT for `futu_orders` / `futu_order_fees` / `futu_closed_trades`; scope
  isolation; batch chunking under the bind ceiling.
- **API:** `orders_payload_for_scope` FUTU branch produces shape-parity with the IB `OpenOrder` /
  `ExecutedOrder` payloads; `?broker=FUTU` scope resolution; blotter + journal FUTU branches.
- **Journal:** `FUTU_AUTO_IMPORT` dedup index prevents duplicates on re-sync.
- **Frontend:** Vitest for scope-threaded hooks (`useOrders`/`useBlotter`/`useJournal` pass `broker`).
- **E2E (chrome-cdp):** switching to the Futu account fills all four sections; order buttons absent.

## Key file map

| Concern | File |
| --- | --- |
| Futu client (add fetchers) | `src/xenon/clients/futu_client.py` |
| Sync service (orders/fees/closed-trades + journal upsert) | `src/xenon/api/services/futu_history_sync.py` |
| FIFO matcher (extract shared lot engine) | `src/xenon/api/services/futu_nav_backfill.py` |
| Tables + scope | `src/xenon/db/schema.py` + new Alembic migration |
| Query helpers | `src/xenon/db/queries/futu_history.py`, `src/xenon/db/queries/journal.py` |
| Orders read branch | `src/xenon/api/routes/orders.py` |
| Scope from `?broker=` | `src/xenon/api/guards.py` |
| Frontend account state | `web/lib/accountContext.ts` |
| Sections (thread broker) | `web/components/WorkspaceSections.tsx` + `useOrders`/`useBlotter`/`useJournal` |
| Next API proxies | `web/app/api/{orders,blotter,journal}/route.ts` |

## Reference: prior-art commit

`9e8a2a90` on `feat/futu-orders-fees-ingestion` — read for the table/ingestion shape and the
asyncpg batch-chunking lesson. Do not rebase it (stale); reimplement fresh on master.
