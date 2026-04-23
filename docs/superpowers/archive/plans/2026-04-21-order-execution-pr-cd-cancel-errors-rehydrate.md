# PR-C/D: Cancel/Modify + Error Propagation + Rehydrate — Straight-Through Plan (F5+F6+F7)

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans`. Steps use `- [ ]` checkboxes.

**Goal:** Deliver phases **F5** (owner-preserving cancel/modify failure propagation), **F6** (reason-code toast library + HTTP-error preservation in Next.js routes), and **F7** (boot-time rehydration of in-flight orders) as a single coordinated effort. UI verification runs **once after F7** — not per-phase.

**Rationale for bundling three phases:** F5, F6, F7 share two render surfaces (`OrderTab.tsx` error states, `orders_submissions` state transitions) and one telemetry channel (`orders_events`). Shipping separately triples the manual browser QA cycles for the same end-user surface.

**Tech Stack:** Python 3.13 (FastAPI, ib_async, DuckDB, pydantic v2, pytest), TypeScript (Next.js App Router, Vitest, Playwright), IB Gateway paper port 4002.

**Source specs:**

- `docs/superpowers/specs/2026-04-20-single-leg-hardening-design.md` §6, §8, §10, §11, §12 (schema reuse), §13.1
- `docs/superpowers/plans/2026-04-20-order-execution-foundation-master.md` (master plan)
- `src/xenon/api/CLAUDE.md` §"Cancel / Modify Failure Propagation" (policy authority)

**Dependencies / prior phase:** PR-A (F1+F2) and PR-B (F3+F4) are merged on master as of 2026-04-21. `orders.duckdb` schema, `orders_store.reserve_attempt`, `quote_guard.check`, `preflight.evaluate`, `contract_normalize`, `universe.py` are all live.

---

## Scope boundary

**In scope:**

- **F5:** failure classification in `ib_order_manage.py` (connection / ib_reject / ownership); 326-retry-as-same-owner loop; `ib_pool` clientId registry so concurrent audit (client 25) and cancel don't race; `modify_sequence` stale-modify rejection; `/orders/cancel` + `/orders/modify` route wiring that maps classified errors to 503 / 4xx / 409.
- **F6:** `web/lib/orderReasonCodes.ts` single source of reason-code → toast copy mapping for the 13 codes in SL §6; `OrderTab.tsx` consumes it (no inline strings); Next.js `/api/orders/{place,cancel,modify}/route.ts` preserve upstream HTTP status + `detail` verbatim; `OrderTab` renders explicit FAILED state on cancel/modify 503/409 (no optimistic success).
- **F7:** `src/xenon/execution/single_leg_rehydrate.py` with three-source reconcile (`reqAllOpenOrders`, `executionDetails`, `positions`); boot-time hook in FastAPI lifespan; `orders_events` rows (`REHYDRATE_RECONCILED`, `REHYDRATE_UNCERTAIN`); `PENDING_TIMEOUT` branch for orphaned PENDING rows older than 60s.

**Out of scope (explicitly NOT in this PR):**

- Wizard (W1+) — blocked by burn-in per master plan.
- Changing the subprocess / pool architecture — the ownership rule is preserved verbatim (`src/xenon/api/CLAUDE.md`).
- Quote-token secret rotation policy (§15 open question).
- BAG/combo-specific cancel or rehydrate edge cases — combo flow bypasses preflight already; rehydrate treats BAG as opaque and reconciles via `perm_id` the same way.
- Deprecating `data/orders.json` — §12.1 dictates the audit keeps reading JSON until a separate deprecation PR. F7 adds the DuckDB reader side-by-side; it does not remove the JSON reader.
- UI redesign of cancel/modify affordances beyond the explicit FAILED toast.
- Multi-user auth expansion — `user_id="local"` sentinel already on master.

If a task seems to require one of the above, stop and push back — it is scope creep.

---

## Success criteria

1. **Cancel ownership race resolved:** with `CLIENT_IDS["ib_order_manage"]=25` held by a synthetic audit process, a concurrent `/orders/cancel` for a different order returns `409 OWNERSHIP` after 3 retries, never rotates clientId. Proven by `test_cancel_ownership_contention.py`.
2. **Cancel connection failure → 503:** with IB Gateway socket closed mid-cancel, `/orders/cancel` returns `HTTP 503` with `{reason_code: "IB_CONNECTION"}`; `orders_events` records `CANCEL` kind with `detail.classification="connection"`. UI `OrderTab` renders FAILED, button re-enables, no optimistic success.
3. **Modify stale sequence → 409:** two concurrent modifies with `modify_sequence=3` → first applies, second returns `409 MODIFY_STALE` with the current applied sequence echoed.
4. **IB semantic reject preserved:** IB `Error 201` on modify surfaces as `4xx IB_REJECT` with the full upstream `errorString` in `detail.upstream`.
5. **Reason codes single-sourced:** `web/lib/orderReasonCodes.ts` is the only file containing any of the 13 reason-code literals outside of tests and the Python enum; a CI regex test fails if `OrderTab.tsx` reintroduces hardcoded copy.
6. **Next.js preserves upstream detail:** harness test POSTs to `/api/orders/place` with a FastAPI stub returning `502 {"detail": {...}}` → Next route returns `502` with the same `detail` payload (not collapsed to `500`).
7. **Rehydrate three-source reconcile:** for each of the four reconcile branches (WORKING-still-open, FILLED-via-executions, CANCELLED-no-position-change, UNKNOWN-position-changed), a unit test against `FakeIBClient` passes and emits the correct `orders_events` row.
8. **Rehydrate PENDING timeout:** a PENDING row with no `ib_order_id` older than 60s becomes `FAILED` with `reason_code="PENDING_TIMEOUT"` on boot; `client_attempt_id` is retained; UI surfaces it.
9. **Per-file coverage ≥95%** on touched modules.
10. **Single end-to-end UI verification after F7** (see "UI verification checkpoint" below).

---

## Smoke-test recipe (paper IB, port 4002 — run before merge)

```bash
# Start gateway (scripts/cloud.sh or scripts/local.sh), approve 2FA.
curl http://localhost:8321/health | jq '.ib_gateway.port_listening'  # → true

