# UW Analyze Page Overhaul — Design

**Date:** 2026-04-08
**Status:** Draft (revised after tribunal review)
**Supersedes UI of:** `web/components/WorkspaceSections.tsx::UwAnalyzeSections`

> **Revision note (2026-04-08, post-review):** Path corrections to existing modules; consolidated existing 30s cache into the new TTL cache; added per-ticker singleflight lock; replaced non-existent `unusual_options` with `flow_alerts`; added `max_pain` fetch path; fixed FlowEvent idempotency; added zero-guards; allowed multi-source candidate tagging; documented atomic cache writes.

## Goal

Replace the single-ticker manual UW Analyze page with a portfolio-aware, periodically-refreshing dashboard that:

1. Adopts the visual language of `flow-analysis` (section stacks, action items strip, brand-compliant primitives).
2. Auto-seeds candidates from current portfolio underlyings ∪ watchlist.
3. Periodically re-runs analyses and surfaces meaningful changes since the last snapshot.
4. Folds per-ticker results by default; auto-expands rows with changes.
5. Adds two new tracking streams: daily open-interest deltas and an unusual-flow lifecycle log.

## Non-goals

- Multi-account UW configurations.
- Mutating watchlist from this page.
- Backfilling historical snapshots beyond `current` + `previous`.
- Replacing the existing per-ticker analysis pipeline (`scripts/analysis/uw_analyze.py`); we wrap it.

---

## Architecture

```
Browser  (UwAnalyzeSections)
  ── poll every 2m → GET /api/uw-analyze/portfolio
  ── manual "Refresh All" / per-row ↻ → POST /api/uw-analyze/refresh
  ── ad-hoc ticker form → POST /api/uw-analyze/refresh { tickers: [X] }
        │
        ▼ xenonFetch (Clerk JWT)
Next.js routes (web/app/api/uw-analyze/portfolio/route.ts, .../refresh/route.ts)
   thin proxy → FastAPI
        │
        ▼
FastAPI — extends existing scripts/api/routes/uw_analyze.py
   (existing 30s _cache dict is removed and replaced by UwAnalyzeCache below)
   GET  /uw-analyze/portfolio
     1. seed_candidates() → { ticker → sources[] } over (positions ∪ watchlist ∪ adhoc)
     2. for each ticker: cache.get_or_run(ttl by market state)
     3. diff vs previous snapshot → changes[]
     4. attach oi_changes, unusual_flow_events
     5. return { tickers: [...], action_items: [...] }
   POST /uw-analyze/refresh body: { tickers?: [...], adhoc?: bool }   force re-run
   POST /uw-analyze   (existing single-ticker route — kept for backward compat,
                      now delegates to cache.get_or_run with force=True)
        │
        ▼
UwAnalyzeCache (scripts/api/services/uw_analyze_cache.py)
   - in-memory dict + on-disk JSON: data/uw_analyze_cache.json
   - asyncio.Semaphore(3) caps total concurrent UW calls
   - per-ticker asyncio.Lock() = singleflight: concurrent requests
     for the same ticker collapse into one upstream fetch
   - per-ticker entry: { current, previous, oi_baseline, sources[], ts }
   - run_analysis() calls scripts.uw_analyze.run_analysis_with_data
     (the existing analyser at scripts/uw_analyze.py)
   - on-disk writes use tmpfile + os.replace for atomicity;
     load failures fall back to empty cache and log a warning

UwAnalyzeDailyJob (scripts/api/services/uw_analyze_daily_job.py)
   - registered as an asyncio task inside the FastAPI lifespan
     in scripts/api/server.py (no cri_scan_service equivalent in-process today)
   - sleep-loop: compute next 15:50 ET trigger, await, run, repeat
   - guarded by a module-level _job_running flag to prevent double-runs
     across hot reloads
   - snapshots EOD OI per ticker → data/uw_oi_snapshots/<ticker>.json
   - advances open unusual_flow_events → updates daily_track + anomaly status
```

> **Multi-worker note.** Uvicorn is currently launched with a single worker for this API (see `scripts/api/server.py`). The lifespan asyncio task is therefore safe today. If workers > 1 is ever introduced, the daily job MUST be moved to an OS-level launchd plist (mirroring `com.xenon.cri-scan.plist`) to avoid duplicate runs. This is documented as a deployment invariant in **Risks**.

