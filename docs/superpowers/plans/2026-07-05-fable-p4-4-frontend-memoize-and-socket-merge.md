# P4.4 — Frontend: memoize consumers + merge usePrices/IBStatusContext socket core (QS-5, QS-8)

- **Date:** 2026-07-05
- **Branch:** `feat/p4-4-frontend-quote-perf`
- **Findings:** QS-5 (Medium) — every batch → `setPrices({...prev, ...updates})` → new object
  identity → full-tree re-render of non-memoized `MetricCards` (1,309 ln) + `WorkspaceSections`
  (2,689 ln) up to 10×/s. QS-8 (Low) — `usePrices` and `IBStatusContext` maintain two parallel
  sockets with duplicated staleness/connect scaffolding.
- **Goal:** Reduce re-render scope to components that actually consume changed symbols;
  eliminate the duplicated socket scaffolding between the two hooks.
- **Acceptance (roadmap):** profiler shows re-render scope reduced to subscribed components.
- **Gating:** This plan is sequenced AFTER P1.3 (React Profiler measurement). At authoring
  HEAD `docs/fable/measurements-*.md` does not exist — that is EXPECTED; P1.3 produces it.
  At EXECUTION time: if `ls docs/fable/measurements-*.md` is empty or the file lacks a QS-5
  render-cost baseline, STOP — run P1.3 first. If the recorded baseline shows the re-render
  cost is negligible, DEFER QS-5 and ship only the QS-8 socket cleanup (or nothing).

## Drift from review (READ FIRST)

- **QS-8 is partly already done.** The reconnect backoff is NOT duplicated — both hooks
  import `createReconnectStrategy` from `web/lib/reconnectStrategy.ts` (shared, with a
  docstring naming both consumers). What remains duplicated is the per-hook connect/staleness
  timer scaffolding (`reconnectTimeoutRef`, `stalenessTimerRef`, the `new WebSocket(url)`
  open/close/onmessage wiring). So QS-8's "shared WS-core hook" is a smaller job than the
  finding implies. Scope it as "extract the connect/reconnect/staleness lifecycle into one
  `useReconnectingSocket` hook used by both" — NOT "one socket, two channels" (that is a
  larger protocol change; the two sockets serve different auth/lifetime profiles and the
  status socket reconnects unlimited while prices caps at 10 — keep them separate).

## Re-verify preamble (MANDATORY — executes after P1.2 reshapes the protocol)

P1.2 adds `seq`/`relay_ts` to the batch message and may add quote-age exposure to
`usePrices`. Confirm the batch handler shape at HEAD before editing:

```bash
cd web
grep -n 'case "batch"' lib/usePrices.ts                    # ~753
grep -n "setPrices((prev) => ({ ...prev, ...updates })" lib/usePrices.ts  # ~755
grep -n "createReconnectStrategy" lib/usePrices.ts lib/IBStatusContext.tsx
grep -n "prices={prices}\|<MetricCards\|<WorkspaceSections" components/WorkspaceShell.tsx
```

If P1.2 has moved batch handling into a `seq`-gated reducer, the `setPrices` anchor changes —
re-read `usePrices.ts` around the batch case and adapt the memoization boundary accordingly.
The memoization strategy (below) is independent of whether `seq` gating exists.

## Key facts (verified at HEAD)

- `web/lib/usePrices.ts` (1,116 ln): single `const [prices, setPrices] =
useState<Record<string, PriceData>>({})`; batch case does `setPrices((prev) => ({ ...prev,
...updates }))` — new top-level identity every flush (≤10 Hz).
- `usePrices` returns the whole map: `UsePricesReturn.prices: Record<string, PriceData>` is
  the first field of the return type (`usePrices.ts` ~81-104); its identity is replaced on
  every batch at `case "batch"` → `setPrices((prev) => ({ ...prev, ...updates }))` (~753-755).
- `web/components/WorkspaceShell.tsx`: single `usePrices({...})` call destructuring
  `prices: rawPrices` (~254); passes `prices={prices}` into THREE consumer sites —
  `<DashboardSurface>` (~556-560), `<MetricCards>` (~568-574), `<WorkspaceSections>`
  (~577-588). None is `React.memo`-wrapped (verified: `grep -n "React.memo\|memo(" ` finds
  none on these).
