# S7 — Relay: bounded per-client delivery + Origin allowlist

- **Date:** 2026-07-05
- **Branch:** `fix/relay-backpressure-origin-allowlist`
- **Findings:** QS-1 (High — backpressure), SEC-2 (Medium — WS Origin), with QS-2 (relay has zero CI tests) partially addressed by shipping the first CI-run relay tests. QS-10 (depth/tape batching duplication) is touched but **not** refactored.
- **Severity:** High (QS-1) + Medium (SEC-2)
- **Goal (one line):** Gate every batched WebSocket flush on `client.bufferedAmount` + cap per-client queued symbols so a slow/stalled client can't grow relay RSS, and reject cross-origin browser WS upgrades via an env-configurable Origin allowlist — while keeping loopback/server-to-server (no-Origin) clients working.

---

## 1. Context (what exists today — verified at HEAD)

File under change: `scripts/infra/ib_realtime/ib_realtime_server.js` (2663 lines; the fable docs call it ~2,256 — see Drift).

**Quote delivery path (QS-1):**

- `sendMessage(client, payload)` (`ib_realtime_server.js:759-767`) is the single send helper. It checks `client.readyState === client.OPEN`, calls `client.send(JSON.stringify(payload))`, and swallows errors. **No `bufferedAmount` check.**
- L1 batch flush loop: `flushBatches()` (`:732-741`) iterates `clientBatchBuffers` (a `Map<client, Map<symbol, PriceData>>`, `:709`), builds `updates = Object.fromEntries(buf)`, **clears the buffer**, then `sendMessage(client, { type: "batch", updates })`. Ends by calling `flushDepthAndTapeBatches()`.
- Per-symbol buffering: `bufferPriceForClient(client, symbol, data)` (`:715-730`) does `buf.set(symbol, data)` with **no cap** on buffer size, then may trigger an early `flushBatches()` when `buf.size >= BATCH_THRESHOLD` (50).
- Depth/tape flush loop (QS-10 duplicate): `flushDepthAndTapeBatches()` (`:1295-1308`) does the same pattern for `clientDepthBuffers` (`type: "depth-batch"`) and `clientTapeBuffers` (`type: "tape-batch"`), also via `sendMessage`. Fed by `bufferDepthForClient(client, key, book)` (`:1274-1281`) and `bufferTapeForClient(client, key, trades)` (`:1283-1290`), both uncapped.
- Both loops share the 100ms cadence via `startBatchFlush()` (`:743-746`, `setInterval(flushBatches, 100)`).

**WS upgrade / auth path (SEC-2):**

- `httpServer.on("upgrade", ...)` (`:442-492`). It computes `isLocalhost = !process.env.CLERK_JWKS_URL || remoteAddr === "127.0.0.1" || "::1" || "::ffff:127.0.0.1"` (`:446-450`). If `isLocalhost`, it **bypasses ticket validation entirely** and accepts the upgrade (`:451-456`). Otherwise it requires a `?ticket=` query param and POSTs it to `TICKET_VALIDATE_URL` (`:458-491`). **No `Origin` header check anywhere.**
- The browser connects over loopback: `web/lib/usePrices.ts:701` / `:913` open `new WebSocket(url)` where `url` resolves to `ws://localhost:PORT` (`web/lib/ibRealtimeWsClient.ts:10`). So the operator's browser is a **loopback** client that carries an `Origin` header. A malicious cross-origin page in that same browser also connects over loopback and is currently admitted by the `isLocalhost` bypass — this is exactly the SEC-2 hole.
- Server-to-server clients send **no** `Origin` header: the Python probe uses `websockets.connect(...)` with no `origin=` (`scripts/infra/ib_realtime/test_ib_realtime.py:149`); Node `ws` clients omit Origin unless explicitly set. These must stay working.

**Status endpoint (verified):** `GET /status` handler at `:406-435` returns JSON:

```json
{ "ib_connected": <bool>, "now_ms": <int>, "ttl_ms": <int>, "subscribers": [...], "anonymous_count": <int> }
```

Access is gated by loopback OR `X-Status-Token: $IB_REALTIME_STATUS_TOKEN` (`:411-422`). We extend the JSON body only.

**Startup ordering (verified):** `httpServer.listen(cli.port, WS_HOST)` at `:632` and `wss.on("connection", ...)` at `:2471` both run **before** `ib.connect()` at `:2534` (and `startBatchFlush()` at `:2535`). The HTTP/WS server is listening and accepting upgrades before IB connects, so the relay is **startable with no IB gateway** (a dead `--ib-port` yields async `ECONNREFUSED` → `scheduleReconnect`, server keeps serving). This is what makes the integration test in Step 9 feasible.

**What the executor does NOT need to understand:** IB tick handling, depth ladder serialization, daily-close backfill, subscriber TTL registry, symbol subscription lifecycle. Touch only the send/flush/buffer helpers, the upgrade handler, and the `/status` body.

---

## 2. Drift from review

1. **File length:** fable docs cite `ib_realtime_server.js` as ~2,256 lines and reference bare line numbers (e.g. `:440-453`, `:756-764`, `:712-751`, `:1231-1265`). At HEAD the file is **2663 lines**. All line numbers in the fable docs have drifted. This plan anchors every edit to a **function name + unique snippet**, not the fable line numbers. Verified real locations: upgrade handler `:442-492`, `sendMessage` `:759-767`, `flushBatches` `:732-741`, `bufferPriceForClient` `:715-730`, `flushDepthAndTapeBatches` `:1295-1308`, `/status` `:406-435`.

