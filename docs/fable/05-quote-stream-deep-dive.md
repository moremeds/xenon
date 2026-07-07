# 5. WebSocket / Quote-Service Deep Dive

## 5.1 What the relay actually is

`scripts/infra/ib_realtime/ib_realtime_server.js` (2,256 lines) is **not** a simple
transport proxy. Verified roles in one process:

1. **Transport proxy** — IB TCP (`@stoqey/ib`) → WS JSON.
2. **Quote cache** — the only copy of live `PriceData` per symbol (`symbolStates`),
   fundamentals LRU (500), search cache (200×5 min), option close prices persisted to disk
   (`data/option_close_cache.json`, relay `:531-593`).
3. **Subscription manager** — reference-counted client↔symbol maps, depth-ticket LRU under
   a `MAX_CONCURRENT_DEPTH=3` budget.
4. **Normalization layer** — the extracted pure modules (`ib_tick_handler.js`,
   `normalize.js`, `depth_book.js`, `tape_feed.js`) — the best-designed part of the stack.
5. **Self-healing supervisor** — stale watchdog + "gateway restart" + reconnect loop +
   snapshot rate limiter.

So: an accidental mixture of five responsibilities, of which (4) is cleanly separated and
(1)–(3)+(5) share module-level mutable state. For the current workload (one operator, a
portfolio-sized symbol set) the _architecture_ is appropriate; the _implementation_ has
specific gaps listed below.

## 5.2 Lifecycle answers (Part 7 checklist)

- **Symbol requests:** client-pushed only; the app derives the set from portfolio +
  open-order legs + focused ticker in the single `usePrices` call site
  (`WorkspaceShell.tsx:221-254`). No server-side universe.
- **Reference counting:** yes for teardown (`:906-927`); **subscribe is not idempotent
  upstream** — `startLiveSubscription` always cancels and re-issues `reqMktData`
  (`:833-862`), so every fresh socket (tab refresh, reconnect) churns IB tickets for
  symbols other tabs already stream (QS-4).
- **Duplicate upstream subscriptions:** prevented at steady state by the maps; violated
  transiently during the resubscribe churn above.
- **Per-client filtering:** yes — clients receive only symbols they subscribed
  (`sendToSymbolSubscribers`, batch fan-out per client buffer).