- `web/lib/IBStatusContext.tsx` (257 ln) and `web/lib/usePrices.ts` both create a
  `new WebSocket(url)`, both hold `reconnectTimerRef`/`stalenessTimerRef`, both import
  `createReconnectStrategy` from `reconnectStrategy.ts`.
- `reconnectStrategy.ts` — shared `ReconnectState` (backoff+jitter). Already deduped.
- Vitest tests exist: `web/tests/usePrices.diff.test.ts`, `usePrices.depth.test.ts` use a
  `MockWebSocket`. Any socket-core extraction MUST keep these green.

## Non-goals

- Do NOT merge to a single socket/channel-multiplexed protocol (different auth + reconnect
  policies; out of scope, larger risk).
- Do NOT change the WS message protocol (that's P1.2).
- Do NOT structurally rewrite `DashboardSurface`/`MetricCards`/`WorkspaceSections` — the only
  internal change allowed is replacing `prices[sym]` reads with `usePriceFor(store, sym)` at
  the components that perform the read, plus the `React.memo` wrapper on each export.
- Do NOT migrate WorkspaceShell's own `rawPrices`-derived values to the store in this PR.

## Steps (TDD — measurement-gated)

### Step 0 — Confirm the baseline (gate)

`ls docs/fable/measurements-*.md` — the file does not exist at plan-authoring HEAD (expected;
P1.3 creates it). If it is still absent, or exists without a QS-5 render-cost baseline, STOP
and report "P1.3 prerequisite unmet". Otherwise note the pre-change React Profiler commit
count for the 60 s volatile window — that number is what the after-measurement compares to.

### Step 1 — Per-symbol selector store (QS-5 core)

Introduce a `useSyncExternalStore`-backed price store so a leaf reading `prices[symbol]`
re-renders ONLY when THAT symbol changes, not on every batch. Add `web/lib/priceStore.ts`:

```ts
// External store: batch updates mutate a mutable map + bump a per-symbol version;
// subscribers registered per symbol re-render only when their symbol's version bumps.
export type PriceSnapshot = Record<string, PriceData>;

export function createPriceStore() {
  let snapshot: PriceSnapshot = {};
  const listeners = new Map<string, Set<() => void>>(); // symbol -> callbacks

  function applyBatch(updates: Record<string, PriceData>) {
    snapshot = { ...snapshot, ...updates }; // new identity for whole-map readers
    for (const sym of Object.keys(updates)) {
      listeners.get(sym)?.forEach((cb) => cb()); // notify only affected symbols
    }
  }
  function subscribeSymbol(sym: string, cb: () => void) {
    let set = listeners.get(sym);
    if (!set) {
      set = new Set();
      listeners.set(sym, set);
    }
    set.add(cb);
    return () => set!.delete(cb);
  }
  function getSymbol(sym: string) {
    return snapshot[sym];
  }
  function getSnapshot() {
    return snapshot;
  }
  return { applyBatch, subscribeSymbol, getSymbol, getSnapshot };
}

export function usePriceFor(
  store: ReturnType<typeof createPriceStore>,
  sym: string,
) {
  return useSyncExternalStore(
    (cb) => store.subscribeSymbol(sym, cb),
    () => store.getSymbol(sym),
  );
}
```

**Migration sites — fixed, per-site decisions (no execution-time choices):**

1. **`usePrices.ts` handlers** (`case "batch"` ~753-755; also `case "price"`/`"snapshot"`
   ~739-751): keep the existing `setPrices` calls UNCHANGED and additionally call
   `storeRef.current.applyBatch(updates)` (batch) / `applyBatch({ [data.symbol]: data })`
   (price/snapshot). Hold the store in a `useRef(createPriceStore())` so its identity is
   stable across renders, and add `store` to `UsePricesReturn` (~81-104). The returned
   `prices` map stays — WorkspaceShell's own derived values (`prices: rawPrices` at ~254,
   `resolveRealtimePrice` etc.) keep using it; that is NOT migrated in this PR.
2. **`<DashboardSurface>` (WorkspaceShell ~556-560):** replace `prices={prices}` with
   `store={store}`; wrap the component export in `React.memo`; inside, replace each
   `prices[sym]` read with `usePriceFor(store, sym)` at the component that performs the read.
3. **`<MetricCards>` (~568-574):** same treatment — `store={store}`, `React.memo` on the
   export, `usePriceFor(store, sym)` at the card leaves that read a symbol's price.
