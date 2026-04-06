import { expect, test } from "@playwright/test";

function parsePrice(text: string | null): number {
  return Number((text ?? "").replace(/[$,]/g, ""));
}

function formatUsd(value: number): string {
  return `$${value.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

const PORTFOLIO_WITH_VERTICAL = {
  bankroll: 100_000,
  peak_value: 100_000,
  last_sync: new Date().toISOString(),
  total_deployed_pct: 9.84,
  total_deployed_dollars: 9842,
  remaining_capacity_pct: 90.16,
  position_count: 1,
  defined_risk_count: 1,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  exposure: {},
  violations: [],
  positions: [
    {
      id: 18,
      ticker: "AAOI",
      structure: "Bull Call Spread $105.0/$130.0",
      structure_type: "Vertical",
      direction: "LONG",
      contracts: 25,
      expiry: "2026-03-20",
      entry_date: "2026-03-03",
      entry_cost: 2735,
      market_value: 26450,
      market_price: 10.58,
      market_price_is_calculated: false,
      avg_cost: 1.09,
      risk_profile: "defined",
      target: null,
      stop: null,
      legs: [
        {
          direction: "LONG",
          contracts: 25,
          type: "Call",
          strike: 105,
          avg_cost: 910,
          entry_cost: 22750,
          market_price: 14.67,
          market_price_is_calculated: false,
          market_value: 36675,
        },
        {
          direction: "SHORT",
          contracts: 25,
          type: "Call",
          strike: 130,
          avg_cost: -801,
          entry_cost: -20015,
          market_price: 4.09,
          market_price_is_calculated: false,
          market_value: 10225,
        },
      ],
    },
  ],
};

const PRICES = {
  AAOI: {
    symbol: "AAOI",
    last: 17.41,
    lastIsCalculated: false,
    bid: 17.38,
    ask: 17.44,
    bidSize: 100,
    askSize: 90,
    volume: 1_245_000,
    high: 17.89,
    low: 16.72,
    open: 17.05,
    close: 17.22,
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
  AAOI_20260320_105_C: {
    symbol: "AAOI_20260320_105_C",
    last: 14.67,
    lastIsCalculated: false,
    bid: 13.8,
    ask: 16.2,
    bidSize: 12,
    askSize: 9,
    volume: 232,
    high: 15.89,
    low: 14.8,
    open: 15.25,
    close: 24.98,
    week52High: null,
    week52Low: null,
    avgVolume: null,
    delta: 0.42,
    gamma: 0.03,
    theta: -0.12,
    vega: 0.21,
    impliedVol: 0.64,
    undPrice: 17.4,
    timestamp: new Date().toISOString(),
  },
  AAOI_20260320_130_C: {
    symbol: "AAOI_20260320_130_C",
    last: 4.09,
    lastIsCalculated: false,
    bid: 3.9,
    ask: 4.25,
    bidSize: 8,
    askSize: 11,
    volume: 146,
    high: 4.3,
    low: 3.7,
    open: 4.05,
    close: 4.21,
    week52High: null,
    week52Low: null,
    avgVolume: null,
    delta: 0.17,
    gamma: 0.01,
    theta: -0.05,
    vega: 0.12,
    impliedVol: 0.58,
    undPrice: 17.4,
    timestamp: new Date().toISOString(),
  },
};

function stubApis(page: import("@playwright/test").Page) {
  page.route("**/api/portfolio", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(PORTFOLIO_WITH_VERTICAL),
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
      body: JSON.stringify({}),
    }),
  );

  page.route("**/api/prices", (route) => route.abort());
}

test.describe("Portfolio order ticket quote telemetry", () => {
  test("shows BID, MID, ASK ordering and raw spread percent in the instrument ticket", async ({ page }) => {
    await page.unrouteAll({ behavior: "ignoreErrors" });
    stubApis(page);

    await page.goto("/portfolio");

    await page.evaluate((prices) => {
      for (const [, priceData] of Object.entries(prices)) {
        window.dispatchEvent(
          new CustomEvent("ws-price", {
            detail: {
              type: "price",
              symbol: (priceData as { symbol: string }).symbol,
              data: priceData,
            },
          }),
        );
      }
    }, PRICES);

    const expandLegs = page.locator('button[aria-label="Expand legs for AAOI"]').first();
    await expandLegs.waitFor({ timeout: 10_000 });
    await expandLegs.click();

    const legTrigger = page.locator(".leg-clickable", { hasText: "LONG 25x Call $105" }).first();
    await legTrigger.waitFor({ timeout: 5_000 });
    await legTrigger.click();

    const modal = page.locator(".instrument-detail-modal");
    await modal.waitFor({ timeout: 5_000 });

    const labels = await modal.locator(".price-bar .price-bar-item .price-bar-label").allTextContents();
    expect(labels.slice(1, 5)).toEqual(["BID", "MID", "ASK", "SPREAD"]);

    const bidText = await modal
      .locator(".price-bar-item")
      .filter({ hasText: "BID" })
      .locator(".price-bar-value")
      .textContent();
    const askText = await modal
      .locator(".price-bar-item")
      .filter({ hasText: "ASK" })
      .locator(".price-bar-value")
      .textContent();
    const spreadValue = modal
      .locator(".price-bar-item")
      .filter({ hasText: "SPREAD" })
      .locator(".price-bar-value");

    const bid = parsePrice(bidText);
    const ask = parsePrice(askText);
    const spread = ask - bid;
    const mid = (bid + ask) / 2;
    const expectedSpread = `${formatUsd(spread)} / ${((spread / mid) * 100).toFixed(2)}%`;

    await expect(spreadValue).toHaveText(expectedSpread);
  });
});