# 1. Cancel against a non-existent order → 502 with ORDER_NOT_FOUND preserved
curl -X POST http://localhost:8321/orders/cancel \
  -H "Content-Type: application/json" -d '{"orderId": 999999}'
# Expect: HTTP 502 {"detail":{"reason_code":"IB_REJECT","upstream":{"code":10147,...}}}

# 2. Cancel with IB Gateway stopped (pkill -f ibgateway) → 503
curl -X POST http://localhost:8321/orders/cancel \
  -H "Content-Type: application/json" -d '{"orderId": 1}'
# Expect: HTTP 503 {"detail":{"reason_code":"IB_CONNECTION",...}}

# 3. Modify with stale sequence
curl -X POST http://localhost:8321/orders/modify \
  -H "Content-Type: application/json" \
  -d '{"orderId":1,"newPrice":1.50,"modifySequence":1}'
curl -X POST http://localhost:8321/orders/modify \
  -H "Content-Type: application/json" \
  -d '{"orderId":1,"newPrice":1.60,"modifySequence":1}'
# Second call: HTTP 409 {"detail":{"reason_code":"MODIFY_STALE","applied":2}}

# 4. Rehydrate dry run — inject a PENDING row, bounce server, expect FAILED
python3.13 -c "from xenon.execution.orders_store import reserve_attempt; \
    reserve_attempt(user_id='local', client_attempt_id='smoke-1', \
        ticker='SPY', security_type='STK', action='BUY', quantity=1, \
        multiplier=100, limit_price='500')"
