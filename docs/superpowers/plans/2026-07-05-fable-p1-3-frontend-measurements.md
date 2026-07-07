# P1.3 — Frontend re-render (QS-5) + fill-to-UI latency MEASUREMENT

- **Date:** 2026-07-05
- **Branch:** `chore/fable-p1-3-frontend-measurements`
- **Finding IDs:** QS-5 (frontend re-render cost — Hypothesis), plus the `fill_to_ui_seconds` line item in 08 §8.3.
- **Severity:** Medium (QS-5). This is a **measurement task**, not a fix.
- **Roadmap:** `docs/fable/10-roadmap.md` **P1.3**. Its output **decides the scope of Phase 4 P4.4** (memoize consumers / per-symbol store).
- **One-line goal:** Produce `docs/fable/measurements-2026-07-05.md` with real numbers for (a) React **render cost** of the `WorkspaceShell → MetricCards + WorkspaceSections` subtree under the live 100 ms quote batch, attributed specifically to price batches, and (b) fill→UI latency on paper, then record a QS-5 verdict + Phase-4 scope decision.

> **This PR ships exactly ONE new file: `docs/fable/measurements-2026-07-05.md`.** All instrumentation described below is **temporary scaffolding** (exactly two files: `web/components/WorkspaceShell.tsx` and `web/lib/usePrices.ts`) that you add, use to capture numbers, then **revert before committing**. The final `git diff` against `master` must show only the new measurements doc (and this plan, if not already committed). If your final diff shows any change under `web/`, you did it wrong — STOP and revert.

---

## Context (what exists today — verified at HEAD)

- **Single `usePrices` hook** lives in `web/components/WorkspaceShell.tsx` (`WorkspaceShell` function, call at line 254: `usePrices({ symbols: allSymbols, ... })`). Its `prices` object is passed as a prop to **both** consumers:
  - `<MetricCards ... prices={prices} />` — `web/components/MetricCards.tsx`, `export default function MetricCards(...)` at line 740. **1,309 lines. NOT wrapped in `React.memo`.**
  - `<WorkspaceSections ... prices={prices} />` — `web/components/WorkspaceSections.tsx`, `export default function WorkspaceSections(...)` at line 2634. **2,689 lines. NOT wrapped in `React.memo`** (only an inner `JournalSections` is memoized, line 959).
  - Render site: `web/components/WorkspaceShell.tsx` lines 555–589.
- **The quote batch cadence** driving re-renders: the relay flushes a `batch` message every `BATCH_INTERVAL_MS = 100` (`scripts/infra/ib_realtime/ib_realtime_server.js:706`), with an early flush when any client has ≥ `BATCH_THRESHOLD = 50` buffered symbols (line 710). The browser applies each batch with a **single top-level `setState`**: `setPrices((prev) => ({ ...prev, ...updates }))` in `web/lib/usePrices.ts:755` (the `case "batch":` block at line 753). That one `setPrices` re-renders `WorkspaceShell`, which — because neither consumer is memoized — re-renders the **entire** `MetricCards` + `WorkspaceSections` subtree. This is the QS-5 mechanism: **up to ~10 full-subtree commits/second during RTH.**
- **Confound (important for attribution):** the same subtree also re-renders from **non-price** state — `useOrders` (30 s auto-sync, `web/lib/useOrders.ts:6` `SYNC_INTERVAL_MS = 30_000`, plus **an immediate sync on mount and on navigation-triggered `ordersSyncNow()` calls**), portfolio refreshes, market-state, and context updates. A raw commit count therefore over-attributes to QS-5. The procedure below tags each price batch and counts **only batch-correlated commits** toward the verdict.
- **Fill persistence:** fills land in `xenon.order_fills` (schema `src/xenon/db/schema.py:650`, columns: `exec_id` PK, `submission_id`, `ticker`, `side`, `qty`, `price`, **`filled_at TIMESTAMP(timezone=True)` at line 674**, scope columns `broker/account_env/broker_account`). There is **no `created_at`/`inserted_at` column** — `filled_at` is the broker's fill time, not the PG-write time. Fills are written by the IB activity-mirror poller (60 s tick by default).
- **UI fill surface refresh:** the executed-orders / open-orders panels are driven by `useOrders`, auto-sync interval 30 s (above), plus immediate syncs on load/navigation.

**The executor does NOT need to understand:** the internals of `usePrices` reconnect/staleness logic, the naked-short guard, combo pricing, or anything about the order-place code path beyond placing one trivial paper order. This task adds no product behavior.

---

## Drift from review

**None material.** All QS-5 anchors in `03-findings-table.md` (WorkspaceShell single `usePrices`; MetricCards 1,309 ln; WorkspaceSections 2,689 ln; neither memoized; ~100 ms cadence) are confirmed at HEAD. The fable line numbers `WorkspaceShell.tsx:254,556-589` are still accurate (usePrices at 254; render block 555–589).

---

## Goal / Non-goals

