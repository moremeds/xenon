# AssetCockpit Phase 3 — L2 Market Depth + Time-and-Sales Tape Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a live L2 order book (stock/option montage + futures ladder) and a time-and-sales tape with click-to-fill to the xenon AssetCockpit, by migrating the IB realtime relay to `@stoqey/ib` and porting radon's depth/tape stack.

**Architecture:** Three phases. **3a** migrates the relay's _upstream_ IB library (`ib@0.2.9` → `@stoqey/ib@1.5.6`) at strict L1 parity, behind a frozen WS-URL contract. **3b** adds the depth/tape _backend_ (subscription orchestration, ladder accumulator, tape ring, additive WS messages). **3c** ports radon's _frontend_ book/ladder/montage/tape components + click-to-fill. Each phase is independently shippable; depth code lands only on a proven migration.

**Tech Stack:** Node ESM relay (`@stoqey/ib`, `ws`), Next.js/React/TypeScript frontend, Vitest, chrome-cdp/Playwright E2E.

---

## Binding & Conventions (read first)

This is a **port with a reference implementation.** To honor "bind all work to radon" and avoid duplicating thousands of lines:

- **Port tasks** name an exact radon `file:line` source and a concrete **adaptation list** (import swaps, type/prop renames, xenon wiring). The implementer opens the radon file and ports it, applying the listed adaptations. This is intentional, not a placeholder.
- **Full inline code** is shown for: the `@stoqey/ib` migration edits, all new xenon-specific glue/types, and **every test**.
- **Radon repo root:** `/Users/chenxi/projects/radon`. **Xenon repo root:** `/Users/chenxi/projects/xenon`.
- **Reference map:** radon relay `scripts/ib_realtime_server.js`; radon frontend `web/components/ticker-detail/{OrderBook,DepthMontage,LadderDOM,TimeAndSales,BookTab,AssetCockpit}.tsx`, `web/lib/book/depthDerivations.ts`, `web/lib/usePrices.ts`, `web/lib/TickerDetailContext.tsx`, `web/lib/pricesProtocol.ts`.

### ⛔ Phase-3a hard invariant — frozen WS-URL contract

The migration is **upstream only** (relay ↔ IB Gateway). The downstream WS server is untouched. The Phase-3a diff **must NOT contain changes** to:

- `web/lib/ibRealtimeWsClient.ts`
- `web/lib/server/ibRealtimeRuntime.ts`
- the `http.createServer` / `httpServer.on("upgrade")` / `new WebSocketServer(...)` block in `ib_realtime_server.js` (`:349-439`)
- the `/status` HTTP handler shape (`:353-385`)
- the L1 WS message protocol (`batch`/`price`/`snapshot`/`fundamentals`/`status`/`subscribed`/`unsubscribed`)

The WS server bind must remain **independent of IB connect** (it already is — `httpServer`/`wss` are created before and regardless of `ib.connect()`). Phase 3b adds _new_ message types only; it never alters existing ones.

---

## File Structure

**Relay (Node ESM, `scripts/infra/ib_realtime/`)**

| File                      | Resp.                                                                                                  | Phase  |
| ------------------------- | ------------------------------------------------------------------------------------------------------ | ------ |
| `ib_realtime_server.js`   | **modify** — library swap, event/contract migration, error/info split, depth/tape orchestration        | 3a, 3b |
| `ib_tick_handler.js`      | **modify** — event-name/enum adjustments for L1 ticks                                                  | 3a     |
| `ib_connection_status.js` | **modify** — classify against error+info split                                                         | 3a     |
| `ib_contracts.js`         | **NEW** — plain-object contract builders (`stock`/`option`/`future`/`index`) replacing `ib.contract.*` | 3a     |
| `depth_book.js`           | **NEW** — pure ladder accumulator (`applyDepthDelta`, `serializeLadder`, `summarizeOptionNbbo`)        | 3b     |
| `tape_feed.js`            | **NEW** — pure tape ring buffer (`applyTrade`, bounded)                                                | 3b     |
| `depth_contracts.js`      | **NEW** — futures front-month resolution + index→future map                                            | 3b     |

**Frontend (`web/`)**

| File                                        | Resp.                                                                          | Phase |
| ------------------------------------------- | ------------------------------------------------------------------------------ | ----- |
| `lib/pricesProtocol.ts`                     | **modify** — add `DepthLevel`/`DepthNbbo`/`DepthBook`/`Trade` + 3 WS msg types | 3c    |
| `lib/book/depthDerivations.ts`              | **NEW** (near-verbatim port) — pure render math                                | 3c    |
| `lib/usePrices.ts`                          | **modify** — depth/tape subscribe + state + handlers                           | 3c    |
| `lib/TickerDetailContext.tsx`               | **modify** — add `OrderPrefill` + `setOrderPrefill`                            | 3c    |
| `components/ticker-detail/OrderBook.tsx`    | **NEW** (port) — book window head + montage/ladder dispatch + tape toggle      | 3c    |
| `components/ticker-detail/DepthMontage.tsx` | **NEW** (port) — stock/option two-sided montage                                | 3c    |
| `components/ticker-detail/LadderDOM.tsx`    | **NEW** (port) — futures centered ladder                                       | 3c    |
| `components/ticker-detail/TimeAndSales.tsx` | **NEW** (port) — tape                                                          | 3c    |
| `components/ticker-detail/BookTab.tsx`      | **modify** — consume depth/tape/onPriceClick; L1 fallback                      | 3c    |
| `components/ticker-detail/AssetCockpit.tsx` | **modify** — thread depth/tape; `onBookPriceClick`                             | 3c    |
| `components/TickerDetailContent.tsx`        | **modify** — pass depth/tape; `depthSymbol = bookKey`                          | 3c    |
| `components/ticker-detail/OrderTab.tsx`     | **modify** — consume `orderPrefill`                                            | 3c    |

**Tests:** Frontend → Vitest under `web/tests/`. Relay pure modules → Vitest (importable Node ESM). **Step 0 of Phase 3b** locates the existing relay test convention; default to Vitest in `web/tests/relay/` importing relay modules by relative path if none exists. **Config check (do once, in Task 3a.2):** confirm the Vitest config's `include` glob actually picks up `web/tests/relay/**`, that a `.test.ts` file can import a plain-`.js` relay module (ESM interop — these JS modules are untyped, so `allowJs`/no-typecheck-on-import must hold), and that `@stoqey/ib` resolves from the web test run (Node walks up to root `node_modules`). If any fails, fix the config before writing more relay tests.

---

## ⛔ SCOPE DECISION (2026-06-16): FUTURES DEFERRED

Execution scope is **stock + option depth/tape only**. The futures ladder is **out of scope for this plan** — verified that xenon's cockpit never produces `bookKind="future"` (the portfolio→`bookKind` pipeline only classifies stock/option; `ib_orders.py:57` handles FUT for _order placement_ but nothing surfaces a futures _instrument_ to the book). Building it now would ship dead code.

**Deferred (do NOT implement this round):** Task 3b.3 (`depth_contracts.js` front-month/index→future), the `LadderDOM` component in Task 3c.4, and the futures branch in Task 3c.7. The `DepthBook.kind` type keeps `"future"` as a forward-compatible value, but no code path emits it. Revisit when a futures portfolio-classification path exists.

---

# PHASE 3A — `@stoqey/ib` migration at L1 parity

**Exit gate (no 3b/3c work merges until all pass):** full L1 parity in dev **and** prod-like Docker — quotes/greeks tick, `IBStatusContext` connected, operator-console `/status` green — plus all 3a unit tests + the WS-URL regression test green, and the diff respects the frozen-contract invariant.

### Task 3a.1: Swap the IB dependency

**Files:**

- Modify: **ROOT** `package.json` — ⚠️ the relay lives at `scripts/infra/ib_realtime/` and resolves modules from the **repo-root** `node_modules`, where `ib`/`ws` are declared (`package.json` root: `"ib": "^0.2.9"`, `"ws": "^8.19.0"`, `"type": "module"`). Installing in `web/` would NOT make `@stoqey/ib` resolvable to the relay.

