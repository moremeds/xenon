import { expect, test, type Page } from "@playwright/test";

const PORTFOLIO = {
  bankroll: 100_000,
  peak_value: 100_000,
  last_sync: new Date().toISOString(),
  positions: [
    {
      id: 1,
      ticker: "AAPL",
      structure: "Stock (100 shares)",
      structure_type: "Stock",
      risk_profile: "equity",
      expiry: "N/A",
      contracts: 100,
      direction: "LONG",
      entry_cost: 18_000,
      max_risk: null,
      market_value: 18_500,
      legs: [
        {
          direction: "LONG",
          contracts: 100,
          type: "Stock",
          strike: null,
          entry_cost: 18_000,
          avg_cost: 180,
          market_price: 185,
          market_value: 18_500,
        },
      ],
      kelly_optimal: null,
      target: null,
      stop: null,
      entry_date: "2026-05-04",
    },
  ],
  total_deployed_pct: 18.5,
  total_deployed_dollars: 18_500,
  remaining_capacity_pct: 81.5,
  position_count: 1,
  defined_risk_count: 0,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  account_summary: {
    net_liquidation: 100_000,
    daily_pnl: 500,
    unrealized_pnl: 500,
    realized_pnl: 0,
    settled_cash: 81_500,
    maintenance_margin: 5_000,
    excess_liquidity: 76_500,
    buying_power: 153_000,
    dividends: 0,
    cash: 81_500,
    initial_margin: 5_000,
    available_funds: 76_500,
    equity_with_loan: 100_000,
    previous_day_ewl: 99_500,
    reg_t_equity: 100_000,
    sma: 76_500,
    gross_position_value: 18_500,
  },
};

const HEALTH = {
  schema_version: 1,
  daemon_alive: true,
  market_window: "closed",
  next_market_event_at: "2026-05-05T13:30:00Z",
  last_tick_at: "2026-05-04T20:00:00Z",
  last_tick_age_seconds: 7200,
  rule_counts_by_state: {
    PENDING_ARM: 0,
    ARMED: 1,
    TRIGGERED: 0,
    CLOSED: 0,
    CANCELED: 0,
    FAILED: 0,
    SUPERSEDED: 0,
  },
  claim_counts_by_status: {
    PENDING: 0,
    SUBMITTED: 0,
    FILLED: 0,
    FAILED: 0,
    ABANDONED: 0,
  },
  in_flight_claims: 0,
  stale_quote_skips_last_hour: 0,
  unprotected_position_count: 0,
  ib_connected: true,
  outbox_dlq_count: 0,
};

async function installMocks(page: Page) {
  let rules = [
    {
      protection_id: 7,
      position_key: "STK::AAPL",
      rule_kind: "stop_loss",
      state: "ARMED",
      asset_class: "stock",
      config: { threshold_pct: -0.08, anchor: "entry_price" },
      state_data: {},
      position_descriptor: {
        legs: [{ sec_type: "STK", symbol: "AAPL", action: "BUY", ratio: 1 }],
      },
      native_order_perm_id: 1234,
      armed_at: "2026-05-04T14:00:00Z",
      triggered_at: null,
    },
  ];

  await page.route("**/api/portfolio", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(PORTFOLIO),
    });
  });
  await page.route("**/api/position-rules/health", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(HEALTH),
    });
  });
  await page.route("**/api/position-rules/7/cancel", async (route) => {
    rules = rules.map((rule) => ({ ...rule, state: "CANCELED", native_order_perm_id: null }));
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ protection_id: 7, state: "CANCELED" }),
    });
  });
  await page.route("**/api/position-rules", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(rules),
    });
  });
  await page.route("**/api/futu/portfolio", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ok: true, positions: [], account_summary: {}, count: 0 }),
    });
  });
  await page.route("**/api/orders", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ last_sync: new Date().toISOString(), open_orders: [], executed_orders: [] }),
    });
  });
  await page.route("**/api/ib-status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ connected: true, ib_connected: true, port_listening: true }),
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
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({}) });
  });
}

test.describe("Position rules UI", () => {
  test.beforeEach(async ({ page }) => {
    await installMocks(page);
    await page.addInitScript(() => {
      try {
        window.localStorage.setItem("xenon.portfolio.view", "risk");
      } catch {
        // no-op
      }
    });
  });

  test("shield badge opens drawer and cancel refreshes the badge", async ({ page }) => {
    await page.goto("/portfolio");
    const badge = page.locator("[data-state='ARMED']").first();
    await expect(badge).toBeVisible();

    await badge.click();
    const drawer = page.getByRole("dialog", { name: /position rules/i });
    await expect(drawer).toBeVisible();
    await expect(drawer.getByText("stop_loss")).toBeVisible();

    await drawer.getByRole("button", { name: /cancel rule/i }).click();

    await expect(page.locator("[data-state='ARMED']")).toHaveCount(0, { timeout: 5000 });
    await expect(page.locator("[data-state='CANCELED']").first()).toBeVisible();
  });

  test("global health indicator is visible in the sidebar", async ({ page }) => {
    await page.goto("/portfolio");
    await expect(page.locator("[data-cls='green']").first()).toBeVisible({ timeout: 10000 });
  });
});