**Goal:** capture and record, in `docs/fable/measurements-2026-07-05.md`:

1. React commit **count** and **render-duration distribution (p50/p95/max of `actualDuration`)** for the `WorkspaceShell` subtree over a **60 s window** on the workspace page during RTH with the browser's normal ~80-symbol subscription — **split into batch-correlated vs other commits**.
2. Whether batch-correlated commits are **tree-wide** (both `MetricCards` and `WorkspaceSections` re-render on price batches) vs already scoped.
3. `fill_to_ui_seconds` from a single paper fill: broker `filled_at` → PG landing → UI display.
4. A **QS-5 verdict** (confirm / refute against a stated numeric threshold — a **render-cost** verdict, see Step 3a for exactly what the threshold does and does not cover) and the resulting **Phase-4 P4.4 scope decision**.

**Non-goals (explicitly NOT done here — one change = one PR):**

- **No** memoization of `MetricCards` / `WorkspaceSections` (that is P4.4 — this task _decides_ whether P4.4 happens).
- **No** per-symbol selector store / `useSyncExternalStore` (P4.4).
- **No** merge of `usePrices` / `IBStatusContext` sockets (QS-8 / P4.4).
- **No** relay `/status` metrics or `seq`/`relay_ts` protocol work (that is P1.2, a separate PR).
- **No** order-path stage-timing logs (P1.1).
- **No** committed instrumentation of any kind. All scaffolding is reverted.

---

## Key facts (verified against the working tree)

| Fact                               | Value                                                                                                                                                                       | Source (verified)                                     |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| Relay L1 batch cadence             | `BATCH_INTERVAL_MS = 100` ms                                                                                                                                                | `scripts/infra/ib_realtime/ib_realtime_server.js:706` |
| Early-flush threshold              | `BATCH_THRESHOLD = 50` symbols                                                                                                                                              | same file, line 710                                   |
| Browser batch apply                | `setPrices((prev) => ({ ...prev, ...updates }))`                                                                                                                            | `web/lib/usePrices.ts:755` (`case "batch":` at 753)   |
| Single `usePrices` call            | `WorkspaceShell.tsx:254`                                                                                                                                                    | verified                                              |
| `MetricCards` export               | `export default function MetricCards` at line 740, no `React.memo`                                                                                                          | `web/components/MetricCards.tsx`                      |
| `WorkspaceSections` export         | `export default function WorkspaceSections` at line 2634, no `React.memo`                                                                                                   | `web/components/WorkspaceSections.tsx`                |
| Render block                       | lines 555–589                                                                                                                                                               | `web/components/WorkspaceShell.tsx`                   |
| React `<Profiler>` `onRender` args | `(id, phase, actualDuration, baseDuration, startTime, commitTime)` — `actualDuration` is **render time of the profiled subtree only**; browser layout/paint is NOT included | React docs (Profiler API)                             |
| Fill timestamp column              | `order_fills.filled_at TIMESTAMP(timezone=True)` (broker fill time; NO insert-time column)                                                                                  | `src/xenon/db/schema.py:674`                          |
| `order_fills` scope columns        | `broker`, `account_env`, `broker_account`, `ticker`, `side`                                                                                                                 | `src/xenon/db/schema.py:669-678`                      |
| Orders UI poll interval            | `SYNC_INTERVAL_MS = 30_000` ms **+ immediate sync on mount/navigation**                                                                                                     | `web/lib/useOrders.ts:6,112-128`                      |
| Dev ports                          | Next **3200**, FastAPI **8421**, relay WS **8866**                                                                                                                          | root `CLAUDE.md`; `web/CLAUDE.md`                     |
| Paper stack                        | `scripts/infra/dev.sh paper` → local IB **port 4002**, writes `core_test` (LOCAL `DATABASE_URL_PAPER` = `127.0.0.1/core_test`)                                              | root `CLAUDE.md` § Two core_test DBs                  |
| Paper DB for psql                  | **local** `127.0.0.1/core_test` (NOT the remote macmini one)                                                                                                                | memory `project_two_core_test_dbs`                    |
| Screenshot dir                     | `output/playwright/` (exists)                                                                                                                                               | verified                                              |

**RTH check (run first, every time):**

```bash
TZ=America/New_York date +"%A %H:%M"   # must be Mon–Fri 09:30–16:00 ET
```

If outside RTH, **STOP** — quote batches won't flow at ~10 Hz and QS-5 numbers will be meaningless. Record "deferred — market closed" and abort.

---

## Procedure

> Each numbered step is strictly ordered. Do not skip. If any **STOP** tripwire fires, write what you have into the artifact under the relevant "verdict" field as `INCONCLUSIVE — <reason>` and stop.

### Step 0 — Boot the paper stack + preconditions

1. RTH check (above). If closed → STOP.
2. **Pre-edit cleanliness precondition (required for safe revert later):**
   ```bash
   cd /Users/chenxi/projects/xenon
   git diff -- web/components/WorkspaceShell.tsx web/lib/usePrices.ts
   ```
   MUST print nothing. If either file already has uncommitted changes → **STOP** — reverting scaffolding with `git checkout --` would destroy them. Report and wait; do not stash (repo memory: bare `git stash` is dangerous here).
