# Xenon Deep Architecture Review — docs/fable/

Deep architectural and code-level review of the xenon repository, focused on
(A) broker order placement and (B) the realtime quote proxy/WebSocket service.

- **Repo state reviewed:** commit `4d864294` (post-v0.7.2), 2026-07-03
- **Method:** parallel evidence-gathering passes (frontend order path, Python execution
  backend, order state machine, Futu capability, realtime quote pipeline, Python/web/relay
  test architecture, docs-vs-code drift), each producing `file:line`-cited evidence;
  synthesized and cross-checked by the reviewing session, with the load-bearing claims
  (place-route timeout handling, relay auth bypass, Next.js middleware/`xenonFetch` auth,
  `expected_states` usage, dormant pool manage path) re-verified first-hand.
  Documentation was treated as hypothesis until confirmed by code — and the central
  order-path doc was found materially stale (finding CX-4).
- **Confidence labels:** every finding carries `Confirmed` (verified in code),
  `Strong inference` (follows from verified code but not exercised), or
  `Hypothesis` (requires instrumentation/benchmark or a live test).
  No benchmark numbers are invented anywhere in this report; latency figures are code
  constants or flagged estimates.

## Contents

| File                                                                     | Section                                                                                                                                                     |
| ------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [01-executive-summary.md](01-executive-summary.md)                       | Assessment, top strengths, top-10 improvements, rewrite verdict                                                                                             |
| [02-current-architecture.md](02-current-architecture.md)                 | Current state + Mermaid sequence diagrams (place/cancel/modify/reconcile/quotes/reconnect/Futu)                                                             |
| [03-findings-table.md](03-findings-table.md)                             | Full findings table (OP/SEC/QS/CX/TS/FU) + failure-mode table                                                                                               |
| [04-broker-execution-deep-dive.md](04-broker-execution-deep-dive.md)     | IB & Futu current state, broker-abstraction answers, order state machine, subprocess-model design comparison                                                |
| [05-quote-stream-deep-dive.md](05-quote-stream-deep-dive.md)             | Relay lifecycle, batching, backpressure, reconnect, quantification, proxy-layer review                                                                      |
| [06-complexity-and-reuse.md](06-complexity-and-reuse.md)                 | Validation/gate duplication map, module complexity, ranked refactoring candidates                                                                           |
| [07-testing-review.md](07-testing-review.md)                             | Test-fidelity map, explicit gap checklist, recommended tests with invariants                                                                                |
| [08-performance-measurement-plan.md](08-performance-measurement-plan.md) | Latency budgets, instrumentation, test workloads                                                                                                            |
| [09-target-architecture-options.md](09-target-architecture-options.md)   | Options A/B/C, recommendation, target diagrams                                                                                                              |
| [10-roadmap.md](10-roadmap.md)                                           | Safety fixes S1–S7 + Phases 1–5 (incl. the Futu decision)                                                                                                   |
| [11-code-sketches.md](11-code-sketches.md)                               | Sketches: idempotent command, capability table, semaphore, persistent session, transition chokepoint, seq/timestamps, bounded delivery, correlation logging |
| [12-final-verdict.md](12-final-verdict.md)                               | Direct answers: fix / refactor / redesign / leave alone; live-readiness; Futu conditions; scale thresholds                                                  |