4. **`<WorkspaceSections>` (~577-588):** same treatment — `store={store}`, `React.memo`,
   `usePriceFor(store, sym)` at the row components that read prices.

None of the three keeps a whole-map `prices` prop: with the parent re-rendering ≤10 Hz (its
own `prices` state still updates), only a STABLE `store` prop lets `React.memo` cut the
cascade; per-symbol `useSyncExternalStore` subscriptions then re-render exactly the leaves
whose symbols changed. WorkspaceShell itself continues to re-render per batch — accepted;
it is thin once the three children are memoized.

### Step 2 — Extract `useReconnectingSocket` (QS-8)

`web/lib/useReconnectingSocket.ts` — encapsulates: open a `WebSocket(url)`, `onopen` reset
backoff, `onclose`/`onerror` schedule reconnect via `createReconnectStrategy`, a staleness
interval, and generation-guarded teardown (the `socketGenRef` / `mountedRef` pattern already
in `usePrices`). Both `usePrices` and `IBStatusContext` consume it, each supplying its own
`onMessage`, `url`, and reconnect config. Keep the two sockets — only the lifecycle scaffold
is shared.

### Step 3 — Tests

- `web/tests/priceStore.test.ts`: `applyBatch` notifies only listeners of changed symbols;
  unrelated-symbol listener NOT called. Use frozen real values (e.g. `TSLA` `{ last: 393.45 }`
  from the v0.8.1 backfill note — no network).
- Keep `usePrices.diff.test.ts` / `usePrices.depth.test.ts` green (they assert diff-subscribe
  - reconnect + eviction via `MockWebSocket`).
- `web/tests/useReconnectingSocket.test.ts`: reconnect scheduled on close, backoff reset on
  open, teardown clears timers.

## Verification matrix

| Check                             | Command                                                                           | Expected                                                                                                   |
| --------------------------------- | --------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| Store unit test                   | `cd web && npm test -- priceStore`                                                | pass; unrelated-symbol listener count 0                                                                    |
| Socket-core test                  | `cd web && npm test -- useReconnectingSocket`                                     | pass                                                                                                       |
| usePrices regressions             | `cd web && npm test -- usePrices`                                                 | all pass (diff + depth)                                                                                    |
| Typecheck                         | `cd web && npx tsc --noEmit`                                                      | exit 0                                                                                                     |
| Lint                              | `cd web && npm run lint`                                                          | exit 0                                                                                                     |
| Profiler after (MANDATORY, gated) | React Profiler, same 60 s volatile window as P1.3 baseline                        | commit count for `MetricCards`/`WorkspaceSections` drops; only leaves reading changed symbols re-render    |
| E2E render (MANDATORY UI)         | `cd web && npx playwright test` (or chrome-cdp: load :3200, let prices tick 30 s) | prices update live; screenshot `output/playwright/p4-4-quote-render-2026-07-05.png`; no stale/frozen tiles |
| Sign/price invariants intact      | visual: credit rows negative, spread mids from legs                               | per web/CLAUDE.md — no `Math.abs`, no underlying-as-option-price                                           |

## Tripwires / abort

- STOP if P1.3 baseline is missing — this plan is measurement-gated.
- STOP if the profiler-after shows NO improvement — revert the store, keep only QS-8 if it
  stands on its own, else abandon (do not ship churn for churn's sake).
- The file set is FIXED: `priceStore.ts` (new), `useReconnectingSocket.ts` (new),
  `usePrices.ts`, `IBStatusContext.tsx`, `WorkspaceShell.tsx`, the three consumer component
  files, plus tests. STOP if the migration demands edits beyond that set (e.g. a consumer
  fans `prices` out to further children) — report the fan-out instead of chasing it.
- UI change → E2E browser verification is MANDATORY (root CLAUDE.md rule 2). Do not mark done
  without the screenshot.
- Preserve every web/CLAUDE.md pricing invariant (sign convention, spread mid, combo cross-
  fields). If a memo comparator ever drops a price field a P&L card needs, that's a bug.

## Rollback

Discard the branch. `priceStore.ts` / `useReconnectingSocket.ts` are additive; reverting the
`usePrices`/`WorkspaceShell` wiring restores the `setPrices` path. No schema, no backend.