3. Start the dev stack on paper (per repo policy, all live probes are PAPER-only):
   ```bash
   scripts/infra/dev.sh paper
   ```
   Wait for readiness:
   ```bash
   curl -s http://localhost:8421/health | uv run python -m json.tool | grep -i "port_listening\|ib_gateway\|status"
   ```
   Expect `ib_gateway.port_listening: true` and a `4002` port. If IB Gateway paper is not connected → STOP (approve 2FA on IBKR mobile, retry). Do NOT fall back to live.
4. Confirm Next is up: `curl -s -o /dev/null -w "%{http_code}\n" http://localhost:3200` → expect `200`.

### Step 1 — Add TEMPORARY instrumentation (scaffolding — reverted in Step 6)

Rationale for using the React `<Profiler>` API (not just the DevTools extension flamegraph): it is **scriptable and deterministic** — a headless/automated executor cannot reliably drive the browser-extension Profiler UI. Samples are **buffered in a window-global array and dumped once at the end** (not `console.log` per commit) so measurement I/O stays out of the measured numbers. The DevTools extension is an **optional manual cross-check** (Step 3b), not the primary capture.

Two temporary edits, both gated on `NEXT_PUBLIC_PROFILE_QS5=1`.

**Edit 1 of 2 — `web/components/WorkspaceShell.tsx`.**

**(a)** Insert this block immediately **before** the `export ... function WorkspaceShell` declaration:

```tsx
// TEMP QS-5 MEASUREMENT SCAFFOLDING — DELETE BEFORE COMMIT (P1.3)
import { Profiler as ReactProfiler } from "react";
const __QS5 =
  typeof window !== "undefined" && process.env.NEXT_PUBLIC_PROFILE_QS5 === "1";
function __qs5Buf(): unknown[] {
  const w = window as unknown as { __QS5_SAMPLES?: unknown[] };
  if (!w.__QS5_SAMPLES) w.__QS5_SAMPLES = [];
  return w.__QS5_SAMPLES;
}
function __qs5OnRender(
  id: string,
  phase: "mount" | "update" | "nested-update",
  actualDuration: number,
  baseDuration: number,
  startTime: number,
  commitTime: number,
) {
  // Buffered, dumped once post-capture — avoids per-commit console I/O
  // contaminating the measurement.
  __qs5Buf().push({
    kind: "commit",
    id,
    phase,
    actualDuration,
    baseDuration,
    startTime,
    commitTime,
  });
}
// END TEMP SCAFFOLDING
```

> If `import { Profiler as ReactProfiler }` conflicts with an existing React import style, instead add `Profiler` to the existing `from "react"` import and reference it as `Profiler`. Either way the callbacks and the wrappers below must compile.

**(b)** Wrap **each** of the two consumers in its own `<ReactProfiler>` so you can tell them apart. Replace the render block at lines 568–589 (`<MetricCards ... />` and `<WorkspaceSections ... />`, each inside its `activeSection` guard) so that each element is wrapped:

```tsx
{
  activeSection !== "dashboard" &&
  activeSection !== "ticker-detail" &&
  activeSection !== "operator" ? (
    __QS5 ? (
      <ReactProfiler id="MetricCards" onRender={__qs5OnRender}>
        <MetricCards
          portfolio={portfolio}
          prices={prices}
          realizedPnl={todayRealizedPnl}
          executedOrders={executedOrders}
          section={activeSection}
        />
      </ReactProfiler>
    ) : (
      <MetricCards
        portfolio={portfolio}
        prices={prices}
        realizedPnl={todayRealizedPnl}
        executedOrders={executedOrders}
        section={activeSection}
      />
    )
  ) : null;
}

{
  activeSection !== "dashboard" && activeSection !== "operator" ? (
    __QS5 ? (
      <ReactProfiler id="WorkspaceSections" onRender={__qs5OnRender}>
        <WorkspaceSections
          section={activeSection}
          portfolio={portfolio}
          portfolioLastSync={portfolioLastSync}
          orders={orders}
          prices={prices}
          tickerParam={tickerParam}
          theme={resolvedTheme}
          marketState={marketState}
          activeAccount={activeAccount}
        />
      </ReactProfiler>
    ) : (
      <WorkspaceSections
        section={activeSection}
        portfolio={portfolio}
        portfolioLastSync={portfolioLastSync}
        orders={orders}
        prices={prices}
        tickerParam={tickerParam}
        theme={resolvedTheme}
        marketState={marketState}
        activeAccount={activeAccount}
      />
    )
  ) : null;
}
```

**Edit 2 of 2 — `web/lib/usePrices.ts` (batch-event tagging for attribution, per the confound in Context).**

In the `ws.onmessage` handler (line 732), inside the `case "batch":` block (line 753), immediately **before** the line `setPrices((prev) => ({ ...prev, ...updates }));` (line 755), insert:

```ts
// TEMP QS-5 MEASUREMENT SCAFFOLDING — DELETE BEFORE COMMIT (P1.3)
if (process.env.NEXT_PUBLIC_PROFILE_QS5 === "1") {
  const w = window as unknown as { __QS5_SAMPLES?: unknown[] };
  if (!w.__QS5_SAMPLES) w.__QS5_SAMPLES = [];
  w.__QS5_SAMPLES.push({
    kind: "batch",
    ts: performance.now(),
    updateCount: Object.keys(updates).length,
  });
}
// END TEMP SCAFFOLDING
```

> `performance.now()` is the same clock as the Profiler's `startTime`/`commitTime`, so batch events and commits are directly comparable.

Typecheck the scaffolding compiles (does not need to be committed):

```bash
cd web && npx tsc --noEmit
```

Expect exit `0`. If it errors, fix the import/typing form until it compiles. If it still won't compile after 2 attempts → STOP.

Restart Next with the flag on so the gate is live:

```bash
# kill the running next dev (Ctrl-C the dev.sh next process) then, from web/:
NEXT_PUBLIC_PROFILE_QS5=1 npm run dev
```

> `NEXT_PUBLIC_*` is inlined at build/start time, so it MUST be set in the environment that starts Next. `dev.sh paper` does not set it; run Next manually with the var for the measurement window, or export it before `dev.sh`.

### Step 2 — Capture the 60 s window (QS-5 primary data)

Use chrome-devtools MCP (preferred) or Playwright to open the workspace and dump the sample buffer.

1. Navigate to `http://localhost:3200` and select a **non-dashboard, non-ticker-detail, non-operator** section (so both `MetricCards` and `WorkspaceSections` are mounted — e.g. the **Positions/Portfolio** section). Confirm live quotes are ticking (numbers changing). Record the **subscribed symbol count** (from the relay status panel or the portfolio row count) — if fewer than ~30 live symbols, note it; below 10 → tripwire (see Tripwires).
2. Reset the buffer via evaluate-script: `window.__QS5_SAMPLES = []`.
3. Wait **exactly 60 seconds** (no clicking, no tab/section changes during the window).
4. Dump once via evaluate-script: `JSON.stringify(window.__QS5_SAMPLES)` — save the result verbatim to `output/playwright/p1-3-qs5-samples-2026-07-05.json`. Sanity: it must contain both `"kind":"batch"` and `"kind":"commit"` entries; if either is absent → tripwire.
5. Take a screenshot of the workspace with live quotes to `output/playwright/p1-3-profiler-2026-07-05.png`.

**Analysis** (a throwaway python one-liner/script over the JSON — analysis tooling, not committed code):

- **Batch events**: count, mean inter-arrival ms, mean `updateCount`.
- **Attribution rule**: a `commit` sample is **batch-correlated** iff there exists a `batch` event with `batch.ts ≤ commit.startTime ≤ batch.ts + 100 ms` (one relay flush interval). All other commits are "other" (orders poll, portfolio, contexts) — reported separately, but they do **not** count toward the QS-5 verdict.
- For each id (`MetricCards`, `WorkspaceSections`), over **batch-correlated commits only**: total count, rate/s, `actualDuration` p50/p95/max. Also record `baseDuration` p50 (the estimated cost of a full un-memoized re-render — the memoization headroom).
- **Combined per-commit subtree render cost** = sum of the two ids' `actualDuration` for commits sharing the same `commitTime` (both consumers commit together since both take `prices`).
- **Tree-wide?** YES if both ids show `phase=update` batch-correlated commits at ≥ ~5/s. NO if one id does not re-render on price batches.
- Also report the **other-commit** counts/durations (context for window noise — e.g. whether a 30 s orders sync landed inside the window).

### Step 3a — QS-5 threshold (stated up front; do not move the goalposts)

**Scope of the metric:** `actualDuration` is **React render time of the profiled subtree only** — it excludes browser style/layout/paint. The verdict below is therefore explicitly a **render-cost verdict**. The 60 fps frame budget of **16.67 ms** is used as the yardstick because render work ≥ one frame guarantees dropped frames even before layout/paint is added; render p95 well under a frame leaves layout/paint headroom.

- **QS-5 CONFIRMED → P4.4 IN SCOPE** if BOTH:
  1. batch-correlated commits are **tree-wide** (both consumers re-render on price batches), AND
  2. **combined per-commit subtree render p95 (`actualDuration`) ≥ 16 ms** at a batch-correlated commit rate ≥ 5/s.
- **QS-5 REFUTED / P4.4 DESCOPED** if EITHER:
  1. combined subtree **render p95 < 8 ms** (well under a frame even with paint headroom), OR
  2. batch-correlated commit rate < 1/s (batches not actually driving re-renders), OR
  3. commits are already scoped (React skips one consumer).
     Then the memoization half of P4.4 is descoped; only the QS-8 socket-merge half (if any) remains, and that is recorded as a separate decision.