sleep 65
# Restart FastAPI (scripts/cloud.sh restart) — rehydrate runs on boot.
duckdb data/orders.duckdb "SELECT state, reason_code FROM orders_submissions WHERE client_attempt_id='smoke-1'"
# Expect: state=FAILED reason_code=PENDING_TIMEOUT
```

## Rollback recipe

```bash
# Single squash-merge per phase ideally; worst-case revert the combined commit:
git revert <merge-sha> -m 1
git push origin master
# Rehydrate reads orders_submissions — no schema change in this PR, so revert is clean.
# If F7 wrote REHYDRATE_UNCERTAIN rows before revert, leave them; they are append-only and harmless.
```

---

## Phase F5 — Cancel / modify failure propagation (owner-preserving)

> **Branch suggestion:** `phase/pr-c-cancel-modify`. No worktree needed if the other two phases land sequentially on the same branch; if parallel agents work F5 / F6 / F7 independently, use worktrees per `superpowers:using-git-worktrees`.

### F5.1 — Failure classification in `ib_order_manage.py`

- [ ] **Test first:** `scripts/tests/test_ib_order_manage_failures.py::test_classifies_connection_error` — mock `IBClient.connect()` to raise `ConnectionError`; expect subprocess stdout JSON `{"status":"error","classification":"connection","message":...}`. **Must fail before code change.**
- [ ] **Test first:** `::test_classifies_clientid_in_use` — mock connect to raise IB `Error 326`; expect after 3 retries `{"status":"error","classification":"ownership"}`.
- [ ] **Test first:** `::test_classifies_ib_semantic_reject` — mock cancel/modify to emit `Error 201`; expect `{"status":"error","classification":"ib_reject","upstream":{"code":201,"message":...}}`.
- [ ] **Test first:** `::test_classifies_ib_reject_10147` — Error 10147 surfaces as `ib_reject` (order-not-found is semantic, not connectivity).
- [ ] Introduce a `classify_failure(exc_or_errors) -> Literal["connection","ownership","ib_reject"]` pure helper at module top.
- [ ] Refactor `cancel_order()` and `modify_order()` to return structured dicts via `output()` with `classification` field. Preserve existing `status` / `message` keys for backwards compat until F6 route migration.
- [ ] Add 326-retry loop: on `ClientIdInUse` connect-time failure, `time.sleep(0.5)` + retry, up to 3× — always with `original_client_id`, never rotated.
- [ ] Commit: `feat(ib-order-manage): classify failures (connection|ownership|ib_reject) with 326 retry (F5)`

### F5.2 — ib_pool clientId registry

- [ ] **Test first:** `src/xenon/api/tests/test_ib_pool_clientid_registry.py::test_audit_holds_client_25_blocks_cancel_same_slot` — spawn a synthetic audit `acquire(25)`; attempt a second acquire of 25 → raises `ClientIdBusy`. The second caller does NOT rotate.
- [ ] **Test first:** `::test_audit_holds_25_does_not_block_cancel_on_different_slot` — audit on 25; cancel against an order placed by clientId 27 → acquire(27) succeeds.
- [ ] Add a lightweight in-process registry (dict of `int -> asyncio.Lock` or a `set` of busy clientIds behind a `threading.RLock`) to `src/xenon/api/ib_pool.py`.
- [ ] Expose `acquire_owner(clientId, timeout_ms) -> AsyncContextManager` / `release_owner(clientId)`.
- [ ] `pool_order_manage.py` and `naked_short_audit.py` both use the registry around their subprocess spawns.
- [ ] Commit: `feat(ib-pool): owner-clientId registry to serialize cancel+audit (F5)`

### F5.3 — `modify_sequence` stale-modify gate

- [ ] **Test first:** `scripts/tests/test_orders_store_modify_sequence.py::test_applies_monotonic_modify_sequence` — two `apply_modify(order_id=1, sequence=2)` then `apply_modify(order_id=1, sequence=2)` → second returns `stale=True, applied=2`.
- [ ] **Test first:** `::test_modify_sequence_resets_per_order_id` — sequence 5 on order 1 does not affect order 2.
- [ ] Add `modify_sequence INTEGER NOT NULL DEFAULT 0` column to `orders_submissions` via a non-destructive `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` migration in `orders_store.init_store`.
- [ ] Add `orders_store.apply_modify(order_id, sequence) -> {applied: bool, current_sequence: int}` with `UPDATE ... SET modify_sequence = :seq WHERE ib_order_id = :oid AND modify_sequence < :seq RETURNING modify_sequence`.
- [ ] Commit: `feat(orders-store): modify_sequence monotonic gate (F5)`

### F5.4 — Route wiring (`/orders/cancel`, `/orders/modify`)

- [ ] **Test first:** `src/xenon/api/tests/test_orders_routes_failures.py::test_cancel_returns_503_on_connection` — subprocess stub emits `classification=connection` → HTTP 503 with `detail.reason_code="IB_CONNECTION"`.
- [ ] **Test first:** `::test_cancel_returns_409_on_ownership` — `classification=ownership` → 409 with `reason_code="OWNERSHIP"`.
- [ ] **Test first:** `::test_cancel_returns_4xx_on_ib_reject_preserves_upstream` — `classification=ib_reject, upstream={code:10147,message:"Order not found"}` → HTTP 400 with full upstream preserved.
- [ ] **Test first:** `::test_modify_returns_409_on_stale_sequence` — posting with `modifySequence` < current → 409 `MODIFY_STALE` with `applied=<current>`.
- [ ] **Test first:** `::test_modify_rejects_missing_sequence` — absence of `modifySequence` → 400 with `MODIFY_SEQUENCE_REQUIRED` (new reason code, also added to enum).
- [ ] Update `server.py:orders_cancel` and `orders_modify` (around `server.py:1431`+) to:
  - Read `classification` from subprocess JSON, map to HTTP status.
  - Write an `orders_events` row with `kind="CANCEL"` / `"MODIFY"` and `detail` containing full classification payload.
  - On `modify`, call `orders_store.apply_modify(order_id, modify_sequence)` BEFORE subprocess; abort with 409 if stale.
- [ ] Add `MODIFY_SEQUENCE_REQUIRED`, `IB_CONNECTION`, `OWNERSHIP`, `IB_REJECT`, `MODIFY_STALE` to the Python reason-code enum.
- [ ] Commit: `feat(api): classify cancel/modify failures into 503/409/4xx (F5)`

### F5.5 — Phase gate

- [ ] `python3.13 scripts/infra/dev/run_pytest_affected.py` green.
- [ ] Coverage ≥95% on `ib_order_manage.py`, `ib_pool.py` (registry additions), `orders_store.py` (modify_sequence additions), `server.py` (cancel/modify routes).
- [ ] `codex-review` pass on the F5 diff before moving to F6.

---

## Phase F6 — Reason-code library + Next.js error preservation

### F6.1 — Shared reason-code module (`web/lib/orderReasonCodes.ts`)

- [ ] **Test first:** `web/tests/order-reason-codes.test.ts::test_every_python_code_has_ts_copy` — read the Python enum (via a small fixture file regenerated by the F0 TS-mirror script, OR duplicate-but-asserted list), assert every code has a `{severity, copy}` entry.
- [ ] **Test first:** `::test_no_unknown_codes_fall_through` — `getReasonToast("BOGUS_CODE")` returns a generic fallback with `severity="error"`, `copy="Unknown error — see logs."`.
- [ ] Create `web/lib/orderReasonCodes.ts`:
  ```ts
  export type ReasonSeverity = "error" | "warn" | "info";
  export interface ReasonToast {
    severity: ReasonSeverity;
    copy: string;
  }
  export const ORDER_REASON_CODES: Record<string, ReasonToast> = {
    /* 13 codes from SL §6 */
  };
  export function getReasonToast(code: string): ReasonToast {
    /* ... */
  }
  ```
- [ ] The 13 codes are: `UNIVERSE_UNKNOWN`, `INDEX_HAS_NO_STOCK`, `INSUFFICIENT_SHARES`, `INSUFFICIENT_CASH`, `INDEX_CALL_UNCOVERED`, `ETF_CALL_UNCOVERED`, `STALE_QUOTE`, `LIMIT_OUT_OF_BAND`, `LIMIT_OFF_TICK`, `ATTEMPT_ID_TERMINAL`, `MODIFY_STALE`, `IB_CONNECTION`, `OWNERSHIP`. Plus the F5 additions `IB_REJECT`, `MODIFY_SEQUENCE_REQUIRED`, `PENDING_TIMEOUT` (F7).
- [ ] Commit: `feat(web): single-source reason-code toast library (F6)`

### F6.2 — `OrderTab.tsx` consumes the library

- [ ] **Test first:** `web/tests/order-tab-reason-toast.test.tsx::test_renders_stale_quote_copy` — render OrderTab with a simulated `STALE_QUOTE` server response → assert toast contains the exact copy from `orderReasonCodes.ts`.
- [ ] **Test first:** `::test_no_hardcoded_reason_strings_in_order_tab` — regex test against the file source: no literal occurrence of reason-code copy text outside of `orderReasonCodes.ts`.
- [ ] Replace inline error-copy branches in `OrderTab.tsx` with `getReasonToast(response.reason_code)`.
- [ ] Cancel/modify buttons: on 503 / 409, render explicit FAILED state pill + re-enable the button. Do not optimistically mark the order cancelled until the server confirms.
- [ ] Commit: `feat(web): OrderTab consumes reason-code library, no optimistic cancel (F6)`

### F6.3 — Next.js route upstream preservation

- [ ] **Test first:** `web/tests/orders-place-upstream-preserved.test.ts::test_preserves_502_detail_verbatim` — mock `xenonFetch` → 502 with `{detail: {reason_code: "IB_CONNECTION", ...}}` → `/api/orders/place` responds 502 with identical body.
- [ ] **Test first:** `::test_preserves_409_modify_stale` — upstream 409 with `detail.applied=3` → Next route passes through 409 + `applied:3` intact.
- [ ] **Test first:** `::test_preserves_503_cancel` — 503 IB_CONNECTION preserved verbatim.
- [ ] **Test first:** `::test_unknown_error_falls_to_500_with_request_id` — upstream throws unexpected → 500 with `{error:"internal", request_id}`.
- [ ] Refactor `/api/orders/place/route.ts`, `/api/orders/cancel/route.ts`, `/api/orders/modify/route.ts`. The existing ATTEMPT_ID_TERMINAL passthrough (F4) is the template — extend it to every non-200 upstream.
- [ ] Extract shared helper `web/lib/passThroughXenonError.ts` so the three routes do not drift.
- [ ] Commit: `feat(web/api): preserve upstream status+detail on orders routes (F6)`

### F6.4 — Phase gate

- [ ] `cd web && npm test` green.
- [ ] No Vitest regressions in existing OrderTab / idempotency tests.
- [ ] Coverage ≥95% on `orderReasonCodes.ts`, `passThroughXenonError.ts`, and the three orders routes.
- [ ] `codex-review` pass on the F6 diff.

---

## Phase F7 — Boot-time rehydrate

### F7.1 — Three-source reconcile helper

- [ ] **Test first:** `scripts/tests/test_single_leg_rehydrate.py::test_reconciles_working_order_still_open` — row state=WORKING, `FakeIBClient.open_orders` contains matching `perm_id` → state stays WORKING, `REHYDRATE_RECONCILED` event emitted.
- [ ] **Test first:** `::test_reconciles_filled_via_executions` — not in open_orders, `executionDetails` has `perm_id` with `shares=100 avg=1.50` → state=FILLED, filled_qty=100, avg_fill_price=1.50, event emitted.
- [ ] **Test first:** `::test_reconciles_cancelled_positions_unchanged` — not in open, no executions, positions snapshot unchanged for (ticker, con_id) → state=CANCELLED.
- [ ] **Test first:** `::test_reconciles_unknown_positions_changed` — not in open, no executions, positions for this contract changed vs submitted_at snapshot → state=UNKNOWN, `REHYDRATE_UNCERTAIN` event. Never auto-CANCELLED.
- [ ] **Test first:** `::test_pending_timeout_older_than_60s` — state=PENDING, `submitted_at < now()-60s`, no ib_order_id → state=FAILED, reason_code=PENDING_TIMEOUT. `client_attempt_id` retained (row not deleted).
- [ ] **Test first:** `::test_pending_within_60s_untouched` — state=PENDING, 30s old → left as PENDING.
- [ ] Create `src/xenon/execution/single_leg_rehydrate.py` with public entry `rehydrate_on_boot(ib_client_factory, orders_store, now=time.time)`.
- [ ] Internal helper `_reconcile_from_three_sources(row, open_orders_by_perm, execs_by_perm, positions_snapshot) -> ReconcileDecision` — **note: this helper is the template the wizard reuses** (SL §11). Keep it pure (no DB writes) so wizard's `rehydrate.py` can import verbatim.
- [ ] Emit `orders_events` rows for every reconciled row (kind = `REHYDRATE_RECONCILED` or `REHYDRATE_UNCERTAIN`, `detail` = `{from_state, to_state, sources: {open_orders, executions, positions}}`).
- [ ] Commit: `feat(rehydrate): three-source reconcile for single-leg orders (F7)`

### F7.2 — FastAPI lifespan integration

- [ ] **Test first:** `src/xenon/api/tests/test_server_rehydrate_boot.py::test_rehydrate_runs_on_startup` — patch `rehydrate_on_boot`; start FastAPI via `TestClient(...)`; assert called once with the pool's sync client.
- [ ] **Test first:** `::test_rehydrate_failure_does_not_block_boot` — `rehydrate_on_boot` raises → server still serves `/health`; failure logged with `request_id`; no exception propagated to lifespan.
- [ ] Add `await asyncio.to_thread(rehydrate_on_boot, ...)` inside `server.py` lifespan startup, wrapped in try/except that logs + swallows (boot must be robust).
- [ ] Ensure the boot-hook only reads via `ib_pool.sync_client` — never the subprocess path, per SL §11.
- [ ] Commit: `feat(api): run single-leg rehydrate in FastAPI lifespan (F7)`

### F7.3 — Observability readiness probe

Per master plan §Verification layer 2, a synthetic probe is needed before burn-in starts. That probe belongs here, not in a later phase:

- [ ] Add `POST /dev/rehydrate/synthetic` (gated on `test_mode` OR a `DEV_PROBES=1` env var — **never enabled in production**) that injects a fake PENDING row, calls `rehydrate_on_boot`, returns the resulting `orders_events` row count.
- [ ] **Test first:** `::test_synthetic_probe_writes_event` — call probe → assert `orders_events` has the reconcile row.
- [ ] Document the probe in `src/xenon/api/CLAUDE.md` under a new "Dev probes" subsection.
- [ ] Commit: `feat(api): synthetic rehydrate probe for observability readiness (F7)`

### F7.4 — Phase gate

- [ ] `python3.13 scripts/infra/dev/run_pytest_affected.py` green across all Python tests.
- [ ] Coverage ≥95% on `single_leg_rehydrate.py`.
- [ ] `codex-review` pass on the F7 diff.

---

## UI verification checkpoint (runs ONCE, after F7)

Per project memory `project_pr_cd_ui_test_deferred.md`: the user runs a single browser QA pass after F7 merges. This checkpoint is the exit gate for the whole PR-C/D bundle.

**Pre-check:**

- [ ] All three phases' automated tests green.
- [ ] Paper IB gateway up: `curl http://localhost:8321/health` shows `ib_gateway.port_listening: true`.
- [ ] `cd web && npm run dev` → open a ticker page.

