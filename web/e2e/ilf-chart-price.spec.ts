/**
 * E2E: ILF ticker detail chart must seed near current market price (~$33-35),
 * not the stale $22 that was hardcoded in mockPriceGenerator.ts.
 *
 * ILF = iShares Latin America 40 ETF, current price ~$33.82 (2026-03-09).
 * The chart tooltip label showed $22.02 because BASE_PRICES had ILF: 22.
 */

import { test, expect } from "@playwright/test";

const ORDERS_WITH_ILF = {
  last_sync: new Date().toISOString(),
  open_orders: [],
  executed_orders: [
    {
      execId: "exec-ilf-1",
      symbol: "ILF",
      contract: { symbol: "ILF", secType: "STK", currency: "USD", exchange: "SMART" },
      side: "BOT",
      quantity: 200,
      avgPrice: 33.50,
      commission: -2.00,
      realizedPNL: -6835.27,
      time: new Date().toISOString(),
      exchange: "NYSE",
    },
  ],
  open_count: 0,
  executed_count: 1,
};

// FIXME: Needs WS mock fixture — page navigation resets React state, so
// chart seed prices from the mock WS don't match expected ranges.
test.fixme("ILF chart seeds above $30, not at stale $22", async ({ page }) => {
  await page.unrouteAll({ behavior: "ignoreErrors" });

  await page.route("**/api/portfolio", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ bankroll: 50000, peak_value: 50000, last_sync: new Date().toISOString(), total_deployed_pct: 0, total_deployed_dollars: 0, remaining_capacity_pct: 100, position_count: 0, defined_risk_count: 0, undefined_risk_count: 0, avg_kelly_optimal: null, positions: [], exposure: {}, violations: [] }) }),
  );
  await page.route("**/api/orders", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(ORDERS_WITH_ILF) }),
  );
  await page.route("**/api/prices", (route) => route.abort());
  await page.route("**/api/regime", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ score: 15, cri: { score: 15 } }) }),
  );
  await page.route("**/api/ib-status", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ connected: false }) }),
  );
  await page.route("**/api/blotter", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ as_of: new Date().toISOString(), summary: { realized_pnl: 0 }, closed_trades: [], open_trades: [] }) }),
  );
  await page.route("**/api/ticker/**", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({}) }),
  );

  // Navigate directly to the ILF ticker detail page
  await page.goto("/ILF");

  // Wait for the ticker detail page to load
  const detail = page.locator(".ticker-detail-page");
  await detail.waitFor({ timeout: 5_000 });

  // The chart value shown on the page must NOT be $22.xx
  // It should be seeded near $34 (the corrected base price)
  // The chart tooltip/label typically shows the current value
  const chartLabel = detail.locator("text=/\\$\\d+\\.\\d+/").first();
  await chartLabel.waitFor({ timeout: 5_000 });

  const text = await chartLabel.textContent();
  const match = text?.match(/\$([\d.]+)/);
  if (match) {
    const price = parseFloat(match[1]);
    // Must be in the $28-$42 range (±25% of $34), NOT stuck around $22
    expect(price).toBeGreaterThan(28);
    expect(price).toBeLessThan(42);
  }
});
