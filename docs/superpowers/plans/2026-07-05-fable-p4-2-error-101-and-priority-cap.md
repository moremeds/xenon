# P4.2 — Explicit error-101 handling + subscription cap with priority (QS-7)

- **Date:** 2026-07-05
- **Branch:** `feat/p4-2-line-budget-priority`
- **Finding:** QS-7 (Medium) — ~100 market-data lines/account shared across all gateway
  clients; relay must handle error-101 explicitly AND cap + prioritize subscriptions
  (portfolio > focused ticker > rest).
- **Goal:** A synthetic error-101 marks the affected symbol degraded and logs loudly; when
  the relay is over the line budget it evicts the lowest-priority line first, and a
  low-priority incoming subscribe is REJECTED (marked stale + logged) rather than evicting a
  higher-priority line.
- **Acceptance (roadmap):** synthetic 101 marks affected symbols degraded and logs loudly.
- **Hard prerequisite:** **P4.3 merges FIRST.** P4.3 delivers the relay CI test lane
  (`node --test` on `scripts/infra/ib_realtime/__tests__/*.test.mjs` in the web-tests job).
  This plan's regression pins run in that lane; without it the 101 branch stays CI-dark.

## Drift from review (READ FIRST)

The fable finding says the relay "has no error-101 branch (`:1855-1956`)". **This is stale.**
At HEAD the error handler ALREADY has an explicit `code === 101 || /Max number of
tickers/i` branch (`ib.on(EventName.error, ...)`, ~line 2287) that clears the dead
`tickerId`, calls `hydrateAndBroadcast` (pushing `stale:true`), and logs a yellow warning.
The July-2 stale-`last` fix added it. So the roadmap acceptance ("synthetic 101 marks
affected symbols degraded and logs loudly") is **already met in code but untested in CI.**

Therefore this plan's real deliverables are:

1. A **CI regression pin** on the existing 101 → stale + loud-log behavior.
2. The **missing** half: a **priority-aware line budget** — eviction prefers lower-priority
   lines, and admission is refused (never "evict a portfolio leg for a hover") when every
   live line outranks the incoming symbol. Portfolio/open-order legs > focused-ticker
   chain > everything else.

## Re-verify preamble (MANDATORY — executes after S7 + P1.2 + P4.3)

S7 rewrites the send path; P1.2 adds `/status` metrics + `seq`; P4.3 extracts the batched
channel and adds the CI mjs lane. None of them touch the error handler or `line_budget.js`.
Confirm anchors at HEAD:

```bash
cd scripts/infra/ib_realtime
grep -n "code === 101 || /Max number of tickers/i.test(msg)" ib_realtime_server.js  # ~2287
grep -n "export function pickEvictable" line_budget.js                              # ~23
grep -n "function makeRoomForLine" ib_realtime_server.js                            # ~1473
grep -n "node --test" ../../../.github/workflows/ci.yml                             # P4.3 lane
```

- If the `code === 101` branch is GONE, STOP — someone reshaped the error handler; re-plan.
- If the `node --test` CI step is absent, STOP — the P4.3 prerequisite has not merged.

## Key facts (verified at HEAD)

- Error handler branch already present (`ib_realtime_server.js` ~2287-2304): on `code===101`
  it does `requestIdToSymbol.delete(tickerId); state.tickerId = null; hydrateAndBroadcast(symbol);`
  and `console.warn("...market-data line cap reached...symbol will read stale")`.
- `computeStale(state)` returns `true` when `state.tickerId == null` — so a 101-cleared line
  reads `stale:true` to clients. Verified (`~1409`).
- `line_budget.js` — `pickEvictable(entries, exceptKey)` picks the OLDEST `lastAccessAt` live
  line; `pickAdmittable(entries)` picks the NEWEST idle. Pure module.
- `entries` are `{ key, tickerId, lastAccessAt }` built by `lineEntries()` (`~1446`).
- **`makeRoomForLine(incomingKey)` (~1473-1479) always evicts whatever `pickEvictable`
  returns** — it has no notion of "the victim outranks the incoming symbol". And
  `startLiveSubscription` (~838) **ignores `makeRoomForLine`'s return value** and calls
  `ib.reqMktData` regardless. Both call sites change in this plan.
- No `priority` field exists on symbol state today. The relay subscribe handler
  (`case "subscribe":` ~1889) iterates `message.contracts` per contract `c` (fields
  `symbol/expiry/strike/right`) — no source tier arrives.
- **Frontend contract merge loses the tier**: `web/components/WorkspaceShell.tsx` (~234-242)
  builds ONE `allContracts` array via `uniqueOptionContracts([...portfolioContracts,
...orderContracts, ...tickerDetail.chainContracts])` and passes it as `contracts:` to
  `usePrices` (~254). A server-side heuristic therefore CANNOT distinguish a portfolio leg
  from a ticker-detail chain contract — the tier must be tagged client-side at the merge
  site, where the source is still known.
- Existing repo pattern for CI-pinning inline relay branches: source-assertion Vitest suites
  that read `ib_realtime_server.js` as text (see `web/tests/ib-realtime-restart-modes.test.ts`).

## Non-goals

- QS-1 backpressure, QS-4 idempotency (P4.1), QS-3 seq — not here.
- Do NOT raise `MAX_CONCURRENT_LINES` or change the ~85 default.
- Do NOT build a server-only tier heuristic — verified impossible (merged contracts payload,
  Key Facts above). The tier travels in the subscribe message.

## Steps (TDD)

### Step 1 — CI-pin the existing 101 → degraded behavior

Extract the "line died → mark stale" bookkeeping into a named helper so the branch body is
one greppable call. Add to the relay near `evictLine`:

```js
// Clear a symbol's live IB line and push stale:true to its subscribers. Shared by
// eviction (budget) and the error-101 / no-sec-def / not-subscribed error branches so
// "line is gone → clients see stale, not a silent held cache" is one code path.
function killLine(key, tickerId) {
  const state = symbolStates.get(key);
  if (!state || state.tickerId !== tickerId) return false;
  requestIdToSymbol.delete(tickerId);
  state.tickerId = null;
  hydrateAndBroadcast(key);
  return true;
}
```

Replace the inline body of the `code === 101` branch (and, to avoid drift, the identical
`code === 200` / `code === 354` bodies) with `killLine(symbol, tickerId)`. Keep the distinct
`console.warn`/`verbose` log lines exactly as-is (the loud log is the acceptance criterion).

**CI pin** (runs in the P4.3 lane's sibling vitest job, repo pattern): add
`web/tests/ib-realtime-line-cap.test.ts`, a source-assertion suite mirroring
`ib-realtime-restart-modes.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = resolve(fileURLToPath(import.meta.url), "..");
const source = readFileSync(
  resolve(
    __dirname,
    "..",
    "..",
    "scripts",
    "infra",
    "ib_realtime",
    "ib_realtime_server.js",
  ),
  "utf8",
);

describe("ib_realtime_server.js error-101 line-cap handling", () => {
  it("routes error 101 through killLine and logs loudly", () => {
    const branch =
      source.match(
        /code === 101 \|\| \/Max number of tickers\/i\.test\(msg\)[\s\S]*?\n    \} else/,
      )?.[0] ?? "";
    expect(branch).toContain("killLine(symbol, tickerId)");
    expect(branch).toContain("market-data line cap reached");
  });
  it("computeStale treats a missing tickerId as stale", () => {
    expect(source).toContain(
      "if (state.tickerId == null) return true; // no active IB market-data line",
    );
  });
});
```

The live paper probe in the matrix is an **optional manual check only** — it is NOT the CI
substitute for this pin.

### Step 2 — Add a `priority` tier to symbol state

In `ensureSymbolState` (`~820`) default `priority` to `0`:

```js
const state = {
  tickerId: null,
  contract: ibContract,
  data: createPriceData(key),
  lastRealTickAt: null,
  lastAccessAt: null,
  priority: 0, // 2=portfolio/open-order, 1=focused-ticker chain, 0=other (QS-7)
};
```

### Step 3 — Tag the tier client-side and carry it in the subscribe message

**Frontend (`web/components/WorkspaceShell.tsx`, ~234-242):** the merge site is the only
place the source is known — tag each contract there. Replace the `allContracts` memo body:

```ts
const allContracts = useMemo(
  () =>
    uniqueOptionContracts([
      ...portfolioContracts.map((c) => ({ ...c, tier: 2 as const })),
      ...orderContracts.map((c) => ({ ...c, tier: 2 as const })),
      ...tickerDetail.chainContracts.map((c) => ({ ...c, tier: 1 as const })),
    ]),
  [portfolioContracts, orderContracts, tickerDetail.chainContracts],
);
```

Check `uniqueOptionContracts` (grep its definition) — if it strips unknown fields, extend it
to preserve `tier`, keeping the FIRST occurrence (portfolio/order entries are spread first,
so a contract in both portfolio and chain keeps tier 2). Extend the contract type it uses
(`OptionContractRef` or equivalent — grep `contracts:` in `web/lib/usePrices.ts`) with
`tier?: 0 | 1 | 2`. `usePrices` serializes contracts into the subscribe message as-is —
verify with `grep -n "action: \"subscribe\"" web/lib/usePrices.ts` that the contract objects
pass through unmodified; if the hook rebuilds them field-by-field, add `tier: c.tier`.

**Relay (`case "subscribe":` ~1889, contracts loop):** after `ensureSymbolState(key,
ibContract)`, set the tier with a conservative default:

```js
const state = symbolStates.get(key);
if (state) state.priority = Number.isInteger(c.tier) ? c.tier : 0;
```

Plain-stock `symbols` and `indexes` entries keep priority 0 (they are cheap and re-admitted
by heat; only option contracts contend for the budget in practice).

**Vitest for the tagging:** `web/tests/workspace-contract-tiers.test.ts` — unit-test the
merge logic (extract it into a pure helper `tagContractTiers(portfolio, orders, chain)` in
`web/lib/` if the memo body is not directly testable) asserting: portfolio/order contracts
carry `tier: 2`, chain contracts `tier: 1`, and a duplicate present in both keeps `tier: 2`.
Use real frozen contract shapes (e.g. AAPL 2026-01-17 200 C) — no placeholders.

### Step 4 — Priority-aware eviction with admission control (pure, fully unit-tested)

**BLOCKER fixed here:** eviction without admission control is broken — `makeRoomForLine`
always evicts the returned victim, so a priority-0 hover could still evict a priority-2
portfolio leg whenever the cap is full of high-priority lines. The rule: **a victim may only
be evicted for an incoming line of equal or higher priority; if every candidate victim
outranks the incoming symbol, the incoming subscribe is REJECTED** (left `tickerId == null`
→ reads `stale:true`) with a loud log. Equal-priority eviction stays allowed so LRU rotation
within a tier keeps working.

`line_budget.js` — new signature carrying the incoming priority:

```js
/**
 * Pick the live line to evict to admit `incoming` ({ key, priority }): the
 * lowest-priority live line, oldest lastAccessAt within that tier. Never picks
 * a victim whose priority EXCEEDS the incoming line's — in that case returns
 * null and the caller must REJECT the admission instead of evicting.
 */
export function pickEvictable(entries, incoming = { key: null, priority: 0 }) {
  let victim = null;
  let bestPriority = Infinity;
  let oldestAt = Infinity;
  for (const e of entries) {
    if (e.tickerId == null) continue;
    if (e.key === incoming.key) continue;
    const p = e.priority ?? 0;
    const at = e.lastAccessAt ?? 0;
    if (p < bestPriority || (p === bestPriority && at < oldestAt)) {
      bestPriority = p;
      oldestAt = at;
      victim = e.key;
    }
  }
  if (victim != null && bestPriority > (incoming.priority ?? 0)) return null;
  return victim;
}
```

Add `priority: state.priority` to the objects `lineEntries()` pushes (`~1446`).

**Relay call sites:**

- `makeRoomForLine(incomingKey)` → pass the incoming priority:

  ```js
  function makeRoomForLine(incomingKey) {
    const incomingPriority = symbolStates.get(incomingKey)?.priority ?? 0;
    while (activeLineCount() >= MAX_CONCURRENT_LINES) {
      const victim = pickEvictable(lineEntries(), {
        key: incomingKey,
        priority: incomingPriority,
      });
      if (victim == null) return false; // nothing evictable at ≤ incoming priority
      evictLine(victim);
    }
    return true;
  }
  ```

- `startLiveSubscription` (~838) currently IGNORES the return value. Make rejection real:

  ```js
  if (!makeRoomForLine(key)) {
    console.warn(
      `\x1b[33m[line-budget] admission rejected for ${key} (priority ${state.priority}) — ` +
        `cap full of higher-priority lines; symbol will read stale\x1b[0m`,
    );
    hydrateAndBroadcast(key); // push stale:true so clients see degraded, not silence
    return;
  }
  ```

- `pickAdmittable` for re-admission: prefer highest `priority`, then newest `lastAccessAt`
  (same two-field comparison, inverted). Update its loop accordingly.

`__tests__/line_budget_priority.test.mjs`:

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { pickEvictable } from "../line_budget.js";

test("evicts lowest-priority live line even if it is the newest", () => {
  const entries = [
    { key: "AAPL", tickerId: 1, lastAccessAt: 100, priority: 2 }, // portfolio, oldest
    { key: "TSLA", tickerId: 2, lastAccessAt: 999, priority: 0 }, // hover, newest
  ];
  assert.equal(pickEvictable(entries, { key: "NVDA", priority: 2 }), "TSLA");
});

test("cap full of portfolio legs, hover arrives → hover rejected (returns null)", () => {
  const entries = [
    { key: "AAPL", tickerId: 1, lastAccessAt: 100, priority: 2 },
    { key: "MSFT", tickerId: 2, lastAccessAt: 200, priority: 2 },
  ];
  assert.equal(pickEvictable(entries, { key: "TSLA", priority: 0 }), null);
});

test("equal-priority eviction stays allowed (LRU within tier)", () => {
  const entries = [
    { key: "A", tickerId: 1, lastAccessAt: 100, priority: 0 },
    { key: "B", tickerId: 2, lastAccessAt: 50, priority: 0 },
  ];
  assert.equal(pickEvictable(entries, { key: "C", priority: 0 }), "B");
});

test("never evicts the incoming key itself", () => {
  const entries = [{ key: "A", tickerId: 1, lastAccessAt: 1, priority: 0 }];
  assert.equal(pickEvictable(entries, { key: "A", priority: 0 }), null);
});
```

## Verification matrix

| Check                                               | Command                                                                                         | Expected                                                                                               |
| --------------------------------------------------- | ----------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| Priority unit tests                                 | `cd scripts/infra/ib_realtime && node --test __tests__/line_budget_priority.test.mjs`           | `pass 4 / fail 0`                                                                                      |
| All relay mjs green (P4.3 CI lane)                  | `node --test scripts/infra/ib_realtime/__tests__/*.test.mjs`                                    | `fail 0`                                                                                               |
| 101 CI pin                                          | `cd web && npm test -- ib-realtime-line-cap`                                                    | pass                                                                                                   |
| Tier-tagging test                                   | `cd web && npm test -- workspace-contract-tiers`                                                | pass; portfolio dup keeps tier 2                                                                       |
| usePrices regressions                               | `cd web && npm test -- usePrices`                                                               | pass                                                                                                   |
| Web typecheck                                       | `cd web && npx tsc --noEmit`                                                                    | exit 0                                                                                                 |
| Web lint                                            | `cd web && npm run lint`                                                                        | exit 0                                                                                                 |
| Relay boots                                         | `node scripts/infra/ib_realtime/ib_realtime_server.js --port 8899` (Ctrl-C)                     | listening, no throw                                                                                    |
| OPTIONAL manual probe — synthetic 101 (paper, RTH)  | `scripts/infra/dev.sh paper`, subscribe > MAX_CONCURRENT_LINES symbols; watch relay `--verbose` | yellow `market-data line cap reached ... will read stale` line AND that symbol renders stale in the UI |
| OPTIONAL manual probe — admission rejection (paper) | fill the cap with portfolio legs (tier 2), then open a ticker-detail chain (tier 1 floods)      | `admission rejected` log for chain contracts; NO portfolio leg goes stale                              |

## Tripwires / abort

- STOP if the `code === 101` branch is absent at HEAD (drift assumption broken).
- STOP if the P4.3 `node --test` CI step is absent — prerequisite unmet; do not land CI-dark.
- STOP if `uniqueOptionContracts` dedupes by a serialized form that cannot preserve `tier` —
  report before restructuring it.
- Paper only. Never live. This plan places no orders.
- File ceiling: relay + `line_budget.js` + mjs test + `WorkspaceShell.tsx` +
  `uniqueOptionContracts`'s module + contract type + 2 web tests = 8. STOP past that.

## Rollback

Discard the branch. `line_budget.js`'s new signature is used only by the relay (verify with
`grep -rn "pickEvictable" scripts/ web/`); `tier` is optional on the wire (`c.tier ?? 0`), so
frontend and relay halves are independently revertible. No schema/migration.
