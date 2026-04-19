import { test, expect, type Page } from "@playwright/test";

const PORTFOLIO_FIXTURE = {
  bankroll: 500_000,
  peak_value: 500_000,
  last_sync: new Date().toISOString(),
  positions: [
    {
      id: 1,
      ticker: "QQQ",
      structure: "Bull Call Spread",
      structure_type: "Bull Call Spread",
      risk_profile: "defined",
      expiry: "2026-04-17",
      contracts: 1,
      direction: "LONG",
      entry_cost: 325,
      max_risk: 325,
      market_value: 360,
      legs: [
        {
          direction: "LONG",
          contracts: 1,
          type: "Call",
          strike: 500,
          entry_cost: 450,
          avg_cost: 450,
          market_price: 5.2,
          market_value: 520,
        },
        {
          direction: "SHORT",
          contracts: 1,
          type: "Call",
          strike: 510,
          entry_cost: -125,
          avg_cost: -125,
          market_price: 1.6,
          market_value: -160,
        },
      ],
      ib_daily_pnl: null,
      kelly_optimal: null,
      target: null,
      stop: null,
      entry_date: "2026-04-01",
    },
  ],
  total_deployed_pct: 10,
  total_deployed_dollars: 50_000,
  remaining_capacity_pct: 90,
  position_count: 1,
  defined_risk_count: 1,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  account_summary: {
    net_liquidation: 500_000,
    daily_pnl: 0,
    unrealized_pnl: 35,
    realized_pnl: 0,
    settled_cash: 50_000,
    maintenance_margin: 5_000,
    excess_liquidity: 45_000,
    buying_power: 90_000,
    dividends: 0,
    cash: 50_000,
    initial_margin: 5_000,
    available_funds: 45_000,
    equity_with_loan: 500_000,
    previous_day_ewl: 0,
    reg_t_equity: 0,
    sma: 0,
    gross_position_value: 360,
  },
};

const FUTU_FIXTURE = {
  ok: true,
  fetched_at: new Date().toISOString(),
  data_as_of: new Date().toISOString(),
  account_id: "FUTU-TEST",
  source: "futu" as const,
  is_stale: false,
  warnings: [] as string[],
  count: 0,
  positions: [],
  account_summary: {
    net_liquidation: 0,
    equity_with_loan: 0,
    cash: 0,
    settled_cash: 0,
    buying_power: 0,
    available_funds: 0,
    initial_margin: 0,
    maintenance_margin: 0,
    excess_liquidity: 0,
    gross_position_value: 0,
    unrealized_pnl: 0,
    daily_pnl: 0,
    realized_pnl: 0,
    dividends: null,
    previous_day_ewl: null,
    reg_t_equity: null,
    sma: null,
  },
};

async function installMocks(page: Page) {
  await page.route("**/api/portfolio", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(PORTFOLIO_FIXTURE),
    });
  });

  await page.route("**/api/futu/portfolio", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(FUTU_FIXTURE),
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
      body: JSON.stringify({
        connected: true,
        port_listening: true,
        ib_connected: true,
        disconnected_since: null,
      }),
    });
  });

  await page.route("**/api/blotter", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ positions: [], daily_pnl: 0 }),
    });
  });

  await page.route("**/api/prices*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({}),
    });
  });
}

test("portfolio table does not emit a colgroup hydration warning", async ({ page }) => {
  const consoleErrors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") {
      consoleErrors.push(msg.text());
    }
  });

  await installMocks(page);
  await page.goto("/portfolio");

  await expect(
    page.getByRole("button", { name: "View details for QQQ" }),
  ).toBeVisible({ timeout: 10_000 });
  await page.waitForTimeout(1_000);

  expect(
    consoleErrors.filter((message) =>
      message.includes("whitespace text nodes cannot be a child of <colgroup>"),
    ),
  ).toHaveLength(0);
});