### Boundaries

| Unit                       | Purpose                     | Inputs                                            | Outputs               |
| -------------------------- | --------------------------- | ------------------------------------------------- | --------------------- |
| `seed_candidates`          | Build the candidate set     | portfolio JSON, `data/watchlist.json`, ad-hoc set | `set[str]` of tickers |
| `UwAnalyzeCache`           | Persist + concurrency-bound | candidate ticker, force flag                      | `CacheEntry`          |
| `uw_analyze_diff`          | Pure diff function          | `prev`, `curr` snapshots                          | `Change[]`            |
| `oi_tracker`               | EOD OI snapshot + diff      | UW chain, prior `OiSnapshot`                      | `OiChange[]`          |
| `unusual_flow_tracker`     | Lifecycle of flow events    | diffs + EOD prices/OI                             | updated `FlowEvent[]` |
| `UwAnalyzeSections` (UI)   | Render dashboard            | `/portfolio` payload                              | DOM                   |
| `GexProfileChart` (shared) | Render GEX bars             | `GexBucket[]`, spot                               | SVG                   |

---

## Data shapes

### Snapshot

```ts
type UwSnapshot = {
  ticker: string;
  ts: string; // ISO
  report: UwReport; // existing scripts/uw_analyze.py output (run_analysis_with_data)
  display: UwDisplay; // existing /uw-analyze response display slice
  derived: {
    gex_sign: "POSITIVE" | "NEGATIVE" | "NEUTRAL";
    gex_flip_strike: number | null;
    max_pain: number | null; // see "Max pain plumbing" — added by this spec
    call_wall: number | null;
    put_wall: number | null;
    iv_rank: number | null;
    net_call_premium: number | null;
    net_put_premium: number | null;
    flow_score: number | null;
    spot: number | null; // captured at snapshot time, used by relative diff rules
  };
};
```

### Max pain plumbing (new)

`max_pain` is not currently on `TickerData` / `AnalysisReport` / the `/uw-analyze` response. To support `MAX_PAIN_SHIFT` it must be threaded through:

1. Add max-pain fetch in `scripts/analysis/ticker_data.py` via the existing UW client (`docs/reference/unusual_whales_api.md` for the endpoint).
2. Add `max_pain: float | None` to `analysis/models.TickerData`.
3. Surface it in `UwAnalyzeDisplay` returned by `scripts/api/routes/uw_analyze.py`.
4. `derived.max_pain` is then a straight copy from `display.max_pain`.

If the upstream call fails or returns null, `derived.max_pain = None` and the `MAX_PAIN_SHIFT` rule is silently skipped (see zero-guards below).

### Cache file (`data/uw_analyze_cache.json`)

```ts
type CacheFile = {
  updated_at: string;
  entries: {
    [ticker: string]: {
      current: UwSnapshot;
      previous: UwSnapshot | null;
      oi_baseline: OiSnapshot | null;
      sources: Array<"portfolio" | "watchlist" | "adhoc">; // a ticker can be in multiple sets
    };
  };
};
```

**Atomic writes.** All persistence to `data/uw_analyze_cache.json` and `data/uw_unusual_flow_log.json` writes to a sibling tmpfile then `os.replace()` → caller observes either old or new file, never a half-written one. On load failure (corrupt JSON, missing file) the cache logs a warning and starts empty; the next analysis run rebuilds it. A single `asyncio.Lock` guards all disk writes within the process.

### Change

```ts
type Change = {
  code:
    | "GEX_FLIP_SIGN"
    | "MAX_PAIN_SHIFT"
    | "IV_RANK_JUMP"
    | "UNUSUAL_CALL_SWEEP"
    | "UNUSUAL_PUT_SWEEP";
  label: string;
  prev: number | string | null;
  curr: number | string | null;
  severity: "info" | "warn" | "alert";
};
```

### `/portfolio` response