- [ ] **Step 1: Confirm the only `ib` importers are the relay (safe to uninstall)**

Run: `grep -rn --exclude-dir=node_modules -E "from ['\"]ib['\"]" scripts src web`
Expected: exactly two hits — `scripts/infra/ib_realtime/ib_realtime_server.js:25` and `scripts/infra/ib_realtime/ib_tick_handler.js:6`. If anything else imports `ib`, migrate it in this phase too before uninstalling.

- [ ] **Step 2: Add `@stoqey/ib`, remove `ib` — at the REPO ROOT**

```bash
cd /Users/chenxi/projects/xenon
npm install @stoqey/ib@1.5.6
npm uninstall ib
```

- [ ] **Step 3: Verify the RELAY resolves it (resolve from the relay's directory, not root)**

Run: `node --input-type=module -e "import('@stoqey/ib').then(m=>console.log('IBApi',typeof m.IBApi,'EventName',typeof m.EventName))"` from `scripts/infra/ib_realtime/`
Expected: `IBApi function EventName object`.
Also confirm the web Vitest run can resolve it (Node walks up from `web/` to root `node_modules`): `cd web && node -e "require.resolve('@stoqey/ib')"`. If it fails, add `@stoqey/ib` to `web/package.json` devDependencies as well.

- [ ] **Step 4: Commit**

```bash
git add package.json package-lock.json
git commit -m "build(relay): add @stoqey/ib at root, drop ib@0.2.9 (phase 3a)"
```

### Task 3a.2: Plain-object contract builders

`@stoqey/ib` has **no `ib.contract.*` factory helpers** (radon `ib_realtime_server.js:196` note). Replace them with a small module mirroring radon's plain-object builders (radon `:202-223`).

**Files:**

- Create: `scripts/infra/ib_realtime/ib_contracts.js`
- Test: `web/tests/relay/ib_contracts.test.ts`

- [ ] **Step 1: Write the failing test**

```ts
import { describe, it, expect } from "vitest";
import {
  stockContract,
  optionContract,
  futureContract,
} from "../../../scripts/infra/ib_realtime/ib_contracts.js";
import { SecType } from "@stoqey/ib";

describe("ib_contracts", () => {
  it("builds a SMART stock contract", () => {
    expect(stockContract("AAPL")).toEqual({
      symbol: "AAPL",
      secType: SecType.STK,
      exchange: "SMART",
      currency: "USD",
    });
  });
  it("builds an option contract (SMART/USD, OCC fields)", () => {
    expect(optionContract("AAPL", "20260116", 200, "C")).toEqual({
      symbol: "AAPL",
      secType: SecType.OPT,
      exchange: "SMART",
      currency: "USD",
      lastTradeDateOrContractMonth: "20260116",
      strike: 200,
      right: "C",
      multiplier: "100",
    });
  });
  it("builds a future contract on its native exchange", () => {
    expect(futureContract("ES", "20260320", "CME")).toEqual({
      symbol: "ES",
      secType: SecType.FUT,
      exchange: "CME",
      currency: "USD",
      lastTradeDateOrContractMonth: "20260320",
    });
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run tests/relay/ib_contracts.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```js
// scripts/infra/ib_realtime/ib_contracts.js
import { SecType } from "@stoqey/ib";

export function stockContract(symbol, exchange = "SMART", currency = "USD") {
  return { symbol, secType: SecType.STK, exchange, currency };
}
export function optionContract(
  symbol,
  expiry,
  strike,
  right,
  exchange = "SMART",
  currency = "USD",
) {
  return {
    symbol,
    secType: SecType.OPT,
    exchange,
    currency,
    lastTradeDateOrContractMonth: expiry,
    strike,
    right,
    multiplier: "100",
  };
}
export function futureContract(symbol, expiry, exchange, currency = "USD") {
  return {
    symbol,
    secType: SecType.FUT,
    exchange,
    currency,
    lastTradeDateOrContractMonth: expiry,
  };
}
export function indexContract(symbol, exchange, currency = "USD") {
  return { symbol, secType: SecType.IND, exchange, currency };
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx vitest run tests/relay/ib_contracts.test.ts`
Expected: PASS (4 assertions).

- [ ] **Step 5: Commit**

```bash
git add scripts/infra/ib_realtime/ib_contracts.js web/tests/relay/ib_contracts.test.ts
git commit -m "feat(relay): plain-object contract builders for @stoqey/ib (phase 3a)"
```

### Task 3a.3: Migrate construction + event registration

**Files:**

- Modify: `scripts/infra/ib_realtime/ib_realtime_server.js` (`:25` import, `:214` construct, `:779`/`:905`/`:929`/`:1126`/`:1154` req/contract sites, `:1305-1334` connect, `:1334-1491` event handlers)
- Modify: `scripts/infra/ib_realtime/ib_tick_handler.js`

**Reference:** radon `scripts/ib_realtime_server.js:26` (import), `:188` (construct), `:2030-2261` (event registration via `EventName.*`).

- [ ] **Step 1: Swap import + construction**

Replace `import IB from "ib";` (`:25`) with:

```js
import { IBApi, EventName, SecType, TickByTickDataType } from "@stoqey/ib";
import { stockContract, optionContract } from "./ib_contracts.js";
```

Replace `new IB({ ... })` (`:214`) with `new IBApi({ ... })` (same options object — host/port/clientId).

- [ ] **Step 2: Convert all string events to `EventName.*`**

For every `ib.on("X", …)` in `ib_realtime_server.js` and `ib_tick_handler.js`, rewrite to `ib.on(EventName.X, …)`. Mapping (handler bodies unchanged — payloads are identical):
`"connected"→EventName.connected`, `"disconnected"→EventName.disconnected`, `"tickPrice"→EventName.tickPrice`, `"tickSize"→EventName.tickSize`, `"tickSnapshotEnd"→EventName.tickSnapshotEnd`, `"fundamentalData"→EventName.fundamentalData`, `"symbolSamples"→EventName.symbolSamples`, plus the option-computation handler at `:1448` → `EventName.tickOptionComputation` (confirm arity matches radon `:2161`).

- [ ] **Step 3: Replace `ib.contract.*` calls (stock, option, AND index)**

`ib.contract.stock(symbol, "SMART", "USD")` (`:905`, `:1126`) → `stockContract(symbol)`.
`ib.contract.option(...)` (`:1154`) → `optionContract(symbol, expiry, strike, right)`.
`ib.contract.index(idx.symbol, "USD", idx.exchange)` (`:1180`) → `indexContract(idx.symbol, idx.exchange)` — **do not skip index**; it feeds the cold-start restore path and is pinned by an existing contract test (Step 3c).
`reqMktData(...)` signatures are unchanged (`:779`, `:929`) — only the contract object changes.

- [ ] **Step 3b: Migrate `ib_tick_handler.js` tick-type constants** — it does `import IB from "ib"; const { TICK_TYPE } = IB;` (`:6`,`:8`) and switches on `TICK_TYPE.BID/ASK/LAST/HIGH/LOW/OPEN/CLOSE/VOLUME` (`:71-95`). Uninstalling `ib` (Task 3a.1) **breaks all L1 tick decoding**. Replace with the `@stoqey/ib` equivalent — verify the exact exported enum name (`IBApiTickType` / `TickType`) and remap each member. This is mandatory in the same commit as the uninstall.

- [ ] **Step 3c: Update the existing contract regression test** — `web/tests/ib-index-stream-contracts.test.ts` hard-codes the old `ib.contract.stock/option/index` source text and the typed-contract restore invariant; it WILL fail after this migration by design. Rewrite it to assert the new `stockContract/optionContract/indexContract` builders + `@stoqey/ib` plain-object shapes, preserving the cold-start restore invariant it guards.

- [ ] **Step 4: Typecheck + boot smoke (manual)**

Run: `cd web && npm run typecheck` (no relay TS, but catches frontend import drift). Then boot the relay alone:
Run: `node scripts/infra/ib_realtime/ib_realtime_server.js --port 8899` (Ctrl-C after "listening")
Expected: process binds the WS port and logs IB connect attempt with no `EventName`/import errors.

- [ ] **Step 5: Commit**

```bash
git add scripts/infra/ib_realtime/ib_realtime_server.js scripts/infra/ib_realtime/ib_tick_handler.js
git commit -m "refactor(relay): migrate construction + events to @stoqey/ib (phase 3a)"
```

### Task 3a.4: Error/info split (the risky one)

`@stoqey/ib` splits informational codes into `EventName.info (message, code)`, separate from `EventName.error (error, code, reqId)` (radon `:2063`, `:2138`). xenon currently treats everything as `error` (`:1359`). Re-wire so info codes (2104/2106/2108/2158 = "market data farm OK") never flip `ibConnected`.

**Files:**

- Modify: `scripts/infra/ib_realtime/ib_connection_status.js`
- Modify: `scripts/infra/ib_realtime/ib_realtime_server.js:1359` (error handler → error + info handlers)
- Test: `web/tests/relay/ib_connection_status.test.ts`

- [ ] **Step 1: Write failing tests**

```ts
import { describe, it, expect } from "vitest";
import {
  classifyIBConnectionError,
  isInfoCode,
} from "../../../scripts/infra/ib_realtime/ib_connection_status.js";

describe("ib_connection_status — error/info split", () => {
  it("treats data-farm-OK codes as info, not a disconnect", () => {
    for (const code of [2104, 2106, 2108, 2158])
      expect(isInfoCode(code)).toBe(true);
  });
  it("treats real fault codes as non-info", () => {
    for (const code of [1100, 504, 502, 10182])
      expect(isInfoCode(code)).toBe(false);
  });
  it("still classifies socket connect failures as MFA/reconnect issue", () => {
    const r = classifyIBConnectionError(
      "connect ECONNREFUSED 192.168.5.2:4001",
    );
    expect(r?.code).toBe("ibc_mfa_required");
  });
  it("returns null for an info message", () => {
    expect(
      classifyIBConnectionError("Market data farm connection is OK:usfarm"),
    ).toBeNull();
  });
});
```

- [ ] **Step 2: Run to verify fail**

Run: `cd web && npx vitest run tests/relay/ib_connection_status.test.ts`
Expected: FAIL — `isInfoCode` not exported.

- [ ] **Step 3: Implement `isInfoCode` (append to `ib_connection_status.js`)**

```js
// IB "informational" notification codes delivered on EventName.info under
// @stoqey/ib (data-farm connect/OK chatter). These must never flip ib_connected.
const INFO_CODES = new Set([
  1101, 1102, 2103, 2104, 2105, 2106, 2107, 2108, 2119, 2157, 2158,
]);
export function isInfoCode(code) {
  return INFO_CODES.has(Number(code));
}
```

(Leave `classifyIBConnectionError` as-is — it already returns null on non-socket text.)

- [ ] **Step 4: Re-wire the relay handlers** (replace the single `ib.on("error", (error, data) => …)` at `:1359`)

⚠️ The existing handler spans `:1359-1446` and is NOT a stub — it carries a full triage chain that must be **preserved verbatim** inside `handleIbError`: the `client id is already in use` clientId-rotation branch (`:1409`), the `classifyIBConnectionError` connection-failure path, the `ibConnected` flip + status broadcast, and the existing info regex (`/farm connection is OK/i`, `:1369`). The migration only ADDS the `isInfoCode` short-circuit and the separate `info` channel — it drops nothing.

```js
ib.on(EventName.error, (error, code, reqId) => {
  // @stoqey may emit (error) alone — code/reqId can be undefined; guard them.
  if (isInfoCode(code)) {
    verbose(`IB info ${code}: ${error}`);
    return;
  }
  handleIbError(error, code, reqId); // ← existing :1359-1446 TRIAGE LOGIC, re-argged
});
ib.on(EventName.info, (message, code) => {
  verbose(`IB info ${code}: ${message}`); // informational only — never touch ibConnected
});
```

⚠️ **Not a verbatim lift — the arg shape changed.** The current handler signature is `ib.on("error", (error, data) => …)` and it reads `const tickerId = data?.id; const code = data?.code;` (`:1359-1362`). Under `@stoqey/ib` the signature is `(error, code, reqId)` — there is no `data` object. So `handleIbError` keeps the **triage logic** (the `client id is already in use` rotation `:1409`, `classifyIBConnectionError`, `ibConnected` flip, status broadcast, and the `/farm connection is OK/i` regex `:1369`) but its req-id/code extraction must be **rewritten**: replace `data?.id`→`reqId`, `data?.code`→`code`. Reference radon `:2063-2145`.

- [ ] **Step 4b: Regression test the preserved triage** — add a unit test asserting a `client id is already in use` error still drives the clientId-rotation path post-migration, and that a `2104`/`farm connection is OK` message does NOT flip `ibConnected`.

- [ ] **Step 5: Run tests to verify pass**

Run: `cd web && npx vitest run tests/relay/ib_connection_status.test.ts`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/infra/ib_realtime/ib_connection_status.js scripts/infra/ib_realtime/ib_realtime_server.js web/tests/relay/ib_connection_status.test.ts
git commit -m "fix(relay): handle @stoqey/ib error/info split so info codes don't drop ib_connected (phase 3a)"
```

### Task 3a.5: WS-URL invariant regression test

**Files:**

- Test: `web/tests/relay/ws-url-contract.test.ts`

- [ ] **Step 1: Write the test (pins downstream URL resolution)**

```ts
import { describe, it, expect, vi } from "vitest";
import {
  resolveServerIbRealtimeWsUrl,
  resolveBrowserIbRealtimeWsUrl,
} from "@/lib/server/ibRealtimeRuntime";

describe("WS-URL contract (frozen across 3a migration)", () => {
  it("server default resolves to loopback :8765", () => {
    expect(
      resolveServerIbRealtimeWsUrl({
        envUrl: undefined,
        runtimeFile: "/nonexistent.json",
      }),
    ).toBe("ws://127.0.0.1:8765");
  });
  it("server honors IB_REALTIME_WS_URL override", () => {
    expect(resolveServerIbRealtimeWsUrl({ envUrl: "ws://relay:9000" })).toBe(
      "ws://relay:9000",
    );
  });
  it("browser falls back to forwarded host + default port", () => {
    expect(
      resolveBrowserIbRealtimeWsUrl({
        envUrl: undefined,
        runtimeFile: "/nonexistent.json",
        forwardedHost: "xenon.example.com",
        forwardedProto: "https",
      }),
    ).toBe("wss://xenon.example.com:8765");
  });
});
```

- [ ] **Step 1b: Cover the REAL failure mode, not just default strings.** The default-string assertions above are necessary but weak. xenon's URL contract actually hinges on the **runtime file + fallback-port** path: the relay picks a port (8765, or +1 if occupied — `:342`), writes it to `xenon-ib-realtime.json`, and consumers read it back. The real migration risk is "relay starts on a different port, or fails before writing the runtime file." So:
  - **Preserve, don't duplicate, the existing `web/tests/ib-realtime-runtime-config.test.ts`** (it already exercises runtime-file + header-derived resolution) — confirm it still passes post-migration; do not weaken it.
  - Add a test for the **fallback chain**: write a runtime file with a non-default port (e.g. `8866`) → assert both `resolveServerIbRealtimeWsUrl` and `resolveBrowserIbRealtimeWsUrl` resolve to that port (proving consumers follow the relay's actual bind, not a hardcoded 8765).

- [ ] **Step 2: Run to verify pass (no code change — pins current behavior)**

Run: `cd web && npx vitest run tests/relay/ws-url-contract.test.ts tests/ib-realtime-runtime-config.test.ts`
Expected: PASS. (If it fails, the migration changed the URL contract — STOP and revert that change.)

- [ ] **Step 3: Diff-shape check**

Run: `git diff --name-only origin/master...HEAD`
Expected: the list does **NOT** include `web/lib/ibRealtimeWsClient.ts` or `web/lib/server/ibRealtimeRuntime.ts`. If it does, the invariant is violated.

- [ ] **Step 4: Commit**

```bash
git add web/tests/relay/ws-url-contract.test.ts
git commit -m "test(relay): pin frozen WS-URL contract across migration (phase 3a)"
```

### Task 3a.6: L1 parity exit gate (verification)

**Files:** none (verification only)

- [ ] **Step 1: Boot dev stack on the migrated relay**

Run: `scripts/infra/dev.sh paper`
Expected: FastAPI `/health` ok; relay logs `EventName.connected`; no info-code error spam.

- [ ] **Step 2: Browser E2E — live L1**

Open the cockpit on a liquid symbol (e.g. QQQ). Confirm: bid/ask/last tick; greeks populate on an option; `IBStatusContext` shows connected (no false "reconnecting"). Capture screenshot to `docs/plans/phase3a-l1-parity.png`.

- [ ] **Step 3: Operator console `/status` green**

Navigate to `/admin`; confirm IB Gateway reachability + realtime subscribers section render healthy (this consumes the relay `/status` shape that must be unchanged).

- [ ] **Step 4: Prod-like Docker parity**

Build + run the relay container per `docs/runbooks/` Docker topology; confirm L1 quotes + `/status` across containers (per the "verify prod Docker topology" lesson — single-host dev is insufficient).

- [ ] **Step 5: Commit verification artifact**

```bash
git add docs/plans/phase3a-l1-parity.png
git commit -m "test(relay): phase 3a L1 parity verified (dev + docker)"
```

---

# PHASE 3B — Depth + tape backend

**Step 0 (once):** locate relay test convention.
Run: `ls scripts/infra/ib_realtime/*test* scripts/tests/*realtime* web/tests/relay/ 2>/dev/null`. Use the existing pattern; else Vitest under `web/tests/relay/` as in Phase 3a.

### Task 3b.1: `depth_book.js` — pure ladder accumulator

**Reference:** radon `scripts/ib_realtime_server.js:1250-1270` (`applyDepthDelta`), `:1272-1292` (`serializeLadder`), `summarizeOptionNbbo`.

**Files:**

- Create: `scripts/infra/ib_realtime/depth_book.js`
- Test: `web/tests/relay/depth_book.test.ts`

- [ ] **Step 1: Write failing tests**

```ts
import { describe, it, expect } from "vitest";
import {
  applyDepthDelta,
  serializeLadder,
} from "../../../scripts/infra/ib_realtime/depth_book.js";

const newLadders = () => ({ bid: [], ask: [] });

describe("depth_book.applyDepthDelta", () => {
  it("insert (op=0) splices a level at position on the correct side", () => {
    const L = newLadders();
    applyDepthDelta(L, 0, "MM1", 0, 1, 100.5, 300); // side=1 → bid
    expect(L.bid).toEqual([{ price: 100.5, size: 300, marketMaker: "MM1" }]);
    expect(L.ask).toEqual([]);
  });
  it("update (op=1) replaces level at position", () => {
    const L = newLadders();
    applyDepthDelta(L, 0, "MM1", 0, 0, 101.0, 200); // ask insert
    applyDepthDelta(L, 0, "MM1", 1, 0, 101.0, 250); // ask update
    expect(L.ask[0].size).toBe(250);
  });
  it("delete (op=2) removes level at position", () => {
    const L = newLadders();
    applyDepthDelta(L, 0, "MM1", 0, 1, 100.5, 300);
    applyDepthDelta(L, 0, "MM1", 2, 1, 100.5, 0);
    expect(L.bid).toEqual([]);
  });
  it("update past end is OOB-defensive (inserts)", () => {
    const L = newLadders();
    applyDepthDelta(L, 5, "MM1", 1, 1, 99.0, 100); // update at empty pos 5
    expect(L.bid.length).toBe(1);
  });
});

describe("depth_book.serializeLadder", () => {
  it("maps internal rows to DepthLevel[] for a stock (marketMaker as venue)", () => {
    const rows = [{ price: 100.5, size: 300, marketMaker: "ARCA" }];
    expect(serializeLadder(rows, false, "stock", "bid")).toEqual([
      { price: 100.5, size: 300, marketMaker: "ARCA", exchange: "ARCA" },
    ]);
  });
});
```

- [ ] **Step 2: Run to verify fail**

Run: `cd web && npx vitest run tests/relay/depth_book.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Port from radon `:1250-1292`.** Port `applyDepthDelta` (radon `:1250-1270`), replacing radon's module-state `key` lookup with a **passed-in `ladders` object** — signature `applyDepthDelta(ladders, position, marketMaker, operation, side, price, size)`. Operations: `0`=insert (`splice(pos,0,row)`), `1`=update (`ladder[pos]=row`, OOB→insert), `2`=delete (`splice(pos,1)`); `side===1`→bid else ask. Port `serializeLadder` with **radon's real 4-arg signature** `serializeLadder(ladder, isFutures, kind, side)` (radon `:1272-1292`) — stock/future: `exchange = marketMaker || null`; option: set the per-row `nbbo` flag via `summarizeOptionNbbo`. Also port `summarizeOptionNbbo(bid, ask)` → `{bestBid,bestAsk,mid,bidSize,askSize}`.

- [ ] **Step 4: Run to verify pass**

Run: `cd web && npx vitest run tests/relay/depth_book.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/infra/ib_realtime/depth_book.js web/tests/relay/depth_book.test.ts
git commit -m "feat(relay): pure L2 ladder accumulator ported from radon (phase 3b)"
```

### Task 3b.2: `tape_feed.js` — pure ring buffer

**Reference:** radon `:1180-1193` (`applyTrade` — stateful, keyed by symbol). Extract the **pure ring-append core** into `appendTrade(ring, trade)` (improves testability; the relay holds each per-symbol ring in `symbolDepthStates`).

**Files:**

- Create: `scripts/infra/ib_realtime/tape_feed.js`
- Test: `web/tests/relay/tape_feed.test.ts`

- [ ] **Step 1: Failing test**

```ts
import { describe, it, expect } from "vitest";
import {
  appendTrade,
  TAPE_RING_SIZE,
} from "../../../scripts/infra/ib_realtime/tape_feed.js";

describe("tape_feed.appendTrade", () => {
  it("appends newest-last and bounds to TAPE_RING_SIZE", () => {
    let ring = [];
    for (let i = 0; i < TAPE_RING_SIZE + 10; i++)
      ring = appendTrade(ring, {
        price: i,
        size: 1,
        exchange: "X",
        time: `${i}`,
      });
    expect(ring.length).toBe(TAPE_RING_SIZE);
    expect(ring[ring.length - 1].price).toBe(TAPE_RING_SIZE + 9);
    expect(ring[0].price).toBe(10);
  });
});
```

- [ ] **Step 2: Run → fail.** `cd web && npx vitest run tests/relay/tape_feed.test.ts`

- [ ] **Step 3: Implement**

```js
// scripts/infra/ib_realtime/tape_feed.js
export const TAPE_RING_SIZE = 50;
export function appendTrade(ring, trade) {
  const next = ring.length >= TAPE_RING_SIZE ? ring.slice(1) : ring.slice();
  next.push(trade); // {price, size, exchange|null, time(ISO)}
  return next;
}
```

- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** — `feat(relay): bounded tape ring buffer (phase 3b)`

### Task 3b.3: `depth_contracts.js` — futures front-month + index→future — ⛔ DEFERRED (futures out of scope; see Scope Decision)

**Reference:** radon `:347-371` (`DEPTH_FUTURES_SYMBOLS`, `FUTURES_ROOT_EXCHANGES`, `INDEX_FUTURE_ROOT`), front-month resolution `:1768-1804`.

**Files:**

- Create: `scripts/infra/ib_realtime/depth_contracts.js`
- Test: `web/tests/relay/depth_contracts.test.ts`

- [ ] **Step 1: Failing test**

```ts
import { describe, it, expect } from "vitest";
import {
  indexToFutureRoot,
  futuresExchange,
  isDepthFuture,
} from "../../../scripts/infra/ib_realtime/depth_contracts.js";

describe("depth_contracts", () => {
  it("maps cash indices to their E-mini future root", () => {
    expect(indexToFutureRoot("SPX")).toBe("ES");
    expect(indexToFutureRoot("NDX")).toBe("NQ");
    expect(indexToFutureRoot("RUT")).toBe("RTY");
    expect(indexToFutureRoot("AAPL")).toBeNull();
  });
  it("resolves native exchange per root", () => {
    expect(futuresExchange("ES")).toBe("CME");
    expect(futuresExchange("CL")).toBe("NYMEX");
  });
  it("recognizes depth-eligible futures incl. VIX", () => {
    expect(isDepthFuture("ES")).toBe(true);
    expect(isDepthFuture("VIX")).toBe(true);
    expect(isDepthFuture("ZZZ")).toBe(false);
  });
});
```

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Port the static maps from radon `:347-371`** into exported `indexToFutureRoot`, `futuresExchange`, `isDepthFuture`. (Async `resolveFuturesFrontMonth(ib, root)` via `reqContractDetails` with a 6s timeout is wired in Task 3b.4; export a stub signature here so the relay imports cleanly.)
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** — `feat(relay): futures front-month + index→future maps (phase 3b)`

### Task 3b.4: Subscribe-depth handler + budget/LRU + event wiring

**Reference:** radon `:1756-1809` (subscribe-depth), `:387-392` (state maps), depth budget/LRU + single-focus, `:2225-2261` (event registration for `updateMktDepth`/`updateMktDepthL2`/`tickByTickAllLast`/`contractDetails`), `:1239` (`reqMktDepth(reqId, contract, numRows, isSmartDepth)`), `:1155` (`reqTickByTickData(id, contract, TickByTickDataType.AllLast, 0, false)`), `:1313-1338` (`hydrateAndBroadcastDepth`).

**Files:**

- Modify: `scripts/infra/ib_realtime/ib_realtime_server.js`
- Test: `web/tests/relay/depth_integration.test.ts` (simulated events → emitted WS frames)

- [ ] **Step 1a: Extend `parseActionMessage()`** (`:166-189`) — it currently returns only `{action, symbols, contracts, indexes}`, so the depth payload never reaches the handler. Add parsing + validation for `subscribe-depth` / `unsubscribe-depth` and their fields (`symbol, expiry?, strike?, right?, instrument?`); return them on the parsed object.

- [ ] **Step 1b: Add depth/tape state + subscribe handler** (port radon `:387-392`, `:1756-1809`). **Key everything by the DEPTH KEY, not the bare symbol** — for options the key is the composite `SYMBOL_YYYYMMDD_STRIKE_RIGHT` (matching the frontend `bookKey`), for stocks/futures it's the symbol. This keeps `subscribe` / `depth-batch` / `tape-batch` / `depth-unavailable` consistent with how `usePrices` keys `depths`/`tape` (ISSUE in 3c.1/3c.3).
  - State: `symbolDepthStates: Map<key,{depthTickerId, tapeTickerId, contract, kind, isFutures, ladders, trades, focusedAt}>`, `depthRequestIdToSymbol`, `tapeRequestIdToSymbol`, `depthSubscribers: Map<key,Set<ws>>`.
  - **Subscriber-aware — this relay multiplexes many clients through shared maps, so a global budget/focus is unsafe (one tab must not recycle another tab's book).** On `subscribe-depth`: `depthSubscribers.get(key).add(ws)`; only OPEN a new IB ticket when the key has no existing state (first subscriber). Ticket IDs come from the SAME monotonic allocator as L1 `reqMktData` (no ID collision).
  - **3-ticket budget over keys WITH ZERO SUBSCRIBERS only.** IB caps `reqMktDepth` at 3. When a 4th distinct key needs a ticket, evict the LRU key that has **no remaining subscribers**. If all 3 are actively watched, reject the 4th with `depth-unavailable{reason:"recycled"}` rather than stealing a live book.
  - `reqMktDepth(id, contract, 10, isSmartDepth)` (`isSmartDepth = !isFutures`) + `reqTickByTickData(tapeId, contract, TickByTickDataType.AllLast, 0, false)`.
  - On `unsubscribe-depth`: `depthSubscribers.get(key).delete(ws)`; **only when the set is empty** call `cancelMktDepth(id)` + `cancelTickByTickData(tapeId)` and clear state — mirroring the existing L1 last-subscriber teardown (`:835-877`). Cancelling on any unsubscribe would tear down other clients' books.
  - **Futures front-month bookkeeping:** dedicated req-id map + a 6s timeout + a **stale-focus discard** — if the focused key changed before `contractDetails` returns, drop the late response instead of hydrating a stale book (memoize the in-flight promise by root so concurrent subscribers don't double-request).

- [ ] **Step 1c: Extend reconnect restore for depth/tape** — the relay clears live ticker ids and calls `restoreSubscriptions()` on `EventName.connected` (`:1089`, `:1346`). Add a parallel depth/tape restore: on reconnect, clear all depth ladders + tape rings + depth/tape ticker-id maps, then re-open `reqMktDepth`/`reqTickByTickData` for **every key that still has subscribers** (emit `depth-unavailable{reason:"recycled"}` transiently while rebuilding). Without this, L2/tape silently die after any IB reconnect.

- [ ] **Step 2: Register depth/tape events** (port radon `:2228-2255`):

```js
ib.on(EventName.updateMktDepth, (id, position, operation, side, price, size) =>
  onDepthDelta(id, position, null, operation, side, price, size),
);
ib.on(
  EventName.updateMktDepthL2,
  (
    id,
    position,
    marketMaker,
    operation,
    side,
    price,
    size /*, isSmartDepth */,
  ) => onDepthDelta(id, position, marketMaker, operation, side, price, size),
);
ib.on(
  EventName.tickByTickAllLast,
  (reqId, _tickType, time, price, size, _attrib, exchange, _special) =>
    onTapeTick(reqId, time, price, size, exchange),
);
```

`onDepthDelta` resolves `key` via `depthRequestIdToSymbol`, calls `applyDepthDelta(state.ladders, …)` then `hydrateAndBroadcastDepth(key)`. `onTapeTick` resolves via `tapeRequestIdToSymbol`, `state.trades = appendTrade(state.trades, {price,size,exchange:exchange||null,time:isoFromUnix(time)})`, buffers a `tape-batch`.

- [ ] **Step 3: Hydrate + broadcast** (port radon `:1313-1338`): `serializeLadder` both sides → `DepthBook {symbol,kind,bid,ask,isSmartDepth,feed,entitled:true,timestamp,nbbo?}` → buffer to subscribers, flushed as `depth-batch` on a **separate** buffer/interval from L1 (own `BATCH_INTERVAL_MS`). Tape flushes as `tape-batch`.

- [ ] **Step 4: Entitlement fallback + robustness** — **Route by reqId, not a hardcoded code list.** When an IB error's `reqId` resolves via `depthRequestIdToSymbol` / `tapeRequestIdToSymbol`, treat it as a depth/tape-scoped failure: emit `{type:"depth-unavailable", symbol, reason, code}` to that symbol's subscribers, clear the ticket, and **never** flip `ibConnected`. Derive `reason` from known codes where possible (`"no-entitlement"` for 10089/2152, `"futures-no-depth"` for futures depth faults), else default `"no-entitlement"` — this avoids the brittleness of enumerating every IB market-data permission code. Plus two robustness additions:
  - **(a) Reset on reconnect** — IB depth deltas are positional and desync across a disconnect/reconnect, so a corrupt ladder would persist forever. The relay-side restore is specified in **Step 1c** (clear ladders/tape/ids, re-open every key that still has subscribers). This step is the _rationale_; Step 1c is the implementation.
  - **(b) Memoize in-flight front-month resolution.** Cache the pending `resolveFuturesFrontMonth(root)` promise by root so concurrent subscribers for the same future (e.g. two tabs on ES) don't fire redundant `reqContractDetails`.

- [ ] **Step 5: Integration test** (simulated, no live IB)

```ts
// web/tests/relay/depth_integration.test.ts — drive the exported pure handlers
import { describe, it, expect } from "vitest";
import {
  applyDepthDelta,
  serializeLadder,
} from "../../../scripts/infra/ib_realtime/depth_book.js";

describe("depth backend integration (pure path)", () => {
  it("a sequence of deltas serializes to a sorted DepthBook side", () => {
    const L = { bid: [], ask: [] };
    applyDepthDelta(L, 0, "A", 0, 1, 100.5, 300);
    applyDepthDelta(L, 1, "B", 0, 1, 100.4, 500);
    const book = serializeLadder(L.bid, false, "stock", "bid");
    expect(book.map((r) => r.price)).toEqual([100.5, 100.4]);
    expect(book[0]).toMatchObject({ size: 300, exchange: "A" });
  });
});
```

(Full WS-emission wiring is covered by the Phase-3b live E2E; the pure handlers carry the unit coverage.)

- [ ] **Step 6: Run + commit**

Run: `cd web && npx vitest run tests/relay/`

```bash
git add scripts/infra/ib_realtime/ib_realtime_server.js web/tests/relay/depth_integration.test.ts
git commit -m "feat(relay): depth+tape subscription, budget/LRU, additive WS messages (phase 3b)"
```

---

# PHASE 3C — Frontend book + tape + click-to-fill

### Task 3c.1: Protocol types

**Reference:** radon `web/lib/pricesProtocol.ts:106-183`.

**Files:**

- Modify: `web/lib/pricesProtocol.ts` (append after `PriceData`; extend `WSMessage` union at `:95-105`)

- [ ] **Step 1: Add the types** (port radon `:106-183`):

```ts
export type DepthLevel = {
  price: number;
  size: number;
  marketMaker: string | null;
  exchange: string | null;
  nbbo?: boolean; // options only: sets NBBO inside
};
export type DepthNbbo = {
  bestBid: number | null;
  bestAsk: number | null;
  mid: number | null;
  bidSize: number | null;
  askSize: number | null;
};
export type DepthBook = {
  symbol: string;
  kind: "stock" | "option" | "future";
  bid: DepthLevel[];
  ask: DepthLevel[];
  isSmartDepth: boolean;
  feed: string | null;
  entitled: boolean;
  nbbo?: DepthNbbo;
  timestamp: string;
};
export type Trade = {
  price: number;
  size: number;
  exchange: string | null;
  time: string;
};

export type WSDepthBatchMessage = {
  type: "depth-batch";
  updates: Record<string, DepthBook>;
};
export type WSTapeBatchMessage = {
  type: "tape-batch";
  updates: Record<string, Trade[]>;
};
export type WSDepthUnavailableMessage = {
  type: "depth-unavailable";
  symbol: string;
  reason: "no-entitlement" | "futures-no-depth" | "recycled";
  code?: number;
};
```

Add the three to the `WSMessage` union.

**⚠️ Depth-key consistency (Codex ISSUE-10).** The `Record<string, …>` keys in `depth-batch`/`tape-batch`, and the `symbol` field in `depth-unavailable` + `DepthBook.symbol`, must all carry the **depth key** — the composite `SYMBOL_YYYYMMDD_STRIKE_RIGHT` for options, the bare symbol for stocks/futures — i.e. the same key `usePrices` uses for its `depths`/`tape` state (`bookKey`). Using the bare underlying symbol for an option would clear/mark the wrong entry. Document this on the types (e.g. rename the field intent to "depth key" in a doc comment) so backend and frontend never drift.

- [ ] **Step 2: Typecheck** — `cd web && npm run typecheck` → PASS.
- [ ] **Step 3: Commit** — `feat(web): L2 depth + tape protocol types (phase 3c)`

### Task 3c.2: `depthDerivations.ts` (near-verbatim port)

**Reference:** radon `web/lib/book/depthDerivations.ts` (entire file — pure, no adaptation beyond the `@/lib/pricesProtocol` import which is identical).

**Files:**

- Create: `web/lib/book/depthDerivations.ts`
- Test: `web/tests/depthDerivations.test.ts`

- [ ] **Step 1: Write failing tests** (cover each pure function)

```ts
import { describe, it, expect } from "vitest";
import {
  groupPriceLevels,
  montageFill,
  buildLadderRows,
  classifyTicks,
  isBestLevel,
  deriveBookHeader,
} from "@/lib/book/depthDerivations";
import type { DepthLevel } from "@/lib/pricesProtocol";

const lvl = (
  price: number,
  size: number,
  mm: string | null = null,
): DepthLevel => ({ price, size, marketMaker: mm, exchange: mm });

describe("depthDerivations", () => {
  it("groupPriceLevels flags first row of each distinct price", () => {
    const r = groupPriceLevels([lvl(100, 1), lvl(100, 2), lvl(99, 3)]);
    expect(r.map((x) => x.firstOfLevel)).toEqual([true, false, true]);
  });
  it("montageFill is size/maxSize, 0 when maxSize<=0", () => {
    expect(montageFill(lvl(100, 50), 100)).toBe(0.5);
    expect(montageFill(lvl(100, 50), 0)).toBe(0);
  });
  it("buildLadderRows pads to fixed rows per side, best adjacent to spine", () => {
    const { askRows, bidRows } = buildLadderRows(
      { bid: [lvl(99, 10)], ask: [lvl(101, 20)] },
      3,
    );
    expect(askRows.length).toBe(3);
    expect(bidRows.length).toBe(3);
    expect(askRows[askRows.length - 1].level?.price).toBe(101); // best ask just above spine
    expect(bidRows[0].level?.price).toBe(99); // best bid just below spine
  });
  it("classifyTicks applies the tick test (first is flat)", () => {
    const t = classifyTicks([
      { price: 10, size: 1, exchange: null, time: "1" },
      { price: 11, size: 1, exchange: null, time: "2" },
      { price: 11, size: 1, exchange: null, time: "3" },
    ]);
    expect(t.map((x) => x.tone)).toEqual(["flat", "up", "flat"]);
  });
  it("isBestLevel: index 0 for stock/future, nbbo flag for option", () => {
    expect(isBestLevel(lvl(100, 1), 0, "stock")).toBe(true);
    expect(isBestLevel({ ...lvl(100, 1), nbbo: true }, 3, "option")).toBe(true);
  });
  it("deriveBookHeader falls back to L1 when no entitled book", () => {
    const h = deriveBookHeader(null, {
      bid: 1,
      ask: 2,
      last: 1.5,
      lastLabel: "LAST",
    });
    expect(h).toEqual({ bid: 1, ask: 2, last: 1.5, lastLabel: "LAST" });
  });
});
```

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Port radon's file verbatim** to `web/lib/book/depthDerivations.ts` (it already matches the types from Task 3c.1).
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** — `feat(web): pure L2 depth derivations ported from radon (phase 3c)`

### Task 3c.3: `usePrices` depth/tape subscription

**Reference:** radon `web/lib/usePrices.ts:43-68` (params/state), `:113,122-123` (defaults), `:351-427` (`syncDepth`), `:551-606` (message handlers).

**Files:**

- Modify: `web/lib/usePrices.ts`
- Test: `web/tests/usePrices.depth.test.ts`

- [ ] **Step 1: Failing test** — feed a fake `depth-batch`/`tape-batch`/`depth-unavailable` through the message handler and assert `depths`/`tape` state. (Mirror the existing `usePrices` test harness; if none, test the reducer logic by extracting `applyWsMessage(state, msg)` as an exported pure helper and unit-test that.)

```ts
import { describe, it, expect } from "vitest";
import { applyDepthMessage } from "@/lib/usePrices"; // pure helper extracted in step 3
import type { DepthBook } from "@/lib/pricesProtocol";

const book = (s: string): DepthBook => ({
  symbol: s,
  kind: "stock",
  bid: [],
  ask: [],
  isSmartDepth: true,
  feed: "SMART",
  entitled: true,
  timestamp: "t",
});

describe("usePrices depth message handling", () => {
  it("depth-batch merges by symbol", () => {
    const s = applyDepthMessage(
      { depths: {}, tape: {} },
      { type: "depth-batch", updates: { QQQ: book("QQQ") } },
    );
    expect(s.depths.QQQ.entitled).toBe(true);
  });
  it("depth-unavailable(no-entitlement) writes an unentitled shell", () => {
    const s = applyDepthMessage(
      { depths: {}, tape: {} },
      { type: "depth-unavailable", symbol: "QQQ", reason: "no-entitlement" },
    );
    expect(s.depths.QQQ.entitled).toBe(false);
  });
  it("depth-unavailable(recycled) drops the stale book", () => {
    const s = applyDepthMessage(
      { depths: { QQQ: book("QQQ") }, tape: {} },
      { type: "depth-unavailable", symbol: "QQQ", reason: "recycled" },
    );
    expect(s.depths.QQQ).toBeUndefined();
  });
  it("tape-batch replaces (newest-last, bounded)", () => {
    const s = applyDepthMessage(
      { depths: {}, tape: {} },
      {
        type: "tape-batch",
        updates: { QQQ: [{ price: 1, size: 1, exchange: null, time: "1" }] },
      },
    );
    expect(s.tape.QQQ.length).toBe(1);
  });
});
```

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** — add `depthSymbol` param, `depths`/`tape` state, `syncDepth` effect (sends `subscribe-depth`/`unsubscribe-depth` for the single focused symbol; option keys carry `expiry/strike/right`), and the message handlers. **Extract** `applyDepthMessage(state, msg)` as an exported pure reducer (depth-batch merge, tape-batch replace-bounded, depth-unavailable recycle/shell) so it's unit-testable — port the body from radon `:551-606`.
  - **⚠️ Debounce focus changes (~250-300ms).** `reqMktDepth` is rate-limited by IB; rapid focus switching (tab-hopping the watchlist) would fire a cancel/re-subscribe storm and trip an IB pacing violation. Debounce `depthSymbol` before `syncDepth` sends `subscribe-depth`, so only a settled focus opens a ticket. (radon relies on single-focus; xenon must add the debounce since the cockpit makes focus changes cheap.)
- [ ] **Step 4: Run → pass.** + typecheck.
- [ ] **Step 5: Commit** — `feat(web): usePrices L2 depth/tape subscription + reducer (phase 3c)`

### Task 3c.4: Book components (OrderBook + DepthMontage + TimeAndSales) — ⛔ `LadderDOM` DEFERRED (futures out of scope)

**Reference:** radon `OrderBook.tsx:1-144`, `DepthMontage.tsx:1-112`, `LadderDOM.tsx:1-108`, `TimeAndSales.tsx:1-86`.

**Adaptations (all four):** swap radon import aliases to xenon's (`@/lib/pricesProtocol`, `@/lib/book/depthDerivations` — identical paths); use xenon brand tokens/classnames already present in `BookTab.tsx` (`var(--font-mono)`, `--text-secondary`, etc.); keep `onPriceClick(p: OrderPrefill-without-nonce)` signature.

**⚠️ CSS port (do not skip — components render unstyled otherwise).** radon's book styling lives in `radon/web/app/globals.css` (~`:5712-6960`): `.book-window`, `.book-window-head`, `.book-body-grid`(+`.tape-hidden`), `.book-montage`, `.book-tape-cell`, the montage depth-fill bars, the ladder spine, and tape-row rules. Port these into xenon `web/app/globals.css`, converting any radon raw hex to xenon brand tokens and enforcing `brand/CLAUDE.md` (≤4px radius, mono for machine, no gradients/soft shadows). Add this as Step 0 of this task so the component tests render against real styles.

**Files:**

- Create: `web/components/ticker-detail/OrderBook.tsx`, `DepthMontage.tsx`, `LadderDOM.tsx`, `TimeAndSales.tsx`
- Test: `web/tests/DepthMontage.test.tsx`, `web/tests/LadderDOM.test.tsx`, `web/tests/TimeAndSales.test.tsx`

- [ ] **Step 1: Failing component tests** (one per component; example for montage):

```tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import DepthMontage from "@/components/ticker-detail/DepthMontage";
import type { DepthBook } from "@/lib/pricesProtocol";

const book: DepthBook = {
  symbol: "QQQ",
  kind: "stock",
  isSmartDepth: true,
  feed: "SMART",
  entitled: true,
  timestamp: "t",
  bid: [{ price: 500.1, size: 300, marketMaker: "ARCA", exchange: "ARCA" }],
  ask: [{ price: 500.2, size: 200, marketMaker: "NSDQ", exchange: "NSDQ" }],
};

describe("DepthMontage", () => {
  it("renders bid and ask rows", () => {
    render(<DepthMontage book={book} onPriceClick={() => {}} />);
    expect(screen.getByText("500.10")).toBeInTheDocument();
    expect(screen.getByText("500.20")).toBeInTheDocument();
  });
  it("clicking a bid level fills SELL at that price", () => {
    const onClick = vi.fn();
    render(<DepthMontage book={book} onPriceClick={onClick} />);
    fireEvent.click(screen.getByText("500.10"));
    expect(onClick).toHaveBeenCalledWith(
      expect.objectContaining({
        price: 500.1,
        action: "SELL",
        source: "montage",
      }),
    );
  });
});
```

(LadderDOM: assert centered ask-above/bid-below + click ask→BUY, bid→SELL. TimeAndSales: assert tick-test tone classes + click uses tone→action.)

- [ ] **Step 2: Run → fail (modules missing).**
- [ ] **Step 3: Port the four components** from radon with the adaptations above. `OrderBook.tsx` dispatches `kind==="future" ? <LadderDOM> : <DepthMontage>` and owns the tape toggle; head uses `deriveBookHeader`.
- [ ] **Step 4: Run → pass.** + `npm run typecheck`.
- [ ] **Step 5: Commit** — `feat(web): port L2 book + tape components from radon (phase 3c)`

### Task 3c.5: BookTab consumes depth/tape + L1 fallback

**Reference:** radon `BookTab.tsx:49-144`, `:108` (bookOnly), `:386-393` (resolve book key/tape from props).

**Files:**

- Modify: `web/components/ticker-detail/BookTab.tsx`
- Test: `web/tests/BookTab.bookOnly.test.tsx` (extend existing)

- [ ] **Step 1: Failing test additions**

```tsx
// when an entitled DepthBook is present, bookOnly renders OrderBook (not bare L1)
it("bookOnly renders the L2 OrderBook when an entitled depth book is present", () => {
  render(
    <BookTab
      {...baseProps}
      bookOnly
      depths={{ [baseProps.bookKey!]: entitledBook }}
    />,
  );
  expect(screen.getByTestId("order-book")).toBeInTheDocument();
});
// falls back to L1 when entitled:false
it("bookOnly falls back to L1 when depth is unentitled", () => {
  render(
    <BookTab
      {...baseProps}
      bookOnly
      depths={{ [baseProps.bookKey!]: { ...entitledBook, entitled: false } }}
    />,
  );
  expect(screen.getByText("ORDER BOOK")).toBeInTheDocument(); // existing L1 header
});
```

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** — destructure the (currently no-op) `depths`/`tape`/`onPriceClick`/`bookKey`/`bookKind`; in `bookOnly` mode resolve `const book = bookKey ? depths?.[bookKey] : null` and render `<OrderBook book={book as DepthBook | null} tape={bookKey ? tape?.[bookKey] : []} kind={bookKind} onPriceClick={onPriceClick} l1Fallback={<L1OrderBook .../>} />`; OrderBook renders L1 fallback when `!book || book.entitled !== true`. Non-bookOnly path unchanged. Add `data-testid="order-book"` to OrderBook root.
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** — `feat(web): BookTab renders L2 book with L1 fallback (phase 3c)`

### Task 3c.6: `OrderPrefill` in TickerDetailContext

**Reference:** radon `web/lib/TickerDetailContext.tsx:15-20` (type), `:55-57,69,118-120` (state + monotonic-nonce setter).

**Files:**

- Modify: `web/lib/TickerDetailContext.tsx`
- Test: `web/tests/TickerDetailContext.prefill.test.tsx`

- [ ] **Step 1: Failing test**

```tsx
import { describe, it, expect } from "vitest";
import { renderHook, act } from "@testing-library/react";
import {
  TickerDetailProvider,
  useTickerDetail,
} from "@/lib/TickerDetailContext";

it("setOrderPrefill stamps a monotonic nonce", () => {
  const { result } = renderHook(() => useTickerDetail(), {
    wrapper: TickerDetailProvider,
  });
  act(() =>
    result.current.setOrderPrefill({
      price: 100,
      action: "BUY",
      source: "montage",
    }),
  );
  const n1 = result.current.orderPrefill!.nonce;
  act(() =>
    result.current.setOrderPrefill({
      price: 100,
      action: "BUY",
      source: "montage",
    }),
  );
  expect(result.current.orderPrefill!.nonce).toBeGreaterThan(n1);
});
```

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** — add `OrderPrefill` type (`{price, action?, quantity?, source:"montage"|"ladder"|"tape", nonce}`), `orderPrefill` state, and `setOrderPrefill: (p: Omit<OrderPrefill,"nonce">) => void` that stamps a monotonic nonce from a `useRef`. Add both to the context value object (`:72-74`). Port radon `:15-20,118-120`.
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** — `feat(web): OrderPrefill click-to-fill channel in TickerDetailContext (phase 3c)`

### Task 3c.7: Thread depth/tape + wire onBookPriceClick

**Files:**

- Modify: `web/lib/usePrices.ts` consumer (find via `grep -rn "usePrices(" web/components web/app`) — pass `depthSymbol={bookKey}`, thread `depths`/`tape` down to `TickerDetailContent`.
- Modify: `web/components/TickerDetailContent.tsx` (`:108-125`) — accept + forward `depths`/`tape`.
  - **✅ Futures reachability — RESOLVED (2026-06-16): deferred.** `TickerDetailContent.tsx:75-80` only returns `"stock"|"option"`; nothing sets `bookKind="future"`. Per the Scope Decision, the futures branch is OUT of scope this round — `bookKind` stays `"stock"|"option"`, no `LadderDOM` wiring. Leave a one-line `// TODO(futures): set bookKind="future" once portfolio surfaces FUT positions` and move on.
- Modify: `web/components/ticker-detail/AssetCockpit.tsx` (`:14-31` props, `:78-91` book region) — add `depths`/`tape` props; add `onBookPriceClick`; pass to `<BookTab>`.

- [ ] **Step 1: Extend `AssetCockpitProps` + book region**

```tsx
// add to AssetCockpitProps:
depths: Record<string, import("@/lib/pricesProtocol").DepthBook>;
tape: Record<string, import("@/lib/pricesProtocol").Trade[]>;

// inside the component (replace the TODO at :61-64):
const { setOrderPrefill } = useTickerDetail();
const onBookPriceClick = useCallback(
  (p: Omit<import("@/lib/TickerDetailContext").OrderPrefill, "nonce">) => {
    setOrderPrefill(p);
    if (mobile) onDeckChange("o"); // open the order ticket deck on phones
  },
  [setOrderPrefill, mobile, onDeckChange],
);

// in the <BookTab .../> at :80-90 add:
depths = { depths };
tape = { tape };
onPriceClick = { onBookPriceClick };
```

- [ ] **Step 2: Forward through `TickerDetailContent`** — accept `depths`/`tape` props (from the usePrices consumer) and pass to `<AssetCockpit>` (`:109-124`).
- [ ] **Step 3: Set focus — ⚠️ resolve the location dependency first.** The only `usePrices()` callsite is `web/components/WorkspaceShell.tsx:233`, but `bookKey` is computed _downstream_ inside `TickerDetailContent` (`:70`), so you cannot just pass `depthSymbol={bookKey}` at the callsite. Pick one and add it as an explicit sub-task:
  - **(i)** lift the quote/`bookKey` resolution (`resolveTickerQuote` → `bookKey`) up into `WorkspaceShell` so `depthSymbol` is available where `usePrices` is called, then pass the resolved `priceData`/`bookKey` down to `TickerDetailContent` as props; **or**
  - **(ii)** publish the focused book key back up via `TickerDetailContext` (a `focusedBookKey` state that `TickerDetailContent` sets and `WorkspaceShell` reads to feed `usePrices`).
    Option (i) is cleaner (single source of truth, no context round-trip). Whichever is chosen, debounce per Task 3c.3.
- [ ] **Step 4: Verify** — `cd web && npm run typecheck` → PASS.
- [ ] **Step 5: Commit** — `feat(web): thread depth/tape + click-to-fill through the cockpit (phase 3c)`

### Task 3c.8: Order form consumes the prefill

**Files:**

- Modify: `web/components/ticker-detail/OrderTab.tsx` (and the stock branch in `BookTab.tsx`'s `StockOrderForm` if the Act-column order entry routes there)
- Test: `web/tests/OrderTab.prefill.test.tsx`

- [ ] **Step 1: Failing test** — render `OrderTab` inside the provider, call `setOrderPrefill({price, action, source})`, assert the limit-price input shows the price and the action toggles.

```tsx
it("prefills limit price + action from a depth click", () => {
  const { result } = ...; // provider + render OrderTab
  act(() => result.current.setOrderPrefill({ price: 123.45, action: "BUY", source: "ladder" }));
  expect(screen.getByDisplayValue("123.45")).toBeInTheDocument();
});
```

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** — `const { orderPrefill } = useTickerDetail();` + `useEffect(() => { if (!orderPrefill) return; setLimitPrice(orderPrefill.price.toFixed(2)); if (orderPrefill.action) setAction(orderPrefill.action); if (orderPrefill.quantity) setQuantity(String(orderPrefill.quantity)); }, [orderPrefill?.nonce]);` (key on nonce). Reference radon's ticket consumer.
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** — `feat(web): order ticket consumes click-to-fill prefill (phase 3c)`

### Task 3c.9: Frontend regression + E2E

**Files:** none (verification)

- [ ] **Step 1: Full unit suite** — `cd web && npm test` → all green (incl. new depth/tape/derivations/prefill tests).
- [ ] **Step 2: Typecheck + lint** — `npm run typecheck && npm run lint` → clean.
- [ ] **Step 3: Browser E2E (chrome-cdp)** — boot `scripts/infra/dev.sh paper` (or `live` read-only); open cockpit on a liquid stock (QQQ); confirm the L2 montage populates (multiple bid/ask rows), the tape streams prints; click a bid level → Act-column order ticket limit price prefilled + action SELL. Screenshot → `docs/plans/phase3c-l2-clickfill.png`.
- [ ] **Step 4: Commit** — `test(web): phase 3c L2 book + click-to-fill E2E verified`

---

## Self-Review

**Spec coverage** — every Section-2 component maps to a task: relay migration (3a.1-3a.6), depth_book/tape_feed/depth_contracts (3b.1-3b.3), subscribe/budget/events (3b.4), protocol types (3c.1), derivations (3c.2), usePrices (3c.3), components (3c.4), BookTab (3c.5), context prefill (3c.6), threading (3c.7), ticket consumption (3c.8), E2E (3c.9). WS-URL invariant (3a.5) + L1 parity gate (3a.6) cover Section-1 phasing. Instrument parity: stock/option montage (3c.4 DepthMontage) + futures ladder (3c.4 LadderDOM + 3b.3 front-month). ✅

**Type consistency** — `DepthLevel`/`DepthBook`/`Trade`/`DepthNbbo` defined in 3c.1, consumed identically in 3b (serialize shape), 3c.2 (derivations), 3c.4/3c.5 (components). `OrderPrefill` defined in 3c.6, consumed in 3c.7 (`onBookPriceClick`) + 3c.8 (ticket). `applyDepthDelta(ladders, position, marketMaker, operation, side, price, size)` signature consistent across 3b.1 test + 3b.4 wiring. ✅

**Placeholder scan** — port tasks intentionally bind to radon `file:line` per the stated convention (not placeholders); all tests + migration edits + glue are inline. Two verification-time lookups are explicit steps (relay test convention in 3b Step 0; usePrices consumer in 3c.7) rather than guesses. ✅

**Risk note** — the highest-risk change (error/info split, 3a.4) has the heaviest unit coverage; the live-system risk (prod Docker) is an explicit exit-gate step (3a.6 Step 4).
