# P4.3 — Extract batched-channel + subscription-registry modules; CI relay integration lane (QS-2, QS-10)

- **Date:** 2026-07-05
- **Branch:** `feat/p4-3-batched-channel-module`
- **Findings:** QS-2 (High) — the 2,256-line relay has ZERO CI-run behavioral tests; its
  `.mjs` unit tests never run in CI. QS-10 (Low) — the depth/tape subsystem re-implements the
  entire L1 batching/subscriber/cleanup pattern in the same file.
- **Goal:** Extract the generic per-client batched-channel core (shared by L1 prices and
  depth/tape) into a unit-tested module, and make `node --test` on the relay `.mjs` tests
  RUN in the `web-tests` CI job.
- **Acceptance (roadmap):** relay core logic covered in CI; `.mjs` tests run in the
  web-tests job.

## Re-verify preamble (MANDATORY — executes after S7 + P1.2 reshape the relay)

This plan touches EXACTLY the code S7 and P1.2 change, so re-verification is critical:

- **S7 (QS-1)** replaces `sendMessage(client, {...})` inside `flushBatches` /
  `flushDepthAndTapeBatches` with a bounded `sendBounded(client, payload)` (drop-on-
  `bufferedAmount`, per-client cap) that **returns a boolean**, and the S7 flush clears a
  client's buffer ONLY when `sendBounded` returns `true` (a dropped flush keeps the buffer;
  the next flush delivers the latest LWW state — see the S7 plan's `sendBounded.js` contract
  and its rewritten `flushBatches`). The extracted module MUST honor exactly this contract:
  injected send fn, clear-on-true-only.
- **P1.2 (QS-3)** adds `seq` + `relay_ts` to the batch payload and a `/status` metrics body.
  The extracted `flush()` must build the payload the SAME shape P1.2 established.

Before writing, capture the CURRENT shape at HEAD:

```bash
cd scripts/infra/ib_realtime
grep -n "function flushBatches\|function flushDepthAndTapeBatches\|function bufferPriceForClient\|function bufferDepthForClient\|function sendMessage\|function sendBounded" ib_realtime_server.js
sed -n '/function flushBatches/,/^}/p' ib_realtime_server.js
sed -n '/function flushDepthAndTapeBatches/,/^}/p' ib_realtime_server.js
```

If `sendBounded` exists (S7 merged) → the module's `send` param binds to it. If `seq`/
`relay_ts` appear in the batch object (P1.2 merged) → replicate them in the module's
`flush`. **STOP and reconcile** if the two flush functions have diverged in a way the single
abstraction below cannot express (e.g. depth flush grew per-message transforms) — narrow the
extraction to L1 only and note QS-10 as partially deferred.

## Key facts (verified at HEAD)

- `flushBatches()` (~732) iterates `clientBatchBuffers: Map<client, Map<symbol, PriceData>>`,
  and for each non-empty buffer sends `{ type: "batch", updates: Object.fromEntries(buf) }`,
  then clears, then calls `flushDepthAndTapeBatches()`.
- `flushDepthAndTapeBatches()` (~1295) does the identical loop twice over
  `clientDepthBuffers` → `{ type: "depth-batch", updates }` and `clientTapeBuffers` →
  `{ type: "tape-batch", updates }`. This is the QS-10 duplication.
- `bufferPriceForClient(client, symbol, data)` (~715) has an adaptive early-flush at
  `BATCH_THRESHOLD = 50` when `Date.now() - lastFlushTime >= BATCH_INTERVAL_MS`.
- `sendMessage(client, payload)` (~759) = `if OPEN: client.send(JSON.stringify(payload))`.
- CI: `web-tests` job in `.github/workflows/ci.yml` (~52-84) has Node 20 + `npm install` and
  runs `npx vitest run ... web/tests`. There is NO `node --test` step. `pyproject.toml:112`
  excludes `scripts/infra/ib_realtime` from pytest (`norecursedirs`) — unrelated to `.mjs`.
- Existing `.mjs` tests: `scripts/infra/ib_realtime/__tests__/{ib_contracts,normalize}.test.mjs`,
  run by `node --test`. `subscriber_registry.js` already exists as an extracted module.

## Non-goals

