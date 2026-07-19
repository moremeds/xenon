# P1.2 — Relay `/status` metrics + `seq`/`relay_ts` in the WS message protocol

- **Date:** 2026-07-05
- **Branch:** `feat/relay-seq-status-metrics`
- **Findings:** QS-3 (Medium — quote messages lack timestamp/sequence metadata), plus the observability half of P1.2 (relay `/status` flush/amplification/quote-age metrics).
- **Severity:** Medium.
- **Goal (one line):** Stamp every batched WebSocket message (`batch`/`depth-batch`/`tape-batch`) with a strictly-increasing `seq` and a `relay_ts` flush clock, let `usePrices` drop stale/reordered batches and expose quote age in dev, and add flush-size / amplification / flush-duration / quote-age counters to `GET /status` — all additive and backward compatible.

---

## ⚠️ Coordination — this PR lands AFTER S7

This plan is written to merge **after** `fix/relay-backpressure-origin-allowlist`
(plan `docs/superpowers/plans/2026-07-05-fable-s7-relay-backpressure-origin.md`),
which merges FIRST. Every relay anchor below is the **post-S7** code, not HEAD.
Before you start:

```bash
git log --oneline -5 | grep -i "backpressure\|origin-allowlist\|QS-1\|SEC-2"
grep -n "sendBounded" scripts/infra/ib_realtime/ib_realtime_server.js
grep -n "relayMetrics" scripts/infra/ib_realtime/ib_realtime_server.js
grep -n "test:relay" package.json
ls scripts/infra/ib_realtime/sendBounded.js scripts/infra/ib_realtime/__tests__/*.mjs
```

**If `sendBounded`, `relayMetrics`, the root `package.json` `"scripts": { "test:relay": ... }` entry, or `scripts/infra/ib_realtime/sendBounded.js` do NOT exist → STOP.** S7 has not merged yet. Do not proceed; report that this PR is blocked on S7. (See Tripwire 1.)

This plan **reuses** S7 primitives and does not modify them beyond additive extension:

- `sendBounded(client, payload, metrics)` call sites — I pass a **stamped** payload (seq/relay*ts added \_into* the payload object). `sendBounded` itself is unchanged (the seam stays clean).
- `relayMetrics` object — I add four fields to its literal.
- The `__tests__/*.mjs` lane + `npm run test:relay` glob + the `relay-tests` CI job — my new `.mjs` files ride the existing glob; **no CI edits**.
- S7's `freePort()` + spawn-the-real-relay harness pattern — I copy that pattern into a new self-contained spawn test (I do NOT edit S7's `relay_integration.test.mjs`, to avoid a merge/anchor collision).

---

## 1. Context (what exists today — verified against HEAD + the S7 plan)

**Relay** `scripts/infra/ib_realtime/ib_realtime_server.js` (ESM, `"type": "module"`):

- Global freshest-tick clock already exists: `let lastTickTimestamp = Date.now();` (`:642`), updated on every `onTickPrice` (`:1538`) and `onTickSize` (`:1561`). `Date.now() - lastTickTimestamp` is a ready-made cheap "quote age" aggregate (age of the newest IB tick across ALL symbols) — **no per-symbol map dump needed.**
- **Post-S7** `flushBatches()` sends `sendBounded(client, { type: "batch", updates }, relayMetrics)` and clears the buffer only on `true`. It ends by calling `flushDepthAndTapeBatches()`.
- **Post-S7** `flushDepthAndTapeBatches()` sends `sendBounded(client, { type: "depth-batch", updates }, relayMetrics)` and `{ type: "tape-batch", updates }` the same way.
- **Post-S7** `const relayMetrics = { droppedFlushes: 0, droppedSymbols: 0 };` is declared just after `const STATUS_TOKEN = ...`.
- **Post-S7** `GET /status` JSON body already carries `dropped_flushes_total`, `dropped_symbols_total`, `max_client_buffered_bytes` in addition to the pre-S7 `ib_connected`, `now_ms`, `ttl_ms`, `subscribers`, `anonymous_count`.
- Sibling modules are imported as `./x.js` (e.g. `./normalize.js`); S7 added `./sendBounded.js` and `./originAllowlist.js`. Unit tests are `node:test` + `node:assert/strict` `.mjs` files in `__tests__/`.

**Frontend** `web/lib/usePrices.ts` + `web/lib/pricesProtocol.ts`:

