# Dashboard Renovation — Design

**Date:** 2026-06-15
**Status:** Design approved (brainstorm). Implementation plan pending.
**Source:** Adapts `docs/plans/2026-06-15-radon-port-ideas.md` § Area 1 — Dashboard.
Reference look: radon `web/components/dashboard/` + `globals.css:1016-1210`.

## Problem

xenon `/dashboard` renders **only** the AI chat panel
(`WorkspaceShell.tsx:446-448` → `<ChatPanel>`). A broker terminal lands on an
empty chat box; the portfolio, today's P&L, and working orders are invisible at
a glance. Meanwhile the shell **already hydrates** `portfolio`, `orders`,
`prices`, and `todayRealizedPnl` for every section — those props are simply
unused on the dashboard branch. This renovation wires them into a real landing
surface. **No new data plumbing.**

## Approved layout

Two-column landing surface, radon card styling re-skinned to xenon brand tokens:

```
        ┌── IB / FUTU account tabs (full width, reused AccountTabBar) ──┐
┌─────────────────────────────┬──────────────────────────┐
│ PORTFOLIO / 01              │ ASSISTANT                │  ← chat moves here
│  Net Liq      Today P&L     │  ┌────────────────────┐  │    (was full-bleed)
│  Open Risk    Cash          │  │ market analysis    │  │
│ ────────────────────────────│  │ thread …           │  │
│ WORKING & FILLED / 02       │  │                    │  │
│  Working(n)   Today's Fills │  └────────────────────┘  │
│  • BUY 1× QQQ @ $630.96     │  [ ask… ][▸]             │
│  [ALL ORDERS →]             │                          │
└─────────────────────────────┴──────────────────────────┘
```

- **Left column (~60%):** `Portfolio` snapshot card + `Working & Filled`
  snapshot card, stacked, each in a collapsible section.
- **Right column (~40%):** the existing `ChatPanel`, moved out of full-bleed
  into the rail. This is xenon's "live market intel" surface — the native
  replacement for radon's news rail.
- **Top:** the existing IB / FUTU `AccountTabBar` (built; today hidden on the
  dashboard branch). Switching account swaps the Portfolio card's numbers.

## Explicitly out of scope

