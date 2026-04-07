# web/ — CLAUDE.md

Frontend (Next.js App Router) + all user-facing calculations, pricing, share cards, and reports. The root `CLAUDE.md` is authoritative for policy and trading rules — this file covers the web layer only.

## ⛔ UI Verification

**E2E browser verification for ALL UI work.** Primary: `chrome-cdp`. Fallback: Playwright (`web/playwright.config.ts`). No UI change done until visually confirmed. Don't assume code changes produce the expected visual result — verify rendered output in the browser before committing.

## CRI/Regime Staleness

`/api/regime` triggers `cri_scan.py` during market hours only. Logic: `web/lib/criStaleness.ts` (single source of truth). Tests: `web/tests/regime-cri-staleness.test.ts`.

| Condition | Stale? | Action |
|-----------|--------|--------|
| `data.date !== today (ET)` | YES | Background scan |
| `market_open + mtime > 60s` | YES | Background scan |
| `market_open === false + date = today` | NO | Serve cached EOD |

## VCG (Volatility-Credit Gap) Tab

Tabbed into `/regime` page alongside CRI. Detects divergence between vol complex (VIX/VVIX) and credit markets (HYG).

| Component | File |
|-----------|------|
| Hook | `web/lib/useVcg.ts` (`VcgData` type, adaptive polling) |
| Staleness | `web/lib/vcgStaleness.ts` (anchored to `scan_time` age) |
| API route | `web/app/api/vcg/route.ts` (GET cached + SWR) |
| Panel | `web/components/VcgPanel.tsx` |
| Scanner | `scripts/vcg_scan.py` (20-session history) |
| Share | `scripts/generate_vcg_share.py` (4 cards + tweet) |
| FastAPI | `POST /vcg/scan` (60s cooldown), `POST /vcg/share` |
| Cache | `data/vcg.json` |

**VCG-R thresholds:** RO = VIX > 28 + VCG > 2.5 + sign_ok. EDR = VIX > 25 + VCG 2.0–2.5. BOUNCE = VCG < -3.5. VVIX is severity amplifier (Tier 1/2/3), not a gate. HDR removed. Credit 5d gate removed. VCG adj replaces vcg_div.

## RegimePanel Market-Closed Rules

When `market_open === false`:
- Use `data.vix`/`data.vvix`/`data.spy` only (never WS `last`)
- `activeCorr` = `data.cor1m` (not rebuilt from sector ETFs)
- `liveCri` / `intradayRvol` = `null` (use `data.cri` / `data.realized_vol`)
- Don't update VIX/VVIX timestamps
- COR1M badge = DAILY

Tests: `regime-market-closed-values.test.ts`, `regime-market-closed-eod.spec.ts`, `regime-cor1m.spec.ts`

## RegimePanel Day Change Indicators

During market hours (`market_open === true`), the regime strip shows day change for live metrics:

| Metric | Component | Source | Display |
|--------|-----------|--------|---------|
| VIX | `DayChange` | WS `last` vs WS `close` | `+1.50 (+6.25%) ↑` |
| VVIX | `DayChange` | WS `last` vs WS `close` | `-5.00 (-4.35%) ↓` |
| SPY | `DayChange` | WS `last` vs WS `close` | `$+0.47 (+0.07%) ↑` |
| RVOL | `PointChange` | `intradayRvol - data.realized_vol` | `-0.01% intraday ↓` |
| COR1M | strip value from WS `last` when available, otherwise `data.cor1m`; `PointChange` remains `data.cor1m_5d_change` | `37.25` + `-0.50 pts 5d chg ↓` |

**Arrow placement**: Arrow icon is always to the **right** of the change text. Uses `display: flex` with `gap: 4px` in `.regime-strip-day-chg`.

Tests: `web/tests/regime-day-change.test.ts` (12 unit), `web/e2e/regime-day-change.spec.ts` (3 E2E)

## Regime History Charts

Two D3 charts, 20 sessions. Left: VIX (`#05AD98`) + VVIX (`#8B5CF6`), dual Y. Right: RVOL (`#F5A623`) + COR1M (`#D946A8`), dual Y. Height 440px. Component: `CriHistoryChart.tsx`.

## Portfolio Table Arrows

Price arrows in `PositionTable.tsx`/`WorkspaceSections.tsx`: `td.last-price-cell { white-space: nowrap }`, `.price-trend-icon { margin-left: 4px }`.

## Options Chain Sticky Header

`OptionsChainTab.tsx` — three required CSS rules:
1. `background: var(--bg-panel-raised)` on `.chain-header` + `.chain-side-label`
2. `position: sticky; top: 0` / `top: 24px`
3. `.chain-grid thead { position: relative; z-index: 10 }`

All three required or overlap bug returns. Tests: `chain-sticky-header.test.ts` (8).

## Exposure Delta Sign Rule

