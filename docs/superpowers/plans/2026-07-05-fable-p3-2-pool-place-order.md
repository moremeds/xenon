# P3.2 — Flagged `pool_place_order` on the persistent orders role + promote `pool_order_manage` (Option B)

- **Date:** 2026-07-05
- **Finding IDs:** OP-13 (dormant pool manage path — this plan WIRES it); fable Option B
  (`docs/fable/09-target-architecture-options.md`); sketch `11-code-sketches.md` §4.
- **Severity of change: HIGH RISK — live order path re-architecture.** Paper-first, feature-
  flagged, subprocess fallback mandatory.
- **HARD GATE (do not execute without it):** P1.1 baselines exist AND show the subprocess
  path's p50 place latency is a real problem for the operator (roadmap: pursue Option B "only
  if Phase-1 measurements justify it"). If the P3.1 measurement shows p50 ≤ ~2s post-S2, this
  plan should be REJECTED at execution time — record that and stop. Acceptance if pursued:
  p50 place ≤1s on paper; automatic fallback engages on a wedged session within one watchdog
  tick.
- **SEQUENCING GATE:** P2.4 must be merged first. At authoring HEAD there is no
  `submit_to_broker` seam — `_orders_place_from_body` in `src/xenon/api/server.py` invokes the
  subprocess inline. This plan wires the pool path into the seam P2.4 creates; executing it
  before P2.4 means rewriting the wiring target and is out of scope.

## Verified Key Facts (at authoring HEAD — re-verify before executing)

- Pool lives at `src/xenon/api/ib_pool.py` (NOT under `execution/`). Its API is
  `acquire(role)`, `get_with_reconnect_sync(role)`, and `run_sync(role, fn, ...)` — there is
  no async `get_with_reconnect`. Use `run_sync("orders", ...)` for all IB calls.
- `src/xenon/api/pool_order_manage.py` is built on the pool **sync/master client
  (clientId=0)**: its module docstring states the master client can manage ALL orders
  regardless of placing clientId, and `pool_cancel_order(client, ...)` /
  `pool_modify_order(client, ...)` take a preselected client, not a role name.
  `scripts/tests/test_pool_order_manage.py` asserts the manage-any-clientId behavior.
- `src/xenon/api/server.py` imports `pool_cancel_order`/`pool_modify_order` but **never calls
  them** — `/orders/cancel` and `/orders/modify` still run the `xenon-ib-order-manage`
  subprocess unconditionally. Wiring the call sites is part of this plan.
- Subprocess placement uses `client_id="auto"` → the 20–49 range
  (`src/xenon/clients/ib_client.py::CLIENT_IDS` + auto-allocation). Do NOT assert a fixed
  placer id in tests or docs; `CLIENT_IDS["ib_place_order"]` (24) and a stale `26` fallback
  both exist and neither is what real placements use.
- `ib_place_order.place_order` is one monolithic function (qualify → build combo/BAG or
  LimitOrder → `client.place_order` → poll). There is **no `build_order` seam** and **no
  sent-state metadata**: exceptions after `placeOrder` collapse into `{"status": "error"}`
  indistinguishable from exceptions before it (`ib_place_order.py` generic `except`;
  `IBClient.place_order` likewise). Both gaps are REQUIRED prep commits below.

## Re-verify preamble (MANDATORY)

This plan is written far ahead of execution; S2/S4/S6/P2.4 will have reshaped the place path.
Re-verify at HEAD before executing: `pool_order_manage.py` still exists with tested
`pool_cancel_order`/`pool_modify_order` (P2.7 removes only the dead _import_, not the module);
`ib_pool.py` roles unchanged; P2.4's `submit_to_broker` seam exists. Anchor everything by
function name.

## Prep commits (each no-behavior-change, own tests, mergeable alone)

1. **`build_order(params)` extraction** from `ib_place_order.place_order` into a shared
   module — qualification + combo/BAG leg construction + LimitOrder creation, returning
   `(contract, order)` without placing. `place_order` becomes qualify→`build_order`→send→poll.
   Proof of no behavior change: existing place tests pass untouched; add a
   contract-equality test (same params → identical `ComboLeg` actions/ratios and order
   fields pre/post refactor).