2. **SEC-2 gating dimension corrected (important):** the brief says "Origin allowlist check … for **NON-loopback** browser connections." But the SEC-2 finding itself describes the threat as _"Cross-origin page on the **operator's machine**"_ — and the operator's browser reaches the relay over **loopback** (`ws://localhost:PORT`, verified above). Gating only non-loopback connections would therefore **not close the finding** — the malicious page is loopback. The correct, testable dimension is **presence of an `Origin` header**, not the peer address:
   - Browsers **always** send `Origin` on a WS upgrade (even to localhost).
   - Server-to-server clients (Python probe, Node `ws`, healthchecks) send **no** `Origin`.
     So this plan enforces: _any upgrade carrying an `Origin` header must have that origin in the allowlist, regardless of loopback; upgrades with no `Origin` header are treated as server-to-server and pass through to the existing ticket/loopback logic unchanged._ This both closes SEC-2 and is verifiable in single-host CI over loopback. This is a deliberate, documented deviation from the brief's literal wording.

3. **QS-2:** the relay currently has zero CI-run tests. Full CI harness is P4.3 (non-goal). This plan ships the first CI-run relay tests (unit + spawn integration) as required by the S7 brief, but does not build the full harness.

---

## 3. Goal / Non-goals

**Goal:**

- Add `sendBounded()` gating on `client.bufferedAmount` (cap 512 KB) used by **both** batch flush loops (L1 and depth/tape).
- Add a hard per-client queued-symbol cap (500) at the buffer-insert layer for L1, depth, and tape buffers.
- Add an env-configurable Origin allowlist enforced on WS upgrade for any request carrying an `Origin` header.
- Expose drop counters on `GET /status`.
- Ship the first CI-run relay tests: unit tests for the two extracted pure modules + a spawn-the-real-relay integration test.

**Non-goals (do NOT do these here — one change = one PR):**

- **QS-10** — do NOT merge `flushBatches` and `flushDepthAndTapeBatches` into one generic batched-channel module. Apply the bound to both loops in place. Unifying them is P4.3.
- **QS-3** — do NOT add `seq`/`relay_ts` to messages (that is finding QS-3 / sketch §6, a separate PR).
- **QS-2 full harness** — do NOT add the EventEmitter IB shim / fake-`@stoqey` mock harness. The integration test spawns the real relay against a dead IB port.
- Rate-limiting `/ws-ticket` mint (the second half of SEC-2's remediation column) — **out of scope**; this PR does the Origin allowlist only. State this in the PR description.
- Do NOT change `sendMessage` behavior for control messages (`ping`, `status`, `error`, snapshot replies). Those stay on `sendMessage` (low-volume, unbounded is fine).

---

## 4. Key facts (verified)

| Fact                                                              | Value                                                                                                                       | Verified at                                     |
| ----------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------- |
| Relay file                                                        | `scripts/infra/ib_realtime/ib_realtime_server.js`                                                                           | HEAD, 2663 lines                                |
| ESM module type                                                   | `"type": "module"`                                                                                                          | root `package.json:3`                           |
| ws OPEN check idiom                                               | `client.readyState === client.OPEN`                                                                                         | `ib_realtime_server.js:761`                     |
| L1 buffer map                                                     | `clientBatchBuffers: Map<client, Map<symbol,data>>`                                                                         | `:709`                                          |
| Depth buffer map                                                  | `clientDepthBuffers: Map<client, Map<key,book>>`                                                                            | `:519`                                          |
| Tape buffer map                                                   | `clientTapeBuffers: Map<client, Map<key,trades>>`                                                                           | `:520`                                          |
| Existing sibling modules imported as `./x.js`                     | `normalize.js`, `tape_feed.js`, `depth_book.js`, `line_budget.js`                                                           | `:32-53`                                        |
| Existing `.mjs` unit tests use `node:test` + `node:assert/strict` | yes                                                                                                                         | `__tests__/normalize.test.mjs`                  |
| `.mjs` tests are NOT run in CI today                              | correct (QS-2)                                                                                                              | `.github/workflows/ci.yml` has no `node --test` |
| Upgrade handler                                                   | `httpServer.on("upgrade", async (req, socket, head) => {...})`                                                              | `:442`                                          |
| Loopback bypass condition                                         | `!process.env.CLERK_JWKS_URL \|\| remoteAddr===127.0.0.1/::1/::ffff:127.0.0.1`                                              | `:446-450`                                      |
| Reject-write idiom                                                | `socket.write("HTTP/1.1 401 Unauthorized\r\n\r\n"); socket.destroy();`                                                      | `:462-464`                                      |
| `/status` body                                                    | `{ib_connected, now_ms, ttl_ms, subscribers, anonymous_count}`                                                              | `:426-433`                                      |
| CLI flags                                                         | `--port <n> --ib-host <h> --ib-port <n> --client-id <n> --verbose`                                                          | `parseArgs`, `:77-123`                          |
| WS bind host                                                      | `WS_HOST = "0.0.0.0"`                                                                                                       | `:246`                                          |
| Listen log line                                                   | `` `WebSocket server listening on ${WS_HOST}:${cli.port}` ``                                                                | `:2660`                                         |
| Prod relay port                                                   | 8765 (host-published)                                                                                                       | `docker-compose.yml:146-147`                    |
| Prod web origin base                                              | `http://localhost:3000` published; real external origin is env-driven (`NEXT_PUBLIC_IB_REALTIME_WS_URL`) and **unverified** | `docker-compose.yml:116`, `:101`                |
| Dev ports                                                         | Next 3200 / relay 8866                                                                                                      | CLAUDE.md startup checklist                     |
| Python probe sends no Origin                                      | `websockets.connect` default (no `origin=`)                                                                                 | `test_ib_realtime.py:149`                       |

**New env vars introduced:**

- `IB_REALTIME_ALLOWED_ORIGINS` — comma-separated origin allowlist. Default (when unset): `http://localhost:3200,http://127.0.0.1:3200,http://localhost:3000,http://127.0.0.1:3000`.
- `IB_REALTIME_ORIGIN_ENFORCE` — `"1"` (default) → return 403 for a disallowed Origin; `"0"` → allow but log a warning (audit/rollback lever, so a wrong prod origin can be reverted without a code redeploy). This is the ONE extra knob justified by the fact that the exact prod browser origin is unverified and a wrong allowlist would break live quotes.

---

## 5. Repo invariants this plan must respect

- All Python via `uv run …` (only used in verification here; no Python source changes).
- Never `git push origin master`; branch + PR; **no AI attribution trailers** in commits.
- Tests: real tickers at real frozen prices; **no** `FOO`/round-number placeholders; no network at test runtime. (The integration test spawns a local relay with a **dead** IB port — no external network.)
- Simplicity first: do not refactor the two flush loops into one (QS-10 non-goal).
- This is the **quote path**, not the order path — the order-path CI guards and incident-history do NOT apply (see §11). No `data/*.json` reads/writes are added.

---

## 6. Step 0 — Reproduce the gap (RED baseline)

Before any change, prove the bound and the Origin check are absent.

1. Confirm no `bufferedAmount` anywhere:

   ```bash
   grep -n "bufferedAmount" scripts/infra/ib_realtime/ib_realtime_server.js
   ```

   **Expected: no output (exit 1).** If this prints a line, STOP — the bound already exists (see Tripwires).

2. Confirm no Origin handling on upgrade:
   ```bash
   grep -n "headers.origin\|headers\[.origin" scripts/infra/ib_realtime/ib_realtime_server.js
   ```
   **Expected: no output.** If it prints, STOP — Origin logic already present.

---

## 7. Step 1 — New module `sendBounded.js` (TDD: test first)

### 7a. Write the failing unit test

Create `scripts/infra/ib_realtime/__tests__/sendBounded.test.mjs`:

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  sendBounded,
  admitSymbol,
  MAX_BUFFERED_BYTES,
  MAX_QUEUED_SYMBOLS,
} from "../sendBounded.js";