- **Borderline (8 ms ≤ render p95 < 16 ms, tree-wide):** run the **paint-inclusive check**: capture a ~15 s Chrome performance trace during quote flow (chrome-devtools MCP `performance_start_trace` / `performance_stop_trace`) and report dropped frames / long tasks from the trace summary. If the trace shows recurring frames > 16.67 ms attributable to React commits → **CONFIRMED**; else record **WEAK CONFIRM — P4.4 optional**, noting that a busier portfolio or slower machine would push it over budget. State the Profiler-overhead caveat (Tripwires, last item) explicitly for borderline results.

### Step 3b — OPTIONAL manual cross-check (React DevTools extension)

If the React DevTools browser extension is available in the profiling browser: open the **Profiler** tab, record a ~10 s session on the same page, and confirm the flamegraph shows `MetricCards` + `WorkspaceSections` re-rendering on each commit (ranked chart, gray = did-not-render). This is a visual sanity check on the buffered numbers; it is not required if the capture is clean. Do not block on it.

### Step 4 — fill_to_ui measurement (one paper fill)

**Place exactly ONE trivial, marketable PAPER order** and time its journey to the UI. Ford (`F`) is a cheap, highly liquid large-cap that fills instantly at market during RTH.

0. **Panel precondition (prevents measuring an immediate sync instead of the poll):** BEFORE submitting, navigate to the section showing the open-orders / executed-orders panel and leave it open. From this point until `t_ui` is recorded: **no tab/section changes, no manual refresh, no "sync now" clicks** — any of those fires an immediate `useOrders` sync (immediate sync on mount; navigation-triggered `ordersSyncNow()`) and would understate the polling latency. If one fires anyway (e.g. an accidental navigation), record `immediate_sync_fired: YES` in the artifact and treat `fill_to_ui_seconds` as a lower bound.
1. Identify the paper broker account id for scoping:
   ```bash
   psql "$DATABASE_URL_PAPER" -c "SELECT DISTINCT broker, account_env, broker_account FROM xenon.order_submissions ORDER BY 1,2,3"
   ```
   Note the paper `broker_account` (call it `<ACCT>`; `account_env` should be `paper`).
   > If `$DATABASE_URL_PAPER` is not exported in your shell, read it from `/Users/chenxi/projects/xenon/.env` — it is the LOCAL `127.0.0.1/core_test` URL, NOT `DATABASE_URL_TEST` (remote macmini). Using the wrong one shows zero rows and looks like a routing bug (memory `project_two_core_test_dbs`).
2. Record wall-clock **t_place** just before submitting, then run a **scoped** 1-second poll loop in a terminal (loop shown; `watch -n 1` on the inner psql is equivalent):
   ```bash
   while true; do
     psql "$DATABASE_URL_PAPER" -c "SELECT now() AS observed_at, exec_id, ticker, side, qty, price, filled_at
       FROM xenon.order_fills
       WHERE broker='IB' AND account_env='paper' AND broker_account='<ACCT>'
         AND ticker='F' AND filled_at >= now() - interval '10 minutes'
       ORDER BY filled_at DESC";
     sleep 1;
   done
   ```
   The scoping predicate is load-bearing: `broker='IB' AND account_env='paper' AND broker_account='<ACCT>' AND ticker='F' AND filled_at >= <t_place window>` — an unscoped query over all of `order_fills` can pick up unrelated rows.
3. In the browser (Order tab on the IB account), build a **BUY 1 share of F** order with a **marketable** price (market order, or a limit a few cents above the ask). Submit. Do not touch the UI afterwards.
4. Watch three observables and record wall-clock timestamps:
   - **t_persist** = the first poll iteration where the new `F BUY` row appears (its `observed_at` column).
   - **filled_at** = the broker fill time on that row (record verbatim).
   - **t_ui** = the wall-clock when the already-open orders panel first shows the F fill **via its own 30 s poll** (row flips working → filled, or appears in executed orders). Screenshot to `output/playwright/p1-3-fill-ui-2026-07-05.png`.
5. Compute and record:
   - `pg_landing_lag = t_persist − filled_at` (broker fill → visible in PG; dominated by the 60 s poller tick).
   - `fill_to_ui_seconds = t_ui − t_persist` (PG → UI; dominated by the 30 s `useOrders` poll).
   - `end_to_end = t_ui − filled_at`.
   - `immediate_sync_fired: YES/NO`.
     The 08 §8.3 doc claims ~120 s worst case; state whether the observed end-to-end confirms or refutes (a single sample is a **point estimate** — tag it as such).

### Step 5 — Clean up the paper position (verified flat, not just visually)

Flatten the 1-share long (selling exactly the shares just bought is a covered reduce-exposure order — allowed by the naked-short guard): in the UI, **SELL 1 share of F**, marketable, same paper account. Then verify ALL THREE via DB/API — the Positions panel alone is insufficient (it can render a stale snapshot):