- **Initial snapshots:** new subscribers get the current in-memory `PriceData` immediately
  (possibly stale if IB hasn't ticked); no explicit snapshot/delta handshake.
- **Batching:** 100 ms last-write-wins per symbol per client (`BATCH_INTERVAL_MS=100`,
  `:703`), early flush at 50 buffered symbols. Confirmed against the docs' claim.
- **Internal consistency:** each flushed entry is a full shallow copy of the per-symbol
  object — no tearing within a message; bid/ask/last/greeks are asynchronous _between_
  ticks because IB delivers them that way.
- **Ordering / sequence numbers:** none (QS-3). **Stale overwriting newer:** across one
  socket, no (single TCP stream + LWW); across reconnects the client guards with a
  stale-socket check (`use-prices-ws-stability` proves old sockets can't overwrite).
- **Timestamps:** `PriceData.timestamp` = relay receive time (`ib_tick_handler.js:179,285`);
  tape prints carry real exchange time. Two clocks under one field name — document or split.
- **Backpressure / slow clients:** none — bare `.send()` in try/catch (`:756-764`), no
  `bufferedAmount` checks, unbounded buffers; only the 65 s pong timeout removes truly dead
  sockets. One slow client does not block others' sends (independent `.send()` calls) but
  inflates relay memory and its own latency unboundedly (QS-1).
- **Serialization:** per client (`JSON.stringify` × N) — CPU amplification at fan-out.
- **Disconnect cleanup:** thorough (`disconnectClient` `:929-952`, depth symmetric).
- **Reconnect/resubscribe:** relay→IB fixed 5 s retries, full state wipe + restore;
  browser→relay exponential backoff (1 s→30 s, 10 attempts; unlimited on the status
  socket), fresh single-use ticket per connect.
- **State across relay restart:** lost by design; only the port survives via
  `/tmp/xenon-ib-realtime.json`.
- **Option contracts:** built inline (`SMART/USD`, multiplier "100"), no qualification
  step, no persistent contract cache; IB error 200 handled per-symbol.
- **Stale-data detection false positives:** watchdog requires RTH + active subscriptions +
  45 s of total silence — low false-positive risk; client 60 s threshold is generous vs the
  30 s ping cadence.
- **Automatic gateway restart safety:** cooldown 120 s, RTH-gated — but in the docker/cloud
  prod branch it only bounces the relay's own socket (`:665-689`); it cannot recover a
  wedged Gateway and the name over-promises (QS-6).

## 5.3 Quantification (from code constants — no invented benchmarks)

- Added latency from batching: 0–100 ms, mean ≈ 50 ms `[COMPUTED]`.
- Max effective rate: 10 updates/s/symbol/client `[COMPUTED]`.
- Serialization cost: O(updated symbols × subscribed clients) stringify per 100 ms
  `[COMPUTED]`; actual CPU unmeasured `[Hypothesis — measure]`.
- Memory: O(symbols) server state + O(clients × their symbols) buffers; fundamentals LRU 500.
- **500 symbols:** infeasible on this account regardless of relay design — the IB
  market-data line budget (~100/account, previously measured, shared across every gateway
  client) is the binding constraint, and the relay has no explicit error-101 branch
  (`:1855-1956`) so starvation would be silent (QS-7).
- Multiple sessions: IB-side deduped; relay-side cost is N× buffers + N× stringify; plus
  reconnect churn per tab (QS-4).
- Volatility bursts: LWW coalescing caps per-symbol rate at 10 Hz; no additional burst
  throttle needed for L1; depth capped at 3 tickets; snapshot requests limited to 50/s.

## 5.4 Frontend consumption

- One quotes socket (`usePrices`) + one status socket (`IBStatusContext`) with copy-pasted
  reconnect/staleness logic and constants (QS-8).
- Every batch → `setPrices({...prev, ...updates})` → new object identity → re-render of
  non-memoized `MetricCards` (1,309 ln) and `WorkspaceSections` (2,689 ln) up to 10×/s
  (QS-5). Structure confirmed; render cost needs React Profiler measurement before
  optimizing.
- Stale-price handling at the data level is genuinely good and tested: synthesized-mid
  flags (`lastIsCalculated`), frozen-close divergence override, `resolveRealtimePrice`.

## 5.5 Proxy-layer review (Part 8)

Two distinct proxies exist; they deserve different verdicts.

**Next.js HTTP proxy (orders/portfolio/etc.) — keep, but fix auth.** What it actually does
on the order path (verified): TypeBox schema + business validation, payload contract
building, error passthrough with upstream status/body preserved (`passThroughXenonError`),
request-id header, timeout envelopes (60 s place / 20 s cancel-modify wrapping FastAPI's
15 s subprocess cap — correctly ordered), post-mutation refresh orchestration, and the
combo-replace orchestration (which should move server-side, OP-6). It also holds the
secrets boundary (`XENON_INTERNAL_API_TOKEN` is server-only) and same-origin surface for
the browser. Removing it would push Clerk validation, CORS, and secret handling into
FastAPI for cross-origin browser calls — more work than the proxy costs. Latency added is
one localhost hop `[Hypothesis: low single-digit ms — measure]`. **But** today it is also
an auth _bypass_: middleware exempts `/api/(.*)` and the proxy self-authenticates with the
internal token (SEC-1). The fix is per-route `auth.protect()` in order-mutating routes,
not proxy removal.

**WS path — already direct.** The browser connects straight to the relay (bypassing
Next.js/FastAPI except for ticket mint + ws-config discovery). This is the right call for
a streaming path; what's missing is Origin validation and rate limiting at the relay
(SEC-2), not another proxy hop.

## 5.6 How it should evolve

At the current workload: keep the topology (separate Node relay process, direct WS,
client-pushed subscriptions). Fix, in order:

1. Bounded per-client send queues + `bufferedAmount` gating + drop-oldest (QS-1).
2. `seq` + explicit `relay_ts` in every message; document the two-clock situation (QS-3).
3. Idempotent upstream subscribe ("already live → attach subscriber") (QS-4).
4. Explicit IB error-101 handling + a subscription cap with priority (portfolio > focused
   ticker > rest) (QS-7).
5. Extract the batched-channel core (used by both L1 and depth/tape) into a module with
   unit tests, and add one CI integration test that spawns the real relay against a fake
   IB event source (QS-2, QS-10).
6. Memoize the two big consumers / move to a per-symbol subscription store (QS-5) — after
   profiling.

A separate quote service, message bus, or per-symbol topic infrastructure is **not**
warranted at one-operator scale; the IB line budget caps usefulness long before the relay
architecture does.
