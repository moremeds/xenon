# 12. Final Verdict

Direct answers, in the order asked.

**1. What should be fixed without changing the architecture?**
The ack protocol (UNCERTAIN state + `orderRef` correlation + runtime reconciliation sweep —
OP-1/2/3), the auth bypass on Next.js order routes (SEC-1), state-write discipline
(`transition()` chokepoint, CHECK constraint, `expected_states` everywhere — OP-8/9),
modify persisting price/qty (OP-4), naked-short audit writing state (OP-5), a semaphore on
order subprocesses (OP-7), bounded relay sends + Origin check (QS-1/SEC-2), and deletion of
the regime-gate dead code. All of this fits the current topology.

**2. What should be refactored?**
`_orders_place_from_body` into a gates/submit/persist service; the triplicated coverage
math into one Python source held to the TS guard by shared fixtures; reason codes by
codegen; combo net-price to one implementation; the relay's duplicated L1/depth batching
into a tested core module; `usePrices`/`IBStatusContext` into one socket core; the twin
poll-confirm loops in `ib_order_manage`. Continue the existing `server.py` →
`routes/`+`services/` migration opportunistically.

**3. What should be redesigned?**
Only the execution acknowledgement/ownership model, and only in stages: event-driven acks
now; the persistent orders-role session (Option B — half of it already exists as the
dormant, tested `pool_order_manage`) once Phase-1 measurements justify it; order events
pushed over the existing WS to retire the 5 s/30 s polling. Nothing else warrants redesign
at this scale.

**4. What should be left alone?**
The Next.js proxy layer (it earns its keep on secrets, same-origin, error passthrough —
fix its auth, don't remove it); the subprocess isolation model as the default execution
substrate; the Postgres-first/`order_events` append-only persistence design; the reservation
idempotency scheme; boot-sequenced reconciliation; the dev/prod DB split and read-only mode;
the relay's client-pushed, reference-counted subscription model and 100 ms LWW batching;
Futu's read-only design including its deliberate JSON-cache exception; the pytest DB
infrastructure.

**5. Is Xenon currently suitable for reliable live order placement?**
Qualified yes — for its actual operating model: a single attentive operator, defined-risk
strategies, three-layer naked-short defense, strong idempotency, and a proven incident
discipline. It is **not** yet suitable for unattended or latency-sensitive execution:
the timeout-after-acceptance window (OP-1) can manufacture duplicate orders precisely when
the operator is most tempted to retry, external-fill visibility is unproven (OP-10), and
the web-layer auth bypass (SEC-1) is unacceptable if the web port is ever reachable beyond
the tailnet. Complete the Immediate Safety Fixes (S1–S7) before placing further trust in it.

**6. Conditions before enabling Futu order placement?**
See 10-roadmap Phase 5 — all six: hardened IB path first; explicit capability table;
deliberate schema decision (the `broker='IB'` CHECKs are a feature until then); designed
`unlock_trade` + Futu-simulate test loop; Futu-aware coverage guard from the shared parity
fixtures; guard/incident tooling extended to the new path. Until all six hold, Futu stays
read-only — and today, Futu execution code simply does not exist.

**7. Can Xenon keep serving both the trading API and the realtime quote proxy?**
Yes. They are already separate processes (FastAPI vs the Node relay) sharing only the IB
Gateway and the ticket-validation call — that is the correct separation for this workload.
The findings against the quote path are implementation gaps (backpressure, seq, tests),
not co-tenancy problems.

**8. At what scale should those responsibilities be separated further?**
When any of these becomes true: more than a handful of concurrent authenticated users
(ticket store and fan-out are single-instance designs); a genuine need for >~100
market-data lines (requires additional gateways/accounts and a subscription broker layer,
QS-7); latency SLOs on execution that demand the dedicated worker (Option C); or Futu
execution goes live (broker adapter + separate execution worker becomes the clean seam).
Until then, the smallest architecture that solves the observed problems is the one already
running — with the fixes above.

---

_Method note: documentation was treated as hypothesis; the central order-path doc was found
materially stale (CX-4) and this report's citations were re-verified against code at
commit `4d864294`. Confidence labels appear per finding in 03-findings-table.md; items that
could not be established are marked there and in each deep dive (notably OP-10
external-fill visibility, real latency numbers, and CI execution of the real-FastAPI Vitest
lane)._