1. **Sell FILLED** (a SELL fill row exists):
   ```bash
   psql "$DATABASE_URL_PAPER" -c "SELECT exec_id, side, qty, filled_at FROM xenon.order_fills WHERE broker='IB' AND account_env='paper' AND broker_account='<ACCT>' AND ticker='F' AND filled_at >= now() - interval '30 minutes' ORDER BY filled_at"
   ```
   Expect one BUY and one SELL row, qty 1 each.
2. **Zero working F orders**:
   ```bash
   psql "$DATABASE_URL_PAPER" -c "SELECT submission_id, state FROM xenon.order_submissions WHERE broker='IB' AND account_env='paper' AND broker_account='<ACCT>' AND ticker='F' AND state NOT IN ('FILLED','CANCELLED','FAILED','REJECTED')"
   ```
   Expect **0 rows**. (If state names differ at HEAD, the intent is: no non-terminal F submission remains.)
3. **Net F position flat** via the API: after the next portfolio sync, `curl -s http://localhost:8421/portfolio | uv run python -m json.tool | grep -B2 -A4 '"F"'` — the F stock position must be absent or qty 0.

**If the SELL does not fill within ~3 min:** cancel it from the UI, confirm the cancel landed (check 2 returns 0 rows), and record in the artifact: `cleanup: BLOCKED — 1 share F long remains on paper account <ACCT>` so the operator can flatten manually. Do not leave a working order behind.

### Step 6 — Revert ALL scaffolding

Precondition Step 0.2 guaranteed both files were clean before editing, so a targeted checkout is safe:

```bash
cd /Users/chenxi/projects/xenon
git checkout -- web/components/WorkspaceShell.tsx web/lib/usePrices.ts
git status --porcelain web/          # MUST print nothing
git diff --stat                      # MUST show only docs/fable/measurements-2026-07-05.md (+ this plan if uncommitted)
```

If `git status` shows any remaining change under `web/` → the revert is incomplete; STOP and clean it. Stop the manually-started `NEXT_PUBLIC_PROFILE_QS5=1 npm run dev` process; the flag never ships.

### Step 7 — Write the artifact

Create `docs/fable/measurements-2026-07-05.md` from the template below with every placeholder filled. Placeholders left as `<...>` are a failure — if a number couldn't be captured, write `INCONCLUSIVE — <reason>`, not `<...>`.

---

## Artifact template — `docs/fable/measurements-2026-07-05.md`

