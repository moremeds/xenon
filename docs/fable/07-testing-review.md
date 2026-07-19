# 7. Testing Review (Part 11)

## 7.1 Overall verdict

The Python order-path test suite is unusually strong where it looks (real Postgres via the
Phase 1–3 fixture infrastructure, genuine concurrency tests, incident-driven regression
pins) and completely dark exactly where the risk is: **no test anywhere executes the real
place subprocess path**, and **no CI job executes any behavior of the Node relay**.
Playwright "E2E" is frontend-rendering coverage only.

## 7.2 Fidelity map (condensed from the three test audits)

| Component                                                                       | Fidelity                                                                                                                       | Notes                                                                      |
| ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------- | -------------------------------------------------- |
| `orders_store` (reserve/mark/events/snapshot/drift/modify-seq)                  | DB-real, incl. 8-thread one-winner                                                                                             | Excellent                                                                  |
| `/orders/place` idempotency + quote/preflight gates                             | DB-real + TestClient, **test mode** (subprocess stubbed)                                                                       | Gates well pinned; subprocess dark                                         |
| `/orders/cancel                                                                 | modify` failure classification                                                                                                 | DB-real, test-mode OFF, subprocess runner faked with canned `ScriptResult` | Good — the only route tests of real classification |
| `/orders/place` real-subprocess branches (timeout, IB reject, post-ack persist) | **UNTESTED** (TS-1)                                                                                                            | The Critical-finding cluster                                               |
| `ib_place_order` CLI                                                            | unit-mocked fake client; TIF passthrough only                                                                                  | No error/reject/qualify-failure cases                                      |
| `ib_order_manage` CLI                                                           | unit-mocked, thorough (326 retries, PendingCancel timeout, disappearance=success)                                              | Good                                                                       |
| `subprocess.py` helper                                                          | subprocess-real (fake executables)                                                                                             | Good; garbage-stdout case missing                                          |
| ib_pool (role pinning, reconnect coalescing, clientId registry)                 | unit-mocked + `inspect.getsource` string checks                                                                                | Behavior pinned; source-string tests brittle                               |
| clientId allocation/exhaustion                                                  | unit-mocked, exhaustive                                                                                                        | Good                                                                       |
| Rehydrate / fills replay / activity mirror / TWS sweep                          | DB-real (committed_db) + unit; partial fills, drift, late commission covered                                                   | Good                                                                       |
| Naked-short: TS guard, preflight parity fixture, audit detection                | unit + fixture                                                                                                                 | Good detection; audit **cancel→state sync** untested (OP-5)                |
| Node relay                                                                      | **zero CI coverage** (probe excluded via `pyproject.toml:112`; `.mjs` tests never run; 2 Vitest suites test reimplementations) | QS-2                                                                       |
| usePrices hook                                                                  | Real hook + MockWebSocket — reconnect, diff-subscribe, eviction, stale-socket, malformed input                                 | Strong                                                                     |
| WS tickets                                                                      | FastAPI-level single-use/expiry tested (`test_auth.py:37-48`); relay-side enforcement untested                                 | Partial                                                                    |
| Playwright order specs                                                          | 6/7 intercept `/api/orders/*` in-browser (Next routes bypassed); nightly runs `next dev` only                                  | Rendering coverage                                                         |

## 7.3 Gap checklist (explicit, from Part 11 list)

| Concern                                 | Status                                                                                           |
| --------------------------------------- | ------------------------------------------------------------------------------------------------ |
| Idempotent submission / duplicate retry | **TESTED** (route + store, concurrent)                                                           |
| Timeout after broker acceptance         | **UNTESTED** (OP-1)                                                                              |
| Concurrent submissions                  | Partially — same attempt id yes; distinct simultaneous orders no                                 |
| Client-ID exhaustion                    | TESTED (allocator) — cross-process contention untested                                           |
| Subprocess crash mid-place              | Helper-level only; route branch untested                                                         |
| IB reconnect during placement           | UNTESTED (`_run_ib_script_with_recovery` never itself tested)                                    |
| DB failure after broker placement       | UNTESTED (OP-2)                                                                                  |
| Partial fills                           | TESTED (rehydrate/aggregator/record_fill)                                                        |
| External TWS cancel / modify            | TESTED (sweep + drift) — external **fills** visibility untestable without live paper run (OP-10) |
| Order polling latency                   | Untested/unmeasured (docs claim ~120 s worst case fill→toast)                                    |
| Futu capability enforcement             | TESTED (403 READ_ONLY_BROKER, `test_place_quote_gate.py:234`)                                    |
| WS backpressure / slow client           | UNTESTED (and unimplemented, QS-1)                                                               |
| Stale-quote recovery                    | TESTED (tick handler + hook + e2e)                                                               |
| Sequence ordering                       | Client-side partially; server none (no seq exists)                                               |
| Symbol unsubscribe                      | Hook-level tested; IB-line-release assertion missing                                             |
| Relay restart with clients              | UNTESTED                                                                                         |
| Multiple concurrent WS clients          | Live-probe only (not CI) + reimplementation tests                                                |
| Auth ticket reuse/expiry/replay         | FastAPI-level TESTED; relay upgrade enforcement UNTESTED                                         |

## 7.4 Recommended tests (each names the invariant protected)

1. **Route tests with a fake place CLI** (highest value). Point the entry-point resolver at
   a stub binary via env; cases: (a) ack printed then process killed → row must become
   `UNCERTAIN`, not FAILED (post-fix invariant: _no terminal state without broker
   confirmation_); (b) timeout with no output; (c) garbage stdout; (d) valid reject JSON
   code 110 → `LIMIT_OFF_TICK`; (e) DB write failure injected after ack → compensating
   event exists (_no ack is ever silently dropped_).
2. **Reconciliation sweep test** (after OP-1 fix): seed an `UNCERTAIN` row + a fake IB
   open-order snapshot carrying `orderRef=client_attempt_id` → row must resolve to
   WORKING with ids attached (_every broker order is re-associable to its reservation_).
3. **State-machine property test** (hypothesis): random interleavings of
   `transition()` calls never produce an illegal edge and never lose an `order_events`
   row per transition (_append-only audit completeness_).
4. **Two-distinct-orders concurrency test**: simultaneous places of different attempt ids
   → both reserved, subprocess spawns bounded by the semaphore (_bounded execution
   concurrency_).
5. **Relay integration test in CI**: spawn the real `ib_realtime_server.js` with a fake
   IB event source (EventEmitter shim replacing `@stoqey/ib`), real WS clients; assert:
   ticket 401 enforcement, LWW batch content, unsubscribe cancels upstream exactly once,
   resubscribe-after-drop is idempotent, slow client (never reads) does not grow relay RSS
   beyond the bound (_backpressure_), seq strictly increases (_ordering_).
6. **Parity fixtures**: gate4 case table executed by both `preflight.py` and
   `nakedShortGuard.ts`; reason-code enum equality test (_safety rules cannot drift
   between languages_).
7. **Naked-short audit → state sync test**: audit cancels a violator → submission row is
   CANCELLED with `NAKED_SHORT_AUDIT` reason (_no silent broker-side mutations_).
8. **Live paper-account probe (manual, scripted)**: place from clientId 24, fill it, check
   whether clientId 3's `fills()` sees it — resolves OP-10 one way or the other
   (_external-fill visibility is known, not assumed_).
9. **CI hardening**: fail (not skip) when `fastapiHarness` can't find `.venv` in CI; run
   the relay `.mjs` unit tests in the web-tests job (TS-3/TS-4).