- `pricesProtocol.ts` is **types only** — no runtime schema validation (no typebox/zod). `usePrices` does `JSON.parse(event.data) as WSMessage`, a plain cast. **Adding fields cannot cause runtime rejection** (brief item 3, verified). `WSBatchMessage`/`WSDepthBatchMessage`/`WSTapeBatchMessage` are the batched types.
- `usePrices` `ws.onmessage` switch (`:738`) handles `case "batch"` (`:753`), the grouped `case "depth"/"depth-batch"/"depth-unavailable"` (`:776`), and `case "tape-batch"` (`:785`).
- `ws.onopen` (`:704`) does a full re-sync on every fresh socket (`lastSentHashRef.current = ""` at `:713`). This is where a `lastSeqRef` reset belongs (handles relay restart: a restarted relay resets its `seq` to 0; the client's socket closes → reconnect → `onopen` → reset, so low post-restart seqs are not falsely dropped).
- The hook return object (`:1067`) currently exposes `prices, fundamentals, depths, tape, connected, ibConnected, ibIssue, ibStatusMessage, error, reconnect, getSnapshot`.
- Dev-log gate already exists: `const WS_DEBUG = process.env.NODE_ENV === "development";` (`:109`) + `wsLog(...)` (`:110`).
- Test harness to reuse: `web/tests/use-prices-ws-stability.test.ts` has a `MockWebSocket` with `simulateOpen()` / `simulateMessage(obj)` driven through `renderHook` from `@testing-library/react`, `vi.stubGlobal("WebSocket", ...)`, `vi.useFakeTimers()`. `web/tests/batched-prices.test.ts` shows the pure-handler-extraction test style. `applyDepthMessage` is already an **exported pure reducer** in `usePrices.ts` — I follow that precedent and export small pure helpers for seq/quote-age.

**What the executor does NOT need to understand:** IB tick decoding, depth ladder serialization, subscriber TTL, line-budget eviction, the S7 Origin allowlist, or the S7 backpressure math. Touch only: flush payload stamping, the `relayMetrics` literal, the `/status` body, and the frontend batch/depth/tape message handling + return.

---

## 2. Drift from review

1. **File length / line numbers:** the fable docs cite `ib_realtime_server.js` at ~2,256 lines; HEAD is 2663 and S7 adds more. **Every anchor below is a function name + a unique code snippet**, never a bare line number. The line numbers in §1 are orientation only.
2. **Sketch §6 field name `prices:` is wrong for this repo.** Code sketch 11 §6 shows `{ type: "batch", seq, relay_ts, prices: ... }`. The real relay + the entire frontend use `updates:` (verified: `WSBatchMessage.updates`, `flushBatches` builds `updates`). **Keep `updates` — renaming to `prices` would break every consumer.** `seq`/`relay_ts` are added _alongside_ `updates`.
3. **Sketch §6 clears the buffer unconditionally after send.** That is the pre-S7 shape. Post-S7 the clear is gated on `sendBounded(...) === true`. This plan keeps the S7 clear-on-success behavior and only adds the stamp to the payload — the two changes compose cleanly.
4. **"stringify ms" metric → replaced by `last_flush_ms` (flush wall time).** The roadmap lists "stringify duration ms". Measuring pure `JSON.stringify` separately would require either double-stringifying (the payload is already stringified inside `sendBounded`) or reaching into the S7 `sendBounded` module and breaking its seam. Instead I expose `last_flush_ms` = `performance.now()` wall time of the whole flush cycle, which is **dominated by** `JSON.stringify` + socket send-enqueue. This is the same CPU-cost signal, measured honestly, with zero double work and no change to `sendBounded`. Documented as a deliberate deviation.
5. **`seq` increment timing — decision + justification.** One global counter, incremented **per message at flush-construction time** (i.e. inside `stampFlush`, evaluated before the `sendBounded` call — this matches sketch §6's `++globalSeq` in the payload literal). Because S7 keeps the buffer on a dropped send, a dropped flush **consumes** a `seq` value that is never delivered → the client sees a **gap**. Gaps are harmless: the frontend check is `seq <= lastSeq → drop`, which tolerates gaps (it only rejects non-increasing values). This is the brief's explicitly-sanctioned "increment before send, allow gaps" option. It is chosen over "increment only inside `sendBounded`'s success path" specifically to keep `sendBounded` unchanged (the clean seam the brief asks me to reuse). `relay_ts` is one `Date.now()` taken at the top of the flush cycle and shared across `batch` + `depth-batch` + `tape-batch` for that cycle.

---

## 3. Goal / Non-goals

**Goal:**

- New relay module `messageSeq.js` (`nextSeq`/`resetSeq`/`stampFlush`) — one global, strictly-increasing, per-message sequence + `relay_ts` stamping, unit-tested in CI.
- Stamp `batch`, `depth-batch`, `tape-batch` payloads via `stampFlush` at every `sendBounded` call site.
- Add `last_flush_symbol_sends`, `last_flush_clients`, `last_flush_ms`, `quote_age_ms` to `GET /status`.
- Frontend: optional `seq?`/`relay_ts?` on the three batched protocol types; `usePrices` drops stale/reordered batches via a `lastSeqRef` (reset on `onopen`); exposes quote age via a `getQuoteAgeMs()` getter (dev-facing, ref-backed, no extra re-render).
- Tests: `node:test` unit for `messageSeq`; a spawn-the-real-relay `node:test` asserting the new `/status` fields; Vitest for the frontend seq-drop + quote-age helpers and a hook-level drop check, including an explicit **backward-compat (no-seq) still-applies** case.

**Non-goals (do NOT do here — one change = one PR):**

- **QS-1 / SEC-2 (S7):** backpressure bound + Origin allowlist. Prerequisite, already merged. Do not touch `sendBounded.js` or `originAllowlist.js`.
- **QS-10:** do NOT unify `flushBatches` and `flushDepthAndTapeBatches`. Stamp both in place. (P4.3.)
- **P1.3 React-Profiler / `fill_to_ui_seconds` measurement** — separate PR.
- Do NOT stamp single `price`/`snapshot`/`fundamentals` messages (they ride `sendMessage`, are low-volume, and carry their own `timestamp`). Only the three batched types get `seq`/`relay_ts`.
- Do NOT add a per-symbol quote-age map to `/status`. The single `quote_age_ms` aggregate is the whole scope.
- No `.env`/config knobs. The four `/status` fields and the caps are constants/derived; no new env vars.

---

## 4. Key facts (verified)

| Fact                                   | Value                                                                                                                                   | Verified at                                     |
| -------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------- |
| Relay file                             | `scripts/infra/ib_realtime/ib_realtime_server.js`                                                                                       | HEAD                                            |
| ESM module type                        | `"type": "module"`                                                                                                                      | root `package.json:3`                           |
| Freshest-tick clock (module global)    | `let lastTickTimestamp = Date.now();`                                                                                                   | `:642`; updated `:1538`, `:1561`                |
| Post-S7 metrics object                 | `const relayMetrics = { droppedFlushes: 0, droppedSymbols: 0 };`                                                                        | S7 plan §9b                                     |
| Post-S7 L1 flush send                  | `sendBounded(client, { type: "batch", updates }, relayMetrics)` inside `flushBatches`                                                   | S7 plan §9d                                     |
| Post-S7 depth/tape flush               | `sendBounded(client, { type: "depth-batch"/"tape-batch", updates }, relayMetrics)` in `flushDepthAndTapeBatches`                        | S7 plan §9f                                     |
| Post-S7 `/status` body                 | adds `dropped_flushes_total`, `dropped_symbols_total`, `max_client_buffered_bytes`                                                      | S7 plan §9h                                     |
| S7 relay-import block                  | `import { parseAllowlist, evaluateOrigin } from "./originAllowlist.js";`                                                                | S7 plan §9a                                     |
| S7 test lane                           | `"test:relay": "node --test scripts/infra/ib_realtime/__tests__/*.mjs"` + `relay-tests` CI job                                          | S7 plan §11                                     |
| S7 spawn harness pattern               | `freePort()` + `spawn(process.execPath, [RELAY, "--port", ...], { env })` + listen-line readiness + `IB_REALTIME_RUNTIME_FILE` redirect | S7 plan §10                                     |
| Relay CLI flags                        | `--port <n> --ib-host <h> --ib-port <n> --client-id <n> --verbose`                                                                      | S7 plan §4                                      |
| Listen log line                        | `` `WebSocket server listening on ${WS_HOST}:${cli.port}` `` (`WS_HOST="0.0.0.0"`)                                                      | S7 plan §4                                      |
| `pricesProtocol.ts` runtime validation | NONE (types only; `JSON.parse ... as WSMessage`)                                                                                        | `web/lib/usePrices.ts:736`, `pricesProtocol.ts` |
| Frontend batched types                 | `WSBatchMessage` / `WSDepthBatchMessage` / `WSTapeBatchMessage`, all keyed on `updates`                                                 | `pricesProtocol.ts:89,161,179`                  |
| Dev-log gate                           | `const WS_DEBUG = process.env.NODE_ENV === "development";`                                                                              | `usePrices.ts:109`                              |
| Exported pure reducer precedent        | `export function applyDepthMessage(...)`                                                                                                | `usePrices.ts:138`                              |
| Vitest WS harness                      | `MockWebSocket` + `renderHook` + `vi.stubGlobal("WebSocket")`                                                                           | `web/tests/use-prices-ws-stability.test.ts`     |
| CI vitest discovery                    | `npx vitest run --config vitest.config.ts web/tests` (auto-discovers `web/tests/*.test.ts`)                                             | `.github/workflows/ci.yml:83`                   |
| Node version (CI)                      | 20 (global `performance` available; explicit import added anyway)                                                                       | `ci.yml:29,46,75`                               |

**No new env vars.** `MAX_*` caps are S7's; this PR adds no tunables.

---

## 5. Repo invariants this plan must respect

- ESM only; sibling imports as `./x.js`.
- This is the **quote path**, not the order path: order-path CI guards, incident-history, `expected_states`, combo/BAG rules, `XENON_READ_ONLY` — **all N/A**. No `data/*.json` reads/writes added.
- Never `git push origin master`; branch + PR; **no AI attribution trailers** in commits.
- Tests: no network at runtime (the spawn test uses a **dead** IB port; the frontend tests use `MockWebSocket`). Real tickers at frozen prices where a price appears (use `AAPL`/`TSLA`/`NVDA` values, never `FOO`/round-number placebos — the existing `makePriceData` fixtures already do this).
- Simplicity first: no env knobs; no loop unification (QS-10 stays out).
- Additive protocol only — old clients must keep working (verified in Step 8 test `(f)`).

---

## 6. Step 0 — RED baseline (prove the gap, confirm S7 landed)

```bash
# S7 must be present (else STOP — Tripwire 1):
grep -n "relayMetrics" scripts/infra/ib_realtime/ib_realtime_server.js         # expect ≥1 line
grep -n "test:relay" package.json                                              # expect 1 line

# QS-3 must be ABSENT (this PR introduces it):
grep -n "relay_ts\|stampFlush\|messageSeq" scripts/infra/ib_realtime/ib_realtime_server.js   # expect NO output
grep -n "lastSeqRef\|relay_ts\|getQuoteAgeMs" web/lib/usePrices.ts                            # expect NO output
```

**If the QS-3 greps print anything → STOP (Tripwire 2): the feature partly exists; anchors are wrong.**

---

## 7. Step 1 — New relay module `messageSeq.js` (TDD: test first)

### 7a. Failing unit test

Create `scripts/infra/ib_realtime/__tests__/messageSeq.test.mjs`:

```js
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { nextSeq, resetSeq, stampFlush } from "../messageSeq.js";

beforeEach(() => resetSeq());

test("nextSeq starts at 1 and strictly increases", () => {
  assert.equal(nextSeq(), 1);
  assert.equal(nextSeq(), 2);
  assert.equal(nextSeq(), 3);
});

test("resetSeq returns the counter to zero", () => {
  nextSeq();
  nextSeq();
  resetSeq();
  assert.equal(nextSeq(), 1);
});

test("stampFlush adds a strictly-increasing seq and the given relay_ts", () => {
  const ts = 1_720_000_000_000;
  const a = stampFlush({ type: "batch", updates: { AAPL: {} } }, ts);
  const b = stampFlush({ type: "depth-batch", updates: {} }, ts);
  assert.equal(a.seq, 1);
  assert.equal(b.seq, 2);
  assert.equal(a.seq < b.seq, true); // monotonic across message types (ONE counter)
  assert.equal(a.relay_ts, ts);
  assert.equal(b.relay_ts, ts);
});

test("stampFlush preserves type and updates untouched", () => {
  const updates = { TSLA: { last: 423.6 } };
  const msg = stampFlush({ type: "tape-batch", updates }, 42);
  assert.equal(msg.type, "tape-batch");
  assert.deepEqual(msg.updates, updates);
});

test("stampFlush returns a NEW object (does not mutate the input)", () => {
  const input = { type: "batch", updates: {} };
  const out = stampFlush(input, 7);
  assert.equal(Object.hasOwn(input, "seq"), false);
  assert.equal(out.seq, 1);
});
```

Run — must FAIL (module missing):

```bash
node --test scripts/infra/ib_realtime/__tests__/messageSeq.test.mjs
```

**Expected: error — cannot find module `../messageSeq.js`.**

### 7b. Implement

Create `scripts/infra/ib_realtime/messageSeq.js`:

```js
/* Global message sequence + flush-time stamping (finding QS-3).
 *
 * ONE module-scoped counter shared across batch / depth-batch / tape-batch so a
 * single client, receiving all three over its ordered TCP connection, observes a
 * strictly-increasing `seq`. The frontend uses `seq <= lastSeq -> drop` to shed
 * stale/reordered/duplicate batches; that comparison tolerates GAPS, which is why
 * it is safe that a backpressure-dropped flush (S7 keeps its buffer) consumes a
 * seq value that is never delivered. `relay_ts` is the receive-side flush clock
 * (named honestly — it is NOT the exchange print time), one Date.now() per flush
 * cycle shared across the cycle's messages.
 */

let seq = 0;

/** Next strictly-increasing sequence number (per relay lifetime). */
export function nextSeq() {
  return ++seq;
}

/** Test-only: reset the counter so node:test cases are isolated. */
export function resetSeq() {
  seq = 0;
}

/**
 * Return a NEW payload with `seq` (one increment) and `relay_ts` added.
 * The input object is not mutated. Callers pass the result to sendBounded().
 */
export function stampFlush(payload, relayTs) {
  return { ...payload, seq: nextSeq(), relay_ts: relayTs };
}
```

Re-run — **Expected: all 5 tests pass, `# fail 0`.**

---

## 8. Step 2 — Wire `messageSeq` + metrics into the relay

All edits in `scripts/infra/ib_realtime/ib_realtime_server.js`. Apply in order.

### 8a. Imports

**Anchor** — the S7 import line:

```js
import { parseAllowlist, evaluateOrigin } from "./originAllowlist.js";
```

**Insert immediately after it:**

```js
import { stampFlush } from "./messageSeq.js";
import { performance } from "node:perf_hooks";
```

(`performance` is a Node 20 global; the explicit import is belt-and-suspenders and harmless.)

### 8b. Extend `relayMetrics`

**Anchor** — the S7 metrics declaration:

```js
const relayMetrics = { droppedFlushes: 0, droppedSymbols: 0 };
```

**Replace with:**

```js
const relayMetrics = {
  droppedFlushes: 0,
  droppedSymbols: 0,
  // P1.2 flush observability (exposed on GET /status):
  lastFlushSymbolSends: 0, // Σ per-client symbols sent in the most recent L1 flush (write-amplification count)
  lastFlushClients: 0, // clients that received a non-empty L1 flush this cycle
  lastFlushMs: 0, // wall time of the most recent flush cycle (dominated by JSON.stringify + send-enqueue)
};
```

### 8c. Stamp + measure in `flushBatches`

**Anchor** — the **post-S7** `flushBatches` body:

```js
function flushBatches() {
  lastFlushTime = Date.now();
  for (const [client, buf] of clientBatchBuffers) {
    if (buf.size === 0) continue;
    const updates = Object.fromEntries(buf);
    // QS-1: only clear the buffer when the flush is actually sent. A dropped
    // flush (backpressured client) leaves the buffer intact; its symbols are a
    // subset of the next flush, which delivers the latest values. Buffer growth
    // is bounded separately by admitSymbol() in bufferPriceForClient.
    if (sendBounded(client, { type: "batch", updates }, relayMetrics)) {
      buf.clear();
    }
  }
  flushDepthAndTapeBatches();
}
```

**Replace the whole function with:**

```js
function flushBatches() {
  const flushStart = performance.now();
  const relayTs = Date.now(); // QS-3: one flush clock shared across this cycle's messages
  lastFlushTime = relayTs;
  let symbolSends = 0;
  let clientsFlushed = 0;
  for (const [client, buf] of clientBatchBuffers) {
    if (buf.size === 0) continue;
    const updates = Object.fromEntries(buf);
    const size = buf.size;
    // QS-1: only clear the buffer when the flush is actually sent. A dropped
    // flush (backpressured client) leaves the buffer intact; its symbols are a
    // subset of the next flush, which delivers the latest values. QS-3: stampFlush
    // adds seq/relay_ts to the payload; a dropped flush consumes a seq value that
    // is never sent (a harmless gap the client's `seq <= lastSeq` check tolerates).
    if (
      sendBounded(
        client,
        stampFlush({ type: "batch", updates }, relayTs),
        relayMetrics,
      )
    ) {
      buf.clear();
      symbolSends += size;
      clientsFlushed += 1;
    }
  }
  relayMetrics.lastFlushSymbolSends = symbolSends;
  relayMetrics.lastFlushClients = clientsFlushed;
  flushDepthAndTapeBatches(relayTs);
  relayMetrics.lastFlushMs = performance.now() - flushStart;
}
```

### 8d. Stamp in `flushDepthAndTapeBatches`

**Anchor** — the **post-S7** `flushDepthAndTapeBatches` body:

```js
function flushDepthAndTapeBatches() {
  for (const [client, buf] of clientDepthBuffers) {
    if (buf.size === 0) continue;
    const updates = Object.fromEntries(buf);
    if (sendBounded(client, { type: "depth-batch", updates }, relayMetrics)) {
      buf.clear();
    }
  }
  for (const [client, buf] of clientTapeBuffers) {
    if (buf.size === 0) continue;
    const updates = Object.fromEntries(buf);
    if (sendBounded(client, { type: "tape-batch", updates }, relayMetrics)) {
      buf.clear();
    }
  }
}
```

**Replace the whole function with:**

```js
function flushDepthAndTapeBatches(relayTs = Date.now()) {
  for (const [client, buf] of clientDepthBuffers) {
    if (buf.size === 0) continue;
    const updates = Object.fromEntries(buf);
    if (
      sendBounded(
        client,
        stampFlush({ type: "depth-batch", updates }, relayTs),
        relayMetrics,
      )
    ) {
      buf.clear();
    }
  }
  for (const [client, buf] of clientTapeBuffers) {
    if (buf.size === 0) continue;
    const updates = Object.fromEntries(buf);
    if (
      sendBounded(
        client,
        stampFlush({ type: "tape-batch", updates }, relayTs),
        relayMetrics,
      )
    ) {
      buf.clear();
    }
  }
}
```

(The `relayTs = Date.now()` default keeps the function correct if ever called without the arg; the only caller — `flushBatches` — always passes it.)

### 8e. Add the four fields to `/status`

**Anchor** — the **post-S7** `/status` body (the two S7 total lines are unique):

```js
        dropped_flushes_total: relayMetrics.droppedFlushes,
        dropped_symbols_total: relayMetrics.droppedSymbols,
```

**Replace those two lines with:**

```js
        dropped_flushes_total: relayMetrics.droppedFlushes,
        dropped_symbols_total: relayMetrics.droppedSymbols,
        last_flush_symbol_sends: relayMetrics.lastFlushSymbolSends,
        last_flush_clients: relayMetrics.lastFlushClients,
        last_flush_ms: Math.round(relayMetrics.lastFlushMs * 100) / 100,
        // Global age of the FRESHEST IB tick across all subscriptions (not
        // per-symbol). null until the first tick — lastTickTimestamp is
        // initialized to Date.now() at boot, so without the flag this would
        // read as relay uptime, not quote age.
        quote_age_ms: hasReceivedTick ? Math.max(0, now - lastTickTimestamp) : null,
```

**Additionally** add a module-scope flag next to `lastTickTimestamp` (`:642`) and set it at the
two update sites (`:1538`, `:1561` — anchor on the `lastTickTimestamp = Date.now();` lines):

```js
let hasReceivedTick = false; // quote_age_ms is meaningless before the first IB tick
```

and at each of the two update sites add `hasReceivedTick = true;` on the following line.

`now` is the same `const now = Date.now()` already computed at the top of the `/status` handler (used by `subscriberRegistry.snapshot(now)`); `lastTickTimestamp` is the module global at `:642`. **Verify** `now` is in scope at this anchor:

```bash
grep -n "const now = Date.now" scripts/infra/ib_realtime/ib_realtime_server.js
```

If `now` is NOT defined in the `/status` handler scope (S7 renamed it), substitute `Date.now()` inline for `now` in the `quote_age_ms` line and report the deviation.

---

## 9. Step 3 — Relay spawn integration test for the new `/status` fields

Create `scripts/infra/ib_realtime/__tests__/status_metrics.test.mjs`. It spawns the **real** relay against a **dead** IB port (reusing S7's `freePort` + spawn + listen-line-readiness + `IB_REALTIME_RUNTIME_FILE` redirection pattern) and asserts the four new numeric `/status` fields are present. (Drop/flush _behavior_ needs a live tick source and is covered by `messageSeq.test.mjs` + the frontend tests + the S7 manual soak — this test asserts the wire shape only.)

```js
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import http from "node:http";
import net from "node:net";
import os from "node:os";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const RELAY = resolve(__dirname, "..", "ib_realtime_server.js");

// Allocate a free port ourselves (the relay logs cli.port verbatim, so we cannot
// use --port 0 and read it back). Same pattern as S7's relay_integration test.
function freePort() {
  return new Promise((res, rej) => {
    const srv = net.createServer();
    srv.listen(0, "127.0.0.1", () => {
      const p = srv.address().port;
      srv.close(() => res(p));
    });
    srv.on("error", rej);
  });
}

let proc;
let port;

before(async () => {
  port = await freePort();
  const env = {
    ...process.env,
    // Redirect the runtime port-discovery file so we never clobber a dev relay's.
    IB_REALTIME_RUNTIME_FILE: join(
      os.tmpdir(),
      `xenon-ib-realtime-p12-${process.pid}.json`,
    ),
  };
  // Dead IB port 65500 → async ECONNREFUSED → relay keeps serving HTTP/WS.
  proc = spawn(
    process.execPath,
    [
      RELAY,
      "--port",
      String(port),
      "--ib-host",
      "127.0.0.1",
      "--ib-port",
      "65500",
    ],
    { env, stdio: ["ignore", "pipe", "pipe"] },
  );
  await new Promise((res, rej) => {
    const to = setTimeout(
      () => rej(new Error("relay did not report listening in 15s")),
      15000,
    );
    let acc = "";
    const onData = (chunk) => {
      acc += chunk.toString();
      if (acc.includes(`WebSocket server listening on 0.0.0.0:${port}`)) {
        clearTimeout(to);
        proc.stdout.off("data", onData);
        res();
      }
    };
    proc.stdout.on("data", onData);
    proc.once("exit", (code) =>
      rej(new Error("relay exited early, code " + code)),
    );
  });
});

after(() => {
  if (proc && !proc.killed) proc.kill("SIGKILL");
});

function getStatus() {
  return new Promise((res, rej) => {
    http
      .get({ host: "127.0.0.1", port, path: "/status" }, (r) => {
        let d = "";
        r.on("data", (c) => (d += c));
        r.on("end", () => res(JSON.parse(d)));
      })
      .on("error", rej);
  });
}

test("/status exposes the P1.2 flush + quote-age metrics as numbers", async () => {
  const body = await getStatus();
  assert.equal(typeof body.last_flush_symbol_sends, "number");
  assert.equal(typeof body.last_flush_clients, "number");
  assert.equal(typeof body.last_flush_ms, "number");
  // Dead IB port → no tick has ever arrived → quote_age_ms must be null
  // (NOT relay uptime; see the hasReceivedTick flag).
  assert.equal(body.quote_age_ms, null);
  // S7 fields still present (no regression of the sibling metric block).
  assert.equal(typeof body.dropped_flushes_total, "number");
  assert.equal(typeof body.max_client_buffered_bytes, "number");
});
```

Run:

```bash
node --test scripts/infra/ib_realtime/__tests__/status_metrics.test.mjs
```

**Expected: 1 test passes, `# fail 0`.** If the relay exits early, see Tripwire 5.

---

## 10. Step 4 — Frontend protocol types (additive)

Edit `web/lib/pricesProtocol.ts`. Add optional `seq`/`relay_ts` to the three batched message types **only**. Optional fields keep old messages (no seq) valid and old code (ignores seq) working.

**Anchor** — `WSBatchMessage`:

```ts
export type WSBatchMessage = {
  type: "batch";
  updates: Record<string, PriceData>;
};
```

**Replace with:**

```ts
export type WSBatchMessage = {
  type: "batch";
  updates: Record<string, PriceData>;
  /** QS-3: strictly-increasing per relay lifetime. Absent from pre-P1.2 relays. */
  seq?: number;
  /** QS-3: relay flush clock (Date.now at flush) — receive-side, not exchange time. */
  relay_ts?: number;
};
```

**Anchor** — `WSDepthBatchMessage` (keep the existing `⚠️ DEPTH-KEY` doc comment above it intact):

```ts
export type WSDepthBatchMessage = {
  type: "depth-batch";
  updates: Record<string, DepthBook>;
};
```

**Replace with:**

```ts
export type WSDepthBatchMessage = {
  type: "depth-batch";
  updates: Record<string, DepthBook>;
  seq?: number;
  relay_ts?: number;
};
```

**Anchor** — `WSTapeBatchMessage`:

```ts
export type WSTapeBatchMessage = {
  type: "tape-batch";
  updates: Record<string, Trade[]>;
};
```

**Replace with:**

```ts
export type WSTapeBatchMessage = {
  type: "tape-batch";
  updates: Record<string, Trade[]>;
  seq?: number;
  relay_ts?: number;
};
```

---

## 11. Step 5 — Frontend pure helpers + hook wiring in `usePrices.ts`

### 11a. Export two pure helpers

**Anchor** — the exported reducer signature (insert the helpers immediately **before** it):

```ts
export function applyDepthMessage(
```

**Insert before that line:**

```ts
/**
 * QS-3 staleness gate. Drop a batched message whose `seq` is not strictly
 * greater than the last accepted `seq` on this connection. Messages with no
 * `seq` (pre-P1.2 relay, or non-batched types) are NEVER dropped — this keeps
 * the client backward compatible with an old relay. Gaps are tolerated: a
 * backpressure-dropped flush upstream leaves a hole in the sequence, and `<=`
 * accepts the next higher value regardless of gap size.
 */
export function shouldDropStaleSeq(
  seq: number | undefined,
  lastSeq: number,
): boolean {
  return typeof seq === "number" && seq <= lastSeq;
}

/**
 * QS-3 quote age (dev telemetry). Milliseconds since the relay stamped the last
 * batch it flushed to us (`relay_ts`) — end-to-end relay→client transport lag.
 * Returns null before any stamped batch has arrived. Clamped at 0.
 */
export function computeQuoteAgeMs(
  lastRelayTs: number | null,
  now: number,
): number | null {
  if (lastRelayTs == null) return null;
  return Math.max(0, now - lastRelayTs);
}
```

### 11b. Add the two refs

**Anchor** — the last-send hash ref (unique):

```ts
const lastSentHashRef = useRef("");
```

**Insert immediately after it:**

```ts
// QS-3: highest batch `seq` accepted on the current socket. Reset to -1 on
// every onopen so a relay restart (its seq resets to 0) is not mistaken for a
// flood of stale messages. lastRelayTsRef feeds the dev-facing quote age.
const lastSeqRef = useRef(-1);
const lastRelayTsRef = useRef<number | null>(null);
```

### 11c. Reset `lastSeqRef` on every fresh socket

**Anchor** — inside `ws.onopen`, the full-sync reset line (unique):

```ts
// Force full send on new connection
lastSentHashRef.current = "";
```

**Replace with:**

```ts
// Force full send on new connection
lastSentHashRef.current = "";
// QS-3: new socket → forget prior sequence (handles relay restart).
lastSeqRef.current = -1;
```

### 11d. Read `seq`/`relay_ts` once, right after parse

**Anchor** — the parse line inside the price `ws.onmessage` (the FIRST occurrence, in the main handler — there is a second identical parse in the snapshot handler at `:930`; edit only the one immediately followed by `switch (message.type) {`):

```ts
          const message = JSON.parse(event.data as string) as WSMessage;

          switch (message.type) {
```

**Replace with:**

```ts
          const message = JSON.parse(event.data as string) as WSMessage;
          // QS-3: seq/relay_ts ride only the batched types; read permissively so
          // the union access does not need every member to declare them.
          const msgSeq = (message as { seq?: number }).seq;
          const msgRelayTs = (message as { relay_ts?: number }).relay_ts;

          switch (message.type) {
```

### 11e. Gate the `batch` case

**Anchor** — the `case "batch"` opening:

```ts
            case "batch": {
              const { updates } = message;
              setPrices((prev) => ({ ...prev, ...updates }));
```

**Replace with:**

```ts
            case "batch": {
              if (shouldDropStaleSeq(msgSeq, lastSeqRef.current)) {
                wsLog("drop-stale-batch", { seq: msgSeq, last: lastSeqRef.current });
                break;
              }
              if (typeof msgSeq === "number") lastSeqRef.current = msgSeq;
              if (typeof msgRelayTs === "number") lastRelayTsRef.current = msgRelayTs;
              const { updates } = message;
              setPrices((prev) => ({ ...prev, ...updates }));
```

### 11f. Gate the depth group + tape case

**Anchor** — the grouped depth case opening:

```ts
            case "depth":
            case "depth-batch":
            case "depth-unavailable": {
              setDepths(
```

**Replace with:**

```ts
            case "depth":
            case "depth-batch":
            case "depth-unavailable": {
              if (shouldDropStaleSeq(msgSeq, lastSeqRef.current)) break;
              if (typeof msgSeq === "number") lastSeqRef.current = msgSeq;
              if (typeof msgRelayTs === "number") lastRelayTsRef.current = msgRelayTs;
              setDepths(
```

**Anchor** — the tape case opening:

```ts
            case "tape-batch": {
              setTape(
```

**Replace with:**

```ts
            case "tape-batch": {
              if (shouldDropStaleSeq(msgSeq, lastSeqRef.current)) break;
              if (typeof msgSeq === "number") lastSeqRef.current = msgSeq;
              if (typeof msgRelayTs === "number") lastRelayTsRef.current = msgRelayTs;
              setTape(
```

(`depth`/`depth-unavailable` carry no `seq` → `shouldDropStaleSeq` returns false → never dropped. Only `depth-batch`/`tape-batch` actually gate.)

### 11g. Expose `getQuoteAgeMs()` on the return

**Anchor** — the return object opening + first fields:

```ts
  return {
    prices,
    fundamentals,
```

**Replace with:**

```ts
  // QS-3: dev-facing quote age (relay flush → now). Ref-backed getter, so reading
  // it forces no re-render; a dev overlay / React DevTools / console can poll it.
  const getQuoteAgeMs = useCallback(
    () => computeQuoteAgeMs(lastRelayTsRef.current, Date.now()),
    [],
  );

  return {
    prices,
    fundamentals,
    getQuoteAgeMs,
```

`useCallback` is already imported (`import { useCallback, useEffect, useMemo, useRef, useState } from "react";` at `usePrices.ts:3` — verified). Adding a field to the return is additive; existing destructuring consumers are unaffected.

**11g-2. Update the explicit return type.** `usePrices` declares an explicit return type
(`UsePricesReturn`, defined near `usePrices.ts:81`, used at the function signature
~`usePrices.ts:314`). Without this, `tsc --noEmit` FAILS on the new field. Anchor on the last
member of `UsePricesReturn` (the `getSnapshot` entry) and add after it:

```ts
/** QS-3 dev-facing: ms since the last stamped relay flush, or null before any. */
getQuoteAgeMs: () => number | null;
```

---

## 12. Step 6 — Frontend Vitest (TDD)

Create `web/tests/prices-seq.test.ts`:

```ts
/**
 * @vitest-environment jsdom
 *
 * QS-3: seq-drop staleness gate + quote-age helper + backward compatibility.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import {
  usePrices,
  shouldDropStaleSeq,
  computeQuoteAgeMs,
} from "../lib/usePrices";
import type { PriceData } from "../lib/pricesProtocol";

// ── pure helpers ──────────────────────────────────────────────
describe("shouldDropStaleSeq", () => {
  it("accepts a strictly higher seq", () => {
    expect(shouldDropStaleSeq(5, 4)).toBe(false);
  });
  it("drops an equal seq (duplicate)", () => {
    expect(shouldDropStaleSeq(4, 4)).toBe(true);
  });
  it("drops a lower seq (reordered/stale)", () => {
    expect(shouldDropStaleSeq(3, 4)).toBe(true);
  });
  it("tolerates gaps (accepts a jump)", () => {
    expect(shouldDropStaleSeq(99, 4)).toBe(false);
  });
  it("NEVER drops when seq is undefined (old relay / non-batch msg)", () => {
    expect(shouldDropStaleSeq(undefined, 4)).toBe(false);
    expect(shouldDropStaleSeq(undefined, -1)).toBe(false);
  });
});

describe("computeQuoteAgeMs", () => {
  it("returns null before any stamped batch", () => {
    expect(computeQuoteAgeMs(null, 1000)).toBe(null);
  });
  it("returns now - relayTs", () => {
    expect(computeQuoteAgeMs(900, 1000)).toBe(100);
  });
  it("clamps negative clock skew to 0", () => {
    expect(computeQuoteAgeMs(1200, 1000)).toBe(0);
  });
});

// ── hook-level drop behavior ─────────────────────────────────
class MockWebSocket {
  static CONNECTING = 0 as const;
  static OPEN = 1 as const;
  static CLOSING = 2 as const;
  static CLOSED = 3 as const;
  readyState: number = MockWebSocket.CONNECTING;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  sent: string[] = [];
  url: string;
  constructor(url: string) {
    this.url = url;
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    if (this.readyState === MockWebSocket.CLOSED) return;
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.(new Event("close"));
  }
  simulateOpen() {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.(new Event("open"));
  }
  simulateMessage(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) });
  }
}

let wsInstances: MockWebSocket[] = [];
const latestWs = () => wsInstances[wsInstances.length - 1];

function makePriceData(symbol: string, last: number): PriceData {
  return {
    symbol,
    last,
    lastIsCalculated: false,
    bid: last - 0.01,
    ask: last + 0.01,
    bidSize: 100,
    askSize: 100,
    volume: 1000,
    high: last + 1,
    low: last - 1,
    open: last,
    close: last - 0.5,
    week52High: null,
    week52Low: null,
    avgVolume: null,
    delta: null,
    gamma: null,
    theta: null,
    vega: null,
    impliedVol: null,
    undPrice: null,
    timestamp: new Date().toISOString(),
  };
}

beforeEach(() => {
  wsInstances = [];
  vi.stubGlobal(
    "WebSocket",
    class extends MockWebSocket {
      constructor(url: string) {
        super(url);
        wsInstances.push(this);
      }
    },
  );
});
afterEach(() => {
  vi.unstubAllGlobals();
});

// Real ticker + real frozen price (AAPL ~ 213.55 close 2026-07-02).
describe("usePrices seq-drop", () => {
  it("applies a fresh batch, then drops a lower-seq (stale) batch", async () => {
    const { result } = renderHook(() =>
      usePrices({ symbols: ["AAPL"], enabled: true }),
    );
    await act(async () => {});
    const ws = latestWs();
    act(() => ws.simulateOpen());

    act(() =>
      ws.simulateMessage({
        type: "batch",
        seq: 10,
        relay_ts: 1_720_000_000_000,
        updates: { AAPL: makePriceData("AAPL", 213.55) },
      }),
    );
    expect(result.current.prices.AAPL.last).toBe(213.55);

    // Stale (lower seq) → dropped: price must NOT change.
    act(() =>
      ws.simulateMessage({
        type: "batch",
        seq: 9,
        relay_ts: 1_720_000_000_001,
        updates: { AAPL: makePriceData("AAPL", 999.99) },
      }),
    );
    expect(result.current.prices.AAPL.last).toBe(213.55);

    // Higher seq → applied.
    act(() =>
      ws.simulateMessage({
        type: "batch",
        seq: 11,
        relay_ts: 1_720_000_000_002,
        updates: { AAPL: makePriceData("AAPL", 214.1) },
      }),
    );
    expect(result.current.prices.AAPL.last).toBe(214.1);
  });

  it("BACKWARD COMPAT: batches with NO seq are always applied (old relay)", async () => {
    const { result } = renderHook(() =>
      usePrices({ symbols: ["TSLA"], enabled: true }),
    );
    await act(async () => {});
    const ws = latestWs();
    act(() => ws.simulateOpen());

    act(() =>
      ws.simulateMessage({
        type: "batch",
        updates: { TSLA: makePriceData("TSLA", 315.0) },
      }),
    );
    act(() =>
      ws.simulateMessage({
        type: "batch",
        updates: { TSLA: makePriceData("TSLA", 316.25) },
      }),
    );
    // No seq on either → neither is dropped; latest wins.
    expect(result.current.prices.TSLA.last).toBe(316.25);
  });

  it("exposes quote age via getQuoteAgeMs after a stamped batch", async () => {
    const { result } = renderHook(() =>
      usePrices({ symbols: ["AAPL"], enabled: true }),
    );
    await act(async () => {});
    const ws = latestWs();
    act(() => ws.simulateOpen());
    expect(result.current.getQuoteAgeMs()).toBe(null); // nothing stamped yet

    act(() =>
      ws.simulateMessage({
        type: "batch",
        seq: 1,
        relay_ts: Date.now() - 50,
        updates: { AAPL: makePriceData("AAPL", 213.55) },
      }),
    );
    const age = result.current.getQuoteAgeMs();
    expect(age).not.toBe(null);
    expect(age as number).toBeGreaterThanOrEqual(0);
  });
});
```

Run — helpers/hook exist after Step 5, so this should pass:

```bash
cd web && npm test -- prices-seq
```

**Expected: all cases green.** (If you write this file BEFORE Step 5 for strict RED-first, the import of `shouldDropStaleSeq`/`getQuoteAgeMs` fails → that is the expected RED. Then apply Step 5 → GREEN.)

---

## 13. Verification matrix

Run every item; literal expected outcomes given.

### 13.1 Relay unit (node:test)

```bash
node --test scripts/infra/ib_realtime/__tests__/messageSeq.test.mjs
```

**Expected:** 5 pass, `# fail 0`.

### 13.2 Relay spawn integration (node:test)

```bash
npm install --no-audit --no-fund --legacy-peer-deps   # ensure root `ws` present (S7 added the dep usage)
node --test scripts/infra/ib_realtime/__tests__/status_metrics.test.mjs
```

**Expected:** 1 pass, `# fail 0`. Asserts `last_flush_symbol_sends`, `last_flush_clients`, `last_flush_ms`, `quote_age_ms` are numbers, `quote_age_ms >= 0`, and the S7 fields survive.

### 13.3 All relay tests via the S7 lane (no regression of sibling `.mjs`)

```bash
npm run test:relay
```

**Expected:** every `.mjs` in `__tests__/` passes — the two new ones (`messageSeq`, `status_metrics`) plus S7's (`sendBounded`, `originAllowlist`, `relay_integration`) plus pre-existing (`normalize`, `ib_contracts`). `# fail 0` overall.

### 13.4 Relay syntax sanity

```bash
node --check scripts/infra/ib_realtime/ib_realtime_server.js
node --check scripts/infra/ib_realtime/messageSeq.js
```

**Expected:** exit 0, no output for both.

### 13.5 Frontend Vitest

```bash
cd web && npm test -- prices-seq
cd web && npm test -- batched-prices        # pre-existing batch handling still green
cd web && npm test -- use-prices-ws-stability
cd web && npm test -- usePrices.depth        # depth-batch handling unaffected by seq gate
cd web && npm test -- pricesProtocol         # protocol type test still green
```

**Expected:** all suites pass. `prices-seq` proves: fresh→applied, stale(lower/equal)→dropped, gap→applied, **no-seq→always applied (backward compat)**, quote-age getter works.

### 13.6 Frontend typecheck + lint (web touched)

```bash
cd web && npx tsc --noEmit
cd web && npm run lint
```

**Expected:** exit 0, no errors. (The permissive `(message as { seq?: number }).seq` read is deliberate — the union does not declare seq on every member.)

### 13.7 Prettier parity

```bash
npx prettier --check \
  "scripts/infra/ib_realtime/messageSeq.js" \
  "scripts/infra/ib_realtime/__tests__/messageSeq.test.mjs" \
  "scripts/infra/ib_realtime/__tests__/status_metrics.test.mjs" \
  "scripts/infra/ib_realtime/ib_realtime_server.js" \
  "web/lib/pricesProtocol.ts" \
  "web/lib/usePrices.ts" \
  "web/tests/prices-seq.test.ts"
```

**Expected:** `All matched files use Prettier code style!` Else `npx prettier --write <same files>` then re-check.

### 13.8 Live probe (PAPER only, optional but recommended) — seq strictly increases + `/status` fields

Only if a dev stack is available. Never live.

```bash
scripts/infra/dev.sh paper        # relay binds :8866; open :3200 so the browser subscribes symbols
# 1) /status shows the new fields:
curl -s http://127.0.0.1:8866/status | python3 -m json.tool
#    Expected keys present: last_flush_symbol_sends, last_flush_clients,
#    last_flush_ms, quote_age_ms (all numbers; quote_age_ms small during RTH).
# 2) seq strictly increases on the wire (subscribe + read a few batches):
node -e '
const { WebSocket } = require("ws");
const ws = new WebSocket("ws://127.0.0.1:8866/");
let last = -1, seen = 0;
ws.on("open", () => ws.send(JSON.stringify({ action:"subscribe", symbols:["SPY","QQQ","AAPL"] })));
ws.on("message", (m) => {
  const msg = JSON.parse(m);
  if (msg.type === "batch") {
    if (typeof msg.seq !== "number") { console.error("FAIL: batch missing seq"); process.exit(1); }
    if (msg.seq <= last) { console.error("FAIL: seq not increasing", last, msg.seq); process.exit(1); }
    if (typeof msg.relay_ts !== "number") { console.error("FAIL: batch missing relay_ts"); process.exit(1); }
    last = msg.seq;
    if (++seen >= 5) { console.log("OK: 5 batches, seq strictly increasing, last=", last); process.exit(0); }
  }
});
setTimeout(() => { console.error("timeout: fewer than 5 batches in 30s"); process.exit(1); }, 30000);
'
```

**Expected:** prints `OK: 5 batches, seq strictly increasing`. (Requires RTH ticks; outside RTH there may be no batches — that is not a failure of this PR, only of the probe timing. Note it and rely on 13.1/13.5.)

### 13.9 Not applicable (state explicitly; do NOT run)

- Python pytest / `run_pytest_affected.py`: **N/A** — no Python changed.
- Order-path CI guards (`no_json_fallback_on_order_path.py`, `no_json_write_on_order_path.py`, `order_path_caller_allowlist.py`): **N/A** — quote path, no `data/*.json`, no order code.
- Alembic / `psql`: **N/A** — no schema change.
- Playwright / chrome-cdp E2E: **N/A** — no UI-visible change. `seq`/`relay_ts` are additive transport metadata; the rendered price surface is byte-identical (dropped stale batches would have been superseded anyway). The dev getter is telemetry, not a visible element. (13.8 is the closest behavioral check; it is PAPER-only.)
- CI YAML edits: **N/A** — `test:relay` glob (S7) picks up new `.mjs`; the vitest job auto-discovers `web/tests/*.test.ts`.

---

## 14. Tripwires / abort criteria — STOP and report if:

1. **S7 not merged** — Step 0 shows no `relayMetrics`, no `test:relay`, or missing `sendBounded.js`. This PR is blocked on S7; do not rebuild S7's pieces. Stop.
2. **QS-3 already partly present** — Step 0's `relay_ts`/`stampFlush`/`lastSeqRef` grep prints anything. Anchors are stale. Stop.
3. **Any anchor snippet not found verbatim.** S7's merged form differs from its plan (e.g. `relayMetrics` field order, `/status` body shape, `flushBatches` body). Do NOT guess — stop and report exactly which anchor is missing so the anchor can be re-derived from the merged S7 code.
4. **A test passes before its code exists** (Step 1 RED, or Step 6 RED-first if you chose to write it first). Impossible unless stale code is present. Stop.
5. **The spawned relay in Step 3 exits early** (`before()` rejects "relay exited early"). Re-run the relay with stderr visible: `node scripts/infra/ib_realtime/ib_realtime_server.js --port 5599 --ib-port 65500` and read the boot error. If it is a genuine synchronous throw on dead IB (S7 proved it is not — S7's own spawn test relies on the relay serving with a dead IB port), mark `status_metrics.test.mjs` `{ skip: true }`, keep the `messageSeq` unit test + frontend tests, and report. The unit + frontend coverage still satisfies the acceptance criteria (seq monotonic; quote age exposed).
6. **`now` is not in scope in the `/status` handler** (Step 8e grep fails). Substitute `Date.now()` inline for the `quote_age_ms` line and note it. If the whole `/status` body has been restructured, stop.
7. **More than these files need editing.** Expected touch set: `messageSeq.js` (new), `__tests__/messageSeq.test.mjs` (new), `__tests__/status_metrics.test.mjs` (new), `ib_realtime_server.js`, `web/lib/pricesProtocol.ts`, `web/lib/usePrices.ts`, `web/tests/prices-seq.test.ts`. If an 8th source file needs changes, stop and report.
8. **`tsc` errors on `message.seq`/`message.relay_ts` union access.** You skipped the permissive-read pattern in Step 11d. Read `seq`/`relay_ts` via the `(message as { seq?: number }).seq` local, not off the narrowed union member. Do not add `seq?` to non-batched message types to paper over it.
9. **Any live-IB requirement.** Never connect to live IB. The spawn test uses a dead port; the optional probe uses `dev.sh paper` (relay :8866) only.

---

## 15. Rollback

- **Code:**
  ```bash
  git checkout master -- scripts/infra/ib_realtime/ib_realtime_server.js \
    web/lib/pricesProtocol.ts web/lib/usePrices.ts
  git rm scripts/infra/ib_realtime/messageSeq.js \
    scripts/infra/ib_realtime/__tests__/messageSeq.test.mjs \
    scripts/infra/ib_realtime/__tests__/status_metrics.test.mjs \
    web/tests/prices-seq.test.ts
  ```
  Or discard the branch: `git checkout master && git branch -D feat/relay-seq-status-metrics`.
- **No migration** — nothing to down-revision.
- **No runtime rollback lever needed:** every change is additive and backward compatible. An old frontend ignores `seq`/`relay_ts`; a new frontend treats a seq-less relay as "never stale". A prod relay carrying the change alongside an un-updated web bundle is safe (the bundle just doesn't read the new fields). No env flag required.

---

## 16. Incident-history row

**N/A** — this is the quote relay (QS-3), not the order path. `docs/reference/order-path-incident-history.md` covers order placement/cancel/modify only. No row to append.

---

## 17. PR description checklist (for the executor)

- Findings: QS-3 (seq/relay_ts protocol metadata) + P1.2 `/status` observability (flush size, amplification via `last_flush_symbol_sends`×`last_flush_clients`, flush duration, quote age).
- Prerequisite: builds on S7 (`sendBounded`/`relayMetrics`/`test:relay` lane) — merged first.
- Deviations from the roadmap/sketch, stated plainly: (a) `updates` kept, not sketch §6's `prices`; (b) "stringify ms" delivered as `last_flush_ms` wall time to avoid double-stringify / keep the `sendBounded` seam; (c) one global counter incremented per message at flush-construction (gaps tolerated), `sendBounded` unchanged.
- Explicitly out of scope: QS-10 loop unification, P1.3 profiler measurement, `sendMessage` single-message stamping.
- Backward compatibility: additive optional fields; old clients unaffected (test `prices-seq` case "no seq → always applied").
- No AI attribution trailer in the commit message (global policy). Open a PR; do not push to master; wait for green CI before merge.

```

```
