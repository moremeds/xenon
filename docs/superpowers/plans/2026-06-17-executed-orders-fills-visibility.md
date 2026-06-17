# Executed-Orders / Fills Visibility — Implementation Plan

**Goal:** Make "Today's Executed Orders" (and Realized P&L) reflect intraday fills for both brokers, including externally-placed (mobile/TWS) IB fills.

**Context (diagnosed 2026-06-17, prod core_dev):**

- Futu: `futu_trades` (panel source) is written only by the nightly 16:30 ET deal sync → today's Futu fills invisible until tonight.
- IB: pool `reqExecutions` is own-client (no master client ID on the Gateway) → manually/mobile-placed fills (e.g. SPX P 6855 BUY) never reach `order_fills`; the `snapshot-<permId>` row stays WORKING. Probe confirmed clientId 0 and clientId 6 both return 0 executions. The fill _does_ show as a **position** in `account_snapshots` (parity with radon, which also has `executed_orders: []`).

## Global constraints

- `uv` for all Python; TDD red→green; scope on every write; `XENON_READ_ONLY=1` no-ops on new persistence; order-path incident-history row appended; no `Co-Authored-By` trailer.

---

## Task 1 — Futu derive-from-orders ✅ DONE (commit b6885287)

Order-grain fallback in `_futu_orders_payload`: FILLED `futu_orders` (US, filled_qty>0, today) → executed rows, deduped vs deal rows by `futu_order_id`. Test: `test_orders_futu_branch.py`.

## Task 2 — IB Flex reconcile → order_fills (Route C) [Fix 3, IB priority]

**Files:** Create `src/xenon/api/services/flex_fill_reconcile.py`; test `scripts/tests/test_flex_fill_reconcile.py`. Wire into the existing Flex/blotter job (daily cadence) — NOT the live pool path.
**Logic:**

1. `FlexQueryClient(token, query_id).fetch_executions(days_back=N)` → `Execution[]` (has `exec_id`, `perm_id`, side, qty, price, commission, time, sec_type, strike/right/expiry).
2. For each: resolve `perm_id → submission_id` against `order_submissions` (scope-filtered); orphans get `submission_id=None` + `metadata={"legacy_source":"flex"}` (satisfies `ck_fills_source_present`). Map `Side` → order_fills side string (match `_record_external_fills` convention — verify BOT/SLD vs BUY/SELL). Call `orders_store.record_fill(...)` — idempotent on `exec_id`, so re-runs are no-ops and live-mirrored fills (same `exec_id`) never double-insert.
3. Reconcile: for WORKING/PARTIALLY_FILLED `snapshot-*` rows whose `perm_id` now has covering `order_fills` qty, `orders_store.mark_terminal(state="FILLED", expected_states=("WORKING","PARTIALLY_FILLED"))` + `record_event("RECONCILED", source="flex_reconcile")`. Reuse the `sweep_disappeared_orders` qty logic.
4. Honor `XENON_READ_ONLY=1` (no writes).
   **Tests:** missing Flex exec inserted; duplicate `exec_id` is a no-op; orphan (no submission) inserts with legacy_source; WORKING snapshot with covering fills → FILLED; read-only no-op.

## Task 3 — Detect external-blind IB feed in /health [Fix 2]

**Files:** `src/xenon/api/server.py` (`_ib_feed_health()` helper + add to `/health` + `/admin/operator`); test `src/xenon/api/tests/`.
**Logic:** Flag `degraded` when the pool reports `connected: true` for all roles but the last poller tick saw `open_orders.registered == 0` AND `fills.inserted == 0` over K consecutive ticks while the account snapshot has open positions/orders — i.e. the feed is "blind." Surface `{state: ok|degraded, reason}`. Tune to avoid false positives on genuinely-empty accounts (gate on snapshot having positions). Heartbeat fields recorded by `activity_poller_loop` (extend `record_service_health`).
**Tests:** blind-feed state → degraded; healthy feed → ok; empty account → ok (no false positive).

## Task 4 — Futu intraday deal sync → futu_trades [Fix 4]

**Files:** `src/xenon/clients/futu_client.py` (`fetch_today_deals()` via `history_deal_list_query` today-window, rate-limit aware); `src/xenon/api/server.py` `/futu/sync` writes today's deals to `futu_trades`; test. **Coordinate with in-flight `feat/futu-orders-fees-ingestion`** (touches `futu_history_sync.py` + `futu_client.py`) to avoid collision.
**Logic:** On `/futu/sync`, pull today's US deals, UPSERT into `futu_trades` (dedup by `futu_deal_id`). Then Task 1's order-grain fallback dedupes those out (futu_order_id now has a deal) → deal-grain takes over with fees. Honor read-only.
**Tests:** today deals UPSERTed; idempotent; Task-1 dedup defers to deal rows.

## Verification / ship

- Per-task `uv run pytest` green; full Futu+orders+health batches green.
- Paper validation for IB feed/health where feasible; Flex reconcile validated against a seeded order_fills + stubbed FlexQueryClient.
- Append order-path incident-history row (Task 2 + 3 touch the fill path).
- Bundle Tasks 1–4 → one PR → CI → release → deploy.
