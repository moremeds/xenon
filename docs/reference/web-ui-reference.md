# Web UI Reference

Policy rules (UI verification, calculation invariants, brand rules) live in `web/CLAUDE.md`. This file is component-level reference.

## CRI/Regime Staleness

`/api/regime` triggers `cri_scan.py` during market hours only. Logic: `web/lib/criStaleness.ts` (single source of truth). Tests: `web/tests/regime-cri-staleness.test.ts`.

| Condition                              | Stale? | Action           |
| -------------------------------------- | ------ | ---------------- |
| `data.date !== today (ET)`             | YES    | Background scan  |
| `market_open + mtime > 60s`            | YES    | Background scan  |
| `market_open === false + date = today` | NO     | Serve cached EOD |

## VCG (Volatility-Credit Gap) Tab

Tabbed into `/regime` page alongside CRI. Detects divergence between vol complex (VIX/VVIX) and credit markets (HYG).

| Component | File                                                       |
| --------- | ---------------------------------------------------------- |
| Hook      | `web/lib/useVcg.ts` (`VcgData` type, adaptive polling)     |
| Staleness | `web/lib/vcgStaleness.ts` (anchored to `scan_time` age)    |
| API route | `web/app/api/vcg/route.ts` (GET cached + SWR)              |
| Panel     | `web/components/VcgPanel.tsx`                              |
| Scanner   | `src/xenon/scanners/vcg.py` (20-session history)           |
| Share     | `src/xenon/shares/generate_vcg_share.py` (4 cards + tweet) |
| FastAPI   | `POST /vcg/scan` (60s cooldown), `POST /vcg/share`         |
| Cache     | `data/vcg.json`                                            |

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

| Metric | Component                                                                                                       | Source                             | Display             |
| ------ | --------------------------------------------------------------------------------------------------------------- | ---------------------------------- | ------------------- |
| VIX    | `DayChange`                                                                                                     | WS `last` vs WS `close`            | `+1.50 (+6.25%) ↑`  |
| VVIX   | `DayChange`                                                                                                     | WS `last` vs WS `close`            | `-5.00 (-4.35%) ↓`  |
| SPY    | `DayChange`                                                                                                     | WS `last` vs WS `close`            | `$+0.47 (+0.07%) ↑` |
| RVOL   | `PointChange`                                                                                                   | `intradayRvol - data.realized_vol` | `-0.01% intraday ↓` |
| COR1M  | strip value from WS `last` when available, otherwise `data.cor1m`; `PointChange` remains `data.cor1m_5d_change` | `37.25` + `-0.50 pts 5d chg ↓`     |

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

## Trade Specification Report — MANDATORY

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