```ts
type PortfolioResponse = {
  fetched_at: string;
  market_state: "open" | "closed";
  ttl_seconds: number;
  tickers: Array<{
    ticker: string;
    sources: Array<"portfolio" | "watchlist" | "adhoc">;
    snapshot: UwSnapshot;
    prev_ts: string | null;
    changes: Change[];
    oi_changes: OiChange[];
    unusual_flow_events: FlowEvent[];
  }>;
  action_items: Array<{
    ticker: string;
    code: string;
    label: string;
    severity: "warn" | "alert";
  }>;
};
```

### Change-detection thresholds (Standard set)

| Code                 | Trigger                                      |
| -------------------- | -------------------------------------------- | ---------- | ------------ |
| `GEX_FLIP_SIGN`      | `derived.gex_sign` flipped POSITIVE↔NEGATIVE |
| `MAX_PAIN_SHIFT`     | `                                            | max_pain Δ | / spot ≥ 2%` |
| `IV_RANK_JUMP`       | `                                            | iv_rank Δ  | ≥ 10pts`     |
| `UNUSUAL_CALL_SWEEP` | `net_call_premium` Δ ≥ +$5M                  |
| `UNUSUAL_PUT_SWEEP`  | `net_put_premium` Δ ≤ −$5M                   |

Severity: `GEX_FLIP_SIGN` and unusual sweeps → `alert`; `MAX_PAIN_SHIFT`, `IV_RANK_JUMP` → `warn`.

**Zero / null guards (mandatory in the diff engine):**

- `MAX_PAIN_SHIFT` skipped if either `prev.max_pain`, `curr.max_pain`, or `curr.spot` is null or 0.
- `IV_RANK_JUMP` skipped if either side is null.
- `UNUSUAL_*_SWEEP` skipped if either side is null. The Δ comparison treats `null → number` as a non-event (no synthetic baseline).
- `GEX_FLIP_SIGN` skipped if either side is `NEUTRAL` or null — only fires on a true POSITIVE↔NEGATIVE crossing.
- OI delta rule (`oi_changes`): if `prev_oi == 0` the percentage gate is bypassed and the absolute-add gate alone decides; if `curr_oi == 0` and `prev_oi > 0` the rule fires when absolute drop ≥ 1000 contracts.

---

## Periodic refresh

| Param                | Market open   | Market closed |
| -------------------- | ------------- | ------------- |
| Cache TTL            | 5 min         | 30 min        |
| Client poll interval | 2 min         | 5 min         |
| Backend concurrency  | 3 in parallel | 3 in parallel |

`get_or_run(ticker, force=False)`:

1. Acquire the per-ticker `asyncio.Lock` (singleflight). Concurrent requests for the same ticker collapse into one upstream call; followers receive the freshly-computed snapshot.
2. Inside the lock, re-check the cache (double-checked locking) — a follower may now see fresh data and return it without running analysis.
3. If `force` or `now - entry.current.ts > TTL` → acquire the global `asyncio.Semaphore(3)`, run analysis, rotate `previous = current`, `current = new`, persist atomically.
4. Else return cached `current`.

Lock + semaphore order is always lock → semaphore (never the reverse), so a follower waiting on a per-ticker lock cannot starve the global pool.

Manual "Refresh All" button posts `/refresh` with no body → full force-refresh.
Per-row ↻ posts `/refresh { tickers: [X] }`.
Ad-hoc form posts `/refresh { tickers: [X], adhoc: true }` → entry tagged `source: "adhoc"`.

---

## Daily OI tracker

**Cron:** asyncio task in FastAPI lifespan, fires at 15:50 ET on trading days. Pattern mirrors existing `cri_scan_service`.

**Per ticker:**

1. Fetch chain via existing UW client.
2. Build `OiSnapshot = { data_date, strikes: { [strike]: { call_oi, put_oi } } }`.
3. Persist to `data/uw_oi_snapshots/<ticker>.json` (keep last 5 days, rotate).
4. Diff against the most recent prior snapshot.

**Notable change rule** (all must hold):

- |Δ OI| / prev_oi ≥ 25%
- |Δ OI| absolute ≥ 1000 contracts
- |strike - spot| / spot ≤ 5%

**OiChange:**