// Minimal ws stand-in. OPEN mirrors the numeric constant ws uses (1).
function fakeClient({ bufferedAmount = 0, readyState = 1 } = {}) {
  return {
    OPEN: 1,
    readyState,
    bufferedAmount,
    sent: [],
    send(str) {
      this.sent.push(str);
    },
  };
}

test("sendBounded sends when buffer is under the cap", () => {
  const c = fakeClient({ bufferedAmount: 0 });
  const metrics = { droppedFlushes: 0 };
  const ok = sendBounded(c, { type: "batch", updates: {} }, metrics);
  assert.equal(ok, true);
  assert.equal(c.sent.length, 1);
  assert.equal(metrics.droppedFlushes, 0);
  assert.equal(c.droppedFlushes, undefined);
});

test("sendBounded drops and counts when bufferedAmount exceeds the cap", () => {
  const c = fakeClient({ bufferedAmount: MAX_BUFFERED_BYTES + 1 });
  const metrics = { droppedFlushes: 0 };
  const ok = sendBounded(c, { type: "batch", updates: {} }, metrics);
  assert.equal(ok, false);
  assert.equal(c.sent.length, 0); // nothing sent
  assert.equal(metrics.droppedFlushes, 1); // global counter incremented
  assert.equal(c.droppedFlushes, 1); // per-client counter incremented
});

test("sendBounded does not send to a non-OPEN client", () => {
  const c = fakeClient({ readyState: 3 /* CLOSED */ });
  const metrics = { droppedFlushes: 0 };
  assert.equal(sendBounded(c, {}, metrics), false);
  assert.equal(c.sent.length, 0);
  assert.equal(metrics.droppedFlushes, 0); // closed != dropped-for-backpressure
});

test("admitSymbol allows updates to existing symbols even at the cap", () => {
  const buf = new Map();
  for (let i = 0; i < MAX_QUEUED_SYMBOLS; i += 1) buf.set("S" + i, {});
  assert.equal(buf.size, MAX_QUEUED_SYMBOLS);
  // existing symbol → allowed (last-write-wins)
  assert.equal(admitSymbol(buf, "S0"), true);
  // new symbol at cap → rejected
  assert.equal(admitSymbol(buf, "SNEW"), false);
});

test("admitSymbol allows new symbols under the cap", () => {
  const buf = new Map([["AAPL", {}]]);
  assert.equal(admitSymbol(buf, "TSLA"), true);
});
```

Run it — it must FAIL because the module does not exist yet:

```bash
node --test scripts/infra/ib_realtime/__tests__/sendBounded.test.mjs
```

**Expected: error — cannot find module `../sendBounded.js`.**

### 7b. Implement the module

Create `scripts/infra/ib_realtime/sendBounded.js`:

```js
/* Bounded per-client WebSocket delivery (finding QS-1).
 *
 * The 100ms batch buffers are last-write-wins per symbol, so a flush that we
 * decline to send is recovered by the NEXT flush (which carries a superset of
 * the same symbols' latest values). That invariant is why dropping a flush for
 * a backpressured client is safe: we never lose the latest state, we only skip
 * one delivery attempt while the socket's kernel send buffer drains. The caller
 * must therefore clear its per-client buffer ONLY when sendBounded returns true.
 */

// Fixed caps. Tunable from /status metrics if contention is observed; kept
// constant here (ponytail: no premature env knob for values we have no data on).
export const MAX_BUFFERED_BYTES = 512 * 1024; // 512 KB kernel send-buffer ceiling
export const MAX_QUEUED_SYMBOLS = 500; // hard cap on distinct symbols per client buffer

/**
 * Send `payload` to `client` unless its socket send buffer is over the cap.
 * @returns {boolean} true iff the message was actually handed to client.send().
 *   Callers use the return value to decide whether to clear their batch buffer.
 */
export function sendBounded(client, payload, metrics) {
  if (!client || client.readyState !== client.OPEN) return false;
  if (client.bufferedAmount > MAX_BUFFERED_BYTES) {
    client.droppedFlushes = (client.droppedFlushes || 0) + 1;
    if (metrics) metrics.droppedFlushes += 1;
    return false; // LWW buffer keeps latest state; next flush delivers a superset
  }
  try {
    client.send(JSON.stringify(payload));
    return true;
  } catch {
    return false; // send failure (mid-close); pong-timeout path will reap the client
  }
}

/**
 * Decide whether a symbol/key may be added to a per-client batch buffer.
 * Existing keys always update (last-write-wins); a NEW key is rejected once the
 * buffer already holds MAX_QUEUED_SYMBOLS distinct keys. This bounds buffer
 * growth for a client that never drains.
 * @returns {boolean} true iff the caller should buf.set(key, value).
 */