- Do NOT extract the IB-side subscription manager, LRU budget, or error handling.
- Do NOT build the full fake-IB spawn-relay integration test (07 §7.4-5) in THIS PR — that
  is a large harness; scope it as a follow-up noted in the PR. This PR delivers: the
  batched-channel module + its unit tests + the CI `node --test` lane wiring so the module
  (and existing `.mjs` tests) run in CI. That satisfies the QS-2 acceptance ("`.mjs` tests
  run in the web-tests job") without the 400-line IB shim.
- QS-1/QS-3 send-shape changes belong to S7/P1.2 — this PR only makes the abstraction
  send-fn-injectable so those survive.

## Steps (TDD)

### Step 1 — New pure module `batched_channel.js`

`scripts/infra/ib_realtime/batched_channel.js`:

```js
/**
 * Generic per-client last-write-wins batch buffer. One instance per logical
 * channel (L1 prices, L2 depth, tape). Buffers keyed updates per client and
 * flushes each client's buffer as one message via an injected `send` fn — so
 * the relay's bounded send (QS-1) and seq/relay_ts envelope (QS-3) live at the
 * call site, not baked in here. Pure of IB + WebSocket specifics → unit-testable.
 */
export function createBatchedChannel({ type, thresholdSize = 50 }) {
  const buffers = new Map(); // client -> Map<key, data>

  function buffer(client, key, data) {
    let buf = buffers.get(client);
    if (!buf) {
      buf = new Map();
      buffers.set(client, buf);
    }
    buf.set(key, data);
    return buf.size; // caller decides on early flush using thresholdSize
  }

  // send: (client, payload) => boolean — MUST return true iff the payload was
  // actually handed to client.send(). S7 contract: a client's buffer clears
  // ONLY on a successful send; a dropped flush (backpressured client) keeps the
  // buffer, and the next flush delivers a superset with the latest LWW values.
  function flush(send, envelope = () => ({})) {
    for (const [client, buf] of buffers) {
      if (buf.size === 0) continue;
      const updates = Object.fromEntries(buf);
      if (send(client, { type, updates, ...envelope() }) === true) {
        buf.clear();
      }
    }
  }

  function remove(client) {
    buffers.delete(client);
  }
  function size(client) {
    return buffers.get(client)?.size ?? 0;
  }

  return { buffer, flush, remove, size, thresholdSize, _buffers: buffers };
}
```

### Step 2 — Unit tests `__tests__/batched_channel.test.mjs`

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { createBatchedChannel } from "../batched_channel.js";

const sendOk = (sent) => (client, payload) => {
  sent.push(payload);
  return true; // successful delivery → module may clear the buffer
};

test("last-write-wins per key, flush sends one message per client then clears", () => {
  const ch = createBatchedChannel({ type: "batch" });
  const c = {};
  ch.buffer(c, "AAPL", { last: 1 });
  ch.buffer(c, "AAPL", { last: 2 }); // overwrites
  ch.buffer(c, "TSLA", { last: 9 });
  const sent = [];
  ch.flush(sendOk(sent));
  assert.deepEqual(sent, [
    { type: "batch", updates: { AAPL: { last: 2 }, TSLA: { last: 9 } } },
  ]);
  ch.flush(sendOk(sent)); // buffer cleared on true return → no 2nd send
  assert.equal(sent.length, 1);
});

test("envelope injects seq/relay_ts (QS-3 call-site metadata)", () => {
  const ch = createBatchedChannel({ type: "depth-batch" });
  ch.buffer({}, "AAPL", { bid: 1 });
  const sent = [];
  ch.flush(sendOk(sent), () => ({ seq: 7, relay_ts: 123 }));
  assert.equal(sent[0].seq, 7);
  assert.equal(sent[0].relay_ts, 123);
  assert.equal(sent[0].type, "depth-batch");
});

test("sendBounded returns false → buffer RETAINED, next successful flush delivers latest", () => {
  const ch = createBatchedChannel({ type: "batch" });
  const c = { over: true }; // simulate a backpressured client
  ch.buffer(c, "AAPL", { last: 1 });
  let drops = 0;
  ch.flush(() => {
    drops++;
    return false; // S7: dropped flush must NOT clear the buffer
  });
  assert.equal(drops, 1);
  assert.equal(ch.size(c), 1); // buffer retained
  ch.buffer(c, "AAPL", { last: 2 }); // LWW keeps only the latest
  const sent = [];
  ch.flush(sendOk(sent)); // client drained → delivery succeeds
  assert.deepEqual(sent, [{ type: "batch", updates: { AAPL: { last: 2 } } }]);
  assert.equal(ch.size(c), 0); // cleared only after the true return
});

test("a send returning undefined (non-boolean) never clears — contract is strict", () => {
  const ch = createBatchedChannel({ type: "batch" });
  const c = {};
  ch.buffer(c, "AAPL", { last: 1 });
  ch.flush(() => undefined); // e.g. raw sendMessage passed without the wrapper
  assert.equal(ch.size(c), 1); // retained — forces callers to wrap correctly
});
```

### Step 3 — Rewire the relay to the module (behavior-preserving)

Replace the three ad-hoc buffer maps + flush functions with three channel instances. Anchor
on `const clientBatchBuffers = new Map();` and the two flush functions.

```js
import { createBatchedChannel } from "./batched_channel.js";

const l1Channel = createBatchedChannel({ type: "batch" });
const depthChannel = createBatchedChannel({ type: "depth-batch" });
const tapeChannel = createBatchedChannel({ type: "tape-batch" });
```

- `bufferPriceForClient(client, symbol, data)` → `const n = l1Channel.buffer(client, symbol,
data);` then keep the adaptive early-flush guard using `n >= BATCH_THRESHOLD`.
- `flushBatches()` → `l1Channel.flush(SEND, ENVELOPE); flushDepthAndTapeBatches();` where
  `SEND` is `sendBounded` if S7 merged (it already returns the boolean the module requires),
  else the wrapper `(c, p) => { sendMessage(c, p); return true; }` (pre-S7 `sendMessage`
  returns `undefined`, which the strict `=== true` contract treats as "not delivered" — the
  wrapper preserves today's always-clear behavior until S7 lands). `ENVELOPE` is the P1.2
  `() => ({ seq: ++globalSeq, relay_ts: Date.now() })` if merged else `undefined`.
- `flushDepthAndTapeBatches()` → `depthChannel.flush(SEND); tapeChannel.flush(SEND);`
- `removeBatchBuffer(client)` and depth/tape cleanup → `l1Channel.remove(client)` etc. Verify
  `disconnectClient` / `disconnectClientFromDepth` call these.

Keep message field names byte-identical (`type`, `updates`) — the frontend `usePrices`
`case "batch"/"depth-batch"/"tape-batch"` reads `message.updates`. Do not rename.

### Step 4 — Run `.mjs` tests in CI (the QS-2 acceptance)

Add a step to the `web-tests` job in `.github/workflows/ci.yml` AFTER the vitest step (Node
is already installed there):

```yaml
- name: Run relay .mjs unit tests
  run: node --test scripts/infra/ib_realtime/__tests__/*.test.mjs
```

`node --test` with an explicit glob is required — a bare directory arg fails to discover
tests. Verified the existing tests use `node:test` + `node:assert/strict`, both stdlib (no
deps), so no install is needed beyond Node 20.

## Verification matrix

| Check                         | Command                                                                            | Expected                                                    |
| ----------------------------- | ---------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| New module tests              | `cd scripts/infra/ib_realtime && node --test __tests__/batched_channel.test.mjs`   | `pass 4 / fail 0`                                           |
| All relay mjs tests (CI glob) | `node --test scripts/infra/ib_realtime/__tests__/*.test.mjs`                       | `fail 0`, ≥3 files discovered                               |
| Relay boots + streams         | `scripts/infra/dev.sh paper`; open :3200; watch a portfolio symbol tick            | prices update in UI (batch path intact)                     |
| Depth/tape still flush        | subscribe-depth on one symbol (Book tab); observe ladder                           | ladder + tape render (channels intact)                      |
| No frontend contract change   | `cd web && npm test -- usePrices`                                                  | pass (batch/depth-batch/tape-batch still `message.updates`) |
| CI yaml valid                 | `uv run python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"` | exit 0                                                      |
| Web typecheck                 | `cd web && npx tsc --noEmit`                                                       | exit 0 (no frontend edit, sanity)                           |

## Tripwires / abort

- STOP if S7/P1.2 have NOT merged and you cannot determine `SEND`/`ENVELOPE` — do the
  extraction with the `(c, p) => { sendMessage(c, p); return true; }` wrapper as `SEND` and
  `ENVELOPE = undefined` (current behavior) and leave a
  `// TODO(S7/P1.2): inject bounded send + seq envelope` comment. Do not invent the bound.
- STOP if the depth/tape flush at HEAD does per-message transforms the generic `flush` can't
  express — extract L1 only, mark QS-10 partial.
- STOP if `node --test` discovers 0 tests in CI (glob/pathing) — the acceptance is unmet.
- No live IB. No orders. Behavior-preserving refactor — if any UI price/ladder stops
  updating on paper, revert immediately.
- File ceiling: `batched_channel.js` + 1 test + relay + `ci.yml` = 4. STOP past that.

## Rollback

Discard the branch. Pure additive module + a behavior-preserving rewire; reverting the relay
hunk restores the inline maps. The CI step is independently revertible.
