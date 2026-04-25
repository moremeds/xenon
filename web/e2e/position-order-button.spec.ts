import { test, expect, type Page } from "@playwright/test";

/**
 * Position-row ⚡ order button: structural E2E checks.
 *
 * Covers (post-rework):
 *  1. ⚡ button renders on IB tab position rows (stock + spread)
 *  2. Clicking ⚡ opens the modal with Close/Add intent bar; Close defaults active
 *  3. Modal shows Quantity input defaulting to position.contracts and Submit close button
 *  4. 50% chip updates quantity to half and shows the partial-close note
 *  5. Futu tab hides ⚡ — no .position-order-btn exists
 *  6. Close button closes the modal (cleanup check)
 *  7. POST /api/orders/place — close payload shape (action SELL for LONG stock)
 *  8. Close/Add toggle switches submit label and payload action (BUY for LONG stock + Add)
 */

const IB_FIXTURE = {
  bankroll: 847231,
  peak_value: 900000,
  last_sync: new Date().toISOString(),
  positions: [
    {
      id: 1,
      ticker: "TSLA",
      structure: "Stock",
      structure_type: "Stock",
      risk_profile: "equity",
      expiry: "",
      contracts: 100,
      direction: "LONG",
      entry_cost: 32000,
      max_risk: null,
      market_value: 35000,
      legs: [
        {
          direction: "LONG",
          contracts: 100,
          type: "Stock",
          strike: null,
          conId: 265598,
          entry_cost: 32000,
          avg_cost: 320,
          market_price: 350,
          market_value: 35000,
          market_price_is_calculated: false,
        },
      ],
      ib_daily_pnl: null,
      kelly_optimal: null,
      target: null,
      stop: null,
      entry_date: "",
    },
    {
      id: 2,
      ticker: "SPY",
      structure: "Bull Call Spread",
      structure_type: "Vertical",
      risk_profile: "defined",
      expiry: "2026-06-20",
      contracts: 5,
      direction: "LONG",
      entry_cost: 1500,
      max_risk: 1500,
      market_value: 1800,
      legs: [
        {
          direction: "LONG",
          contracts: 5,
          type: "Call",
          strike: 500,
          entry_cost: 2000,
          avg_cost: 4.0,
          market_price: 5.2,
          market_value: 2600,
          market_price_is_calculated: false,
        },
        {
          direction: "SHORT",
          contracts: 5,
          type: "Call",
          strike: 510,
          entry_cost: -500,
          avg_cost: 1.0,
          market_price: 1.6,
          market_value: -800,
          market_price_is_calculated: false,
        },
      ],
      ib_daily_pnl: null,
      kelly_optimal: null,
      target: null,
      stop: null,
      entry_date: "2026-01-15",
    },
  ],
  total_deployed_pct: 15,
  total_deployed_dollars: 36800,
  remaining_capacity_pct: 85,
  position_count: 2,
  defined_risk_count: 1,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  account_summary: {
    net_liquidation: 847231,
    daily_pnl: 3104,
    unrealized_pnl: 2850,
    realized_pnl: 0,
    settled_cash: 38811,
    maintenance_margin: 31786,
    excess_liquidity: 11551,
    buying_power: 49533,
    dividends: 0.08,
    cash: 38811,
    initial_margin: 35903,
    available_funds: 7430,
    equity_with_loan: 843333,
    previous_day_ewl: 0,
    reg_t_equity: 0,
    sma: 0,
    gross_position_value: 19210,
  },
};

const FUTU_FIXTURE = {
  ok: true,
  fetched_at: new Date().toISOString(),
  data_as_of: new Date().toISOString(),
  account_id: "281756478831553263",
  source: "futu" as const,
  is_stale: false,
  warnings: [] as string[],
  count: 1,
  positions: [
    {
      futu_code: "US.TSLA",
      normalized: {
        kind: "STK" as const,
        symbol: "TSLA",
        exchange: "SMART",
        currency: "USD",
        live_data: true,
      },
      quantity: 300,
      avg_cost: 320.71,
      market_price: 351.67,
      market_value: 105501.0,
      unrealized_pnl: 9288.0,
      unrealized_pnl_pct: 9.65,
      currency: "USD",
      position_side: "LONG",
    },
  ],
  account_summary: {
    net_liquidation: 148000,
    equity_with_loan: 148000,
    cash: -14585,
    settled_cash: -14585,
    buying_power: 29917,
    available_funds: 29917,
    initial_margin: 130962,
    maintenance_margin: 114285,
    excess_liquidity: 33715,
    gross_position_value: 105501,
    unrealized_pnl: 9288,
    daily_pnl: 9288,
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
      body: JSON.stringify(IB_FIXTURE),
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
  await page.route("**/api/ticker/**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        uw_info: {
          name: "Test Inc.",
          sector: "Technology",
          description: "Test",
        },
        stock_state: {},
        profile: {},
        stats: {},
      }),
    });
  });
  await page.route("**/api/options/expirations*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ symbol: "TSLA", expirations: ["20260619"] }),
    });
  });
  await page.route("**/api/options/chain*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        symbol: "TSLA",
        expiry: "20260619",
        exchange: "SMART",
        strikes: [300, 310, 320, 330, 340],
        multiplier: "100",
      }),
    });
  });
}