export function admitSymbol(buf, key) {
  if (!buf.has(key) && buf.size >= MAX_QUEUED_SYMBOLS) return false;
  return true;
}
```

Run the unit test again — **Expected: all 5 tests pass.**

---

## 8. Step 2 — New module `originAllowlist.js` (TDD: test first)

### 8a. Failing unit test

Create `scripts/infra/ib_realtime/__tests__/originAllowlist.test.mjs`:

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { parseAllowlist, evaluateOrigin } from "../originAllowlist.js";

test("parseAllowlist splits, trims, and drops empties", () => {
  assert.deepEqual(
    parseAllowlist(" http://localhost:3200 , http://localhost:3000 ,"),
    ["http://localhost:3200", "http://localhost:3000"],
  );
});

test("parseAllowlist returns the built-in default when env is empty/undefined", () => {
  const def = parseAllowlist(undefined);
  assert.ok(def.includes("http://localhost:3200"));
  assert.ok(def.includes("http://localhost:3000"));
  assert.ok(def.includes("http://127.0.0.1:3200"));
  assert.ok(def.includes("http://127.0.0.1:3000"));
});

const LIST = ["http://localhost:3200", "http://localhost:3000"];

test("no Origin header → allowed as server-to-server", () => {
  const r = evaluateOrigin(undefined, LIST, true);
  assert.equal(r.allow, true);
  assert.equal(r.reason, "no-origin");
});

test("empty-string Origin → treated as no-origin", () => {
  assert.equal(evaluateOrigin("", LIST, true).allow, true);
});

test("allowlisted Origin → allowed", () => {
  const r = evaluateOrigin("http://localhost:3200", LIST, true);
  assert.equal(r.allow, true);
  assert.equal(r.reason, "allowlisted");
});

test("disallowed Origin with enforce=true → blocked", () => {
  const r = evaluateOrigin("http://evil.example", LIST, true);
  assert.equal(r.allow, false);
  assert.equal(r.reason, "blocked");
});

test("disallowed Origin with enforce=false → allowed but flagged audit", () => {
  const r = evaluateOrigin("http://evil.example", LIST, false);
  assert.equal(r.allow, true);
  assert.equal(r.reason, "audit-not-enforced");
});
```

Run:

```bash
node --test scripts/infra/ib_realtime/__tests__/originAllowlist.test.mjs
```

**Expected: FAIL — module `../originAllowlist.js` not found.**

### 8b. Implement

Create `scripts/infra/ib_realtime/originAllowlist.js`:

```js
/* Origin allowlist for WS upgrades (finding SEC-2).
 *
 * Threat: a cross-origin page loaded in the operator's LOCAL browser can open a
 * WebSocket to the loopback relay and, today, is admitted by the isLocalhost
 * ticket bypass. Browsers ALWAYS send an Origin header on a WS upgrade (even to
 * localhost); server-to-server clients (Python probe, Node ws, healthchecks)
 * send NONE. So we gate on the PRESENCE of an Origin header, not the peer's IP:
 *   - no Origin header      → server-to-server → pass through (ticket/loopback
 *                             logic downstream is unchanged)
 *   - Origin in allowlist   → allowed browser
 *   - Origin not allowlisted → blocked (or audit-logged when enforce=false)
 */

const DEFAULT_ALLOWED_ORIGINS = [
  "http://localhost:3200",
  "http://127.0.0.1:3200",
  "http://localhost:3000",
  "http://127.0.0.1:3000",
];

/** Parse IB_REALTIME_ALLOWED_ORIGINS. Empty/undefined → the built-in default. */
export function parseAllowlist(envValue) {
  if (!envValue || !envValue.trim()) return [...DEFAULT_ALLOWED_ORIGINS];
  return envValue
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

/**
 * @param {string|undefined} originHeader  req.headers.origin
 * @param {string[]} allowlist
 * @param {boolean} enforce  true → block disallowed; false → allow + audit
 * @returns {{allow: boolean, reason: string}}
 */
export function evaluateOrigin(originHeader, allowlist, enforce) {
  if (!originHeader) return { allow: true, reason: "no-origin" };
  if (allowlist.includes(originHeader))
    return { allow: true, reason: "allowlisted" };
  if (!enforce) return { allow: true, reason: "audit-not-enforced" };
  return { allow: false, reason: "blocked" };
}
```

Run the unit test again — **Expected: all 7 tests pass.**

---

## 9. Step 3 — Wire the modules into the relay

All edits are in `scripts/infra/ib_realtime/ib_realtime_server.js`. Apply in order.

### 9a. Import the new modules

**Anchor** — the existing import of `normalize.js` (`:32`):

```js
import { normalizeForex, normalizeStocksMeta } from "./normalize.js";
```

**Insert immediately after it:**

```js
import {
  sendBounded,
  admitSymbol,
  MAX_BUFFERED_BYTES,
  MAX_QUEUED_SYMBOLS,
} from "./sendBounded.js";
import { parseAllowlist, evaluateOrigin } from "./originAllowlist.js";
```

### 9b. Module-scope config + metrics

**Anchor** — the `STATUS_TOKEN` declaration (`:615`):

```js
const STATUS_TOKEN = process.env.IB_REALTIME_STATUS_TOKEN || "";
```

**Insert immediately after it:**

```js
// ── Origin allowlist config (SEC-2) ──────────────────────────────────────
const ALLOWED_ORIGINS = parseAllowlist(process.env.IB_REALTIME_ALLOWED_ORIGINS);
// Enforce by default; set IB_REALTIME_ORIGIN_ENFORCE=0 to audit-only (rollback
// lever for a mis-set prod allowlist — logs instead of 403, no code redeploy).
const ORIGIN_ENFORCE = process.env.IB_REALTIME_ORIGIN_ENFORCE !== "0";

// ── Backpressure metrics (QS-1) — exposed on GET /status ─────────────────
const relayMetrics = { droppedFlushes: 0, droppedSymbols: 0 };
```

