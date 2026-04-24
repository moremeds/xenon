import { test, expect, type Page } from "@playwright/test";

/**
 * E2E verification for Task 4 of the Futu combo presentation plan.
 *
 * Seeds two single-leg puts on the same ticker + expiry with equal contract
 * counts and opposite directions. The Futu-only virtual-pair fuser
 * (buildTickerGroups({ fuseVirtualPairs: true })) collapses them into a
 * single "Bear Put Spread" combo row. This spec asserts the rendered DOM on
 * `/portfolio` matches the component-level Vitest expectation:
 *   - the combo row exists and shows "Bear Put Spread"
 *   - the redundant standalone cell-muted header does NOT appear
 */

const FUTU_FIXTURE = {
  ok: true,
  fetched_at: new Date().toISOString(),
  data_as_of: new Date().toISOString(),
  account_id: "281756478831553263",
  source: "futu" as const,
  is_stale: false,
  warnings: [] as string[],
  count: 2,
  positions: [
    {
      futu_code: "US.TSLA270115P400000",
      normalized: {
        kind: "OPT" as const,
        symbol: "TSLA",
        expiry: "20270115",
        strike: 400,
        right: "P" as const,
        exchange: "SMART",
        currency: "USD",
        trading_class: null,
        live_data: true,
      },
      quantity: 5,
      avg_cost: 40,
      market_price: 38,
      market_value: 19000,
      unrealized_pnl: -1000,
      unrealized_pnl_pct: -5,
      currency: "USD",
      position_side: "LONG",
    },
    {
      futu_code: "US.TSLA270115P390000",
      normalized: {
        kind: "OPT" as const,
        symbol: "TSLA",
        expiry: "20270115",
        strike: 390,
        right: "P" as const,
        exchange: "SMART",
        currency: "USD",
        trading_class: null,
        live_data: true,
      },
      quantity: -5,
      avg_cost: 10,
      market_price: 8,
      market_value: -4000,
      unrealized_pnl: 1000,
      unrealized_pnl_pct: 20,
      currency: "USD",
      position_side: "SHORT",
    },
  ],
  account_summary: {
    net_liquidation: 148000,
    equity_with_loan: 148000,
    cash: 10000,
    settled_cash: 10000,
    buying_power: 29917,
    available_funds: 29917,
    initial_margin: 0,
    maintenance_margin: 0,
    excess_liquidity: 33715,
    gross_position_value: 15000,
    unrealized_pnl: 0,
    daily_pnl: 0,
    realized_pnl: 0,
    dividends: null,
    previous_day_ewl: null,
    reg_t_equity: null,
    sma: null,
  },
};

const IB_FIXTURE = {
  bankroll: 0,
  peak_value: 0,
  last_sync: new Date().toISOString(),
  positions: [],
  total_deployed_pct: 0,
  total_deployed_dollars: 0,
  remaining_capacity_pct: 100,
  position_count: 0,
  defined_risk_count: 0,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  account_summary: {
    net_liquidation: 0,
    daily_pnl: 0,
    unrealized_pnl: 0,
    realized_pnl: 0,
    settled_cash: 0,
    maintenance_margin: 0,
    excess_liquidity: 0,
    buying_power: 0,
    dividends: 0,
    cash: 0,
    initial_margin: 0,
    available_funds: 0,
    equity_with_loan: 0,
    previous_day_ewl: 0,
    reg_t_equity: 0,
    sma: 0,
    gross_position_value: 0,
  },
};

async function installMocks(page: Page) {
  await page.route("**/api/futu/portfolio", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(FUTU_FIXTURE),
    });
  });
  await page.route("**/api/portfolio", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(IB_FIXTURE),
    });
  });
  await page.route("**/api/orders", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        last_sync: new Date().toISOString(),
        open_orders: [],
        executed_orders: [],
        open_count: 0,
        executed_count: 0,
      }),
    });
  });
  await page.route("**/api/ib-status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ connected: true }),
    });
  });
  await page.route("**/api/regime", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ score: 15, cri: { score: 15 } }),
    });
  });
  await page.route("**/api/blotter", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        as_of: new Date().toISOString(),
        summary: { realized_pnl: 0 },
        closed_trades: [],
        open_trades: [],
      }),
    });
  });
}

test.describe("Futu combo presentation parity with IB tab", () => {
  test("fuses two opposite-direction puts into one Bear Put Spread row", async ({
    page,
  }) => {
    await installMocks(page);
    await page.goto("/portfolio");

    const futuTab = page.getByRole("button", {
      name: /Switch to FUTU account/,
    });
    await futuTab.waitFor();
    await futuTab.click();

    // Combo row structure cell should contain "Bear Put Spread"
    await expect(
      page.locator("tr").filter({ hasText: "Bear Put Spread" }).first(),
    ).toBeVisible();

    // Redundant standalone header (div.cell-muted containing the pair label)
    // must NOT appear on the Futu tab — Task 3 suppresses it.
    const strayHeader = page
      .locator("div.cell-muted")
      .filter({ hasText: /Bear Put Spread \$390\/\$400/ });
    await expect(strayHeader).toHaveCount(0);
  });
});