test.describe("Position-row ⚡ order button", () => {
  test.beforeEach(async ({ page }) => {
    await installMocks(page);
    await page.goto("/portfolio");
    await page.waitForSelector(".position-order-btn", { timeout: 10_000 });
  });

  test("1. ⚡ button renders on IB tab position rows (stock and spread)", async ({
    page,
  }) => {
    const buttons = page.locator(".position-order-btn");
    await expect(buttons).toHaveCount(2);
  });

  test("2. clicking ⚡ opens modal with Close/Add intent bar; Close defaults active", async ({
    page,
  }) => {
    await page.locator(".position-order-btn").first().click();

    const intentBar = page.locator('[aria-label="Order intent"]');
    await expect(intentBar).toBeVisible({ timeout: 5_000 });

    const tiles = intentBar.locator(".preset-tile");
    await expect(tiles).toHaveCount(2);

    const closeTile = tiles.filter({ hasText: "Close" });
    await expect(closeTile).toHaveAttribute("aria-pressed", "true");
    const addTile = tiles.filter({ hasText: "Add" });
    await expect(addTile).toHaveAttribute("aria-pressed", "false");
  });

  test("3. modal shows Quantity input defaulting to position.contracts and Submit close button", async ({
    page,
  }) => {
    await page.locator(".position-order-btn").first().click();

    const qtyInput = page.locator("#position-order-qty");
    await expect(qtyInput).toBeVisible({ timeout: 5_000 });
    await expect(qtyInput).toHaveValue("100");

    const submitBtn = page.getByRole("button", { name: /submit close/i });
    await expect(submitBtn).toBeVisible();
  });

  test("4. 50% chip updates quantity to half and shows partial-close note", async ({
    page,
  }) => {
    await page.locator(".position-order-btn").first().click();
    await page.waitForSelector("#position-order-qty", { timeout: 5_000 });

    const chipRow = page.locator('[aria-label="Close size chips"]');
    await chipRow.getByRole("button", { name: "50%" }).click();

    const qtyInput = page.locator("#position-order-qty");
    await expect(qtyInput).toHaveValue("50");

    const note = page.locator(".partial-close-note");
    await expect(note).toBeVisible();
    await expect(note).toHaveText(/partial close — 50 of 100 contracts/i);
  });

  test("5. Futu tab hides ⚡ buttons", async ({ page }) => {
    const futuTab = page.getByRole("button", {
      name: /Switch to FUTU account/,
    });
    await futuTab.waitFor({ timeout: 5_000 });
    await futuTab.click();

    await page.waitForSelector("button.ticker-link", { timeout: 5_000 });

    await expect(page.locator(".position-order-btn")).toHaveCount(0);
  });

  test("6. modal close button dismisses the modal", async ({ page }) => {
    await page.locator(".position-order-btn").first().click();

    const intentBar = page.locator('[aria-label="Order intent"]');
    await expect(intentBar).toBeVisible({ timeout: 5_000 });

    const closeBtn = page.locator("button.modal-close");
    await closeBtn.click();

    await expect(intentBar).not.toBeVisible({ timeout: 3_000 });
  });

  test("7. POST /api/orders/place payload shape (close — action SELL for LONG stock)", async ({
    page,
  }) => {
    let capturedBody: Record<string, unknown> | null = null;

    await page.route("**/api/orders/place", async (route) => {
      const req = route.request();
      try {
        capturedBody = JSON.parse(req.postData() ?? "{}");
      } catch {
        capturedBody = null;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ orderId: "mock-order-123" }),
      });
    });

    await page.locator(".position-order-btn").first().click();
    await page.waitForSelector("#position-order-price", { timeout: 5_000 });

    const limitInput = page.locator("#position-order-price");
    await limitInput.fill("350");

    const tifGroup = page.getByRole("group", { name: "Time in force" });
    await expect(tifGroup).toBeVisible();
    await tifGroup.getByRole("button", { name: "GTC" }).click();

    const submitBtn = page.getByRole("button", { name: /submit close/i });
    await expect(submitBtn).toBeEnabled({ timeout: 2_000 });
    await submitBtn.click();

    await page.waitForFunction(() => true);

    expect(capturedBody).not.toBeNull();
    expect(capturedBody!.type).toBe("stock");
    expect(capturedBody!.symbol).toBe("TSLA");
    expect(capturedBody!.action).toBe("SELL");
    expect(typeof capturedBody!.quantity).toBe("number");
    expect(typeof capturedBody!.client_attempt_id).toBe("string");
    expect((capturedBody!.client_attempt_id as string).length).toBeGreaterThan(
      0,
    );
    expect(capturedBody!.tif).toBe("GTC");
  });

  test("8. Close/Add toggle switches the submit button label and the payload action", async ({
    page,
  }) => {
    let capturedBody: Record<string, unknown> | null = null;

    await page.route("**/api/orders/place", async (route) => {
      const req = route.request();
      try {
        capturedBody = JSON.parse(req.postData() ?? "{}");
      } catch {
        capturedBody = null;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ orderId: "mock-order-456" }),
      });
    });

    await page.locator(".position-order-btn").first().click();
    await page.waitForSelector("#position-order-price", { timeout: 5_000 });

    const intentBar = page.locator('[aria-label="Order intent"]');
    await expect(
      intentBar.getByRole("button", { name: /^Close$/ }),
    ).toHaveAttribute("aria-pressed", "true");
    await expect(
      page.getByRole("button", { name: /Submit close/i }),
    ).toBeVisible();

    await intentBar.getByRole("button", { name: /^Add$/ }).click();
    await expect(
      intentBar.getByRole("button", { name: /^Add$/ }),
    ).toHaveAttribute("aria-pressed", "true");
    await expect(
      page.getByRole("button", { name: /Submit add/i }),
    ).toBeVisible();

    const limitInput = page.locator("#position-order-price");
    await limitInput.fill("350");

    const submitBtn = page.getByRole("button", { name: /submit add/i });
    await expect(submitBtn).toBeEnabled({ timeout: 2_000 });
    await submitBtn.click();

    await page.waitForFunction(() => true);

    expect(capturedBody).not.toBeNull();
    expect(capturedBody!.action).toBe("BUY");
    expect(capturedBody!.symbol).toBe("TSLA");
  });
});