This is defined before `httpServer.listen` (`:632`), so both the `/status` request callback and the flush loops (which run only after startup) reference an initialized binding — no TDZ (matches the guardrail comment at `:628-631`).

### 9c. Origin check on upgrade

**Anchor** — the first two lines inside the upgrade handler (`:442-445`):

```js
httpServer.on("upgrade", async (req, socket, head) => {
  // Skip ticket validation if Clerk is not configured (local dev)
  // or if the connection is from localhost (server-to-server / local browser)
  const remoteAddr = socket.remoteAddress || "";
```

**Insert BETWEEN the `httpServer.on("upgrade"...` line and the `// Skip ticket validation` comment** (i.e. as the very first statements in the handler body):

```js
// SEC-2: reject cross-origin browser upgrades before any auth work. Browsers
// always send Origin; server-to-server clients (probes, healthchecks) send
// none and fall straight through to the ticket/loopback logic below.
const originDecision = evaluateOrigin(
  req.headers.origin,
  ALLOWED_ORIGINS,
  ORIGIN_ENFORCE,
);
if (!originDecision.allow) {
  verbose(`WS upgrade rejected: Origin ${req.headers.origin} not in allowlist`);
  socket.write("HTTP/1.1 403 Forbidden\r\n\r\n");
  socket.destroy();
  return;
}
if (originDecision.reason === "audit-not-enforced") {
  console.warn(
    `[origin-audit] WS upgrade with non-allowlisted Origin ${req.headers.origin} ` +
      `admitted because IB_REALTIME_ORIGIN_ENFORCE=0`,
  );
}
```

Everything below (`const remoteAddr = ...`, the `isLocalhost` bypass, the ticket path) is left **unchanged**.

### 9d. L1 flush loop — gate + clear-on-success only

**Anchor** — `flushBatches()` body (`:732-741`):

```js
function flushBatches() {
  lastFlushTime = Date.now();
  for (const [client, buf] of clientBatchBuffers) {
    if (buf.size === 0) continue;
    const updates = Object.fromEntries(buf);
    buf.clear();
    sendMessage(client, { type: "batch", updates });
  }
  flushDepthAndTapeBatches();
}
```

**Replace the whole function with:**

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

### 9e. L1 buffer cap

**Anchor** — `bufferPriceForClient` body (`:715-721`):

```js
function bufferPriceForClient(client, symbol, data) {
  let buf = clientBatchBuffers.get(client);
  if (!buf) {
    buf = new Map();
    clientBatchBuffers.set(client, buf);
  }
  buf.set(symbol, data);
```

**Replace those lines (up to and including `buf.set(symbol, data);`) with:**

```js
function bufferPriceForClient(client, symbol, data) {
  let buf = clientBatchBuffers.get(client);
  if (!buf) {
    buf = new Map();
    clientBatchBuffers.set(client, buf);
  }
  if (!admitSymbol(buf, symbol)) {
    relayMetrics.droppedSymbols += 1;
    return; // QS-1: hard cap on distinct queued symbols for a stalled client
  }
  buf.set(symbol, data);
```

Leave the adaptive early-flush block (`if (buf.size >= BATCH_THRESHOLD ...`) that follows **unchanged**.

### 9f. Depth/tape flush loop — gate + clear-on-success only

**Anchor** — `flushDepthAndTapeBatches()` body (`:1295-1308`):

```js
function flushDepthAndTapeBatches() {
  for (const [client, buf] of clientDepthBuffers) {
    if (buf.size === 0) continue;
    const updates = Object.fromEntries(buf);
    buf.clear();
    sendMessage(client, { type: "depth-batch", updates });
  }
  for (const [client, buf] of clientTapeBuffers) {
    if (buf.size === 0) continue;
    const updates = Object.fromEntries(buf);
    buf.clear();
    sendMessage(client, { type: "tape-batch", updates });
  }
}
```

**Replace the whole function with:**

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

### 9g. Depth/tape buffer caps

**Anchor** — `bufferDepthForClient` (`:1274-1281`):

```js
function bufferDepthForClient(client, key, book) {
  let buf = clientDepthBuffers.get(client);
  if (!buf) {
    buf = new Map();
    clientDepthBuffers.set(client, buf);
  }
  buf.set(key, book);
}
```

**Replace with:**

```js
function bufferDepthForClient(client, key, book) {
  let buf = clientDepthBuffers.get(client);
  if (!buf) {
    buf = new Map();
    clientDepthBuffers.set(client, buf);
  }
  if (!admitSymbol(buf, key)) {
    relayMetrics.droppedSymbols += 1;
    return;
  }
  buf.set(key, book);
}
```

**Anchor** — `bufferTapeForClient` (`:1283-1290`):

```js
function bufferTapeForClient(client, key, trades) {
  let buf = clientTapeBuffers.get(client);
  if (!buf) {
    buf = new Map();
    clientTapeBuffers.set(client, buf);
  }
  buf.set(key, trades);
}
```

**Replace with:**

```js
function bufferTapeForClient(client, key, trades) {
  let buf = clientTapeBuffers.get(client);
  if (!buf) {
    buf = new Map();
    clientTapeBuffers.set(client, buf);
  }
  if (!admitSymbol(buf, key)) {
    relayMetrics.droppedSymbols += 1;
    return;
  }
  buf.set(key, trades);
}
```

### 9h. Expose drop counters on `/status`

**Anchor** — the `/status` response body (`:426-433`):

```js
      JSON.stringify({
        ib_connected: ibConnected,
        now_ms: now,
        ttl_ms: SUBSCRIBER_TTL_MS,
        subscribers: subscriberRegistry.snapshot(now),
        anonymous_count: Math.max(0, clients.size - clientId.size),
      }),
```

**Replace with:**

