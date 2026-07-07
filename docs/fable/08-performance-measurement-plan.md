# 8. Performance Measurement Plan (Part 9)

No benchmark numbers in this section are measured; everything is a **code-constant ceiling**
or an explicitly marked estimate. Measure before optimizing — the only latency figures the
repo itself contains are timeouts and sleeps.

## 8.1 Order-submission latency budget

| Stage                                     | Bound / estimate                                        | Source                                |
| ----------------------------------------- | ------------------------------------------------------- | ------------------------------------- |
| Browser event → fetch                     | ~0                                                      | —                                     |
| Next.js route (validate + build payload)  | est. <5 ms                                              | pure JS                               |
| Next → FastAPI hop                        | est. 1–5 ms (localhost/tailnet)                         | topology                              |
| Preflight Gate-4 (PG snapshot + coverage) | est. 5–30 ms                                            | 1–3 queries                           |
| Quote gate (non-combo)                    | up to 500 ms by freshness rule; est. 100–300 ms typical | `quote_guard.py:20`; pool snapshot    |
| `reserve_attempt`                         | est. 1–5 ms                                             | single INSERT                         |
| Subprocess: interpreter + imports         | **est. 0.3–1.5 s** — measure first, this is the unknown | `uv`-managed python + ib_async import |
| IB connect (auto clientId)                | est. 0.3–1 s; ceiling 10 s                              | `ib_place_order.py:51`                |
| qualifyContracts                          | est. 50–300 ms (1 RTT)                                  | —                                     |
| placeOrder + **fixed sleep**              | **2 s single-leg / 5 s combo** (hard floor)             | `ib_place_order.py:147-148`           |
| `mark_submitted`                          | est. 1–5 ms                                             | single UPDATE                         |
| Next refresh + orders refetch             | ≤10 s budget, best-effort                               | `place/route.ts:232`                  |
| **Typical single-leg total**              | **est. 3.5–6 s; ceiling 15 s**                          | dominated by sleep + spawn            |

The two levers, in order of certainty: remove the blind sleep (event-driven ack: −2/−5 s
guaranteed), then persistent session (−spawn −connect, Option B).

## 8.2 Quote-delivery latency budget

| Stage                                              | Bound / estimate                                             |
| -------------------------------------------------- | ------------------------------------------------------------ |
| IB event → tick-handler mutation                   | µs                                                           |
| Batch wait                                         | 0–100 ms (mean ≈ 50 ms)                                      |
| JSON.stringify per client                          | est. µs–ms per flush; grows with symbols × clients — measure |
| WS transmission                                    | est. ≤ a few ms (LAN/tailnet)                                |
| Browser parse + `setPrices`                        | est. ≤ ms                                                    |
| React re-render of MetricCards + WorkspaceSections | **unknown — the number to get first** (QS-5)                 |

## 8.3 Instrumentation to add (before any optimization PR)

Order path — one structured log line / span per stage keyed by `client_attempt_id`
(which already exists end-to-end and is the natural correlation id):

- monotonic timestamps at: route-in (Next), FastAPI-in, gates-done, reserved,
  subprocess-spawned, subprocess-first-output, ack-parsed, persisted, response-out.
  Cheapest implementation: the subprocess prints a second early line
  `{"stage":"ack",...}` (sketch §5 in 10-code-sketches) and FastAPI logs stage deltas.
- Prometheus (or plain log-derived) histograms: `order_place_duration_seconds{stage=…}`,
  `order_ack_latency_seconds`, counter `order_uncertain_total`,
  gauge `order_subprocesses_inflight` (the semaphore makes this trivial),
  `ib_pool_connections{role}`, `reconciliation_sweep_resolved_total`.
- Event-loop lag sampler in FastAPI (asyncio drift) — cheap, catches pool misuse.

Quote path:

- Relay: per-flush metrics — `flush_symbols`, `flush_clients`, `stringify_ms`,
  `client_buffered_amount` max, `dropped_coalesced_updates_total` (LWW overwrite count),
  `ib_ticks_total` vs `messages_sent_total` (amplification ratio), quote age at send
  (`now - state.lastTickTs`). Expose on the existing `GET /status`.
- Add `seq` + `relay_ts` to messages (QS-3) — then the browser can compute true
  end-to-end age; log p50/p95 quote age in `usePrices` dev mode.
- React Profiler session on `WorkspaceSections`/`MetricCards` during a 10 Hz burst —
  decides whether QS-5 is worth fixing.

Reconciliation visibility:

- `fill_to_ui_seconds` (fill event ts → orders refetch that contained it) — the docs claim
  ~120 s worst case; verify.
- Poller tick duration + per-surface failure counters (already partially logged via
  `record_service_health`).

## 8.4 Test workloads

1. **Order burst**: 5 concurrent distinct paper orders (post-semaphore) — measures queueing
   - gateway behavior; assert no clientId exhaustion, no 326 storms.
2. **Quote fan-out**: 3 browser sessions × ~80-symbol portfolio during RTH open — capture
   relay CPU, stringify ms, per-client bufferedAmount, browser frame drops.
3. **Slow-client soak**: one WS client that stops reading for 5 min during RTH — relay RSS
   must stay bounded (post-QS-1 fix), other clients' quote age unaffected.
4. **Reconnect churn**: kill/restart the relay 5× — count upstream `reqMktData` calls per
   symbol (should be 1 per restart post-QS-4 fix), time-to-first-quote.
5. **Paper ack-loss drill**: SIGKILL the place subprocess right after IB accepts (breakpoint
   or fake gateway) — verifies OP-1 fix end-to-end: row → UNCERTAIN → sweep → WORKING.