```ts
type OiChange = {
  strike: number;
  side: "call" | "put";
  prev_oi: number;
  curr_oi: number;
  delta: number;
  delta_pct: number;
  label: string; // "+12.4K calls @ $900 (+38%)"
};
```

`oi_changes` are attached to the `/portfolio` response; the UI shows them in a folded `OPEN INTEREST DELTA` panel inside each ticker card. The most extreme change per ticker also surfaces in the global ACTION ITEMS strip.

---

## Unusual flow lifecycle tracker

**Storage:** `data/uw_unusual_flow_log.json`

```ts
type FlowEvent = {
  id: string; // sha1(ticker|side|strike|expiry|trade_date) — STABLE, no detected_at
  ticker: string;
  side: "call" | "put";
  strike: number;
  expiry: string; // YYYY-MM-DD
  detected_at: string; // first time we observed it (informational, NOT in id)
  initial: {
    premium_usd: number;
    oi: number;
    volume: number;
    mid: number;
    underlying_price: number; // captured at detection — required by anomaly rules
  };
  daily_track: Array<{
    date: string;
    oi: number;
    mid: number;
    underlying_price: number; // needed to compute relative underlying move
    pct_change_premium: number; // vs initial.mid
  }>;
  status: "open" | "closed" | "anomaly" | "expired";
  anomaly_reason?: string;
  closed_at?: string;
};
```

**Capture (during normal refresh):** When a Change with `code in {UNUSUAL_CALL_SWEEP, UNUSUAL_PUT_SWEEP}` is emitted, upsert a `FlowEvent` using the dominant alert from `td.flow_alerts` for that side (largest `total_premium`). `flow_alerts` already exists on `TickerData` (`scripts/analysis/models.py:82`); this spec does NOT introduce a new `unusual_options` field. Pull `strike`, `expiry`, `volume`, `open_interest`, `mid`/`fill_price`, and `total_premium` from the alert payload; pull `underlying_price` from `display.spot`. The `id` (sha1 of `ticker|side|strike|expiry|trade_date`) makes the upsert truly idempotent — multiple polls on the same trading day for the same contract collapse to one event.

**Daily track (15:50 ET cron, after OI snapshot):**
For each `status: "open"` event, fetch contract OI + mid, append a `daily_track` row.

**Anomaly rules** (any one triggers `status="anomaly"`; all rules skipped within 3 DTE of `expiry` to avoid late-cycle decay false positives):

1. **Premium collapse** — `mid` dropped ≥ 60% from `initial.mid` AND `|underlying_price - initial.underlying_price| / initial.underlying_price < 1.5%`.
2. **OI evaporation** — `oi` dropped ≥ 50% from `initial.oi` within 3 trading days of `detected_at`.
3. **Closing volume spike** — single-day volume > 80% of OI in opposite direction.

**Closeout:**

- `oi <= initial.oi` → `status="closed"`, set `closed_at`.
- Past `expiry` → `status="expired"`.

**UI surface:**

- Per-ticker folded `UNUSUAL FLOW TRACKER` panel showing open + recently-closed events with status pill (`OPEN`/`CLOSED`/`ANOMALY`/`EXPIRED`) and a sparkline of `daily_track.mid`.
- Anomaly events appear in the global ACTION ITEMS strip with severity `alert`:
  `NVDA $900C 5/17 — premium collapsed -68% (positioning unwound)`.

---

## UI

### Page-level layout

```
┌─────────────────────────────────────────────────────────────────────────┐
│  UW ANALYZE                                       [↻ REFRESH ALL]  9:47 │
│  12 underlyings · auto-refresh 2m · 3 changed since 9:42                │
│  [+ ad-hoc ticker ____  ANALYSE]                                        │
├─────────────────────────────────────────────────────────────────────────┤
│  ⚠ ACTION ITEMS                                                         │
│   • NVDA — GEX flipped negative (was +$2.1B → -$480M)                   │
│   • TSLA — unusual put sweep $4.2M @ 240P                               │
│   • NVDA $900C 5/17 — premium collapsed -68% (positioning unwound)      │
├─────────────────────────────────────────────────────────────────────────┤
│  ▼ NVDA   $872.40  +1.2%  [GEX FLIP] [CHANGED]  Bias BULL Grade A 09:47│   ← expanded (changed)
│  (full ticker card body — see "Per-ticker expanded card" below)         │
├─────────────────────────────────────────────────────────────────────────┤
│  ▶ TSLA   $243.10  -0.8%  [PUT SWEEP] [CHANGED]   9:47  ⌄  ↻            │
│  ▶ MSFT   $418.20  +0.4%                          9:47  ⌄  ↻            │
│  ▶ ...                                                                   │
└─────────────────────────────────────────────────────────────────────────┘
```