**Scenarios (chrome-cdp or Playwright — user's choice):**

1. **Cancel against stopped gateway → FAILED toast.**
   - Place an order (paper), stop gateway (`pkill -f ibgateway`), click Cancel. Expect red "IB connection lost — retry" toast, order stays WORKING in UI, Cancel button re-enabled. No optimistic success pill.

2. **Cancel with busy owner clientId → OWNERSHIP toast.**
   - Start a `xenon-naked-short-audit` run (busies clientId 25). Place and immediately cancel an order. Expect "Order owned by another session" toast after retries; order stays WORKING.

3. **Modify with stale sequence → MODIFY_STALE toast.**
   - Open two browser tabs on the same order. Modify price in tab A (sequence advances to 1), then modify in tab B (still holding sequence 0). Tab B gets the red "Modify sequence stale; refresh and retry" toast.

4. **IB 201 reject → preserved upstream detail.**
   - Submit a limit too far from market, override `LIMIT_OUT_OF_BAND`. IB returns 201. Toast should show the upstream IB error text verbatim (not "Order failed" generic).

5. **Rehydrate FAILED on orphan PENDING.**
   - Inject a PENDING row via the smoke-test §4 command, wait 65s, restart FastAPI. Reload the ticker page — the order surfaces as FAILED with `PENDING_TIMEOUT` reason; UI allows retry (fresh `client_attempt_id` auto-generated).

6. **Rehydrate UNKNOWN banner.**
   - With a WORKING row in orders.duckdb, manually delete the matching open order in IB and close the position (simulate positions change). Restart FastAPI. UI shows the "order state uncertain — reconcile manually" banner. Confirm the row is NOT auto-CANCELLED.

7. **No console errors** in chrome-devtools across all six scenarios.

**Exit:** all 7 scenarios pass visually, confirmed with screenshots saved to `docs/status.md` under a new entry. User explicitly signs off on this page.

If any scenario fails → file the failure as a fresh bug, fix inside this PR before merging.

---

## Coordination notes between phases

- **F5 → F6 coupling:** F5 introduces the `MODIFY_SEQUENCE_REQUIRED`, `IB_CONNECTION`, `OWNERSHIP`, `IB_REJECT`, `MODIFY_STALE` reason codes at the API boundary. F6 adds them to `orderReasonCodes.ts`. Keep the Python enum and TS map in lockstep — if F5 code review adds / renames a code, F6 must follow in the same PR.
- **F7 → F5 coupling:** F7 adds `PENDING_TIMEOUT` as a reason code. Add it to the same Python enum F5 grows, and F6's TS map.
- **Schema change in F5.3 (`modify_sequence` column)** is non-destructive (`ADD COLUMN IF NOT EXISTS`) so F7's rehydrate reads the expanded schema without migration sequencing concerns.
- **Do not merge F5, F6, F7 independently** — they are one logical PR. Land as a single squash merge titled `feat: PR-C/D cancel/modify + error propagation + rehydrate (F5+F6+F7)`.

## Verification reference card

Per-phase gates (automated) run at the end of each phase section. The full bundle also satisfies master-plan §Verification layer 2 observability readiness (synthetic rehydrate probe from F7.3 covers the requirement). Layer 3 program-level release verification does not run until the wizard program is complete — out of scope here.

## Rollback posture

Both `orders_submissions` additions (F5.3 column) and `orders_events` writes (F5/F7) are append-only or non-destructive. A full revert of this PR leaves the DB healthy; stale rows with `modify_sequence=0` are valid under the old code path.