`rawDelta = sign * lp.delta` where `sign = -1` for SHORT. LONG Call → +, SHORT Call → −, LONG Put → −, SHORT Put → +. Impl: `web/lib/exposureBreakdown.ts`. Tests: `exposure-breakdown.test.ts` (3).

## WebSocket Connection State Machine (`usePrices.ts`)

`idle → connecting → open → closed`. Key design:
- `connStateRef` (ref) — `connect()` idempotent
- `socketGenRef` — ignores stale socket events
- Diff-based subscribe/unsubscribe over existing connection
- Callback refs eliminate stale closures
- Exponential backoff: `min(1000 * 2^n, 30000) + jitter`, max 10 attempts

Tests: `use-prices-ws-stability.test.ts` (25), `ws-connection-stability.spec.ts` (4).

## Seasonality Fallback

UW → EquityClock Vision → Cache. Route: `web/app/api/ticker/seasonality/route.ts`.
1. Cache check (`data/seasonality_cache/{TICKER}.json`)
2. UW API — all 12 months valid → done
3. Missing months → EquityClock chart → Claude Haiku Vision extraction
4. Merge (UW priority), cache as `uw+equityclock`, expires 1st of next month
5. Vision fails → return UW partial

API key: `resolveApiKey()` checks `ANTHROPIC_API_KEY`, `CLAUDE_CODE_API_KEY`, `CLAUDE_API_KEY`.

## ⭐ Trade Specification Report — MANDATORY

Required for any eval reaching Milestone 5.

```
Template : .pi/skills/html-report/trade-specification-template.html
Output   : reports/{ticker}-evaluation-{YYYY-MM-DD}.html
Reference: reports/goog-evaluation-2026-03-04.html
```

**10 required sections:** Header + gate status | 6 Summary Metrics | Milestone pass/fail | Dark Pool Flow | Options Flow | Context (seasonality + ratings) | Structure & Kelly | Trade Spec (exact order) | Thesis & Risk | Four Gates table.

Workflow: Complete M1-6 → Generate HTML → User confirmation → Execute via IB → Update `trade_log.json`, `portfolio.json`, `docs/status.md`.

## P&L Report

```
Template: .pi/skills/html-report/pnl-template.html
Output:   reports/pnl-{TICKER}-{YYYY-MM-DD}.html
Return on Risk = P&L / Capital at Risk (debit=net debit, credit=width−credit, long=premium)
```

## Share PnL Card

1200x630 PNG via `next/og` (Satori). Route: `web/app/api/share/pnl/route.tsx`. Component: `SharePnlButton.tsx`. Fonts: IBM Plex Mono `.woff` (Satori requires woff, not ttf). Theme: `web/lib/og-theme.ts`.

Wired into Executed Orders + Historical Trades on `/orders`. Position grouping: `groupExecutedOrders()`, `positionGroupShareData()`, `deriveGroupDescription()` in `WorkspaceSections.tsx`. Clipboard: `navigator.clipboard.write()` with `ClipboardItem`.

Tests: `share-pnl.test.ts` (24), `share-pnl.spec.ts` (6).

## Calculations — Correctness Rules

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

| Context | Source |
|---------|--------|
| Stock | `prices[ticker].last` |
| Single-leg option | `prices[optionKey(...)].last` |
| Multi-leg spread | Net from each leg's `prices[legPriceKey(...)]` |
| BAG order last | `resolveOrderLastPrice()` — net mid from legs |
| BAG modify BID/MID/ASK | `resolveOrderPriceData()` in `ModifyOrderModal.tsx` |
| Order form BID/MID/ASK | Same as PriceBar |
| PriceBar in modal | `resolvePriceBar()` — option-level for single-leg, underlying for multi-leg |

**Never show underlying price where user expects option/spread price. Show "---" if unavailable.**

### IB Combo (BAG) Order Leg Convention

**ComboLeg.action = spread structure, NOT trade direction.** `Order.action` (BUY/SELL) controls open/close; IB reverses legs when SELL.

**Rule:** Always `LONG → BUY`, `SHORT → SELL` in ComboLeg.action regardless of order direction. Never flip — causes double-reversal → IB error 201.

Impl: `ComboOrderForm` (`OrderTab.tsx`), `OrderBuilder` (`OptionsChainTab.tsx`).

### Data Normalization

JSON data files: always `"ticker"`. IB contracts: `"symbol"`. Read defensively: `t.get("ticker") or t.get("symbol")`.

## Xenon API Client

`xenonFetch()` in `web/lib/xenonApi.ts` — all Next.js routes call FastAPI via this helper (never `spawn()`). Attaches Clerk Bearer token automatically. Errors surfaced as `XenonApiError` with upstream status + detail preserved.

## ⛔ Brand Identity — Mandatory for UI Work

See `brand/CLAUDE.md` for the full spec. Non-negotiables:
- 4px max border-radius on panels (badges: 999px capsule)
- All colors via tokens — no raw hex
- Mono for machine, sans for product — never reversed
- No decorative elements (glassmorphism, gradients, soft shadows)