### Per-ticker expanded card (canonical reference)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ ▼ NVDA   $872.40  +1.2%   [GEX FLIP] [CHANGED]  Bias BULL  Grade A  09:47  ⌃│
├──────────────────────────────────────────────────────────────────────────────┤
│ IDENTITY   Sector Tech · Mode FULL · IV rank 42 · Fetched 09:47:12          │
│ THESIS     Structure LONG_CALL_VERTICAL · Regime POSITIVE_GAMMA · Bias BULL │
│            Dealers short gamma below 870 → vol amplification on breakdown   │
├──────────────────────────────────────────────────────────────────────────────┤
│ ┌─ MARKET STRUCTURE 22/28 ─┐ ┌─ VOLATILITY 18/28 ─┐                          │
│ │ GEX sign     POSITIVE    │ │ IV rank    42      │                          │
│ │ Flip dist    -0.4%       │ │ IV         28.3    │                          │
│ │ Call wall    900         │ │ RV         24.1    │                          │
│ │ Put wall     840         │ │ Term       CONTANGO│                          │
│ │ γ per 1%     $1.2B       │ │                    │                          │
│ └──────────────────────────┘ └────────────────────┘                          │
│ ┌─ FLOW 17/24 ─────────────┐ ┌─ POSITIONING 14/20 ┐                          │
│ │ Net call prem  +$28M     │ │ (or n/a notice)    │                          │
│ │ Net put prem   -$11M     │ │                    │                          │
│ │ Short vol      0.42      │ │                    │                          │
│ └──────────────────────────┘ └────────────────────┘                          │
├──────────────────────────────────────────────────────────────────────────────┤
│ GEX PROFILE — Net gamma by strike      ■ Positive(stab)  ■ Negative(destab)  │
│                                                                              │
│   910  +4.6% │                       ████████  +$2.1B      MAX MAGNET ▲      │
│   900  +3.2% │                  ██████         +$1.2B      CALL WALL         │
│   890  +2.0% │              ████               +$680M                        │
│   880  +0.9% │          ███                    +$310M                        │
│   872  SPOT ─┼─────────────────────────────────────────────  ◄ SPOT          │
│   870  -0.3% │ ██                              -$120M       GEX FLIP ◄       │
│   860  -1.4% │ █████                           -$480M                        │
│   850  -2.5% │ ████████                        -$980M                        │
│   840  -3.7% │ ████████████                    -$1.6B      PUT WALL          │
│   830  -4.8% │ ██████████████                  -$2.1B      MAX ACCEL ▼       │
├──────────────────────────────────────────────────────────────────────────────┤
│ ▶ OPEN INTEREST DELTA (since prior session)               3 notable  ⌄       │  ← folded
│ ▶ UNUSUAL FLOW TRACKER                                    1 OPEN · 1 ANOM ⌄  │  ← folded
├──────────────────────────────────────────────────────────────────────────────┤
│ NOTES   • Unusual call sweep at 900 strike, $4.2M premium                    │
│         • Max pain shifted 860 → 855 since last run                          │
└──────────────────────────────────────────────────────────────────────────────┘
```

The GEX section is rendered by the shared `GexProfileChart` component (extracted from `GexPanel.tsx`). The strike-row glyph above is an ASCII proxy for the SVG divergent bar chart: strike label + % from spot on the left, bar from a center spine, GEX value + tag (`CALL WALL`, `PUT WALL`, `GEX FLIP`, `SPOT`, `MAX MAGNET`, `MAX ACCEL`) on the right. SPOT row gets a dashed indicator line. Colors come from `var(--signal-core)` (positive) and `var(--fault)` (negative) — same tokens as the Regime page.

### Component structure

| Component           | File                                                      | Notes                                                                                                                  |
| ------------------- | --------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `UwAnalyzeSections` | `web/components/WorkspaceSections.tsx` (rewrite in place) | Top-level page; calls `useUwPortfolio()`                                                                               |
| `UwTickerRow`       | inline                                                    | Collapsible `.section`; header always visible                                                                          |
| `UwTickerBody`      | inline                                                    | identity + thesis + 4 buckets + GexProfileChart + OI panel + flow tracker + notes                                      |
| `GexProfileChart`   | `web/components/charts/GexProfileChart.tsx`               | **Extracted from** `GexPanel.tsx`. Shared by Regime page + UW Analyze. Props: `{ buckets: GexBucket[], spot: number }` |
| `OiDeltaPanel`      | inline                                                    | Folded by default; lists `OiChange[]`                                                                                  |
| `UnusualFlowPanel`  | inline                                                    | Folded by default; lists `FlowEvent[]` with status pills + sparkline                                                   |
| `useUwPortfolio`    | `web/lib/useUwPortfolio.ts`                               | Hook: poll every 2m / 5m by market state, expose `{ data, refresh, refreshOne }`                                       |

### Folding rules

- All ticker rows collapsed by default.
- Rows with `changes.length > 0` auto-expanded and sorted to the top.
- Within a row, OI Delta and Unusual Flow sub-panels are folded; user can expand independently.
- Ticker order: changed (alphabetical) → unchanged (alphabetical).

### Brand compliance

- Reuse existing `.section`, `.section-header`, `.section-body`, `.alert-box`, `.alert-item`, `.pill` primitives → 4px radius and token colors come for free.
- All colors via tokens (no raw hex). Pills use `.bullish` / `.bearish` / `.warning` / `.undefined` / `.neutral` variants already present.
- Mono for numeric/strike values, sans for labels.
- No new shadows, gradients, or glassmorphism.

---

## File map

### New

```
scripts/api/services/uw_analyze_cache.py        # cache + per-ticker locks + semaphore
scripts/api/services/uw_analyze_diff.py         # pure diff function with zero-guards
scripts/api/services/uw_analyze_oi_tracker.py   # OI snapshot + diff
scripts/api/services/uw_analyze_flow_tracker.py # unusual flow lifecycle
scripts/api/services/uw_analyze_daily_job.py    # 15:50 ET asyncio loop
scripts/tests/test_uw_analyze_cache.py
scripts/tests/test_uw_analyze_diff.py
scripts/tests/test_uw_analyze_oi_tracker.py
scripts/tests/test_uw_analyze_flow_tracker.py

