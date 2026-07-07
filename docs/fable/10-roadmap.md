# 10. Prioritized Implementation Roadmap

Every item lists: affected files · value · risk · prerequisite · acceptance criteria · tests.
Ordering within a phase is the recommended PR order. One change = one PR.

## Immediate safety fixes (before any refactoring)

**S1. Clerk-gate the order-mutating Next.js routes (SEC-1).**
`web/app/api/orders/{place,cancel,modify}/route.ts`, wizard mutation routes.
Value: closes unauthenticated order placement through the public `/api/(.*)` matcher.
Risk: low (dev bypass envs already exist for tests).
Prereq: none.
Accept: unauthenticated POST → 401 in prod config; Playwright/dev flows unaffected under `XENON_DISABLE_AUTH=1`.
Tests: route test with/without mocked Clerk session; e2e smoke.

**S2. Timeout ≠ terminal: `UNCERTAIN` state + orderRef (OP-1).**
`src/xenon/execution/ib_place_order.py` (set `order.orderRef = client_attempt_id`; emit early ack line; event-driven wait),
`src/xenon/api/server.py` place handler (ambiguity → UNCERTAIN), `orders_store.py`.
Value: eliminates the duplicate-order window — the Critical finding.
Risk: medium (touches the live order path) — paper-first per repo policy.
Prereq: none (S3 pairs naturally).
Accept: SIGKILL-after-ack drill (08 §8.4-5) ends with the row WORKING and correct ids; no FAILED row while the order is live.
Tests: fake-CLI route tests (07 §7.4-1), sweep test (07 §7.4-2).

**S3. Reconciliation sweep for UNCERTAIN/PENDING in the poller (OP-3).**
`src/xenon/api/services/ib_activity_mirror.py`.
Value: stuck rows resolve within one poll tick instead of next restart.
Risk: low. Prereq: S2 (orderRef).
Accept: seeded UNCERTAIN row resolves in ≤2 ticks; PENDING older than 60 s with no subprocess alive resolves.
Tests: 07 §7.4-2; poller unit tests extended.

**S4. Protect the post-ack persist (OP-2).**
`server.py:2325-2331` → try/except + compensating `order_events` write + loud log/alert.
Accept: injected `mark_submitted` failure still returns ack to the client with a warning flag and leaves a recoverable event.
Tests: fake-CLI route test (d)/(e).

**S5. Naked-short audit writes state (OP-5).**
`naked_short_audit.py` — after `cancel_order`, call `mark_terminal(CANCELLED, reason_code="NAKED_SHORT_AUDIT", expected_states=("WORKING","PARTIALLY_FILLED"))` + event.
Accept: audit-cancelled order shows CANCELLED with correct provenance within the same run.
Tests: 07 §7.4-7. Append a row to `docs/reference/order-path-incident-history.md`.

**S6. Bound order-subprocess concurrency (OP-7).**
`server.py` — `asyncio.Semaphore(2)` around the three order-mutating `_run_ib_script_with_recovery` sites; expose an in-flight gauge.
Accept: burst of 5 places runs ≤2 subprocesses concurrently; no behavior change otherwise.
Tests: two-distinct-orders concurrency test (07 §7.4-4).

**S7. Relay: bounded per-client delivery + Origin allowlist (QS-1, SEC-2).**
`scripts/infra/ib_realtime/ib_realtime_server.js`.
Accept: slow-client soak (08 §8.4-3) keeps RSS bounded; upgrade from a non-allowlisted Origin → 403; loopback server-to-server unaffected.
Tests: relay integration test (07 §7.4-5) — lands with Phase 1 CI harness if needed.

## Phase 1 — Observability & characterization

**P1.1** Stage-timing log lines keyed by `client_attempt_id` (order path) — files per 08 §8.3. Accept: one log line per stage with monotonic deltas. Tests: log-shape unit test.
**P1.2** Relay `/status` metrics (flush size, stringify ms, bufferedAmount, amplification, quote age) + `seq`/`relay_ts` in the message protocol (QS-3). Accept: `usePrices` exposes quote age in dev; seq strictly increases across a session. Tests: protocol unit tests + relay integration.
**P1.3** React Profiler measurement of QS-5 + `fill_to_ui_seconds` measurement. Accept: numbers recorded in `docs/fable/measurements-<date>.md`. (Decides Phase 4 scope.)
**P1.4** OP-10 live paper probe (external-fill visibility). Accept: documented yes/no with capture; memory + docstring corrected.