```markdown
# Fable P1.3 measurements — 2026-07-05

Measurement task per `docs/fable/10-roadmap.md` P1.3 and `docs/fable/08-performance-measurement-plan.md` §8.2–8.3.
Decides the scope of Phase 4 **P4.4** (memoize consumers / per-symbol store; usePrices/IBStatusContext socket merge).

## Environment

- Date/time (ET): <Mon–Fri HH:MM ET> — RTH: <YES/NO>
- Market state: <open / closed / half-day>
- Stack: `dev.sh paper` (IB paper :4002, core_test LOCAL). Next :3200, FastAPI :8421, relay :8866.
- Machine: <model / chip / RAM> — Browser: <Chrome version>
- Subscribed symbol count during capture: <N ~80> — quote entitlement: <live / delayed>
- Active workspace section during capture: <e.g. Positions>

## 1. QS-5 — React render cost (60 s window, batch-attributed)

Instrumentation: temporary React `<Profiler>` wrappers (buffered samples: actualDuration, baseDuration,
startTime, commitTime) around MetricCards + WorkspaceSections, plus a batch-event tag in usePrices;
`NEXT_PUBLIC_PROFILE_QS5=1`; reverted post-capture (git diff clean).
Raw samples: `output/playwright/p1-3-qs5-samples-2026-07-05.json`.
**Scope: numbers below are React RENDER time only (`actualDuration`); browser layout/paint excluded** — see paint-check row.

- Batch events in window: <n> — mean inter-arrival: <ms> — mean updateCount: <n>
- Attribution rule: commit counted as batch-correlated iff batch.ts ≤ commit.startTime ≤ batch.ts + 100 ms.

| Consumer (batch-correlated commits only) | Commits / 60 s | Rate (/s) | render p50 (ms) | render p95 (ms) | max (ms) | baseDuration p50 (ms) |
| ---------------------------------------- | -------------- | --------- | --------------- | --------------- | -------- | --------------------- |
| MetricCards                              | <n>            | <r>       | <p50>           | <p95>           | <max>    | <b50>                 |
| WorkspaceSections                        | <n>            | <r>       | <p50>           | <p95>           | <max>    | <b50>                 |
| **Combined per-commit subtree**          | —              | <r>       | <p50>           | <p95>           | <max>    | —                     |

- Other (non-batch) commits in window: <n> — orders sync fired inside window: <YES/NO>
- Tree-wide re-render on price batches? **<YES/NO>** (evidence: <both ids commit at batch rate / one skipped>)
- Paint-inclusive check (required if borderline 8–16 ms): <dropped frames / long tasks from 15 s perf trace, or `n/a`>
- Threshold used: **render p95 ≥ 16 ms combined subtree** at ≥5 batch-correlated commits/s = CONFIRMED; **< 8 ms** = REFUTED; 8–16 ms → paint check decides (justified in the P1.3 plan).

### QS-5 verdict (render-cost verdict)

**<CONFIRMED / REFUTED / WEAK CONFIRM / INCONCLUSIVE>** — <one-line reason tied to the threshold>.

## 2. fill_to_ui — one paper fill (point estimate, n=1)

Order: BUY 1 F (Ford), marketable, paper account <ACCT>. Orders panel open BEFORE submit; no navigation
or manual sync between submit and t_ui. immediate_sync_fired: <YES/NO>.
Screenshot: `output/playwright/p1-3-fill-ui-2026-07-05.png`.

| Event                                       | Wall-clock (ET) |
| ------------------------------------------- | --------------- |
| t_place (submit clicked)                    | <HH:MM:SS>      |
| filled_at (broker, from order_fills)        | <HH:MM:SS±TZ>   |
| t_persist (row visible in PG, scoped query) | <HH:MM:SS>      |
| t_ui (fill shown in already-open panel)     | <HH:MM:SS>      |

| Derived latency                           | Seconds |
| ----------------------------------------- | ------- |
| pg_landing_lag = t_persist − filled_at    | <s>     |
| **fill_to_ui_seconds = t_ui − t_persist** | <s>     |
| end_to_end = t_ui − filled_at             | <s>     |

- 08 §8.3 claimed ~120 s worst-case. Observed end_to_end: <s> → **<confirms / below claim / above claim>** (n=1 point estimate; poller tick 60 s + useOrders 30 s poll bound the worst case).
- Cleanup: SELL 1 F <FILLED / BLOCKED — details>; working F orders after cleanup: <0 / n>; net F position via API: <flat / not flat>.

## 3. Phase-4 scope decision (P4.4)

- Memoize MetricCards / WorkspaceSections + per-symbol store: **<IN SCOPE / DESCOPED / OPTIONAL>** — because <QS-5 verdict>.
- usePrices / IBStatusContext socket merge (QS-8): **<recommend / defer>** (independent of QS-5 cost; note if double-socket observed).
- fill_to_ui: **<acceptable / needs work>** — if needs work, lever is the 30 s `useOrders` poll and/or a WS push of order events (08 §8.3 "push order events over WS to cut lag").

## Notes / caveats

- Profiler overhead: the `<Profiler>` wrapper adds per-commit bookkeeping; samples were buffered in-memory
  and dumped once (no per-commit console I/O). Borderline results (8–16 ms band) are sensitive to this.
- <single fill sample; machine faster/slower than prod; portfolio size vs typical; delayed-vs-live entitlement>
```

---

## Verification matrix

Because this is a measurement plan, "verification" = the artifact exists and is complete, evidence files are saved, and **no code shipped**. Every check has an exact command and exact expected outcome.

| #   | Check                                                      | Command                                                                                                                                                                                             | Expected                                                   |
| --- | ---------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| V1  | Artifact exists                                            | `test -f docs/fable/measurements-2026-07-05.md && echo OK`                                                                                                                                          | prints `OK`                                                |
| V2  | No `<...>` placeholders left                               | `grep -c '<[a-z]' docs/fable/measurements-2026-07-05.md` — manually confirm any hit is real prose, not an unfilled placeholder                                                                      | 0 unfilled placeholders                                    |
| V3  | QS-5 verdict present + scoped to render cost               | `grep -q 'render-cost verdict' docs/fable/measurements-2026-07-05.md && grep -Eq 'CONFIRMED\|REFUTED\|WEAK CONFIRM\|INCONCLUSIVE' docs/fable/measurements-2026-07-05.md && echo OK`                 | `OK`                                                       |
| V4  | Batch attribution recorded                                 | `grep -q 'batch-correlated' docs/fable/measurements-2026-07-05.md && echo OK`                                                                                                                       | `OK`                                                       |
| V5  | Phase-4 decision present                                   | `grep -q 'Phase-4 scope decision' docs/fable/measurements-2026-07-05.md && echo OK`                                                                                                                 | `OK`                                                       |
| V6  | fill_to_ui + immediate-sync flag present                   | `grep -q 'fill_to_ui_seconds' docs/fable/measurements-2026-07-05.md && grep -q 'immediate_sync_fired' docs/fable/measurements-2026-07-05.md && echo OK`                                             | `OK`                                                       |
| V7  | Cleanup verified in artifact                               | `grep -q 'Cleanup: SELL 1 F' docs/fable/measurements-2026-07-05.md && echo OK`                                                                                                                      | `OK`                                                       |
| V8  | Profiler screenshot saved                                  | `test -f output/playwright/p1-3-profiler-2026-07-05.png && echo OK`                                                                                                                                 | `OK`                                                       |
| V9  | Fill-UI screenshot saved                                   | `test -f output/playwright/p1-3-fill-ui-2026-07-05.png && echo OK`                                                                                                                                  | `OK`                                                       |
| V10 | Raw samples saved, both kinds present                      | `grep -q '"kind":"batch"' output/playwright/p1-3-qs5-samples-2026-07-05.json && grep -q '"kind":"commit"' output/playwright/p1-3-qs5-samples-2026-07-05.json && echo OK`                            | `OK`                                                       |
| V11 | **Scaffolding removed**                                    | `git status --porcelain web/`                                                                                                                                                                       | **prints nothing** (empty)                                 |
| V12 | Diff is docs-only                                          | `git diff --stat master -- . ':(exclude)docs/**' ':(exclude)output/**'`                                                                                                                             | **prints nothing** (no code changes)                       |
| V13 | Flag never committed                                       | `grep -rn "NEXT_PUBLIC_PROFILE_QS5\|__qs5OnRender\|__QS5_SAMPLES\|QS5 MEASUREMENT SCAFFOLDING" web/ --exclude-dir=.next --exclude-dir=node_modules`                                                 | **no matches**                                             |
| V14 | Paper book clean                                           | `psql "$DATABASE_URL_PAPER" -c "SELECT count(*) FROM xenon.order_submissions WHERE broker='IB' AND account_env='paper' AND ticker='F' AND state NOT IN ('FILLED','CANCELLED','FAILED','REJECTED')"` | `0` (or artifact records `cleanup: BLOCKED`)               |
| V15 | Web unit tests still green (sanity — scaffolding reverted) | `cd web && npm test -- WorkspaceShell 2>/dev/null; echo "exit $?"`                                                                                                                                  | tests pass or "no tests matched"; a FAIL is not acceptable |