```js
      JSON.stringify({
        ib_connected: ibConnected,
        now_ms: now,
        ttl_ms: SUBSCRIBER_TTL_MS,
        subscribers: subscriberRegistry.snapshot(now),
        anonymous_count: Math.max(0, clients.size - clientId.size),
        dropped_flushes_total: relayMetrics.droppedFlushes,
        dropped_symbols_total: relayMetrics.droppedSymbols,
        max_client_buffered_bytes: (() => {
          let max = 0;
          for (const c of clients)
            if (typeof c.bufferedAmount === "number" && c.bufferedAmount > max)
              max = c.bufferedAmount;
          return max;
        })(),
      }),
```

---

## 10. Step 4 — Relay spawn integration test (first CI-run relay behavioral test)

Create `scripts/infra/ib_realtime/__tests__/relay_integration.test.mjs`. It spawns the **real** relay against a **dead** IB port (no gateway, no external network) and asserts the Origin gate + `/status` shape over loopback.

```js
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import http from "node:http";
import net from "node:net";
import os from "node:os";
import { WebSocket } from "ws";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const RELAY = resolve(__dirname, "..", "ib_realtime_server.js");

// The relay's listen log prints cli.port VERBATIM (verified: `listening on
// ${WS_HOST}:${cli.port}`), so `--port 0` would log ":0", not the bound port.
// Allocate a free port ourselves, then pass it explicitly.
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
  // Dead IB port: nothing listens on 65500 → async ECONNREFUSED → the relay
  // schedules reconnects while it keeps serving HTTP/WS. CLERK_JWKS_URL is set
  // so the loopback ticket bypass is NOT auto-enabled by missing-Clerk; we
  // still expect no-Origin acceptance.
  // IB_REALTIME_RUNTIME_FILE is redirected to a test-scoped temp path — the
  // default is $TMPDIR/xenon-ib-realtime.json, which a spawned test relay
  // would otherwise CLOBBER, breaking a concurrently running dev relay's
  // port-discovery file.
  const env = {
    ...process.env,
    CLERK_JWKS_URL: "https://example.test/jwks",
    IB_REALTIME_ALLOWED_ORIGINS: "http://localhost:3200,http://localhost:3000",
    IB_REALTIME_ORIGIN_ENFORCE: "1",
    IB_REALTIME_RUNTIME_FILE: join(
      os.tmpdir(),
      `xenon-ib-realtime-test-${process.pid}.json`,
    ),
  };
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
  // Wait for the listen line as the readiness signal.
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

function upgradeStatus(origin) {
  // Raw HTTP upgrade request; capture the status line the server writes.
  return new Promise((res, rej) => {
    const headers = {
      Connection: "Upgrade",
      Upgrade: "websocket",
      "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
      "Sec-WebSocket-Version": "13",
    };
    if (origin) headers.Origin = origin;
    const req = http.request({ host: "127.0.0.1", port, path: "/", headers });
    req.on("upgrade", (r) => {
      r.socket.destroy();
      res({ upgraded: true, status: r.statusCode });
    });
    req.on("response", (r) => {
      r.resume();
      res({ upgraded: false, status: r.statusCode });
    });
    req.on("error", rej);
    req.end();
  });
}

test("(a) disallowed Origin → 403, no upgrade", async () => {
  const r = await upgradeStatus("http://evil.example");
  assert.equal(r.upgraded, false);
  assert.equal(r.status, 403);
});

test("(b) allowlisted Origin → upgrade succeeds", async () => {
  const ws = new WebSocket(`ws://127.0.0.1:${port}/`, {
    headers: { Origin: "http://localhost:3200" },
  });
  const [ev] = await Promise.race([
    once(ws, "open").then(() => ["open"]),
    once(ws, "unexpected-response").then(() => ["rejected"]),
  ]);
  assert.equal(ev, "open");
  ws.close();
});

test("(c) no Origin (server-to-server) → upgrade succeeds", async () => {
  // `ws` does not send an Origin header unless we set one.
  const ws = new WebSocket(`ws://127.0.0.1:${port}/`);
  const [ev] = await Promise.race([
    once(ws, "open").then(() => ["open"]),
    once(ws, "unexpected-response").then(() => ["rejected"]),
  ]);
  assert.equal(ev, "open");
  ws.close();
});

test("(d) /status exposes the new backpressure counters", async () => {
  const body = await new Promise((res, rej) => {
    http
      .get({ host: "127.0.0.1", port, path: "/status" }, (r) => {
        let d = "";
        r.on("data", (c) => (d += c));
        r.on("end", () => res(JSON.parse(d)));
      })
      .on("error", rej);
  });
  assert.equal(typeof body.dropped_flushes_total, "number");
  assert.equal(typeof body.dropped_symbols_total, "number");
  assert.equal(typeof body.max_client_buffered_bytes, "number");
});
```

**Note on drop _behavior_ coverage:** exercising a real `bufferedAmount > 512 KB` drop requires a live tick source, which this stubbed-IB spawn does not have. That behavior is deterministically covered by the `sendBounded.test.mjs` unit test (Step 1) and by the manual soak in §12.4. This split is the brief's sanctioned fallback (extract + unit-test the core; keep the live RSS soak as scripted manual verification).

Run:

```bash
node --test scripts/infra/ib_realtime/__tests__/relay_integration.test.mjs
```

**Expected: 4 tests pass.** If the relay process exits early (before printing the listen line), see Tripwires.

---

## 11. Step 5 — Run all relay tests via one command + wire CI

### 11a. Root `package.json` script

**Anchor** — root `package.json` (`:2`):

```json
  "version": "0.8.1",
  "type": "module",
```

**Insert a `scripts` block after `"type": "module",`:**

```json
  "version": "0.8.1",
  "type": "module",
  "scripts": {
    "test:relay": "node --test scripts/infra/ib_realtime/__tests__/*.mjs"
  },
