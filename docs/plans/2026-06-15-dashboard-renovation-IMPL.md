# Dashboard Renovation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert xenon `/dashboard` from a full-bleed AI chat box into a 2-column landing surface — Portfolio + Working/Filled snapshot cards on the left, the existing ChatPanel moved into a right rail, IB/FUTU account tabs on top.

**Architecture:** Port radon's `dashboard/` components (DashboardSection, PortfolioSnapshotCard, OrdersSnapshotCard, DashboardSurface) into `web/components/dashboard/`, re-skinned to xenon brand tokens. No new data plumbing — `WorkspaceShell` already hydrates `portfolio`, `orders`, `prices`. Wire them into a new dashboard branch; the FUTU-correct Today-P&L comes from the existing `resolveAccountDayPnlValue()` (not radon's IB-realized fallback). Drop radon's Trading Candidates (signal generation — off-identity) and Live Market Feed (scraper xenon lacks) entirely.

**Tech Stack:** Next.js App Router, React, TypeScript, Vitest + @testing-library/react (jsdom), chrome-cdp / Playwright for E2E. lucide-react icons. Brand: 4px max radius, tokens only, no gradients/glass/shadows.

**Design spec:** `docs/plans/2026-06-15-dashboard-renovation-design.md`

---

## File Structure

| File                                                       | Responsibility                                                                                                         |
| ---------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `web/components/dashboard/DashboardSection.tsx` (new)      | Collapsible section wrapper: eyebrow label + count chip + chevron toggle. Pure presentational.                         |
| `web/components/dashboard/PortfolioSnapshotCard.tsx` (new) | 4-cell account snapshot: Net Liq / Today P&L / Open Risk / Cash. FUTU-aware Today-P&L via `resolveAccountDayPnlValue`. |
| `web/components/dashboard/OrdersSnapshotCard.tsx` (new)    | Top-3 working + top-3 today's fills, counts, `All orders →` link, empty state.                                         |
| `web/components/dashboard/DashboardSurface.tsx` (new)      | 2-col grid: snapshot cards (left) + `<ChatPanel>` (right rail).                                                        |
| `web/components/WorkspaceShell.tsx` (modify)               | Dashboard branch + tab-bar render condition; remove bare `<ChatPanel>`; swap imports.                                  |
| `web/app/globals.css` (modify)                             | Append dashboard/snapshot/panel CSS, re-skinned to xenon tokens.                                                       |
| `web/tests/dashboard-section.test.tsx` (new)               | Collapse toggle behavior.                                                                                              |
| `web/tests/portfolio-snapshot-card.test.tsx` (new)         | 4 cells, tone, FUTU-vs-IB Today P&L, null graceful.                                                                    |
| `web/tests/orders-snapshot-card.test.tsx` (new)            | Top-3 cap, counts, empty, OPT/BAG/stock describe, link href.                                                           |
| `web/tests/dashboard-surface.test.tsx` (new)               | Renders both cards + chat rail; orders=null → empty.                                                                   |

**Token map (radon → xenon):** `--line-grid` → `--border-dim`; `--bg-panel`/`--font-mono`/`--font-sans`/`--signal-core`/`--fault`/`--text-primary`/`--text-secondary`/`--text-muted` are identical. radon `.panel-edge-trace` `linear-gradient(...)` → flat `var(--signal-core)` (brand: no gradients).

**Running tests (canonical commands):** the project's Vitest config (`vitest.config.ts` at repo root) owns the `@` → `web` path alias and the `NODE_ENV=test ASSISTANT_MOCK=1` env. A bare `npx vitest run tests/foo` will NOT resolve `@/...` imports. Use exactly:

- Single file: `cd web && NODE_ENV=test ASSISTANT_MOCK=1 npx vitest run --config ../vitest.config.ts web/tests/<file>`
- Full suite: `cd web && npm test`
- Component render tests run under jsdom only via the per-file `/** @vitest-environment jsdom */` docblock (the global config default is `node`) — every new test file below includes it.

---

## Task 0: Feature branch

**Files:**

- Commit: `docs/plans/2026-06-15-dashboard-renovation-design.md`, `docs/plans/2026-06-15-dashboard-renovation-IMPL.md` (currently untracked)

- [ ] **Step 1: Create the branch**

```bash
cd /Users/chenxi/projects/xenon
git checkout -b feat/dashboard-renovation
```

- [ ] **Step 2: Commit the planning docs (they are untracked, not clean)**

The design spec and this plan are untracked working-tree files — `git status` will NOT be clean. Land them as the first commit on the branch so the work is self-documenting:

```bash
git add docs/plans/2026-06-15-dashboard-renovation-design.md docs/plans/2026-06-15-dashboard-renovation-IMPL.md
git commit -m "docs(dashboard): renovation design spec + implementation plan"
git status
```

Expected after commit: `On branch feat/dashboard-renovation` / `nothing to commit, working tree clean`.

---

## Task 1: DashboardSection (collapsible wrapper)

**Files:**

- Create: `web/components/dashboard/DashboardSection.tsx`
- Test: `web/tests/dashboard-section.test.tsx`

- [ ] **Step 1: Write the failing test**

Create `web/tests/dashboard-section.test.tsx`:

```tsx
/**
 * @vitest-environment jsdom
 */

import React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { DashboardSection } from "@/components/dashboard/DashboardSection";

afterEach(() => cleanup());

describe("DashboardSection", () => {
  it("renders label, count, and expanded body by default", () => {
    render(
      <DashboardSection id="portfolio" label="Portfolio" count="01">
        <p>child content</p>
      </DashboardSection>,
    );
    expect(screen.getByText("Portfolio")).toBeTruthy();
    expect(screen.getByText("01")).toBeTruthy();
    const toggle = screen.getByRole("button");
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("child content")).toBeTruthy();
  });

  it("collapses and re-expands the body on toggle", () => {
    render(
      <DashboardSection id="orders" label="Working & Filled">
        <p>child content</p>
      </DashboardSection>,
    );
    const toggle = screen.getByRole("button");
    const body = document.getElementById("dashboard-section-body-orders")!;
    expect(body.hasAttribute("hidden")).toBe(false);

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(body.hasAttribute("hidden")).toBe(true);

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(body.hasAttribute("hidden")).toBe(false);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && NODE_ENV=test ASSISTANT_MOCK=1 npx vitest run --config ../vitest.config.ts web/tests/dashboard-section.test.tsx`
Expected: FAIL — cannot resolve `@/components/dashboard/DashboardSection`.

- [ ] **Step 3: Write minimal implementation**

Create `web/components/dashboard/DashboardSection.tsx`:

```tsx
"use client";

import { useState, type ReactNode } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

type Props = {
  id: string;
  label: string;
  count?: string;
  children: ReactNode;
};

/**
 * DashboardSection — collapsible wrapper for a dashboard panel.
 * Eyebrow label + optional count chip + chevron. Body hidden when collapsed.
 * Ported from radon DashboardSurface's inner DashboardSection.
 */
export function DashboardSection({ id, label, count, children }: Props) {
  const [open, setOpen] = useState(true);
  return (
    <section
      className={`dashboard-section dashboard-section--${id}`}
      data-testid={`dashboard-section-${id}`}
    >
      <button
        type="button"
        className="dashboard-section__toggle"
        aria-expanded={open}
        aria-controls={`dashboard-section-body-${id}`}
        onClick={() => setOpen((prev) => !prev)}
      >
        <span className="dashboard-section__title">{label}</span>
        <span className="dashboard-section__meta">
          {count ? <span>{count}</span> : null}
          {open ? (
            <ChevronDown size={16} aria-hidden />
          ) : (
            <ChevronRight size={16} aria-hidden />
          )}
        </span>
      </button>
      <div
        id={`dashboard-section-body-${id}`}
        className="dashboard-section__body"
        hidden={!open}
      >
        {children}
      </div>
    </section>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && NODE_ENV=test ASSISTANT_MOCK=1 npx vitest run --config ../vitest.config.ts web/tests/dashboard-section.test.tsx`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add web/components/dashboard/DashboardSection.tsx web/tests/dashboard-section.test.tsx
git commit -m "feat(dashboard): add collapsible DashboardSection wrapper"
```

---

## Task 2: PortfolioSnapshotCard

**Files:**

- Create: `web/components/dashboard/PortfolioSnapshotCard.tsx`
- Test: `web/tests/portfolio-snapshot-card.test.tsx`

Key correctness rule (web/CLAUDE.md): Today P&L must be FUTU-aware. Use `resolveAccountDayPnlValue(portfolio, prices)` from `@/components/MetricCards` — it returns IB's streamed `daily_pnl` for IB and intraday-from-prices for FUTU. Do NOT replicate radon's `ibDaily ?? realizedPnl` fallback.

- [ ] **Step 1: Write the failing test**

Create `web/tests/portfolio-snapshot-card.test.tsx`:

```tsx
/**
 * @vitest-environment jsdom
 */

import React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render } from "@testing-library/react";
import { PortfolioSnapshotCard } from "@/components/dashboard/PortfolioSnapshotCard";
import type { PortfolioData } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";

afterEach(() => cleanup());

function ibPortfolio(dailyPnl: number | null): PortfolioData {
  return {
    source: "ib",
    bankroll: 200000,
    peak_value: 200000,
    last_sync: "2026-06-15T14:00:00Z",
    positions: [],
    total_deployed_pct: 52.7,
    total_deployed_dollars: 105501,
    remaining_capacity_pct: 47.3,
    position_count: 0,
    defined_risk_count: 0,
    undefined_risk_count: 0,
    avg_kelly_optimal: null,
    account_summary: {
      net_liquidation: 148000,
      daily_pnl: dailyPnl,
      unrealized_pnl: 0,
      realized_pnl: 0,
      settled_cash: 9000,
      maintenance_margin: 0,
      excess_liquidity: 0,
      buying_power: 0,
      dividends: null,
      cash: -14585,
    },
  };
}

const FUTU_PORTFOLIO: PortfolioData = {
  source: "futu",
  bankroll: 148000,
  peak_value: 148000,
  last_sync: "2026-06-15T14:00:00Z",
  positions: [
    {
      id: 1,
      ticker: "TSLA",
      structure: "Stock",
      structure_type: "Stock",
      risk_profile: "equity",
      expiry: "",
      contracts: 300,
      direction: "LONG",
      entry_cost: 96213,
      max_risk: null,
      market_value: 105501,
      legs: [
        {
          direction: "LONG",
          contracts: 300,
          type: "Stock",
          strike: null,
          entry_cost: 96213,
          avg_cost: 320.71,
          market_price: 351.67,
          market_value: 105501,
          market_price_is_calculated: false,
        },
      ],
      kelly_optimal: null,
      target: null,
      stop: null,
      entry_date: "2026-04-01",
    },
  ],
  total_deployed_pct: 71.2,
  total_deployed_dollars: 105501,
  remaining_capacity_pct: 28.8,
  position_count: 1,
  defined_risk_count: 0,
  undefined_risk_count: 1,
  avg_kelly_optimal: null,
  account_summary: {
    net_liquidation: 148000,
    daily_pnl: 9288,
    unrealized_pnl: 9288,
    realized_pnl: 0,
    settled_cash: -14585,
    maintenance_margin: 114285,
    excess_liquidity: 33715,
    buying_power: 29917,
    dividends: null,
  },
};

const PRICES: Record<string, PriceData> = {
  TSLA: {
    symbol: "TSLA",
    last: 351.67,
    lastIsCalculated: false,
    bid: 351.5,
    ask: 351.8,
    bidSize: 1,
    askSize: 1,
    volume: 100,
    high: null,
    low: null,
    open: null,
    close: 350.67,
    week52High: null,
    week52Low: null,
    avgVolume: null,
    delta: null,
    gamma: null,
    theta: null,
    vega: null,
    impliedVol: null,
    undPrice: null,
    timestamp: "2026-06-15T14:00:00Z",
  },
};

function todayCell(container: HTMLElement): HTMLElement | undefined {
  return Array.from(container.querySelectorAll(".snapshot-cell")).find((el) =>
    el.textContent?.includes("Today"),
  ) as HTMLElement | undefined;
}

