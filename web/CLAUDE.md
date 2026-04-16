# web/ — CLAUDE.md

Frontend (Next.js App Router) + all user-facing calculations, pricing, share cards. Root `CLAUDE.md` is authoritative for policy. Component-level reference (regime, VCG, reports, share cards, WS state machine, seasonality): `docs/reference/web-ui-reference.md`. Brand spec: `brand/CLAUDE.md`.

## ⛔ UI Verification

**E2E browser verification for ALL UI work.** Primary: `chrome-cdp`. Fallback: Playwright (`web/playwright.config.ts`). No UI change done until visually confirmed. Don't assume code changes produce the expected visual result — verify rendered output in the browser before committing.

## Calculations — Correctness Rules

These are bug-prevention invariants. Every PR touching pricing/P&L must preserve them.

### Credit/Debit Sign Convention

**Preserve the sign throughout the entire display pipeline.** Never use `Math.abs()` or equivalent on option prices/values without explicit approval. Credits display as negative, debits as positive. Applies to P&L cards, share images, order forms, and all price displays.

### Daily Change %

```
Day Chg % = Daily P&L / |Yesterday's Close Value| × 100
NEVER divide by entry cost.
```

Per-leg: `sign × (last - close) × contracts × 100`. Denominator: `sign × close × contracts × 100`. Impl: `getOptionDailyChg()` in `WorkspaceSections.tsx`. Tests: `daily-chg.test.ts`.

### Spread Net Mid

```
Spread Mid = SUM(sign × (bid + ask) / 2) per leg
```

Via `legPriceKey()` WS bid/ask. Never use underlying for spread orders. Impl: `resolveOrderLastPrice()`.

### Combo Natural Market Bid/Ask

**CRITICAL:** Always use cross-fields for natural market, never `sign * bid` and `sign * ask`.

```
To BUY combo:  pay ASK on BUY legs, receive BID on SELL legs
To SELL combo: receive BID on BUY legs, pay ASK on SELL legs

Example (bull call spread: BUY $200C, SELL $210C):
  $200C: bid=4.50, ask=4.70
  $210C: bid=2.00, ask=2.20

  netAsk (cost to open) = 4.70 - 2.00 = 2.70
  netBid (proceeds to close) = 4.50 - 2.20 = 2.30
  mid = 2.50

WRONG (mid-mid):
  netBid = sign*bid = 4.50 - 2.00 = 2.50
  netAsk = sign*ask = 4.70 - 2.20 = 2.50
  Result: bid = ask = mid = 2.50 ❌
```

**Implementations (all use correct algorithm):**

- `computeNetOptionQuote()` in `optionsChainUtils.ts`
- `ComboOrderForm.netPrices` in `OrderTab.tsx`
- `resolveOrderPriceData()` for BAG in `ModifyOrderModal.tsx`

Tests: `order-reliability.test.ts` ("ComboOrderForm net price calculation").

### Total P&L %

```
P&L % = (Market Value - Entry Cost) / |Entry Cost| × 100
```

### Per-Leg P&L (expanded combo rows)

```
Leg P&L = sign × (|MV| − |EC|)   // LONG: MV−EC, SHORT: EC−MV
```

Sum of legs = position P&L. Uses WS price, fallback IB sync. Impl: `LegRow` in `PositionTable.tsx`.

### Price Resolution Priority

| Context                | Source                                                                      |
| ---------------------- | --------------------------------------------------------------------------- |
| Stock                  | `prices[ticker].last`                                                       |
| Single-leg option      | `prices[optionKey(...)].last`                                               |
| Multi-leg spread       | Net from each leg's `prices[legPriceKey(...)]`                              |
| BAG order last         | `resolveOrderLastPrice()` — net mid from legs                               |
| BAG modify BID/MID/ASK | `resolveOrderPriceData()` in `ModifyOrderModal.tsx`                         |
| Order form BID/MID/ASK | Same as PriceBar                                                            |
| PriceBar in modal      | `resolvePriceBar()` — option-level for single-leg, underlying for multi-leg |

**Never show underlying price where user expects option/spread price. Show "---" if unavailable.**

### IB Combo (BAG) Order Leg Convention

**ComboLeg.action = spread structure, NOT trade direction.** `Order.action` (BUY/SELL) controls open/close; IB reverses legs when SELL.

**Rule:** Always `LONG → BUY`, `SHORT → SELL` in ComboLeg.action regardless of order direction. Never flip — causes double-reversal → IB error 201.

Impl: `ComboOrderForm` (`OrderTab.tsx`), `OrderBuilder` (`OptionsChainTab.tsx`).

### Exposure Delta Sign Rule

`rawDelta = sign * lp.delta` where `sign = -1` for SHORT. LONG Call → +, SHORT Call → −, LONG Put → −, SHORT Put → +. Impl: `web/lib/exposureBreakdown.ts`. Tests: `exposure-breakdown.test.ts` (3).

### Data Normalization

JSON data files: always `"ticker"`. IB contracts: `"symbol"`. Read defensively: `t.get("ticker") or t.get("symbol")`.

## Xenon API Client

`xenonFetch()` in `web/lib/xenonApi.ts` — all Next.js routes call FastAPI via this helper (never `spawn()`). Attaches Clerk Bearer token automatically. Errors surfaced as `XenonApiError` with upstream status + detail preserved.

## Multi-Broker Account Tabs

`AccountTabBar.tsx` — switches between IB (live trading) and Futu (read-only positions snapshot). Futu is observe-only: never send orders, never treat as a quote source. Adapter: `futuPortfolioAdapter.ts`. Sync hook: `useFutuPortfolio.ts` — **POST polling** (`/futu/sync`), not GET; the GET endpoint returns cached data and was causing stale-snapshot bugs (commit 1be17ea).

## uw-analyze — Cache-First Loading

`/uw-analyze` loads from disk cache instantly on page open, then refreshes in background via SSE. Do not block initial render on a fresh fetch. Hook: `useUwAnalyze.ts`. Cross-mount snapshot cache keeps tickers warm across route changes (commit 6cd7b49). Last-known-good merge preserves sticky enrichment fields across refreshes (commit 1faa663). Contract tests: `web/tests/uw-analyze-*.test.ts`.

## UW API Telemetry

`useUwStats.ts` polls `/api/uw-stats` every 10s. The "UW Today" sidebar row shows **daily-scoped** counters aligned to UW's 8PM ET quota reset: request count, cache-hit %, 2xx/4xx/5xx breakdown, and latency p95. Backed by the process-wide `scripts/utils/uw_api_stats.py` singleton, which rolls up hourly buckets across the current daily window (`get_stats_with_daily()` returns session + daily under a single lock). DST boundary via `ZoneInfo("America/New_York")`. Silent-fail hook — sidebar shows `—` placeholders when unavailable.

## Dev Commands

```bash
npm run dev           # next + IB realtime server + FastAPI (concurrent, from web/)
npm run typecheck     # tsc --noEmit
npm test              # Vitest (ASSISTANT_MOCK=1, NODE_ENV=test)
npm run test:e2e      # Playwright (no-server config)
```

## ⛔ Brand Identity — Mandatory for UI Work

See `brand/CLAUDE.md` for the full spec. Non-negotiables:

- 4px max border-radius on panels (badges: 999px capsule)
- All colors via tokens — no raw hex
- Mono for machine, sans for product — never reversed
- No decorative elements (glassmorphism, gradients, soft shadows)