```

This runs **all** `.mjs` tests in `__tests__/` via the glob (the directory form of `node --test` fails with MODULE_NOT_FOUND on this Node version; keep the `*.mjs` glob) — the two new ones plus the pre-existing `normalize.test.mjs` and `ib_contracts.test.mjs`), finally giving the previously-dark `.mjs` tests a CI runner (partial QS-2).

Run locally:

```bash
npm install --no-audit --no-fund --legacy-peer-deps   # ensure root ws/@stoqey present
npm run test:relay
```

**Expected: all suites pass** (2 new files + 2 pre-existing).

### 11b. New CI job

**Anchor** — `.github/workflows/ci.yml`, the `web-lint:` job block ending at `:50`:

```yaml
      - run: cd web && npm install --no-audit --no-fund --legacy-peer-deps
      - run: cd web && npm run lint

  web-tests:
```

**Insert a new job between `web-lint` and `web-tests`:**

```yaml
      - run: cd web && npm install --no-audit --no-fund --legacy-peer-deps
      - run: cd web && npm run lint

  relay-tests:
    # First CI-run behavioral tests for the Node quote relay (QS-2 partial).
    # Unit tests for sendBounded/originAllowlist + a spawn-the-real-relay
    # integration test that runs against a dead IB port (no gateway needed).
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-node@v6
        with:
          node-version: 20
          cache: npm
          cache-dependency-path: package-lock.json
      - run: npm install --no-audit --no-fund --legacy-peer-deps
      - run: npm run test:relay

  web-tests:
```

**Note:** the root `package-lock.json` must exist for the `cache-dependency-path`. Verify with `ls package-lock.json`. If it does NOT exist, drop the `cache:`/`cache-dependency-path:` lines from the `setup-node` step (npm install still works without the cache) — do not fabricate a lockfile.

---

## 12. Verification matrix

Run every item. Literal expected outcomes are given.

### 12.1 Unit (Node) — new modules

```bash
node --test scripts/infra/ib_realtime/__tests__/sendBounded.test.mjs
node --test scripts/infra/ib_realtime/__tests__/originAllowlist.test.mjs
```

**Expected:** `sendBounded` → 5 pass; `originAllowlist` → 7 pass; `# fail 0` in both summaries.

### 12.2 Integration (Node) — spawn real relay

```bash
npm install --no-audit --no-fund --legacy-peer-deps
node --test scripts/infra/ib_realtime/__tests__/relay_integration.test.mjs
```

**Expected:** 4 pass, `# fail 0`. Specifically: test (a) asserts HTTP **403** for `Origin: http://evil.example`; (b) and (c) open successfully; (d) confirms the three numeric `/status` fields.

### 12.3 All relay tests + no regression of pre-existing `.mjs`

```bash
npm run test:relay
```

**Expected:** all five `.mjs` files pass (`sendBounded`, `originAllowlist`, `relay_integration`, plus pre-existing `normalize`, `ib_contracts`), `# fail 0`.

### 12.4 Slow-client soak (MANUAL, dev/PAPER only — encodes 08-perf §8.4-3)

This proves RSS stays bounded under a stalled reader. Requires a live dev relay during RTH (real ticks). **Do this against the dev stack only (relay :8866), never prod.**

1. Start the dev stack: `scripts/infra/dev.sh paper` (relay binds **:8866**). Confirm subscriptions exist (open the app at :3200 so the browser subscribes ~80 symbols).
2. Capture baseline: `curl -s http://127.0.0.1:8866/status | python3 -m json.tool` — note `dropped_flushes_total`, `max_client_buffered_bytes`, and the relay RSS: `ps -o rss= -p $(lsof -nP -iTCP:8866 -sTCP:LISTEN -t | head -1)`.
3. Run this stalled-reader script (`node scripts/infra/ib_realtime/__tests__/slow_client_soak.mjs` — create as below), which subscribes then **never reads** for 5 minutes:
   ```js
   // scripts/infra/ib_realtime/__tests__/slow_client_soak.mjs  (manual tool, not a node:test)
   import net from "node:net";
   const PORT = Number(process.argv[2] || 8866);
   const key = "dGhlIHNhbXBsZSBub25jZQ==";
   const sock = net.connect(PORT, "127.0.0.1", () => {
     sock.write(
       `GET / HTTP/1.1\r\nHost: 127.0.0.1:${PORT}\r\nUpgrade: websocket\r\n` +
         `Connection: Upgrade\r\nSec-WebSocket-Key: ${key}\r\nSec-WebSocket-Version: 13\r\n\r\n`,
     );
     // Send a subscribe frame for a liquid name, then STOP reading. We pause the
     // socket so the kernel receive window fills and the relay's bufferedAmount grows.
     setTimeout(() => {
       // Minimal masked text frame carrying the subscribe JSON.
       const json = JSON.stringify({
         type: "subscribe",
         symbols: ["SPY", "QQQ", "AAPL", "TSLA", "NVDA"],
       });
       sock.write(encodeMaskedTextFrame(json));
       sock.pause(); // never read again → backpressure builds on the relay side
     }, 500);
   });
   function encodeMaskedTextFrame(str) {
     const payload = Buffer.from(str);
     const len = payload.length; // assume <126 for these small frames
     const mask = Buffer.from([1, 2, 3, 4]);
     const header = Buffer.from([0x81, 0x80 | len]);
     const masked = Buffer.alloc(len);
     for (let i = 0; i < len; i++) masked[i] = payload[i] ^ mask[i % 4];
     return Buffer.concat([header, mask, masked]);
   }
   console.log("stalled reader attached; leave running ~5 min, then Ctrl-C");
   ```
   ```bash
   node scripts/infra/ib_realtime/__tests__/slow_client_soak.mjs 8866
   ```
4. After ~5 min, re-check `/status` and RSS:
   - **Expected:** `dropped_flushes_total` has **increased** (the stalled client is being dropped) OR `max_client_buffered_bytes` is pinned at/under ~512 KB (never climbing unbounded); relay RSS is within a few MB of baseline (no monotonic growth).
   - **Expected:** other browser clients' quotes keep updating (open the app in a second window; prices tick).
   - **Pre-fix contrast (do not run against prod):** without this change, `bufferedAmount` and RSS climb without bound. The pass criterion is _bounded_, not zero.

### 12.5 Whole-file sanity