## Phase 2 — Order-path consolidation

**P2.1** Delete regime dead code (web + `override_audit` plumbing) (CX-3). Accept: grep-clean; bundle size drop; no behavior change. Tests: existing suites green.
**P2.2** `transition()` chokepoint + `order_submissions.state` CHECK + `expected_states` at every terminal write (OP-8, OP-9). Prereq: S2 (state set final). Accept: property test (07 §7.4-3) green; migration applies on core_test.
**P2.3** Modify persists price/qty + event (OP-4). Accept: post-modify row matches IB snapshot; drift mirror stops skip-logging UUID rows for this field class.
**P2.4** Decompose `_orders_place_from_body` per 06 §6.2 spec (CX-2). Prereq: fake-CLI tests exist. Accept: route file shrinks to HTTP concerns; all order tests green.
**P2.5** Coverage-math parity fixtures + reason-code codegen (CX-1). Accept: single JSON case table drives Python and TS guards; enum parity test.
**P2.6** Combo replace server-side or place-first (OP-6); combo net-price single implementation; combo limit-band gate (OP-17). Accept: replace failure leaves the original order intact (place-first) or a recovery record; one `computeNetOptionQuote` call site set.
**P2.7** `ib_execute.py`: route through preflight or add to the caller-allowlist guard; place-CLI error classification (OP-12, OP-14); fix `ib_pool` docstring + dead import (OP-13/15). Accept: guard script covers all four execution CLIs.
**P2.8** Doc repair (CX-4): re-verify `order-stack-end-to-end.md`, fix 403→400 claims, README CLI table. Accept: drift table items resolved.

## Phase 3 — Execution lifecycle (Option B, gated on P1 measurements)

**P3.1** Event-driven ack in the subprocess (drop blind sleep) if not already done in S2. Accept: p50 place latency drops ≥2 s vs P1 baseline.
**P3.2** Flagged `pool_place_order` on the persistent orders role; promote `pool_order_manage` for cancel/modify; subprocess path as fallback with automatic circuit-back. Prereq: P1.1 baselines, paper soak. Accept: p50 place ≤1 s on paper; fallback engages on session wedge within one watchdog tick.
**P3.3** Order events pushed over the existing WS (kills 5 s/30 s polling). Accept: fill→UI ≤ poller tick + 1 s.

## Phase 4 — Quote-stream improvements (scope decided by P1.3)

**P4.1** Idempotent upstream subscribe (QS-4). Accept: reconnect churn workload (08 §8.4-4) shows 0 redundant `reqMktData` for already-live symbols.
**P4.2** Explicit error-101 handling + subscription cap with priority (QS-7). Accept: synthetic 101 marks affected symbols degraded and logs loudly.
**P4.3** Extract batched-channel + subscription-registry modules with unit tests; CI relay integration lane (QS-2, QS-10). Accept: relay core logic covered in CI; `.mjs` tests run in the web-tests job.
**P4.4** Frontend: memoize consumers or per-symbol store; merge usePrices/IBStatusContext socket core (QS-5, QS-8). Accept: profiler shows re-render scope reduced to subscribed components.
**P4.5** Rename/repair the prod "gateway restart" branch (QS-6): compose-level restart hook or explicit alert. Accept: watchdog action matches its name in the docker topology.

## Phase 5 — Futu decision

Recommendation: **remain read-only** for now, and write that down as a decision record.
Grounds: no order-path parity (idempotency ledger, naked-short guard, incident tooling are
all IB-shaped); the schema deliberately blocks it; the one-operator workflow uses Futu as a
custody/reporting account. Conditions that must ALL hold before revisiting limited
execution (close-only, cash-secured):

1. Option A hardening complete and soaked (S1–S6, P2.2).
2. Broker capability table exists and drives the 403s (04 §4.3-1).
3. Schema decision executed deliberately: either relax the `broker='IB'` CHECKs on the
   execution ledger with migration + tests, or a parallel `futu_order_submissions` ledger.
4. `unlock_trade` flow designed with the same secrets discipline as IB creds; Futu paper
   (simulate env) test loop established.
5. Futu-aware naked-short/coverage guard implemented from the same parity fixture table.
6. Caller-allowlist guard + incident-history discipline extended to the Futu CLI.

Affected files if pursued: `futu_client.py` (trade calls), new `futu_place_order` CLI or
service, `schema.py` + migration, `guards.py` capability map, `preflight.py`, web order
forms (broker selector). Risk: high — treat as a project, not a PR.
