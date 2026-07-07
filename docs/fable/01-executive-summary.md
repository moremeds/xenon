# 1. Executive Summary

**Repo state:** commit `4d864294` (post-v0.7.2), reviewed 2026-07-03. Method and confidence
conventions: see `README.md` in this directory.

## Overall assessment

Xenon is a deliberately conservative, Postgres-first broker terminal whose order path shows
real engineering discipline — durable idempotency reservation before any broker call, an
append-only event log, structurally idempotent fill ingestion, three-layer naked-short
defense, and an incident-history culture that visibly turned past bugs into guards and
tests. The topology (Next.js proxy → FastAPI → subprocess-per-write / pooled reads; a
separate Node relay for quotes) is appropriate for its actual workload: one operator, one
IB account, a portfolio-sized symbol set.

The defects are concentrated, not diffuse. On the order path, everything between IB's
acceptance of an order and Xenon's persistence of that fact is fragile: a 15-second
SIGKILL treats ambiguity as failure, nothing correlates a broker-side order back to its
reservation, and no runtime process repairs the resulting orphans. On the quote path, the
relay is functionally sound but unhardened: no backpressure, no sequence numbers, no Origin
check, and — decisive for a review — effectively zero CI-enforced behavioral tests on its
2,256 lines. A third cluster is drift: dead regime-gate code across both languages, a
central architecture doc that documents deleted systems as live, and safety math duplicated
three times held in parity by comments.

## Five strongest aspects

1. **Reservation-before-submit idempotency** — durable `PENDING` row with a unique attempt
   key before any broker interaction; one-winner semantics proven under 6–8 concurrent
   threads (`orders_store.py:106-160`).
2. **Structurally idempotent reconciliation** — `order_fills.exec_id` PK with
   `ON CONFLICT DO NOTHING`, idempotent late-commission patching, self-healing P&L
   re-aggregation, sequenced boot (rehydrate → replay → poller).
3. **Fault-isolated execution substrate** — broker writes in disposable subprocesses with
   argv-only exec (no shell), pooled reads pinned per role to respect ib_async's threading;
   FastAPI's event loop cannot be wedged by a broker call.
4. **Layered safety rails** — naked-short defense at UI/preflight/audit, read-only mode,
   dev/prod DB role split with a boot guard, caller-allowlist CI checks, single-use 30 s
   WS tickets.
5. **Incident discipline** — `docs/reference/order-path-incident-history.md` maps to real
   guards and regression tests; most past failure classes are demonstrably closed.

## Ten highest-value improvements (ranked)

1. **OP-1 (Critical):** timeout-after-broker-acceptance → `UNCERTAIN` state, IB `orderRef`
   correlation, event-driven ack instead of the blind 2–5 s sleep.
2. **SEC-1 (High):** Clerk-gate the Next.js order routes — today `/api/(.*)` is public and
   the proxy self-authenticates to FastAPI with the internal token.
3. **OP-3 (High):** runtime reconciliation sweep for UNCERTAIN/PENDING rows (today: boot-only).
4. **OP-2 (High):** protect the post-ack `mark_submitted` write with a compensating event.
5. **QS-1 (High):** bounded per-client WS delivery + `bufferedAmount` gating in the relay.
6. **QS-2 (High):** put the relay under CI tests (extract cores + one integration lane);
   likewise TS-1: route tests for the real place-subprocess path via a fake CLI.
7. **CX-1 (High):** one source of truth for coverage math + reason codes (parity fixtures /
   codegen instead of comment-synced triplication).
8. **OP-4/OP-5 (High):** modify persists price/qty; naked-short audit writes order state.
9. **OP-6 (High):** move combo replace server-side / place-first — the current Next.js
   cancel-then-place can leave a position naked.
10. **CX-3/CX-4 (Medium):** delete regime-gate dead code and repair the stale
    order-stack doc (it documents a deleted system and a never-accurate 403 claim).

## Most serious risks

- **Order path:** the ambiguous-acknowledgement window (OP-1 + OP-2 + OP-3). A slow gateway
  at the wrong moment shows the operator "FAILED" while the order is live; a natural retry
  doubles the position. Everything else on the order path is defensible; this is not.
- **Quote stream:** an unhardened fan-out (QS-1) on a component with no CI behavioral
  coverage (QS-2) — a slow client during a volatility burst degrades the relay unboundedly,
  and no test would have caught it before production.

## Is a major rewrite justified?

**No.** One Critical finding is a protocol bug fixable inside the current design; the
topology's conservatism is precisely what has kept a live-money system safe through its
incident history. The recommended path is Option A (minimal refactor, ~10–15 shippable PRs)
now, the persistent-session Option B later only if Phase-1 measurements justify it, and a
dedicated execution worker only if Futu execution and/or multi-user ever arrive. Futu order
placement, verified against code, **does not exist** — and should stay that way until the
six conditions in the roadmap's Phase 5 hold.
