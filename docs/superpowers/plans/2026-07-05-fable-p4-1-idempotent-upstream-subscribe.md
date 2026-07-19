# P4.1 — Idempotent upstream subscribe (QS-4)

- **Date:** 2026-07-05
- **Branch:** `fix/p4-1-idempotent-upstream-subscribe`
- **Finding:** QS-4 (Medium) — `startLiveSubscription` always cancels + re-issues
  `reqMktData` for a symbol, so every tab refresh / socket reconnect churns IB tickets for
  symbols other tabs already stream.
- **Goal:** When a symbol already holds a live IB line with the same contract, attaching a
  new client subscriber must NOT cancel + re-request upstream. Reconnect churn → 0 redundant
  `reqMktData` for already-live symbols.
- **Acceptance (roadmap):** reconnect-churn workload (08 §8.4-4) shows 0 redundant
  `reqMktData` for already-live symbols.

## Re-verify preamble (MANDATORY — executes after S7 + P1.2 reshape the relay)

S7 (QS-1/SEC-2) rewrites `sendMessage`/`flushBatches` into a bounded `sendBounded` path and
adds an Origin allowlist to the upgrade handler; P1.2 adds `seq`/`relay_ts` to the batch
message and a `/status` metrics body. **Neither touches `startLiveSubscription` or the
subscribe handler this plan edits** — but confirm the anchors still exist at HEAD before
editing:

```bash
cd scripts/infra/ib_realtime
grep -n "function startLiveSubscription" ib_realtime_server.js         # ~838
grep -n "ib.reqMktData(nextTickerId, ibContract" ib_realtime_server.js # ~860
grep -n "function subscribeClientToSymbol" ib_realtime_server.js       # ~926
```

If `startLiveSubscription` no longer begins by cancelling `state.tickerId` (snippet below),
**STOP** — the churn this plan removes was already fixed and the anchor is wrong.

## Key facts (verified at HEAD)

- `scripts/infra/ib_realtime/ib_realtime_server.js` — the relay (single file, ~2256 ln).
- `startLiveSubscription(key, ibContract)` currently ALWAYS tears the existing line down:

  ```js
  if (state.tickerId != null) {
    try { ib.cancelMktData(state.tickerId); } catch { /* Ignore. */ }
    requestIdToSymbol.delete(state.tickerId);
    state.tickerId = null; // free the slot before the budget check re-counts
  }
  makeRoomForLine(key);
  try {
    ib.reqMktData(nextTickerId, ibContract, "233,165", false, false);
    ...
  ```

- The subscribe handler (`case "subscribe":` ~1889) calls `subscribeClientToSymbol(client,
symbol)` (attach — pure map bookkeeping, ~926) and THEN `startLiveSubscription(symbol,
ibContract)` for every symbol on every fresh socket. That second call is the churn source.
- Contract identity: the relay builds contracts via `stockContract`, `optionContract`,
  `indexContract`, `forexContract`. A stable equality key is `conId` when present, else the
  tuple `(secType, symbol, lastTradeDateOrContractMonth, strike, right, exchange, currency)`.
- `state.lastAccessAt` is the LRU heat signal read by `pickEvictable`/`pickAdmittable`
  (`line_budget.js`). Bumping it on a no-op re-subscribe is correct (a fresh subscriber IS an
  access) and must be preserved.
- Existing `.mjs` unit tests run via `node --test` and live in
  `scripts/infra/ib_realtime/__tests__/*.test.mjs` (pure-module tests only today).
- `line_budget.js` exports `pickEvictable`, `pickAdmittable` (pure, unit-tested).

## Non-goals (NOT fixed here — one change, one PR)

- QS-1 backpressure / QS-7 priority cap / QS-2 relay CI harness / QS-3 seq — separate P4.x
  and S7 items.
- Do NOT change the LRU eviction policy. Do NOT change contract-building functions.

## Steps (TDD)

### Step 1 — Extract a pure contract-identity helper (unit-testable)

Add near the contract builders (search `function stockContract`), a pure function:

```js
// Stable identity for "is this the SAME upstream contract we already stream?"
// conId is authoritative once qualified; fall back to the defining tuple otherwise.
function contractIdentity(c) {
  if (c && c.conId) return `conid:${c.conId}`;
  if (!c) return "none";
  return [
    c.secType,
    c.symbol,
    c.lastTradeDateOrContractMonth ?? "",
    c.strike ?? "",
    c.right ?? "",
    c.exchange ?? "",
    c.currency ?? "",
  ].join("|");
}
```

