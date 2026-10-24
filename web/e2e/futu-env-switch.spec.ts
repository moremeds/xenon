import { test, expect, type Page } from "@playwright/test";
import path from "node:path";

const SCREEN_DIR = path.join(process.cwd(), "..", "output", "playwright");

function portfolioFixture(ticker: string) {
  return {
    bankroll: 100_000,
    peak_value: 100_000,
    last_sync: new Date().toISOString(),
    positions: [
      {
        id: 1,
        ticker,
        structure: "Stock (100 shares)",
        structure_type: "Stock",
        risk_profile: "equity",
        expiry: "N/A",
        contracts: 100,
        direction: "LONG",
        entry_cost: 15_000,
        max_risk: null,
        market_value: 17_850,
        legs: [
          {
            direction: "LONG",
            contracts: 100,
            type: "Stock",
            strike: null,
            entry_cost: 15_000,
            avg_cost: 150,
            market_price: 178.5,
            market_value: 17_850,
          },
        ],
        kelly_optimal: null,
        target: null,
        stop: null,
        entry_date: "2026-01-01",
      },
    ],
    total_deployed_pct: 3,
    total_deployed_dollars: 17_850,
    remaining_capacity_pct: 97,
    position_count: 1,
    defined_risk_count: 0,
    undefined_risk_count: 0,
    avg_kelly_optimal: null,
    account_summary: {
      net_liquidation: 100_000,
      daily_pnl: 0,
      unrealized_pnl: 0,
      realized_pnl: 0,
      settled_cash: 40_000,
      maintenance_margin: 0,
      excess_liquidity: 0,
      buying_power: 80_000,
      dividends: 0,
      cash: 40_000,
      initial_margin: 0,
      available_funds: 0,
      equity_with_loan: 100_000,
      previous_day_ewl: 0,
      reg_t_equity: 0,
      sma: 0,
      gross_position_value: 17_850,
    },
  };
}

async function installMocks(page: Page) {
  const futuUrls: string[] = [];
  const orderUrls: string[] = [];

  await page.route("**/api/futu/portfolio**", async (route) => {
    const url = route.request().url();
    futuUrls.push(url);
    const env = new URL(url).searchParams.get("account_env") ?? "paper";
    if (env === "live") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          scope_reason: "not_configured",
          positions: [],
          count: 0,
        }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        fetched_at: new Date().toISOString(),
        data_as_of: new Date().toISOString(),
        account_id: "FUTU-PAPER",
        source: "futu",
        is_stale: false,
        warnings: [],
        positions: [
          {
            futu_code: "US.PAPER",
            normalized: {
              kind: "STK",
              symbol: "PAPER",
              exchange: "US",
              currency: "USD",
              live_data: true,
            },
            quantity: 100,
            avg_cost: 150,
            market_price: 178.5,
            market_value: 17850,
            unrealized_pnl: 0,
            unrealized_pnl_pct: 0,
            currency: "USD",
            position_side: "LONG",
          },
        ],
        count: 1,
        account_summary: {
          net_liquidation: 100_000,
          equity_with_loan: 100_000,
          cash: 40_000,
          settled_cash: 40_000,
          buying_power: 80_000,
          available_funds: 80_000,
          initial_margin: 0,
          maintenance_margin: 0,
          excess_liquidity: 80_000,
          gross_position_value: 17_850,
          unrealized_pnl: 0,
          daily_pnl: 0,
          realized_pnl: 0,
          dividends: null,
          previous_day_ewl: null,
          reg_t_equity: null,
          sma: null,
        },
      }),
    });
  });

  await page.route("**/api/portfolio", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(portfolioFixture("IBSYM")),
    });
  });

  await page.route("**/api/orders**", async (route) => {
    const url = route.request().url();
    orderUrls.push(url);
    const env = new URL(url).searchParams.get("account_env") ?? "paper";
    const marker = env === "live" ? "LIVE-ORD" : "PAPER-ORD";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        last_sync: new Date().toISOString(),
        open_orders: [
          {
            orderId: 1,
            permId: 1,
            action: "BUY",
            totalQuantity: 1,
            orderType: "LMT",
            limitPrice: 1,
            status: "Submitted",
            contract: {
              symbol: marker,
              secType: "STK",
            },
          },
        ],
        executed_orders: [],
        open_count: 1,
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

  await page.route("**/api/blotter**", async (route) => {
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

  return { futuUrls, orderUrls };
}

test.describe("Futu SIMULATE / REAL switch", () => {
  test.beforeEach(async ({ context }) => {
    await context.clearCookies();
  });

  test("toggle paper → live → paper; URLs and badge follow; reload persists", async ({
    page,
  }) => {
    const { futuUrls, orderUrls } = await installMocks(page);

    await page.goto("/portfolio");
    await page.evaluate(() =>
      window.localStorage.removeItem("xenon.futu.account_env"),
    );
    await page.reload();
    await page.getByRole("button", { name: "Switch to FUTU account" }).click();
    await expect(page.getByTestId("futu-env-switch")).toBeVisible();
    await expect(page.getByTestId("futu-env-badge")).toHaveText("SIMULATE");

    await expect
      .poll(() => futuUrls.some((u) => u.includes("account_env=paper")))
      .toBeTruthy();
    await expect(page.getByText(/FUTU-PAPER · simulate/i)).toBeVisible();

    await page.screenshot({
      path: path.join(SCREEN_DIR, "futu-env-switch-2026-10-24-paper.png"),
      fullPage: true,
    });

    await page.getByTestId("futu-env-real").click();
    await expect(page.getByTestId("futu-env-badge")).toHaveText("REAL");
    await expect(page.getByTestId("futu-empty-state")).toHaveText(
      "No REAL Futu account configured for this environment.",
    );
    await expect(page.getByTestId("futu-not-configured-panel")).toHaveCount(1);
    await expect(page.getByTestId("portfolio-view-toggle")).toHaveCount(0);
    await expect(page.getByTestId("futu-retry")).toHaveCount(0);
    await expect
      .poll(() => futuUrls.some((u) => u.includes("account_env=live")))
      .toBeTruthy();

    await page.screenshot({
      path: path.join(SCREEN_DIR, "futu-env-switch-2026-10-24-live.png"),
      fullPage: true,
    });

    await page.goto("/orders");
    await page.getByRole("button", { name: "Switch to FUTU account" }).click();
    await expect
      .poll(() => orderUrls.some((u) => u.includes("account_env=live")))
      .toBeTruthy();
    await expect(page.getByTestId("futu-env-badge")).toHaveText("REAL");

    await page.getByTestId("futu-env-simulate").click();
    await expect(page.getByTestId("futu-env-badge")).toHaveText("SIMULATE");
    await expect
      .poll(
        () => orderUrls.filter((u) => u.includes("account_env=paper")).length,
      )
      .toBeGreaterThan(0);

    await page.reload();
    await page.getByRole("button", { name: "Switch to FUTU account" }).click();
    await expect(page.getByTestId("futu-env-badge")).toHaveText("SIMULATE");
  });
});