> `output/playwright/*` and `output/` are gitignored (root `CLAUDE.md` directory table) — evidence files, not committed artifacts. V12 excludes `output/**` for that reason.

---

## Tripwires / abort criteria (STOP and report)

- **Market closed / outside RTH** at Step 0 → STOP. Record "deferred — market closed"; the ~10 Hz batch that drives QS-5 does not flow.
- **Pre-edit `git diff` on either scaffolding file is non-empty** (Step 0.2) → STOP. Reverting would destroy user work; do not stash.
- **IB Gateway paper not connected** (`/health` not `port_listening:true` on 4002) → STOP. Do NOT switch to live IB. Approve 2FA and retry, or defer.
- **`dev.sh` refuses to boot** citing `core_dev` → STOP. `DATABASE_URL` is misconfigured; do not override — fix `.env`.
- **Scaffolding won't typecheck after 2 attempts** → STOP. Do not commit broken code; revert and report the import-form problem.
- **`git status` after Step 6 shows any `web/` change** → STOP. The revert is incomplete; clean it before writing the artifact.
- **Any step tempts a live-money order** → STOP. Paper only (port 4002). This task never touches live.
- **More than the two named files need editing for scaffolding** (`WorkspaceShell.tsx`, `usePrices.ts`) → STOP. The anchors have drifted — reconcile against HEAD before proceeding.
- **Empty/one-sided sample buffer** (no `"kind":"batch"` or no `"kind":"commit"` entries) despite live quotes → STOP. Either the flag didn't inline (Next started without `NEXT_PUBLIC_PROFILE_QS5=1`), the mounted section is dashboard/operator (consumers not rendered), or quotes aren't flowing. Fix and re-capture; do not fabricate numbers.
- **Delayed quotes / missing entitlement / starved symbols**: if the paper session serves delayed data, shows entitlement/line-budget errors (e.g. error 101 starvation, memory `project_ib_marketdata_line_budget_shared`), or fewer than **10 live-ticking symbols** are streaming → mark the QS-5 run **INCONCLUSIVE — degraded quote flow (<reason>)** in the artifact; a starved feed does not represent normal RTH load. Do not present its numbers as the QS-5 answer.
- **No fill after ~3 min** on the paper BUY → record `fill_to_ui: INCONCLUSIVE — paper order did not fill`, cancel the working order, verify 0 working F orders (Step 5.2 query), and move on.
- **Measurement-overhead caveat (record, don't abort):** the `<Profiler>` wrapper adds per-commit cost. Buffering (not per-commit console.log) keeps it small; for borderline results (8–16 ms band) state the caveat explicitly and let the paint-inclusive trace decide.

---

## Rollback

Nothing to roll back in the product — this PR adds only `docs/fable/measurements-2026-07-05.md`. If the branch is abandoned: `git checkout master && git branch -D chore/fable-p1-3-frontend-measurements`. The temporary scaffolding in `web/components/WorkspaceShell.tsx` + `web/lib/usePrices.ts` is reverted in Step 6 before any commit (safe because Step 0.2 verified both files started clean); if a stray commit captured it, `git checkout <commit>^ -- <file>` and amend. No migrations, no schema, no order-path code touched → no incident-history row required (this is not an order-path change).