2. **Sent-state metadata**: `place_order` result gains `sent_to_broker: bool` — `False` for
   any exception raised before `client.place_order` is invoked, `True` from that point on.
   `IBClient.place_order` must surface (not swallow) whether `placeOrder` was reached. This
   is the load-bearing fact for the fallback rule; without it the fallback is unsafe.

## Design (adapt sketch §4 to the post-refactor code)

1. **Feature flag** `XENON_POOL_PLACE=1` (default OFF). Read once at handler entry.
2. **`pool_place_order(pool, body) -> PlaceOutcome`** in `xenon/api/pool_order_manage.py`
   (promoting the module from manage-only to place+manage):
   - All IB work inside `pool.run_sync("orders", ...)` (orders role, pinned thread):
     qualify (with a per-session contract cache), `build_order(params)` (prep commit 1 — do
     NOT duplicate combo/BAG leg semantics), set `orderRef = client_attempt_id` (S2
     invariant), `placeOrder`, event-driven ack wait (same deadline constants as S2's CLI).
   - Returns the same outcome shape the staged subprocess runner produces (ack fields +
     final status + `sent_to_broker`) so the handler's ack/UNCERTAIN/reject branches work
     UNCHANGED.
3. **Handler wiring:** in P2.4's `submit_to_broker` seam: `if pool_place_enabled(): try pool
path; on failure with sent_to_broker=False → log loudly + fall back to the subprocess
path in the SAME request` (circuit-back). Fallback rule (uses prep commit 2):
   - exception with `sent_to_broker=False` ⇒ IB never saw the order ⇒ fallback is safe
     (reservation already exists; `orderRef` carries the same `client_attempt_id`).
   - exception/timeout with `sent_to_broker=True` ⇒ treat as S2 UNCERTAIN, do NOT fall back
     (a second send could double-fill; S3's sweep reconciles by orderRef).
4. **Cancel/modify promotion:** switch `/orders/cancel` + `/orders/modify` from the
   `xenon-ib-order-manage` subprocess to the already-written-and-tested
   `pool_cancel_order`/`pool_modify_order` under the same flag. Per the module's model this
   goes through the pool **sync client (clientId=0, master)** — which manages ALL orders
   regardless of placer, so no per-order `placing_client_id` routing is needed.
   **Paper verification gate for that claim:** place one order via the subprocess path, then
   cancel it via the pool path. If IB refuses (error 326 / "cannot modify"), the master-client
   assumption is wrong for this gateway config → STOP and add per-order routing
   (`placing_client_id == pool id → pool; else → subprocess`) before proceeding.
5. **Watchdog:** the pool already has reconnect logic; add a wedge detector (a place that
   neither acks nor raises within N s marks the session suspect → next request uses
   subprocess and a background reconnect recycles the role).

## Tests (offline, no live IB)

- Prep commit 1: contract-equality test (pre/post `build_order` extraction).
- Prep commit 2: `sent_to_broker` False/True on exception-before vs exception-after
  (fake client raising at each site).
- Unit: `pool_place_order` against a fake pool (`run_sync` executing inline): ack → outcome
  mapping; `sent_to_broker=False` failure → fallback-allowed; `=True` → UNCERTAIN.
- Route: flag ON with the pool faked — the fake-CLI route suite from S2 must pass with the
  pool path substituted (contract-identical response).
- Wiring: with flag OFF, greps prove `/orders/cancel`/`modify` still hit the subprocess;
  with flag ON, a fake pool records the `pool_cancel_order` call.
- Paper soak (manual gate before enabling by default): 20 place/cancel cycles on paper with
  the flag ON, including the step-4 cross-client cancel probe; zero UNKNOWN/UNCERTAIN
  residue; p50 ≤1s from P1.1 stage logs.

## Rollback

Flag OFF restores the subprocess path instantly (env change, no deploy). Keep the flag for
≥1 month of paper+live soak before considering default-ON.

## Tripwires

- STOP if the P1/P3.1 measurements do not justify this (record the rejection in the index).
- STOP if P2.4's seam is absent at HEAD (sequencing gate above).
- STOP if `build_order` extraction changes any test snapshot — prep commit 1 must be
  provably no-behavior-change.
- STOP if the step-4 paper probe shows the master client cannot cancel subprocess-placed
  orders — switch to per-order routing before any further wiring.
- Incident-history row required when this merges (it changes order execution ownership).
- Paper only until the soak gate passes; live enablement is an operator decision, not the
  executor's.
