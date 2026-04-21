/**
 * E2E: Multi-leg spread positions show net spread pricing in the PriceBar,
 * not the underlying stock price.
 *
 * Bug: GOOG bull call spread ticker detail modal was showing "GOOG (underlying)"
 * with stock prices instead of computing net spread bid/ask/last from per-leg
 * WS option prices.
 */

import { test, expect } from "@playwright/test";

const PORTFOLIO_WITH_SPREAD = {
  bankroll: 100_000,
  peak_value: 100_000,
  last_sync: new Date().toISOString(),
  total_deployed_pct: 22.88,
  total_deployed_dollars: 22880,
  remaining_capacity_pct: 77.12,
  position_count: 1,
  defined_risk_count: 1,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  exposure: {},
  violations: [],
  positions: [
    {
      id: 1,
      ticker: "GOOG",
      structure: "DEBIT 44X BULL CALL SPREAD $315.0/$340.0",
      structure_type: "Vertical",
      direction: "LONG",
      contracts: 44,
      expiry: "2026-03-20",
      entry_date: "2026-03-05",
      entry_cost: 22880,
      market_value: 19800,
      market_price: 4.5,
      market_price_is_calculated: false,
      avg_cost: 5.2,
      risk_profile: "defined",
      target: null,
      stop: null,
      legs: [
        {
          direction: "LONG",
          contracts: 44,
          type: "Call",
          strike: 315,
          avg_cost: 12.9,
          entry_cost: 56760,
          market_price: 8.5,
          market_price_is_calculated: false,
          market_value: 37400,
        },
        {
          direction: "SHORT",
          contracts: 44,
          type: "Call",
          strike: 340,
          avg_cost: 7.7,
          entry_cost: 33880,
          market_price: 4.0,
          market_price_is_calculated: false,
          market_value: 17600,
        },
      ],
    },
  ],
};

/** WS prices: underlying + per-leg option prices */
const PRICES = {
  GOOG: {
    symbol: "GOOG",
    last: 307.84,
    lastIsCalculated: false,
    bid: 307.8,
    ask: 307.88,
    bidSize: 100,
    askSize: 80,
    volume: 15_000_000,
    high: 310.5,
    low: 305.2,
    open: 306.0,
    close: 308.1,
    week52High: null,
    week52Low: null,
    avgVolume: null,
    delta: null,
    gamma: null,
    theta: null,
    vega: null,
    impliedVol: null,
    undPrice: null,
    timestamp: new Date().toISOString(),
  },
  GOOG_20260320_315_C: {
    symbol: "GOOG_20260320_315_C",
    last: 8.65,
    lastIsCalculated: false,
    bid: 8.5,
    ask: 8.8,
    bidSize: 20,
    askSize: 15,
    volume: 500,
    high: null,
    low: null,
    open: null,
    close: 9.0,
    week52High: null,
    week52Low: null,
    avgVolume: null,
    delta: 0.55,
    gamma: 0.02,
    theta: -0.15,
    vega: 0.3,
    impliedVol: 0.35,
    undPrice: 307.84,
    timestamp: new Date().toISOString(),
  },
  GOOG_20260320_340_C: {
    symbol: "GOOG_20260320_340_C",
    last: 4.1,
    lastIsCalculated: false,
    bid: 4.0,
    ask: 4.2,
    bidSize: 25,
    askSize: 18,
    volume: 300,
    high: null,
    low: null,
    open: null,
    close: 4.5,
    week52High: null,
    week52Low: null,
    avgVolume: null,
    delta: 0.3,
    gamma: 0.015,
    theta: -0.1,
    vega: 0.25,
    impliedVol: 0.33,
    undPrice: 307.84,
    timestamp: new Date().toISOString(),
  },
};

function stubApis(page: import("@playwright/test").Page) {
  page.route("**/api/portfolio", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(PORTFOLIO_WITH_SPREAD),
    }),
  );
  page.route("**/api/orders", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        last_sync: new Date().toISOString(),
        open_orders: [],
        executed_orders: [],
        open_count: 0,
        executed_count: 0,
      }),
    }),
  );
  page.route("**/api/regime", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ score: 15, cri: { score: 15 } }),
    }),
  );
  page.route("**/api/ib-status", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ connected: false }),
    }),
  );
  page.route("**/api/blotter", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        as_of: new Date().toISOString(),
        summary: { realized_pnl: 0 },
        closed_trades: [],
        open_trades: [],
      }),
    }),
  );
  page.route("**/api/ticker/**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        uw_info: {
          name: "Alphabet Inc.",
          sector: "Technology",
          description: "Test",
        },
        stock_state: {},
        profile: {},
        stats: {},
      }),
    }),
  );
  page.route("**/api/prices", (route) => route.abort());
}

test.describe("Spread PriceBar — net pricing from per-leg WS data", () => {
  // The positive-case test ("shows net spread bid/ask/last…") was a
  // test.fixme stub that required a WS mock fixture surviving navigation.
  // Net spread pricing is asserted in the Vitest unit tests
  // web/tests/spread-price-bar.test.ts and web/tests/order-reliability.test.ts
  // ("ComboOrderForm net price calculation"), so dropping the E2E stub
  // doesn't reduce coverage.

  test("falls back to underlying when per-leg prices are unavailable", async ({
    page,
  }) => {
    await page.unrouteAll({ behavior: "ignoreErrors" });
    stubApis(page);

    await page.goto("/portfolio");

    // Only inject underlying price, NOT leg prices
    await page.evaluate((price) => {
      window.dispatchEvent(
        new CustomEvent("ws-price", {
          detail: { type: "price", symbol: price.symbol, data: price },
        }),
      );
    }, PRICES.GOOG);

    const googLink = page
      .locator('[aria-label="View details for GOOG"]')
      .first();
    await googLink.waitFor({ timeout: 10_000 });
    await googLink.click();
    await page.waitForURL("**/GOOG**", { timeout: 5_000 });

    const detail = page.locator(".ticker-detail-page");
    await detail.waitFor({ timeout: 5_000 });

    // Re-inject underlying price only after page navigation
    await page.evaluate((price) => {
      window.dispatchEvent(
        new CustomEvent("ws-price", {
          detail: { type: "price", symbol: price.symbol, data: price },
        }),
      );
    }, PRICES.GOOG);

    // Should fall back to underlying when leg prices are missing
    const priceBar = detail.locator(".price-bar");
    await priceBar.waitFor({ timeout: 5_000 });
    await expect(priceBar).toContainText("(underlying)");
  });
});