- **Trading Candidates** (radon Scanner/Discover/LEAP/GARCH) — signal
  generation; violates xenon identity ("no signal generation, bring your own
  thesis"). Dropped, no empty frame.
- **Live Market Feed** (radon `DashboardNewsFeed` → themarketear.com scraper) —
  xenon runs no scraper, has no creds, and it's a standalone service project.
  Dropped, no empty frame. (See conversation 2026-06-15 for the scraper anatomy.)
- **Futu order support** — Futu is read-only today. `Working & Filled` is
  **IB-only**; under the FUTU tab it shows its empty state. Futu order support
  is a separate future effort (user-flagged).

## Components

### New

- `web/components/dashboard/DashboardSurface.tsx` — the 2-col grid. Props:
  `{ portfolio, orders, prices, realizedPnl, activeAccount }`. Renders the two
  snapshot cards (left) + `<ChatPanel activeSection="dashboard">` (right rail).
- `web/components/dashboard/PortfolioSnapshotCard.tsx` — Net Liq / Today P&L /
  Open Risk / Cash. Ported from radon, re-skinned.
- `web/components/dashboard/OrdersSnapshotCard.tsx` — top-3 working + top-3
  today's fills, with `All orders →` link. Ported from radon, re-skinned.
- `web/components/dashboard/DashboardSection.tsx` — collapsible section wrapper
  (eyebrow label + count + chevron). Ported from radon `DashboardSurface`'s
  inner `DashboardSection`.

### Reused (no change beyond render-condition)

- `AccountTabBar.tsx` — extend its render condition in `WorkspaceShell` to
  include the dashboard section.
- `ChatPanel.tsx` — rendered inside the rail instead of full-bleed.

## Data mapping (xenon types — verified)

`PortfolioSnapshotCard`, from `portfolio.account_summary` + `portfolio`:

| Cell      | Source                                                         |
| --------- | -------------------------------------------------------------- |
| Net Liq   | `account_summary.net_liquidation`                              |
| Today P&L | **`resolveAccountDayPnlValue(portfolio, prices)`** (see below) |
| Open Risk | `portfolio.total_deployed_dollars`                             |
| Cash      | `account_summary.cash ?? account_summary.settled_cash`         |

**Today P&L correctness (Futu):** do NOT use radon's `ibDaily ?? realizedPnl`
fallback — xenon's `realizedPnl` is IB-fill-derived and would be wrong under the
FUTU tab. Reuse `resolveAccountDayPnlValue()` from `MetricCards.tsx` (already
exported; branches on `source==="futu"` → intraday-from-prices, else IB streamed
`daily_pnl`). Tone: `>0` → `--signal-core`, `<0` → `--fault`, else
`--text-primary`. Preserve credit/debit sign convention (web/CLAUDE.md) — no
`Math.abs`.

`OrdersSnapshotCard`, from `orders`:

- Working = `orders.open_orders` (count + first 3, with `status`).
- Today's Fills = `orders.executed_orders` (count + first 3, with time).
- Describe helpers handle `OPT` / `BAG` / stock (port radon's `describeOrder` /
  `describeFill`).

**Formatters:** reuse `web/lib/format.ts` (`fmtUsd`, `fmtSignedUsd`) — NOT
radon's `lib/format/money.ts` (does not exist in xenon).

## Styling

Port radon `globals.css:1016-1210` (`.dashboard-surface`, `.dashboard-section*`,
`.snapshot-card*`, `.snapshot-grid*`, `.snapshot-cell*`) + create
`.panel-eyebrow` / `.panel-title` / `.panel-edge-trace` (xenon lacks them).
Re-skin to xenon tokens (`--signal-core`, `--fault`, `--text-primary/secondary/
muted`, `--border-dim`, `--border-focus`). Brand rules: **4px max radius, all
colors via tokens (no raw hex), no gradients / glass / soft shadows.**

Tone token map: radon `--core` → xenon `--signal-core`; radon `--fault` →
xenon `--fault`. radon's snapshot-cell value tone classes
(`--core`/`--fault`/`--neutral`) carry over.

## Wiring (`WorkspaceShell.tsx`)

- Dashboard branch (line 446): replace the bare `<ChatPanel>` with
  `<DashboardSurface portfolio={portfolio} orders={orders} prices={prices}
realizedPnl={todayRealizedPnl} activeAccount={activeAccount} />`.
- Extend the `AccountTabBar` render condition (currently
  `activeSection !== "dashboard" && activeSection !== "ticker-detail"`) to
  include `"dashboard"`. `MetricCards` stays excluded from the dashboard (the
  snapshot card is the dashboard's portfolio surface; the rich `MetricCards`
  row remains on the portfolio section).
- The dashboard `.content` becomes a 2-col grid container; the chat panel needs
  a height constraint inside the rail (radon's rail uses `min-height:0` +
  internal scroll — verify the chat thread scrolls, input pinned).

## Click-through

- `OrdersSnapshotCard` → `All orders →` links to `/orders` (radon pattern).
- Ticker links from rows are **deferred** (radon wires them; xenon has the
  `/[ticker]` route but the doc grades row-link wiring separately — keep this
  pass to the orders link only unless trivial).

## Mobile

Single-column stack at the existing breakpoint (radon `globals.css:1398`):
account tabs → Portfolio card → Working & Filled card → chat. Reuse xenon's
existing responsive `.content` behavior; verify the chat rail collapses below
the cards rather than beside them.

## Testing (red/green TDD; 95% target)

- **Vitest** (`web/tests/`):
  - `PortfolioSnapshotCard`: renders the 4 cells from a fixture; Today-P&L tone
    sign (pos/neg/zero); **Futu fixture uses intraday P&L, not IB realized**;
    null/empty `account_summary` → graceful `---`.
  - `OrdersSnapshotCard`: top-3 cap; working/fills counts; both-empty state;
    OPT/BAG/stock describe formatting; `All orders →` href.
  - `DashboardSection`: collapse toggle (`aria-expanded`, `hidden` body).
- **E2E (chrome-cdp, fallback Playwright):** land on `/dashboard` → portfolio
  card + orders card + chat rail all visible (no full-bleed chat); toggle a
  section; switch IB↔FUTU and confirm Portfolio numbers change while Working &
  Filled shows IB-only / empty under FUTU. **No UI change is done until visually
  confirmed in the browser.**

## Files

- **New:** `web/components/dashboard/{DashboardSurface,PortfolioSnapshotCard,OrdersSnapshotCard,DashboardSection}.tsx`
  - matching `web/tests/*` specs.
- **Modified:** `web/components/WorkspaceShell.tsx` (dashboard branch + tab-bar
  condition), `web/app/globals.css` (dashboard/snapshot styles + panel-\*).
- **Reference only:** radon `web/components/dashboard/*`, radon `globals.css:1016-1210`.