describe("PortfolioSnapshotCard", () => {
  it("renders the four account cells from an IB portfolio", () => {
    const { container } = render(
      <PortfolioSnapshotCard portfolio={ibPortfolio(1234)} />,
    );
    const text = container.textContent ?? "";
    expect(text).toContain("Net Liquidation");
    expect(text).toContain("$148,000");
    expect(text).toContain("Open Risk");
    expect(text).toContain("$105,501");
    expect(text).toContain("Cash");
    expect(text).toContain("-$14,585");
    expect(text).toContain("Today");
    expect(text).toContain("+$1,234");
  });

  it("applies core tone to positive Today P&L", () => {
    const { container } = render(
      <PortfolioSnapshotCard portfolio={ibPortfolio(1234)} />,
    );
    const cell = todayCell(container);
    expect(cell?.querySelector(".snapshot-cell__value--core")).toBeTruthy();
  });

  it("applies fault tone to negative Today P&L", () => {
    const { container } = render(
      <PortfolioSnapshotCard portfolio={ibPortfolio(-890)} />,
    );
    const cell = todayCell(container);
    expect(cell?.querySelector(".snapshot-cell__value--fault")).toBeTruthy();
    expect(cell?.textContent).toContain("-$890");
  });

  it("applies neutral tone to null Today P&L", () => {
    const { container } = render(
      <PortfolioSnapshotCard portfolio={ibPortfolio(null)} />,
    );
    const cell = todayCell(container);
    expect(cell?.querySelector(".snapshot-cell__value--neutral")).toBeTruthy();
    expect(cell?.textContent).toContain("---");
  });

  it("uses FUTU intraday P&L from live prices, not snapshot daily_pnl", () => {
    const { container } = render(
      <PortfolioSnapshotCard portfolio={FUTU_PORTFOLIO} prices={PRICES} />,
    );
    const cell = todayCell(container);
    // (351.67 - 350.67) * 300 = +$300
    expect(cell?.textContent).toContain("+$300");
    expect(cell?.textContent).not.toContain("9,288");
  });

  it("renders --- for every cell when portfolio is null", () => {
    const { container } = render(<PortfolioSnapshotCard portfolio={null} />);
    const text = container.textContent ?? "";
    expect(text).toContain("Net Liquidation");
    expect((text.match(/---/g) ?? []).length).toBeGreaterThanOrEqual(4);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && NODE_ENV=test ASSISTANT_MOCK=1 npx vitest run --config ../vitest.config.ts web/tests/portfolio-snapshot-card.test.tsx`
Expected: FAIL — cannot resolve `@/components/dashboard/PortfolioSnapshotCard`.

- [ ] **Step 3: Write minimal implementation**

Create `web/components/dashboard/PortfolioSnapshotCard.tsx`:

```tsx
"use client";

import type { PortfolioData } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import { fmtUsd, fmtSignedUsd } from "@/lib/format";
import { resolveAccountDayPnlValue } from "@/components/MetricCards";

type Props = {
  portfolio: PortfolioData | null;
  prices?: Record<string, PriceData>;
};

function pnlTone(value: number | null): "core" | "fault" | "neutral" {
  if (value == null || value === 0) return "neutral";
  return value > 0 ? "core" : "fault";
}

const money = (n: number | null | undefined): string =>
  n == null || !Number.isFinite(n) ? "---" : fmtUsd(n);

const moneySigned = (n: number | null | undefined): string =>
  n == null || !Number.isFinite(n) ? "---" : fmtSignedUsd(n);

/**
 * PortfolioSnapshotCard — top-of-dashboard account summary: Net Liquidation,
 * Today P&L, Open Risk (deployed capital), free cash. Reads the portfolio prop
 * already hydrated by WorkspaceShell — no new data plumbing.
 *
 * Today P&L is broker-aware via resolveAccountDayPnlValue(): IB's streamed
 * daily_pnl for IB, intraday-from-live-prices for FUTU. (Credit/debit sign
 * preserved — no Math.abs on P&L.)
 */
export function PortfolioSnapshotCard({ portfolio, prices }: Props) {
  const acct = portfolio?.account_summary;
  const netLiq = acct?.net_liquidation ?? null;
  const todayPnl = portfolio
    ? resolveAccountDayPnlValue(portfolio, prices)
    : null;
  const cash = acct?.cash ?? acct?.settled_cash ?? null;
  const openRisk = portfolio?.total_deployed_dollars ?? null;
  const todayTone = pnlTone(todayPnl);

  return (
    <section className="snapshot-card">
      <span className="panel-edge-trace" aria-hidden />
      <header className="snapshot-card__header">
        <p className="panel-eyebrow">Portfolio / 01</p>
        <h3 className="panel-title">Account</h3>
      </header>
      <div className="snapshot-grid snapshot-grid--portfolio">
        <div className="snapshot-cell">
          <span className="snapshot-cell__label">Net Liquidation</span>
          <span className="snapshot-cell__value">{money(netLiq)}</span>
        </div>
        <div className="snapshot-cell">
          <span className="snapshot-cell__label">Today P&amp;L</span>
          <span
            className={`snapshot-cell__value snapshot-cell__value--${todayTone}`}
          >
            {moneySigned(todayPnl)}
          </span>
        </div>
        <div className="snapshot-cell">
          <span className="snapshot-cell__label">Open Risk</span>
          <span className="snapshot-cell__value">{money(openRisk)}</span>
        </div>
        <div className="snapshot-cell">
          <span className="snapshot-cell__label">Cash</span>
          <span className="snapshot-cell__value">{money(cash)}</span>
        </div>
      </div>
    </section>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && NODE_ENV=test ASSISTANT_MOCK=1 npx vitest run --config ../vitest.config.ts web/tests/portfolio-snapshot-card.test.tsx`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add web/components/dashboard/PortfolioSnapshotCard.tsx web/tests/portfolio-snapshot-card.test.tsx
git commit -m "feat(dashboard): add PortfolioSnapshotCard with FUTU-aware Today P&L"
```

---

## Task 3: OrdersSnapshotCard

**Files:**

- Create: `web/components/dashboard/OrdersSnapshotCard.tsx`
- Test: `web/tests/orders-snapshot-card.test.tsx`

Ported verbatim from radon — xenon's `OpenOrder` / `ExecutedOrder` / `OrderContract` are field-identical. `Math.abs` here is on quantity (a count), not on price/P&L, so the credit/debit sign rule does not apply.

- [ ] **Step 1: Write the failing test**

Create `web/tests/orders-snapshot-card.test.tsx`:

```tsx
/**
 * @vitest-environment jsdom
 */

import React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { OrdersSnapshotCard } from "@/components/dashboard/OrdersSnapshotCard";
import type { OrdersData, OpenOrder, ExecutedOrder } from "@/lib/types";

afterEach(() => cleanup());

function openOrder(over: Partial<OpenOrder> & { orderId: number }): OpenOrder {
  return {
    orderId: over.orderId,
    permId: over.permId ?? over.orderId,
    symbol: over.symbol ?? "QQQ",
    contract: over.contract ?? {
      conId: 1,
      symbol: "QQQ",
      secType: "OPT",
      strike: 630,
      right: "C",
      expiry: "20260619",
    },
    action: over.action ?? "BUY",
    orderType: over.orderType ?? "LMT",
    totalQuantity: over.totalQuantity ?? 2,
    limitPrice: over.limitPrice ?? 1.25,
    auxPrice: null,
    status: over.status ?? "Submitted",
    filled: 0,
    remaining: over.totalQuantity ?? 2,
    avgFillPrice: null,
    tif: "DAY",
  };
}

function fill(
  over: Partial<ExecutedOrder> & { execId: string },
): ExecutedOrder {
  return {
    execId: over.execId,
    symbol: over.symbol ?? "QQQ",
    contract: over.contract ?? {
      conId: 2,
      symbol: "QQQ",
      secType: "OPT",
      strike: 600,
      right: "P",
      expiry: "20260619",
    },
    side: over.side ?? "SELL",
    quantity: over.quantity ?? 1,
    avgPrice: over.avgPrice ?? 3.4,
    commission: null,
    realizedPNL: null,
    time: over.time ?? "2026-06-15T14:05:00Z",
    exchange: "SMART",
  };
}

function ordersData(over: Partial<OrdersData>): OrdersData {
  return {
    last_sync: "2026-06-15T14:00:00Z",
    open_orders: over.open_orders ?? [],
    executed_orders: over.executed_orders ?? [],
    open_count: over.open_orders?.length ?? 0,
    executed_count: over.executed_orders?.length ?? 0,
  };
}

describe("OrdersSnapshotCard", () => {
  it("renders the empty state when orders is null", () => {
    render(<OrdersSnapshotCard orders={null} />);
    expect(screen.getByText("No open or filled orders today.")).toBeTruthy();
  });

  it("links to /orders", () => {
    render(
      <OrdersSnapshotCard
        orders={ordersData({ open_orders: [openOrder({ orderId: 1 })] })}
      />,
    );
    const link = screen.getByText("All orders →") as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe("/orders");
  });

  it("caps the working list at 3 but shows the true count", () => {
    const orders = ordersData({
      open_orders: [1, 2, 3, 4].map((n) => openOrder({ orderId: n })),
    });
    const { container } = render(<OrdersSnapshotCard orders={orders} />);
    const lists = container.querySelectorAll(".snapshot-list");
    const workingRows = lists[0].querySelectorAll(".snapshot-list__row");
    expect(workingRows.length).toBe(3);
    expect(lists[0].querySelector(".snapshot-list__count")?.textContent).toBe(
      "4",
    );
  });

  it("describes OPT, BAG, and stock open orders", () => {
    const orders = ordersData({
      open_orders: [
        openOrder({
          orderId: 1,
          action: "BUY",
          totalQuantity: 2,
          limitPrice: 1.25,
          contract: {
            conId: 1,
            symbol: "QQQ",
            secType: "OPT",
            strike: 630,
            right: "C",
            expiry: "20260619",
          },
        }),
        openOrder({
          orderId: 2,
          action: "BUY",
          totalQuantity: 1,
          limitPrice: 2.5,
          contract: {
            conId: 2,
            symbol: "SPY",
            secType: "BAG",
            strike: null,
            right: null,
            expiry: null,
          },
        }),
        openOrder({
          orderId: 3,
          action: "SELL",
          totalQuantity: 100,
          limitPrice: 150,
          contract: {
            conId: 3,
            symbol: "AAPL",
            secType: "STK",
            strike: null,
            right: null,
            expiry: null,
          },
        }),
      ],
    });
    const { container } = render(<OrdersSnapshotCard orders={orders} />);
    const text = container.textContent ?? "";
    expect(text).toContain("BUY 2× QQQ Call $630 @ $1.25");
    expect(text).toContain("BUY 1× SPY combo @ $2.50");
    expect(text).toContain("SELL 100× AAPL @ $150.00");
  });

  it("renders a credit combo limit price with a leading minus, not $-", () => {
    const orders = ordersData({
      open_orders: [
        openOrder({
          orderId: 1,
          action: "SELL",
          totalQuantity: 1,
          limitPrice: -0.4, // credit combo
          contract: {
            conId: 1,
            symbol: "SPY",
            secType: "BAG",
            strike: null,
            right: null,
            expiry: null,
          },
        }),
      ],
    });
    const { container } = render(<OrdersSnapshotCard orders={orders} />);
    expect(container.textContent).toContain("SELL 1× SPY combo @ -$0.40");
    expect(container.textContent).not.toContain("$-0.40");
  });

  it("guards nullable OPT strike so it never renders $null", () => {
    const orders = ordersData({
      open_orders: [
        openOrder({
          orderId: 1,
          action: "BUY",
          totalQuantity: 2,
          limitPrice: 1.25,
          contract: {
            conId: 1,
            symbol: "QQQ",
            secType: "OPT",
            strike: null,
            right: "C",
            expiry: "20260619",
          },
        }),
      ],
    });
    const { container } = render(<OrdersSnapshotCard orders={orders} />);
    expect(container.textContent).toContain("BUY 2× QQQ Call @ $1.25");
    expect(container.textContent).not.toContain("$null");
  });

  it("normalizes BOT/SLD fill sides to BUY/SELL and shows the fill time", () => {
    const orders = ordersData({
      executed_orders: [
        fill({
          execId: "e1",
          side: "SLD", // real IB value from orders.py (not "SELL")
          quantity: 1,
          avgPrice: 3.4,
          time: "2026-06-15T14:05:00Z",
          contract: {
            conId: 9,
            symbol: "QQQ",
            secType: "OPT",
            strike: 600,
            right: "P",
            expiry: "20260619",
          },
        }),
      ],
    });
    const { container } = render(<OrdersSnapshotCard orders={orders} />);
    expect(container.textContent).toContain("SELL 1× QQQ Put $600 @ $3.40");
    expect(container.textContent).not.toContain("SLD");
    // TZ-agnostic: assert an HH:MM time rendered, not an exact local string.
    const fillsList = container.querySelectorAll(".snapshot-list")[1];
    expect(
      fillsList.querySelector(".snapshot-list__row-meta")?.textContent,
    ).toMatch(/\d{2}:\d{2}/);
  });

  it("caps the fills list at 3 but shows the true count", () => {
    const orders = ordersData({
      executed_orders: [1, 2, 3, 4].map((n) =>
        fill({ execId: `e${n}`, side: "BOT" }),
      ),
    });
    const { container } = render(<OrdersSnapshotCard orders={orders} />);
    const fillsList = container.querySelectorAll(".snapshot-list")[1];
    expect(fillsList.querySelectorAll(".snapshot-list__row").length).toBe(3);
    expect(fillsList.querySelector(".snapshot-list__count")?.textContent).toBe(
      "4",
    );
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && NODE_ENV=test ASSISTANT_MOCK=1 npx vitest run --config ../vitest.config.ts web/tests/orders-snapshot-card.test.tsx`
Expected: FAIL — cannot resolve `@/components/dashboard/OrdersSnapshotCard`.

- [ ] **Step 3: Write minimal implementation**

Create `web/components/dashboard/OrdersSnapshotCard.tsx`:

```tsx
"use client";

import Link from "next/link";
import type {
  OrdersData,
  OpenOrder,
  ExecutedOrder,
  OrderContract,
} from "@/lib/types";
import { fmtSignedPrice } from "@/lib/format";

type Props = {
  orders: OrdersData | null;
};

function fmtTime(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

// IB fill side comes through the orders API as BOT/SLD (orders.py:151).
// Normalize to BUY/SELL for a human-readable snapshot; pass through anything else.
function normalizeSide(side: string): string {
  if (side === "BOT") return "BUY";
  if (side === "SLD") return "SELL";
  return side;
}

function rightLabel(right: string | null): string {
  if (right === "C") return "Call";
  if (right === "P") return "Put";
  return right ?? "";
}

// Sign-preserving price tag: credit combos have negative prices and must render
// "-$0.40", not "$-0.40" (web/CLAUDE.md credit/debit convention). Empty when null.
function priceTag(value: number | null): string {
  return value != null ? `@ ${fmtSignedPrice(value)}` : "";
}

// Contract label, shared by orders + fills. Guards nullable OPT strike/right so
// incomplete metadata never renders "$null".
function describeContract(c: OrderContract): string {
  if (c.secType === "BAG") return `${c.symbol} combo`;
  if (c.secType === "OPT") {
    const parts = [c.symbol, rightLabel(c.right)];
    if (c.strike != null) parts.push(`$${c.strike}`);
    return parts.filter(Boolean).join(" ");
  }
  return c.symbol;
}

function describeOrder(o: OpenOrder): string {
  const qty = Math.abs(o.totalQuantity ?? 0);
  return `${o.action || ""} ${qty}× ${describeContract(o.contract)} ${priceTag(o.limitPrice)}`.trim();
}

function describeFill(f: ExecutedOrder): string {
  const qty = Math.abs(f.quantity ?? 0);
  return `${normalizeSide(f.side || "")} ${qty}× ${describeContract(f.contract)} ${priceTag(f.avgPrice)}`.trim();
}

/**
 * OrdersSnapshotCard — compressed "what am I working on right now" view: top-3
 * working orders + top-3 of today's fills, click-through to /orders. IB-only;
 * when handed null (FUTU tab) it renders its empty state.
 */
export function OrdersSnapshotCard({ orders }: Props) {
  const open = (orders?.open_orders ?? []).slice(0, 3);
  const recent = (orders?.executed_orders ?? []).slice(0, 3);
  const hasAny = open.length > 0 || recent.length > 0;

  return (
    <section className="snapshot-card">
      <span className="panel-edge-trace" aria-hidden />
      <header className="snapshot-card__header">
        <p className="panel-eyebrow">Orders / 02</p>
        <h3 className="panel-title">Working &amp; Filled</h3>
        <Link className="snapshot-card__see-all" href="/orders">
          All orders →
        </Link>
      </header>

      {!hasAny ? (
        <div className="snapshot-card__empty">
          No open or filled orders today.
        </div>
      ) : (
        <div className="snapshot-card__split">
          <div className="snapshot-list">
            <p className="snapshot-list__kicker">
              Working
              <span className="snapshot-list__count">
                {orders?.open_count ?? 0}
              </span>
            </p>
            {open.length === 0 ? (
              <div className="snapshot-list__empty">No working orders.</div>
            ) : (
              <ul className="snapshot-list__items">
                {/* Index key: pending DB orders can have permId AND orderId
                    both coerced to 0 (orders.py _int_or_zero), so an id-based
                    key would collide. This list is a read-only top-3 snapshot
                    with no per-row state, so the index is a safe, stable key. */}
                {open.map((o, i) => (
                  <li key={`o-${i}`} className="snapshot-list__row">
                    <span className="snapshot-list__row-desc">
                      {describeOrder(o)}
                    </span>
                    <span className="snapshot-list__row-meta">
                      {o.status ?? "—"}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="snapshot-list">
            <p className="snapshot-list__kicker">
              Today&apos;s Fills
              <span className="snapshot-list__count">
                {orders?.executed_count ?? 0}
              </span>
            </p>
            {recent.length === 0 ? (
              <div className="snapshot-list__empty">No fills today.</div>
            ) : (
              <ul className="snapshot-list__items">
                {recent.map((f, i) => (
                  <li key={`f-${f.execId || i}`} className="snapshot-list__row">
                    <span className="snapshot-list__row-desc">
                      {describeFill(f)}
                    </span>
                    <span className="snapshot-list__row-meta">
                      {fmtTime(f.time)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && NODE_ENV=test ASSISTANT_MOCK=1 npx vitest run --config ../vitest.config.ts web/tests/orders-snapshot-card.test.tsx`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add web/components/dashboard/OrdersSnapshotCard.tsx web/tests/orders-snapshot-card.test.tsx
git commit -m "feat(dashboard): add OrdersSnapshotCard (working + filled snapshot)"
```

---

## Task 4: DashboardSurface (2-col grid + chat rail)

**Files:**

- Create: `web/components/dashboard/DashboardSurface.tsx`
- Test: `web/tests/dashboard-surface.test.tsx`

The test mocks `ChatPanel` so DashboardSurface is tested in isolation (ChatPanel pulls heavy assistant deps).

- [ ] **Step 1: Write the failing test**

Create `web/tests/dashboard-surface.test.tsx`:

```tsx
/**
 * @vitest-environment jsdom
 */

import React from "react";
import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import type { PortfolioData, OrdersData } from "@/lib/types";

vi.mock("@/components/ChatPanel", () => ({
  default: () => <div data-testid="chat-panel-mock" />,
}));

import DashboardSurface from "@/components/dashboard/DashboardSurface";

afterEach(() => cleanup());

const PORTFOLIO: PortfolioData = {
  source: "ib",
  bankroll: 200000,
  peak_value: 200000,
  last_sync: "2026-06-15T14:00:00Z",
  positions: [],
  total_deployed_pct: 0,
  total_deployed_dollars: 0,
  remaining_capacity_pct: 100,
  position_count: 0,
  defined_risk_count: 0,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  account_summary: {
    net_liquidation: 148000,
    daily_pnl: 1234,
    unrealized_pnl: 0,
    realized_pnl: 0,
    settled_cash: 9000,
    maintenance_margin: 0,
    excess_liquidity: 0,
    buying_power: 0,
    dividends: null,
  },
};

const ORDERS: OrdersData = {
  last_sync: "2026-06-15T14:00:00Z",
  open_orders: [],
  executed_orders: [],
  open_count: 0,
  executed_count: 0,
};

describe("DashboardSurface", () => {
  it("renders portfolio card, orders card, and the chat rail", () => {
    const { container } = render(
      <DashboardSurface portfolio={PORTFOLIO} orders={ORDERS} />,
    );
    expect(container.querySelector(".dashboard-surface")).toBeTruthy();
    expect(container.querySelector(".dashboard-surface__rail")).toBeTruthy();
    expect(screen.getByText("Net Liquidation")).toBeTruthy();
    // "Working & Filled" appears twice — the collapsible section title AND the
    // card's panel-title — so getByText would throw on multiple matches.
    expect(screen.getAllByText("Working & Filled").length).toBeGreaterThan(0);
    expect(screen.getByTestId("chat-panel-mock")).toBeTruthy();
  });

  it("shows the orders empty state when orders is null (FUTU tab)", () => {
    render(<DashboardSurface portfolio={PORTFOLIO} orders={null} />);
    expect(screen.getByText("No open or filled orders today.")).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && NODE_ENV=test ASSISTANT_MOCK=1 npx vitest run --config ../vitest.config.ts web/tests/dashboard-surface.test.tsx`
Expected: FAIL — cannot resolve `@/components/dashboard/DashboardSurface`.

- [ ] **Step 3: Write minimal implementation**

Create `web/components/dashboard/DashboardSurface.tsx`:

```tsx
"use client";

import type { OrdersData, PortfolioData } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import ChatPanel from "@/components/ChatPanel";
import { DashboardSection } from "./DashboardSection";
import { PortfolioSnapshotCard } from "./PortfolioSnapshotCard";
import { OrdersSnapshotCard } from "./OrdersSnapshotCard";

type Props = {
  portfolio: PortfolioData | null;
  orders: OrdersData | null;
  prices?: Record<string, PriceData>;
};

/**
 * DashboardSurface — the /dashboard landing surface. Two columns:
 *
 *   LEFT  — Portfolio snapshot + Working/Filled orders (collapsible).
 *   RIGHT — AI Assistant (ChatPanel) in a rail; xenon's "live market intel"
 *           surface, the native replacement for radon's news rail.
 *
 * Off-identity radon widgets (Trading Candidates / scanner, Live Market Feed /
 * scraper) are intentionally omitted. Orders is IB-only; under the FUTU tab the
 * shell passes orders={null} and OrdersSnapshotCard shows its empty state.
 */
export default function DashboardSurface({ portfolio, orders, prices }: Props) {
  return (
    <div className="dashboard-surface">
      <div className="dashboard-surface__main">
        <DashboardSection id="portfolio" label="Portfolio" count="01">
          <PortfolioSnapshotCard portfolio={portfolio} prices={prices} />
        </DashboardSection>
        <DashboardSection id="orders" label="Working & Filled" count="02">
          <OrdersSnapshotCard orders={orders} />
        </DashboardSection>
      </div>
      <aside className="dashboard-surface__rail" aria-label="AI Assistant">
        <ChatPanel activeSection="dashboard" />
      </aside>
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && NODE_ENV=test ASSISTANT_MOCK=1 npx vitest run --config ../vitest.config.ts web/tests/dashboard-surface.test.tsx`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add web/components/dashboard/DashboardSurface.tsx web/tests/dashboard-surface.test.tsx
git commit -m "feat(dashboard): add DashboardSurface 2-col grid with chat rail"
```

---

## Task 5: Dashboard CSS (re-skinned to xenon tokens)

**Files:**

- Modify: `web/app/globals.css` (append at end of file)

No unit test for pure CSS — verified in Task 7 (browser). This task only adds styles; no class names conflict with existing xenon CSS (verified: xenon has no `.dashboard-surface`, `.snapshot-card`, `.panel-eyebrow`, `.panel-title`, or `.panel-edge-trace`).

- [ ] **Step 1: Append the CSS block**

Append to the end of `web/app/globals.css`:

```css
/* ───────────────────────────────────────────────────────────
   Dashboard landing surface (ported from radon, re-skinned to
   xenon brand tokens). 2-col: snapshot cards (left) + chat rail
   (right). 4px max radius, flat tokens, no gradients/glass.
   ─────────────────────────────────────────────────────────── */
.dashboard-surface {
  display: grid;
  grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr);
  gap: 20px;
  align-items: start;
}

.dashboard-surface__main {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}

.dashboard-surface__rail {
  min-width: 0;
  position: sticky;
  top: 0;
  /* .content has 20px vertical padding; header + banners sit above it. Bound
     the rail so ChatPanel scrolls internally instead of stretching the page.
     Tune the offset during browser verification (Task 7). */
  height: calc(100vh - 160px);
  display: flex;
  flex-direction: column;
}

.dashboard-section {
  min-width: 0;
}

.dashboard-section__toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  min-height: 44px;
  margin: 0 0 8px;
  padding: 0;
  background: transparent;
  border: 0;
  color: var(--text-primary);
  font-family: var(--font-mono);
  cursor: pointer;
}

.dashboard-section__title {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}

.dashboard-section__meta {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--text-muted);
  font-size: 10px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
}

.dashboard-section__body[hidden] {
  display: none;
}

/* Panel header helpers — xenon lacked bare panel-eyebrow/title/edge-trace. */
.panel-edge-trace {
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 2px;
  background: var(
    --signal-core
  ); /* flat — radon used a gradient (brand: none) */
  pointer-events: none;
  z-index: 1;
}

.panel-eyebrow {
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--text-muted);
  line-height: 1.2;
  margin-bottom: 4px;
  display: block;
}

.panel-title {
  font-family: var(--font-sans);
  font-size: 14px;
  font-weight: 600;
  letter-spacing: 0.005em;
  line-height: 1.25;
  color: var(--text-primary);
  text-transform: none;
}

/* Snapshot cards — Portfolio / Orders */
.snapshot-card {
  position: relative;
  background: var(--bg-panel);
  border: 1px solid var(--border-dim);
  border-radius: 4px;
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.snapshot-card__header {
  display: flex;
  align-items: baseline;
  gap: 12px;
  flex-wrap: wrap;
}

.snapshot-card__header .panel-eyebrow {
  margin: 0;
  flex-shrink: 0;
}

.snapshot-card__header .panel-title {
  flex: 1;
}

.snapshot-card__see-all {
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--signal-core);
  text-decoration: none;
  flex-shrink: 0;
}

.snapshot-card__see-all:hover {
  text-decoration: underline;
}

.snapshot-card__empty {
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-muted);
  padding: 14px 0;
}

.snapshot-grid--portfolio {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.snapshot-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 0;
  border-top: 1px solid var(--border-dim);
}

.snapshot-cell:nth-child(-n + 2) {
  border-top: 0;
  padding-top: 0;
}

.snapshot-cell__label {
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--text-muted);
}

.snapshot-cell__value {
  font-family: var(--font-sans);
  font-size: 22px;
  font-weight: 600;
  letter-spacing: -0.01em;
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
}

.snapshot-cell__value--core {
  color: var(--signal-core);
}
.snapshot-cell__value--fault {
  color: var(--fault);
}
.snapshot-cell__value--neutral {
  color: var(--text-primary);
}

/* Orders + fills two-column split */
.snapshot-card__split {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.snapshot-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}

.snapshot-list__kicker {
  display: flex;
  align-items: center;
  gap: 8px;
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--text-muted);
}

.snapshot-list__count {
  display: inline-flex;
  align-items: center;
  height: 16px;
  padding: 0 6px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--signal-core) 8%, transparent);
  border: 1px solid
    color-mix(in srgb, var(--signal-core) 30%, var(--border-dim));
  color: var(--signal-core);
  font-size: 9px;
  letter-spacing: 0.1em;
}

.snapshot-list__empty {
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--text-muted);
  padding: 6px 0;
}

.snapshot-list__items {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.snapshot-list__row {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px 0;
  border-top: 1px solid var(--border-dim);
}

.snapshot-list__row:first-child {
  border-top: 0;
  padding-top: 0;
}

.snapshot-list__row-desc {
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
}

.snapshot-list__row-meta {
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-muted);
}

@media (max-width: 1280px) {
  .dashboard-surface {
    grid-template-columns: 1fr;
  }
  .snapshot-card__split {
    grid-template-columns: 1fr;
  }
  /* Below the dual-column breakpoint the rail stacks beneath the cards and
     grows naturally — drop the sticky/clamp behaviour. */
  .dashboard-surface__rail {
    position: static;
    height: auto;
    min-height: 420px;
  }
}
```

- [ ] **Step 2: Verify the dev build compiles the CSS**

Run: `cd web && npm run typecheck`
Expected: no errors (CSS isn't typechecked, but this confirms no accidental TS breakage from prior tasks).

- [ ] **Step 3: Commit**

```bash
git add web/app/globals.css
git commit -m "style(dashboard): add snapshot-card + dashboard-surface CSS (brand tokens)"
```

---

## Task 6: Wire DashboardSurface into WorkspaceShell

**Files:**

- Modify: `web/components/WorkspaceShell.tsx` (import swap + dashboard branch + tab-bar condition)

- [ ] **Step 1: Swap the imports**

In `web/components/WorkspaceShell.tsx`, remove the now-unused ChatPanel import (line 30):

```tsx
import ChatPanel from "@/components/ChatPanel";
```

Replace it with:

```tsx
import DashboardSurface from "@/components/dashboard/DashboardSurface";
```

- [ ] **Step 2: Replace the dashboard render block + tab-bar condition**

Find this block (currently lines 446–490):

```tsx
{
  activeSection === "dashboard" ? (
    <ChatPanel activeSection={activeSection} />
  ) : null;
}

{
  activeSection !== "dashboard" && activeSection !== "ticker-detail" ? (
    <>
      <AccountTabBar
        active={activeAccount}
        onChange={setActiveAccount}
        ib={{
          label: "IB",
          accountId: ibData.data?.account_summary ? "IB Account" : null,
          environment: "real",
          positionCount: ibData.data?.positions.length ?? 0,
          lastSync: ibData.lastSync,
          netLiquidation: ibData.data?.account_summary?.net_liquidation ?? null,
          status: ibConnected ? "live" : "down",
        }}
        futu={{
          label: "FUTU",
          accountId: futuData.envelope?.account_id ?? null,
          environment: "real",
          positionCount: futuData.data?.positions.length ?? 0,
          lastSync: futuData.lastSync,
          netLiquidation:
            futuData.data?.account_summary?.net_liquidation ?? null,
          status: computeFutuStaleness({
            envelope: futuData.envelope,
            error: futuData.error,
            neverSynced: futuData.neverSynced,
            marketOpen: isMarketActive,
          }),
        }}
      />
      <MetricCards
        portfolio={portfolio}
        prices={prices}
        realizedPnl={todayRealizedPnl}
        executedOrders={executedOrders}
        section={activeSection}
      />
    </>
  ) : null;
}
```

Replace the entire block above with:

```tsx
{
  activeSection !== "ticker-detail" ? (
    <AccountTabBar
      active={activeAccount}
      onChange={setActiveAccount}
      ib={{
        label: "IB",
        accountId: ibData.data?.account_summary ? "IB Account" : null,
        environment: "real",
        positionCount: ibData.data?.positions.length ?? 0,
        lastSync: ibData.lastSync,
        netLiquidation: ibData.data?.account_summary?.net_liquidation ?? null,
        status: ibConnected ? "live" : "down",
      }}
      futu={{
        label: "FUTU",
        accountId: futuData.envelope?.account_id ?? null,
        environment: "real",
        positionCount: futuData.data?.positions.length ?? 0,
        lastSync: futuData.lastSync,
        netLiquidation: futuData.data?.account_summary?.net_liquidation ?? null,
        status: computeFutuStaleness({
          envelope: futuData.envelope,
          error: futuData.error,
          neverSynced: futuData.neverSynced,
          marketOpen: isMarketActive,
        }),
      }}
    />
  ) : null;
}

{
  activeSection === "dashboard" ? (
    <DashboardSurface
      portfolio={portfolio}
      orders={activeAccount === "ib" ? orders : null}
      prices={prices}
    />
  ) : null;
}

{
  activeSection !== "dashboard" && activeSection !== "ticker-detail" ? (
    <MetricCards
      portfolio={portfolio}
      prices={prices}
      realizedPnl={todayRealizedPnl}
      executedOrders={executedOrders}
      section={activeSection}
    />
  ) : null;
}
```

Net effect: the account tab bar now renders on dashboard too; `MetricCards` stays excluded from dashboard; `DashboardSurface` renders only on dashboard; the bare `<ChatPanel>` is gone (ChatPanel now lives inside DashboardSurface). Orders are gated to IB so the FUTU tab shows the orders empty state.

- [ ] **Step 3: Confirm no dangling ChatPanel reference remains**

Run: `cd web && rg -n "ChatPanel" components/WorkspaceShell.tsx`
Expected: no matches (import removed, JSX usage removed).

**Why no isolated unit test for this wiring:** `WorkspaceShell` mounts the full provider tree (Clerk auth, IB status context, portfolio/orders/prices hooks, toast context). Rendering it in jsdom would require mocking that entire tree — high-cost, low-signal, and brittle. The render-condition change is instead verified end-to-end in Task 7 (dashboard shows tab bar + DashboardSurface and NO `MetricCards`; a non-dashboard section still shows `MetricCards`). That browser check is the mandated UI gate per `web/CLAUDE.md`.

- [ ] **Step 4: Typecheck + lint**

Run: `cd web && npm run typecheck && npm run lint`
Expected: both pass (no unused-import error for ChatPanel; `orders`, `prices`, `portfolio`, `activeAccount` all already in scope).

- [ ] **Step 5: Commit**

```bash
git add web/components/WorkspaceShell.tsx
git commit -m "feat(dashboard): render DashboardSurface on /dashboard + tab bar on top"
```

---

## Task 7: Full gate + browser verification (mandatory UI gate)

**Files:** none (verification + final checks)

- [ ] **Step 1: Run the full Vitest suite**

Run: `cd web && npm test`
Expected: PASS, including the 4 new dashboard test files (18 new tests total: DashboardSection 2 + PortfolioSnapshotCard 6 + OrdersSnapshotCard 8 + DashboardSurface 2).

- [ ] **Step 2: Start the dev stack**

Run: `scripts/infra/dev.sh paper` (from repo root, in a separate terminal/background)
Expected: Next on :3200, FastAPI on :8421, realtime WS on :8866. Wait for "ready".

- [ ] **Step 3: Browser verify the dashboard (chrome-cdp; Playwright fallback)**

Navigate to `http://localhost:3200/dashboard` and confirm visually:

- IB / FUTU account tab bar at the top.
- LEFT column: "PORTFOLIO / 01" card showing Net Liquidation / Today P&L / Open Risk / Cash; "WORKING & FILLED / 02" card below it.
- RIGHT rail: the AI Assistant chat. NOTE: ChatPanel's existing DOM renders the composer (textarea + quick-prompt pills) ABOVE the `.chat-messages` thread (ChatPanel.tsx — `<form>` precedes `.chat-messages`). Do NOT try to re-pin the input to the bottom; that is ChatPanel's established layout and is out of scope. Verify only that: the composer is visible, the message thread scrolls INSIDE the rail (it has its own `overflow:auto`), and the page itself does not balloon vertically.
- No empty "Trading Candidates" or "Live Market Feed" frames anywhere.
- **No `MetricCards` account row on the dashboard** (the rich metric strip stays on /portfolio; the snapshot card is the dashboard's portfolio surface). This confirms the Task 6 condition split.
- Collapse a section via its chevron → body hides; expand → returns.
- Today P&L color: green (`--signal-core`) when positive, red (`--fault`) when negative.

Take a screenshot for the record. If the chat rail height looks wrong (overflows the viewport or is too short), adjust the `calc(100vh - 160px)` offset in `.dashboard-surface__rail` and re-verify.

- [ ] **Step 3b: Browser verify the non-dashboard regression (Task 6 condition split)**

Navigate to `http://localhost:3200/portfolio` and confirm the condition split did NOT break existing sections:

- The IB / FUTU `AccountTabBar` still renders.
- The full `MetricCards` account row still renders (it was pulled out of the shared fragment in Task 6 — confirm it survived).
- The portfolio positions table renders as before.

- [ ] **Step 4: Browser verify the FUTU tab**

Click the FUTU tab and confirm:

- Portfolio card numbers switch to the Futu account (Net Liq / Cash from Futu snapshot; Today P&L derived from live prices, may show `---` if no intraday price data).
- Working & Filled card shows "No open or filled orders today." (orders are IB-only).
- Switch back to IB → numbers and orders return.

- [ ] **Step 5: Browser verify mobile/narrow layout**

Resize the viewport to ≤1280px wide and confirm the layout collapses to a single column: tabs → Portfolio card → Working & Filled card → chat (rail stacked beneath, not beside).

- [ ] **Step 6: Final commit (verification note)**

No code change expected here unless the rail offset was tuned in Step 3. If it was:

```bash
git add web/app/globals.css
git commit -m "style(dashboard): tune chat rail height after browser verification"
```

---

## Task 8: Delivery (PR lifecycle) — only on explicit user go-ahead

**Files:** none (git/GitHub operations)

Per global + xenon policy: never push to `master` directly; open a PR, let CI run, then merge. **Do not run this task autonomously** — wait for the user to say "ship"/"push". No `Co-Authored-By` trailer on any commit.

- [ ] **Step 1: Push the branch**

```bash
git push -u origin feat/dashboard-renovation
```

- [ ] **Step 2: Open the PR**

```bash
gh pr create --base master --head feat/dashboard-renovation \
  --title "feat(dashboard): radon-style landing surface (portfolio + orders snapshot, chat rail)" \
  --body "Renovates /dashboard per docs/plans/2026-06-15-dashboard-renovation-design.md. See the IMPL plan for task breakdown. Trading Candidates + Live Market Feed intentionally out of scope (xenon identity); Futu orders deferred."
```

- [ ] **Step 3: Verify CI conclusion by JSON (do not trust `--watch` exit code)**

```bash
gh pr checks --json name,state,conclusion   # confirm every required check conclusion == SUCCESS
```

Expected: `web-typecheck`, `web-lint`, `web-tests`, `python-tests`, `version-sync` all SUCCESS. Fix forward if any fail; never merge red.

- [ ] **Step 4: Merge after approval, then sync local master**

```bash
gh pr merge --squash --delete-branch
git checkout master && git pull --ff-only
```

---

## Self-Review

**Spec coverage (against `docs/plans/2026-06-15-dashboard-renovation-design.md`):**

- 2-col layout (cards left, chat rail right) → Task 4 + Task 5. ✓
- IB/FUTU tab bar on top → Task 6 (condition extended to dashboard). ✓
- Portfolio snapshot (Net Liq / Today P&L / Open Risk / Cash) → Task 2. ✓
- FUTU-correct Today P&L via `resolveAccountDayPnlValue` → Task 2 (impl + test). ✓
- Working & Filled snapshot, IB-only, top-3, `All orders →` → Task 3 + Task 6 (orders gated to IB). ✓
- Collapsible sections → Task 1. ✓
- Chat moved into rail (no full-bleed) → Task 4 + Task 6 (bare ChatPanel removed). ✓
- Drop Trading Candidates + Live Market Feed (no empty frames) → omitted from Task 4 by design. ✓
- Re-skin to xenon tokens, 4px radius, no gradients → Task 5 (gradient flattened, `--line-grid`→`--border-dim`). ✓
- Mobile single-column stack → Task 5 media query + Task 7 Step 5. ✓
- Testing (Vitest + browser E2E) → Tasks 1–4 (unit) + Task 7 (browser). ✓
- Click-through: `All orders →` only; ticker row links deferred → Task 3 (no row links). ✓

**Placeholder scan:** none — every component, test, and CSS block is full content.

**Type consistency:** `DashboardSection` props (`id/label/count/children`), `PortfolioSnapshotCard` props (`portfolio/prices`), `OrdersSnapshotCard` props (`orders`), `DashboardSurface` props (`portfolio/orders/prices`) are consistent across impl, tests, and the WorkspaceShell call site. `PriceData` imported from `@/lib/pricesProtocol` everywhere (matches `resolveAccountDayPnlValue`'s signature). `resolveAccountDayPnlValue` is a named export of `@/components/MetricCards` (verified). All `OpenOrder`/`ExecutedOrder`/`OrderContract` fields used (`totalQuantity`, `limitPrice`, `contract.secType/right/strike`, `permId`, `orderId`, `status`, `execId`, `side`, `quantity`, `avgPrice`, `time`) exist in `web/lib/types.ts` (verified).

**Notes for the executor:** Per xenon policy, commits land on the `feat/dashboard-renovation` branch only — never push to master directly; open a PR after the gate passes (`gh pr create`). Do not weaken the naked-short guard or add JSON writes (neither is touched here). No `Co-Authored-By` trailer on commits.