web/app/api/uw-analyze/portfolio/route.ts       # proxy
web/app/api/uw-analyze/refresh/route.ts         # proxy
web/lib/useUwPortfolio.ts                       # polling hook
web/components/charts/GexProfileChart.tsx       # extracted shared chart
web/lib/uwAnalyzeTypes.ts                       # shared TS types matching backend

data/uw_analyze_cache.json                      # created on first run
data/uw_oi_snapshots/                           # dir, per-ticker JSON
data/uw_unusual_flow_log.json                   # created on first event
```

### Modified

```
scripts/api/server.py                           # register lifespan asyncio task for daily job
scripts/api/routes/uw_analyze.py                # extend with /portfolio + /refresh; remove the existing 30s _cache dict (replaced by UwAnalyzeCache)
scripts/analysis/ticker_data.py                 # add max_pain fetch
scripts/analysis/models.py                      # add max_pain to TickerData
web/components/WorkspaceSections.tsx            # rewrite UwAnalyzeSections
web/components/GexPanel.tsx                     # import shared GexProfileChart instead of inline
```

### Untouched (intentional)

```
scripts/uw_analyze.py                           # existing analyser entrypoint run_analysis_with_data; cache calls into it
web/lib/useUwAnalyze.ts                         # existing hook stays for any other consumers
```

---

## Testing

Target: 95% coverage on new code; red/green TDD.

| Layer         | Test file                         | Coverage                                                                                                                                        |
| ------------- | --------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| Diff engine   | `test_uw_analyze_diff.py`         | All 5 change codes — fires at threshold, silent below; severity correct; null-safety on missing fields                                          |
| Cache         | `test_uw_analyze_cache.py`        | TTL behavior open/closed, force refresh, semaphore caps to 3, persist + reload from disk, candidate seeding from positions + watchlist + ad-hoc |
| OI tracker    | `test_uw_analyze_oi_tracker.py`   | 25%/1k/±5% gates, snapshot rotation (keep last 5), no-op when prior snapshot missing                                                            |
| Flow tracker  | `test_uw_analyze_flow_tracker.py` | Capture from sweep, daily track append, all 3 anomaly rules, closeout when oi ≤ initial, expired transition                                     |
| Routes        | `test_uw_analyze_routes.py`       | `/portfolio` 200 with sample fixtures; `/refresh` honors body; auth required                                                                    |
| Frontend hook | `useUwPortfolio.test.ts` (Vitest) | Polling cadence by market state; refresh + refreshOne; error surfacing                                                                          |
| UI            | E2E via chrome-cdp                | Page loads, auto-expand on changed row, manual fold/unfold, ↻ triggers refresh; brand 4px radius / no raw hex visual check                      |

---

## Tribunal review log (2026-04-08)

Findings from Gemini (advisory) + Claude (codebase-aware) review of the first draft. All accepted; spec updated above.

| #   | Issue                                                                                   | Resolution                                                                                                |
| --- | --------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| 1   | File path drift: `scripts/analysis/uw_analyze.py` and `scripts/api/main.py` don't exist | Corrected to `scripts/uw_analyze.py` and `scripts/api/server.py` everywhere                               |
| 2   | `report.unusual_options` doesn't exist                                                  | Replaced with `td.flow_alerts` (verified at `scripts/analysis/models.py:82`)                              |
| 3   | FlowEvent `id` included `detected_at` → not idempotent across polls                     | Removed `detected_at` from id; uses `trade_date` instead                                                  |
| 4   | `max_pain` not on existing models                                                       | Added "Max pain plumbing" section with concrete add path through `ticker_data.py` → `models.py` → display |
| 5   | FlowEvent missing `underlying_price` for the "underlying moved <1.5%" rule              | Added to `initial` and `daily_track`; rule rewritten to use it                                            |
| 6   | No per-ticker singleflight under the global semaphore                                   | Added per-ticker `asyncio.Lock` with double-checked locking                                               |
| 7   | ZeroDivision risks on `prev_oi=0` and null `spot`                                       | Added explicit zero-guards section under change-detection thresholds                                      |
| 8   | `source` was a single string but a ticker can be in portfolio AND watchlist             | Changed to `sources: string[]` in cache and response                                                      |
| 9   | Existing 30s `_cache` in `scripts/api/routes/uw_analyze.py` not addressed               | Existing dict removed; legacy `/uw-analyze` route delegates to `UwAnalyzeCache`                           |
| 10  | Cache file write atomicity / corruption recovery undefined                              | Added "Atomic writes" paragraph; tmpfile + `os.replace()`; corrupt files start empty                      |
| 11  | `cri_scan_service` referenced as a pattern but no such in-process service exists        | Replaced with explicit asyncio loop spec; added multi-worker invariant note                               |

## Risks / open issues

- **UW rate limits.** 3-way concurrency + 5-min TTL = up to 12 calls per refresh. Watch logs first week; if 429s appear, drop concurrency to 2.
- **Watchlist drift.** Watchlist file has ~30+ tickers; portfolio adds more. Each refresh = N analyses. Mitigated by TTL — most calls hit cache.
- **OI snapshot accuracy at 15:50.** Some venues report final OI next morning. If we see drift, move snapshot to T+1 09:25 ET.
- **Anomaly false positives.** Premium collapse rule may fire on simple decay near expiry. Mitigation: skip events within 3 DTE of `expiry`.

---

## Out of scope (explicitly)

- Cross-ticker correlations.
- Historical snapshot beyond `current` + `previous` (no time series DB).
- Push notifications / email alerts.
- Mutating watchlist from this UI.
- Backfilling OI history before deploy date.