### Step 2 — Idempotent fast path in `startLiveSubscription`

Replace the unconditional teardown block. Anchor on the exact existing lines above.

```js
function startLiveSubscription(key, ibContract) {
  if (!ibConnected) return;

  const existing = ensureSymbolState(key, ibContract);
  const state = existing;
  state.lastAccessAt = Date.now(); // subscribe counts as access (LRU heat)

  // Idempotent upstream subscribe (QS-4): a live line for the SAME contract needs
  // no cancel + re-req — a new client subscriber just attaches. Bump heat + return.
  if (
    state.tickerId != null &&
    contractIdentity(state.contract) === contractIdentity(ibContract)
  ) {
    return;
  }

  const nextTickerId = (nextRequestId += 1);
  if (state.tickerId != null) {
    try { ib.cancelMktData(state.tickerId); } catch { /* Ignore. */ }
    requestIdToSymbol.delete(state.tickerId);
    state.tickerId = null; // free the slot before the budget check re-counts
  }
  makeRoomForLine(key);
  try {
    ib.reqMktData(nextTickerId, ibContract, "233,165", false, false);
    ...  // unchanged remainder
```

Leave everything after `makeRoomForLine(key)` unchanged.

### Step 3 — Unit test the identity helper

`scripts/infra/ib_realtime/__tests__/contract_identity.test.mjs` — but `contractIdentity`
is module-private in the relay. Export it: add to the relay's existing export surface only
if one exists; otherwise test the behavior through the relay integration harness (P4.3). To
keep this PR self-contained WITHOUT the P4.3 harness, move `contractIdentity` into
`ib_contracts.js` (already an extracted pure module — `grep -n "export" ib_contracts.js`)
and import it in the relay. Then:

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { contractIdentity } from "../ib_contracts.js";

test("same conId → identical identity", () => {
  assert.equal(
    contractIdentity({ conId: 265598 }),
    contractIdentity({ conId: 265598 }),
  );
});
test("differing strike → different identity", () => {
  const base = {
    secType: "OPT",
    symbol: "AAPL",
    lastTradeDateOrContractMonth: "20260117",
    strike: 200,
    right: "C",
    exchange: "SMART",
    currency: "USD",
  };
  assert.notEqual(
    contractIdentity(base),
    contractIdentity({ ...base, strike: 210 }),
  );
});
```

## Verification matrix

| Check                                             | Command                                                                                                                                                      | Expected                                                                                        |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------- |
| Unit (identity)                                   | `cd scripts/infra/ib_realtime && node --test __tests__/contract_identity.test.mjs`                                                                           | `pass 2 / fail 0`                                                                               |
| Existing mjs still green                          | `cd scripts/infra/ib_realtime && node --test __tests__/*.test.mjs`                                                                                           | `fail 0`                                                                                        |
| Relay boots                                       | `cd web && node ../scripts/infra/ib_realtime/ib_realtime_server.js --port 8899` (Ctrl-C)                                                                     | listening log, no throw                                                                         |
| Manual churn probe (paper)                        | `scripts/infra/dev.sh paper`; open two browser tabs on :3200; in relay `--verbose` logs, `grep 'reqMktData\|subscribe' ` for AAPL across the 2nd tab connect | 2nd tab logs `subscribe` attach but NO new `reqMktData` for symbols the 1st tab already streams |
| Regression: fresh symbol still subscribes         | first tab subscribes a symbol no tab held                                                                                                                    | exactly one `reqMktData` line for it                                                            |
| Web typecheck (if ib_contracts.js typed via d.ts) | n/a — pure JS                                                                                                                                                | —                                                                                               |

## Tripwires / abort

- STOP if `startLiveSubscription` at HEAD no longer unconditionally cancels — QS-4 already fixed.
- STOP if `ib_contracts.js` is NOT already an importable ES module (`grep export`) — do not
  create a new module graph; instead keep `contractIdentity` in the relay and defer its unit
  test to the P4.3 harness, noting that in the PR description.
- Paper only. Never live IB. Never leave a resting order (this plan places none).
- If more than 2 files change (relay + ib_contracts.js + one test = 3 is the ceiling), STOP.

## Rollback

`git checkout master -- scripts/infra/ib_realtime/` and delete the new test file, or discard
the branch. No schema, no migration, no frontend change.
