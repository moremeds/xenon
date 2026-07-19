# Fable Remediation — Plan Index (2026-07-05)

Master tracker for the per-item implementation plans derived from the deep architecture
review in `docs/fable/` (commit `4d864294`). Each plan is written to be executed
standalone by any agent (see verification matrix inside each plan). Review state:
`drafted` → `review-cycled` → `approved` → `executed`.

**Canonical merge order (integration-verified):**
`P1.1 → S1 → S5 → S6 → S7 → S2 → S4 → S3 → P1.2 → P1.3/P1.4 (anytime after P1.1) → P2.1 → P2.2 → P2.3 → P2.7 → P2.5 → P2.6 → P2.8 (last, documents merged reality) → P2.4 (after S2+S4+S6+P1.1) → P3.x (gated on P1/P3.1 measurements) → P4.x (scope gated on P1.3; within P4: P4.3 before P4.2 — P4.2's CI pin depends on P4.3's test lane; P4.4 gated on a P1.3 measurements file)`.
Load-bearing orderings: P1.1 BEFORE S2 (P1.1's own tripwire stops if S2's ack line merged first); S2 before S4 (S4 wraps S2's persist sites incl. the early-ack branch); S2 before S3 (orderRef + UNCERTAIN); S6's wrapper forwards `**kwargs` so S2's `runner=` composes either way; S4 before P2.1 (P2.1 deletes two branches S4 instruments); S2+S3+S5 before P2.2 (final state set).

Execution order = table order. P3/P4 plans are written now but gated on P1 measurements.

| #   | Item                                                      | Finding IDs                | Plan file                                           | State         |
| --- | --------------------------------------------------------- | -------------------------- | --------------------------------------------------- | ------------- |
| 1   | S1 Clerk-gate order-mutating Next routes                  | SEC-1                      | 2026-07-05-fable-s1-clerk-gate-order-routes.md      | review-cycled       |
| 2   | S2 UNCERTAIN state + orderRef                             | OP-1, OP-11                | 2026-07-05-fable-s2-uncertain-orderref.md           | review-cycled       |
| 3   | S3 Poller reconciliation sweep                            | OP-3                       | 2026-07-05-fable-s3-poller-reconciliation-sweep.md  | review-cycled       |
| 4   | S4 Protect post-ack persist                               | OP-2                       | 2026-07-05-fable-s4-protect-post-ack-persist.md     | review-cycled       |
| 5   | S5 Naked-short audit writes state                         | OP-5                       | 2026-07-05-fable-s5-naked-short-audit-state.md      | review-cycled |
| 6   | S6 Bound order-subprocess concurrency                     | OP-7                       | 2026-07-05-fable-s6-order-subprocess-semaphore.md   | review-cycled       |
| 7   | S7 Relay bounded delivery + Origin allowlist              | QS-1, SEC-2                | 2026-07-05-fable-s7-relay-backpressure-origin.md    | review-cycled       |
| 8   | P1.1 Order-path stage-timing logs                         | —                          | 2026-07-05-fable-p1-1-stage-timing-logs.md          | review-cycled       |
| 9   | P1.2 Relay /status metrics + seq/relay_ts                 | QS-3                       | 2026-07-05-fable-p1-2-relay-metrics-seq.md          | review-cycled       |
| 10  | P1.3 React Profiler + fill-to-UI measurement              | QS-5                       | 2026-07-05-fable-p1-3-frontend-measurements.md      | review-cycled       |
| 11  | P1.4 OP-10 external-fill paper probe                      | OP-10                      | 2026-07-05-fable-p1-4-external-fill-probe.md        | review-cycled       |
| 12  | P2.1 Delete regime dead code                              | CX-3                       | 2026-07-05-fable-p2-1-delete-regime-dead-code.md    | review-cycled       |
| 13  | P2.2 transition() chokepoint + state CHECK                | OP-8, OP-9                 | 2026-07-05-fable-p2-2-transition-chokepoint.md      | review-cycled       |
| 14  | P2.3 Modify persists price/qty                            | OP-4                       | 2026-07-05-fable-p2-3-modify-persists-price-qty.md  | review-cycled       |
| 15  | P2.4 Decompose \_orders_place_from_body                   | CX-2                       | 2026-07-05-fable-p2-4-decompose-place-handler.md    | review-cycled       |
| 16  | P2.5 Coverage-math parity fixtures + reason-code codegen  | CX-1                       | 2026-07-05-fable-p2-5-coverage-parity-fixtures.md   | review-cycled       |
| 17  | P2.6 Combo replace server-side + net-price + limit band   | OP-6, OP-17                | 2026-07-05-fable-p2-6-combo-replace-server-side.md  | review-cycled       |
| 18  | P2.7 ib_execute guard + place-CLI error classification    | OP-12, OP-14, OP-13, OP-15 | 2026-07-05-fable-p2-7-execution-cli-hardening.md    | review-cycled       |
| 19  | P2.8 Doc repair (order-stack doc, 403→400, README CLI)    | CX-4                       | 2026-07-05-fable-p2-8-doc-repair.md                 | review-cycled       |
| 20  | P3.1 Event-driven ack (if not fully in S2)                | OP-11                      | 2026-07-05-fable-p3-1-event-driven-ack.md           | review-cycled (quick)       |
| 21  | P3.2 Flagged pool_place_order + promote pool_order_manage | OP-13                      | 2026-07-05-fable-p3-2-pool-place-order.md           | review-cycled (quick)       |
| 22  | P3.3 Order events over WS                                 | —                          | 2026-07-05-fable-p3-3-order-events-over-ws.md       | review-cycled (quick)       |
| 23  | P4.1 Idempotent upstream subscribe                        | QS-4                       | 2026-07-05-fable-p4-1-idempotent-upstream-subscribe.md     | review-cycled (quick)       |
| 24  | P4.2 Error-101 handling + subscription cap                | QS-7                       | 2026-07-05-fable-p4-2-error-101-and-priority-cap.md | review-cycled (quick)       |
| 25  | P4.3 Relay core extraction + CI test lane                 | QS-2, QS-10                | 2026-07-05-fable-p4-3-batched-channel-module-and-ci-lane.md             | review-cycled (quick)       |
| 26  | P4.4 Frontend memoization / per-symbol store              | QS-5, QS-8                 | 2026-07-05-fable-p4-4-frontend-memoize-and-socket-merge.md       | review-cycled (quick)       |
| 27  | P4.5 Gateway-restart branch repair                        | QS-6                       | 2026-07-05-fable-p4-5-gateway-restart-rename-and-alert.md     | review-cycled (quick)       |
| 28  | P5 Futu decision record                                   | FU-1                       | 2026-07-05-fable-p5-futu-decision-record.md         | review-cycled (quick)       |
Not planned separately (absorbed): OP-16 near-duplicate gate (optional; revisit after S2
soak), SEC-3/SEC-4/SEC-5 (low; fold into P2.7-adjacent hygiene or backlog), CX-5 (fold
into P2.7 area work), QS-9 (accepted behavior, documented), TS-1/TS-2/TS-3 (test work is
embedded in S2/P2.4/P4.3 verification matrices).
