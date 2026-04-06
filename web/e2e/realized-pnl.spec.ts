/**
 * E2E: Realized P&L card on /portfolio
 *
 * RED/GREEN TDD:
 * - RED: No fills today → Realized P&L shows $0.00 (not -$6,835 from IB account summary)
 * - GREEN: Fills present → shows correct sum; click card → fills modal appears
 */

import { test, expect } from "@playwright/test";

// ── Shared mock data ────────────────────────────────────────────────────────

const PORTFOLIO_MOCK = {
  bankroll: 1_131_051.65,
  peak_value: 1_131_051.65,
  last_sync: new Date().toISOString(),
  total_deployed_pct: 5.78,
  total_deployed_dollars: 2891.57,
  remaining_capacity_pct: 94.22,
  position_count: 1,
  defined_risk_count: 1,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  positions: [],
  exposure: {},
  violations: [],
  account_summary: {
    net_liquidation: 1_131_051.65,
    daily_pnl: -17_071.27,
    unrealized_pnl: -212_251.69,
    realized_pnl: -6_835.27,  // IB's number — should NOT be used
    settled_cash: -14_654.04,
    maintenance_margin: 513_065.33,
    excess_liquidity: 185_943.44,
    buying_power: 743_773.78,
    dividends: 910.0,
  },
};

const ORDERS_NO_FILLS = {
  last_sync: new Date().toISOString(),
  open_orders: [],
  executed_orders: [],
  open_count: 0,
  executed_count: 0,
};

const ORDERS_WITH_FILLS = {
  last_sync: new Date().toISOString(),
  open_orders: [],
  executed_orders: [
    {
      execId: "exec-1",
      symbol: "AAPL",
      contract: { symbol: "AAPL", secType: "STK", currency: "USD", exchange: "SMART" },
      side: "BOT",
      quantity: 100,
      avgPrice: 214.50,
      commission: -1.05,
      realizedPNL: null,
      time: new Date().toISOString(),
      exchange: "SMART",
    },
    {
      execId: "exec-2",
      symbol: "AAPL",
      contract: { symbol: "AAPL", secType: "STK", currency: "USD", exchange: "SMART" },
      side: "SLD",
      quantity: 100,
      avgPrice: 219.25,
      commission: -1.05,
      realizedPNL: 473.0,
      time: new Date().toISOString(),
      exchange: "SMART",
    },
    {
      execId: "exec-3",
      symbol: "GOOG",
      contract: { symbol: "GOOG", secType: "OPT", currency: "USD", exchange: "SMART" },
      side: "SLD",
      quantity: 2,
      avgPrice: 4.50,
      commission: -2.10,
      realizedPNL: -215.0,
      time: new Date().toISOString(),
      exchange: "SMART",
    },
  ],
  open_count: 0,
  executed_count: 3,
};

// ── Helpers ──────────────────────────────────────────────────────────────────

async function setupBaseMocks(page: import("@playwright/test").Page) {
  await page.unrouteAll({ behavior: "ignoreErrors" });

  await page.route("**/api/portfolio", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(PORTFOLIO_MOCK) }),
  );
  await page.route("**/api/prices", (route) => route.abort());
  await page.route("**/api/regime", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ score: 15, level: "LOW", cri: { score: 15 } }) }),
  );
  await page.route("**/api/ib-status", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ connected: false }) }),
  );
  await page.route("**/api/blotter", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ as_of: new Date().toISOString(), summary: { realized_pnl: 3847.20 }, closed_trades: [], open_trades: [] }) }),
  );
}

// ── Tests ────────────────────────────────────────────────────────────────────

test.describe("Realized P&L — fills-derived, not IB account summary", () => {
  test("RED: no fills today → Realized P&L shows $0.00 not -$6,835", async ({ page }) => {
    await setupBaseMocks(page);
    await page.route("**/api/orders", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(ORDERS_NO_FILLS) }),
    );

    await page.goto("/portfolio");

    // Find the Realized P&L card in the ACCOUNT row
    const realizedCard = page.locator(".metric-card", { hasText: "Realized P&L" }).first();
    await realizedCard.waitFor({ timeout: 10_000 });

    // Must show $0.00 (no fills), NOT -$6,835 from IB account summary
    const value = realizedCard.locator(".metric-value");
    await expect(value).toHaveText("$0.00");
    await expect(value).not.toContainText("-$6,835");
    await expect(value).not.toContainText("-6,835");
  });

  test("GREEN: fills present → Realized P&L shows sum of fill P&L", async ({ page }) => {
    await setupBaseMocks(page);
    await page.route("**/api/orders", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(ORDERS_WITH_FILLS) }),
    );

    await page.goto("/portfolio");

    // $473 + (-$215) = $258
    const realizedCard = page.locator(".metric-card", { hasText: "Realized P&L" }).first();
    await realizedCard.waitFor({ timeout: 10_000 });

    const value = realizedCard.locator(".metric-value");
    await expect(value).toContainText("258");
  });

  test("GREEN: click Realized P&L card → fills modal appears with breakdown", async ({ page }) => {
    await setupBaseMocks(page);
    await page.route("**/api/orders", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(ORDERS_WITH_FILLS) }),
    );

    await page.goto("/portfolio");

    const realizedCard = page.locator(".metric-card", { hasText: "Realized P&L" }).first();
    await realizedCard.waitFor({ timeout: 10_000 });
    await realizedCard.click();

    // Modal should appear with fills breakdown
    const modal = page.locator(".fills-modal");
    await modal.waitFor({ timeout: 5_000 });
    await expect(modal).toBeVisible();

    // Should list the fills
    await expect(modal).toContainText("AAPL");
    await expect(modal).toContainText("GOOG");

    // Should show total
    await expect(modal).toContainText("258");
  });
});
