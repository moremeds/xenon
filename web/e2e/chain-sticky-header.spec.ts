/**
 * E2E: Options chain thead stays sticky above tbody rows when scrolled.
 */

import { test, expect } from "@playwright/test";

const PORTFOLIO = {
  bankroll: 100_000,
  peak_value: 100_000,
  last_sync: new Date().toISOString(),
  total_deployed_pct: 0,
  total_deployed_dollars: 0,
  remaining_capacity_pct: 100,
  position_count: 0,
  defined_risk_count: 0,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  exposure: {},
  violations: [],
  positions: [],
};

const ORDERS = {
  last_sync: new Date().toISOString(),
  open_orders: [],
  executed_orders: [],
  open_count: 0,
  executed_count: 0,
};

// Generate enough strikes to overflow the 520px wrapper
const strikes = Array.from({ length: 40 }, (_, i) => 150 + i * 2.5);

const EXPIRATIONS = {
  symbol: "AAPL",
  expirations: ["20260320", "20260417"],
};

const CHAIN_STRIKES = {
  symbol: "AAPL",
  expiry: "20260417",
  exchange: "SMART",
  strikes,
  multiplier: "100",
};

function stubApis(page: import("@playwright/test").Page) {
  page.route("**/api/portfolio", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(PORTFOLIO) }),
  );
  page.route("**/api/orders", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(ORDERS) }),
  );
  page.route("**/api/regime", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ score: 15, cri: { score: 15 } }) }),
  );
  page.route("**/api/ib-status", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ connected: false }) }),
  );
  page.route("**/api/blotter", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ as_of: new Date().toISOString(), summary: { realized_pnl: 0 }, closed_trades: [], open_trades: [] }),
    }),
  );
  page.route("**/api/ticker/**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        uw_info: { name: "Apple Inc.", sector: "Technology", description: "Test" },
        stock_state: {},
        profile: {},
        stats: {},
      }),
    }),
  );
  page.route("**/api/options/expirations*", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(EXPIRATIONS) }),
  );
  page.route("**/api/options/chain*", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(CHAIN_STRIKES) }),
  );
  page.route("**/api/prices", (route) => route.abort());
}

test.describe("Options chain sticky header", () => {
  test("thead stays above tbody rows after scrolling", async ({ page }) => {
    await page.unrouteAll({ behavior: "ignoreErrors" });
    stubApis(page);

    // Navigate directly to the ticker detail chain tab route
    await page.goto("/AAPL?tab=chain");

    // Wait for chain grid to load
    const chainGrid = page.locator(".chain-grid");
    await chainGrid.waitFor({ timeout: 10_000 });

    const wrapper = page.locator(".chain-grid-wrapper");
    const header = page.locator("th.chain-header").first();

    // Verify sticky position and z-index on the header cells
    const headerStyles = await header.evaluate((el) => {
      const cs = window.getComputedStyle(el);
      return { position: cs.position, zIndex: cs.zIndex };
    });
    expect(headerStyles.position).toBe("sticky");
    expect(Number(headerStyles.zIndex)).toBeGreaterThanOrEqual(10);

    // Scroll the wrapper down significantly
    await wrapper.evaluate((el) => {
      el.scrollTop = 300;
    });

    // After scrolling, the header top should be at or near the wrapper top
    const positions = await page.evaluate(() => {
      const w = document.querySelector(".chain-grid-wrapper")!;
      const th = document.querySelector("th.chain-header")!;
      const wRect = w.getBoundingClientRect();
      const thRect = th.getBoundingClientRect();
      return {
        wrapperTop: wRect.top,
        headerTop: thRect.top,
      };
    });

    // Header should be pinned at the top of the wrapper (within 2px tolerance)
    expect(Math.abs(positions.headerTop - positions.wrapperTop)).toBeLessThan(2);
  });

  test("chain-side-label has correct sticky z-index", async ({ page }) => {
    await page.unrouteAll({ behavior: "ignoreErrors" });
    stubApis(page);

    await page.goto("/AAPL?tab=chain");

    const chainGrid = page.locator(".chain-grid");
    await chainGrid.waitFor({ timeout: 10_000 });

    const sideLabel = page.locator("th.chain-side-label").first();
    const styles = await sideLabel.evaluate((el) => {
      const cs = window.getComputedStyle(el);
      return { position: cs.position, zIndex: cs.zIndex };
    });
    expect(styles.position).toBe("sticky");
    expect(Number(styles.zIndex)).toBeGreaterThanOrEqual(10);
  });
});