```bash
node --check scripts/infra/ib_realtime/ib_realtime_server.js
node --check scripts/infra/ib_realtime/sendBounded.js
node --check scripts/infra/ib_realtime/originAllowlist.js
```

**Expected:** exit 0, no output (syntax OK) for all three.

### 12.6 Prettier / lint parity (repo uses prettier at root)

```bash
npx prettier --check "scripts/infra/ib_realtime/sendBounded.js" \
  "scripts/infra/ib_realtime/originAllowlist.js" \
  "scripts/infra/ib_realtime/__tests__/sendBounded.test.mjs" \
  "scripts/infra/ib_realtime/__tests__/originAllowlist.test.mjs" \
  "scripts/infra/ib_realtime/__tests__/relay_integration.test.mjs" \
  "scripts/infra/ib_realtime/ib_realtime_server.js"
```

**Expected:** `All matched files use Prettier code style!` If it reports issues, run `npx prettier --write <same files>` and re-check.

### 12.7 Negative-direction confirmation (SEC-2 both ways) — already in 12.2

- **Blocked without allowlisted Origin:** test (a) → 403. ✅
- **Allowed with allowlisted Origin:** test (b) → open. ✅
- **Server-to-server (no Origin) still works:** test (c) → open. ✅
- **Audit lever:** optional extra manual check — spawn with `IB_REALTIME_ORIGIN_ENFORCE=0` and `Origin: http://evil.example`; expect upgrade to **succeed** with a `[origin-audit]` warning on stderr.

### 12.8 Not applicable (state explicitly, do NOT run)

- Python pytest / `run_pytest_affected.py`: **N/A** — no Python source changed.
- Order-path CI guards (`no_json_fallback_on_order_path.py`, etc.): **N/A** — quote path, no `data/*.json`, no order code.
- Alembic / `psql`: **N/A** — no schema change.
- Playwright / chrome-cdp browser E2E: **N/A** — no UI-visible change (the relay is transport; the frontend protocol is unchanged; `batch`/`depth-batch`/`tape-batch` message shapes are byte-identical). The soak in 12.4 is the closest behavioral check and is manual.
- IB live probes: **N/A** — integration test uses a dead IB port; the manual soak uses PAPER dev only.

---

## 13. Tripwires / abort criteria — STOP and report if:

1. **Step 0 fails** — `grep bufferedAmount` or `grep headers.origin` prints a line. The bound/Origin check may already exist; the anchors are wrong. Stop.
2. **Any anchor snippet is not found verbatim** in `ib_realtime_server.js` (the file drifted further since 2026-07-05). Do NOT guess a new location — stop and report which anchor is missing.
3. **A unit test passes BEFORE its module exists** (Step 1/2 RED phase). That is impossible unless a stale module is present — stop.
4. **The spawned relay in Step 4 exits early** (the `before()` hook rejects with "relay exited early"). This means `ib.connect()` to the dead port threw synchronously or another boot error occurred. Do NOT paper over it: first re-run with the relay's own stderr visible (`node scripts/infra/ib_realtime/ib_realtime_server.js --port 0 --ib-port 65500` and read the error). If it is a genuine synchronous-throw-on-dead-IB, mark the integration test `{ skip: true }`, keep the unit tests + CI job, and report that the spawn path needs a mock IB (P4.3). The unit + manual-soak coverage still satisfies the acceptance criteria.
5. **More than these files need editing.** Expected touch set: `sendBounded.js` (new), `originAllowlist.js` (new), 3 test files (new), `ib_realtime_server.js`, root `package.json`, `.github/workflows/ci.yml`. If a 7th source file needs changes, stop and report.
6. **The soak (12.4) shows RSS still climbing monotonically** after the fix → the bound is not effective; stop and report (likely the `buf.clear()`-on-success logic or the `admitSymbol` cap was mis-applied).
7. **Any live-IB requirement** — never connect to live IB. Integration test uses a dead port; the soak uses `dev.sh paper` (relay :8866) only.
8. **Prod-origin uncertainty:** the exact prod browser Origin is unverified (§4). Do NOT hard-code a guessed prod origin into the default allowlist. If prod quotes must be validated, that is an operator step: append the real origin to `IB_REALTIME_ALLOWED_ORIGINS` in the prod `.env`, or set `IB_REALTIME_ORIGIN_ENFORCE=0` first and watch `[origin-audit]` logs to learn the real origin. Note this prominently in the PR description.

---

## 14. Rollback

- **Code:** `git checkout master -- scripts/infra/ib_realtime/ib_realtime_server.js package.json .github/workflows/ci.yml && git rm scripts/infra/ib_realtime/sendBounded.js scripts/infra/ib_realtime/originAllowlist.js scripts/infra/ib_realtime/__tests__/{sendBounded,originAllowlist,relay_integration}.test.mjs scripts/infra/ib_realtime/__tests__/slow_client_soak.mjs`. Or discard the whole branch: `git checkout master && git branch -D fix/relay-backpressure-origin-allowlist`.
- **No migration** — nothing to down-revision.
- **Live prod rollback WITHOUT redeploy:** set `IB_REALTIME_ORIGIN_ENFORCE=0` in the prod relay's `.env` and restart the `realtime` container. This disables the 403 (audit-only) instantly while keeping the backpressure fix (which has no rollback lever because it is strictly safer — a dropped flush is recovered by the next flush).

---

## 15. Incident-history row

**N/A** — this is the quote relay (SEC-2/QS-1), not the order path. `docs/reference/order-path-incident-history.md` covers order placement/cancel/modify only. No row to append.

---

## 16. PR description checklist (for the executor)

- Findings: QS-1 (backpressure), SEC-2 (Origin allowlist), QS-2 partial (first CI-run relay tests).
- Explicitly note out-of-scope: QS-10 (loop unification), QS-3 (seq/relay_ts), `/ws-ticket` rate-limiting.
- Call out the two new env vars and the **prod-origin operator action** (§13 tripwire 8).
- No AI attribution trailer in the commit message (global policy).
- Open a PR; do not push to master; wait for green CI before merge.
