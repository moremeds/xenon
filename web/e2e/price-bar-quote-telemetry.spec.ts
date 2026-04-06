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

const PORTFOLIO_WITH_SINGLE_CALL = {
  bankroll: 100_000,
  peak_value: 100_000,
  last_sync: new Date().toISOString(),
  total_deployed_pct: 22.78,
  total_deployed_dollars: 22_775,
  remaining_capacity_pct: 77.22,
  position_count: 1,
  defined_risk_count: 1,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  exposure: {},
  violations: [],
  positions: [
    {
      id: 1,
      ticker: "AAOI",
      structure: "Long Call $105",
      structure_type: "Long Call",
      risk_profile: "defined",
      expiry: "2026-03-20",
      contracts: 25,
      direction: "LONG",
      entry_cost: 22_775,
      max_risk: 22_775,
      market_value: 36_675,
      market_price: 14.67,
      market_price_is_calculated: false,
      legs: [
        {
          direction: "LONG",
          contracts: 25,
          type: "Call",
          strike: 105,
          entry_cost: 22_775,
          avg_cost: 911,
          market_price: 14.67,
          market_price_is_calculated: false,
          market_value: 36_675,
        },
      ],
      kelly_optimal: null,
      target: null,
      stop: null,
      entry_date: "2026-03-05",
    },
  ],
};

const PRICES = {
  AAOI: {
    symbol: "AAOI",
    last: 19.6,
    lastIsCalculated: false,
    bid: 19.55,
    ask: 19.65,
    bidSize: 20,
    askSize: 15,
    volume: 500_000,
    high: 20.3,
    low: 19.1,
    open: 19.25,
    close: 19.0,
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
  "AAOI_20260320_105_C": {
    symbol: "AAOI_20260320_105_C",
    last: 14.67,
    lastIsCalculated: false,
    bid: 13.8,
    ask: 16.2,
    bidSize: 10,
    askSize: 12,
    volume: 232,
    high: 15.89,
    low: 14.8,
    open: 15.1,
    close: 25.16,
    week52High: null,
    week52Low: null,
    avgVolume: null,
    delta: 0.48,
    gamma: 0.04,
    theta: -0.19,
    vega: 0.11,
    impliedVol: 0.54,
    undPrice: 19.6,
    timestamp: new Date().toISOString(),
  },
};

function stubApis(page: import("@playwright/test").Page) {
  page.route("**/api/portfolio", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(PORTFOLIO_WITH_SINGLE_CALL),
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
        uw_info: { name: "Applied Optoelectronics, Inc.", sector: "Technology", description: "Test" },
        stock_state: {},
        profile: {},
        stats: {},
      }),
    }),
  );

  page.route("**/api/prices", (route) => route.abort());
}

test.describe("Portfolio ticket quote telemetry", () => {
  // FIXME: Needs WS mock fixture — page navigation resets React state, so
  // injected ws-price custom events no longer flow to usePrices on the ticker page.
  test.fixme("shows BID, MID, ASK order and raw spread percent on the shared ticker modal", async ({ page }) => {
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

    const aoiiLink = page.locator('[aria-label="View details for AAOI"]').first();
    await aoiiLink.waitFor({ timeout: 10_000 });
    await aoiiLink.click();
    await page.waitForURL("**/AAOI**", { timeout: 5_000 });

    const detail = page.locator(".ticker-detail-page");
    await detail.waitFor({ timeout: 5_000 });

    // Re-inject prices after page navigation (prices lost on route change)
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

    const priceBar = detail.locator(".price-bar");
    await priceBar.waitFor({ timeout: 5_000 });

    const labels = await priceBar.locator(".price-bar-label").allTextContents();
    expect(labels.slice(1, 5)).toEqual(["BID", "MID", "ASK", "SPREAD"]);
    const bidText = await priceBar
      .locator(".price-bar-item")
      .filter({ hasText: "BID" })
      .locator(".price-bar-value")
      .textContent();
    const askText = await priceBar
      .locator(".price-bar-item")
      .filter({ hasText: "ASK" })
      .locator(".price-bar-value")
      .textContent();
    const spreadText = await priceBar
      .locator(".price-bar-item")
      .filter({ hasText: "SPREAD" })
      .locator(".price-bar-value")
      .textContent();

    const bid = parsePrice(bidText);
    const ask = parsePrice(askText);
    const spread = ask - bid;
    const mid = (bid + ask) / 2;
    const expectedSpread = `${formatUsd(spread)} / ${((spread / mid) * 100).toFixed(2)}%`;

    expect(spreadText).toBe(expectedSpread);
  });
});
