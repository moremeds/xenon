import { test, expect, type Page, type Route } from "@playwright/test";
import path from "node:path";

const SCREEN_DIR = path.join(process.cwd(), "..", "output", "playwright");
const BROKER_ORDER_ID = "80001";

type CapturedRequest = {
  method: string;
  url: string;
  body: Record<string, unknown> | null;
};

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

function assertFutuBrokerScope(url: string) {
  const u = new URL(url, "http://localhost");
  expect(u.searchParams.get("broker")).toBe("FUTU");
  expect(u.searchParams.get("account_env")).toMatch(/^(paper|live)$/);
}

function assertFutuPaperScope(url: string) {
  const u = new URL(url, "http://localhost");
  expect(u.searchParams.get("broker")).toBe("FUTU");
  expect(u.searchParams.get("account_env")).toBe("paper");
}

function assertFutuMutationBody(body: Record<string, unknown>) {
  expect(body.brokerOrderId).toBe(BROKER_ORDER_ID);
  expect(body.orderId).toBeUndefined();
  expect(body.permId).toBeUndefined();
  expect(body.type).not.toBe("combo");
  expect(body.replaceOrder).toBeUndefined();
}

async function parseBody(
  route: Route,
): Promise<Record<string, unknown> | null> {
  try {
    const raw = route.request().postData();
    if (!raw) return null;
    return JSON.parse(raw) as Record<string, unknown>;
  } catch {
    return null;
  }
}

async function installOrderFlowMocks(
  page: Page,
  trading: { paper: boolean; live: boolean },
) {
  const captured: CapturedRequest[] = [];
  let working = false;
  let limitPrice = 178.5;
  let quantity = 100;
  let modifySequence = 1;

  const openOrder = () => ({
    brokerOrderId: BROKER_ORDER_ID,
    orderId: 0,
    permId: 0,
    broker: "FUTU",
    accountEnv: "paper",
    brokerAccount: "PAPER",
    symbol: "AAPL",
    action: "SELL",
    orderType: "LMT",
    totalQuantity: quantity,
    limitPrice,
    modifySequence,
    auxPrice: null,
    status: "Submitted",
    filled: 0,
    remaining: quantity,
    avgFillPrice: null,
    tif: "DAY",
    contract: {
      conId: null,
      symbol: "AAPL",
      secType: "STK",
      strike: null,
      right: null,
      expiry: null,
    },
  });

  const ordersPayload = () => ({
    last_sync: new Date().toISOString(),
    open_orders: working ? [openOrder()] : [],
    executed_orders: [],
    open_count: working ? 1 : 0,
    executed_count: 0,
  });

  const record = async (route: Route) => {
    captured.push({
      method: route.request().method(),
      url: route.request().url(),
      body: await parseBody(route),
    });
  };

  await page.route("**/api/health**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "ok",
        trading_mode: "paper",
        futu: { trading },
      }),
    });
  });

  await page.route("**/api/futu/portfolio**", async (route) => {
    const env = new URL(route.request().url()).searchParams.get("account_env");
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
        account_id: "FUTU-PAPER-5678",
        source: "futu",
        is_stale: false,
        warnings: [],
        positions: [
          {
            futu_code: "US.AAPL",
            normalized: {
              kind: "STK",
              symbol: "AAPL",
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
        account_summary: portfolioFixture("AAPL").account_summary,
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

  await page.route("**/api/orders/place**", async (route) => {
    await record(route);
    assertFutuPaperScope(route.request().url());
    working = true;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "ok",
        broker: "FUTU",
        accountEnv: "paper",
        brokerOrderId: BROKER_ORDER_ID,
        orderId: 0,
        permId: 0,
        initialStatus: "Submitted",
        state: "working",
        submission_id: "sub-1",
        message: "Order accepted by Futu",
      }),
    });
  });

  await page.route("**/api/orders/modify**", async (route) => {
    await record(route);
    assertFutuPaperScope(route.request().url());
    const body = (await parseBody(route)) ?? {};
    assertFutuMutationBody(body);
    if (typeof body.newPrice === "number") limitPrice = body.newPrice;
    if (typeof body.newQuantity === "number") quantity = body.newQuantity;
    modifySequence =
      typeof body.modifySequence === "number"
        ? body.modifySequence
        : modifySequence + 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "ok",
        brokerOrderId: BROKER_ORDER_ID,
        confirmed: true,
        confirmedQuantity: quantity,
        confirmedPrice: limitPrice,
        applied_sequence: modifySequence,
        message: "Modify accepted by Futu",
        orders: ordersPayload(),
      }),
    });
  });

  await page.route("**/api/orders/cancel**", async (route) => {
    await record(route);
    assertFutuPaperScope(route.request().url());
    const body = (await parseBody(route)) ?? {};
    assertFutuMutationBody(body);
    working = false;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "ok",
        brokerOrderId: BROKER_ORDER_ID,
        state: "cancelled",
        confirmed: true,
        message: "Cancel accepted by Futu",
        orders: ordersPayload(),
      }),
    });
  });

  await page.route("**/api/orders**", async (route) => {
    const url = route.request().url();
    if (
      url.includes("/place") ||
      url.includes("/modify") ||
      url.includes("/cancel")
    ) {
      await route.fallback();
      return;
    }
    if (
      route.request().method() === "GET" ||
      route.request().method() === "POST"
    ) {
      const scopedFutu = url.includes("broker=FUTU");
      if (scopedFutu) {
        assertFutuBrokerScope(url);
        if (route.request().method() === "POST") {
          await record(route);
        }
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          scopedFutu
            ? ordersPayload()
            : {
                last_sync: new Date().toISOString(),
                open_orders: [],
                executed_orders: [],
                open_count: 0,
                executed_count: 0,
              },
        ),
      });
      return;
    }
    await route.continue();
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

  return { captured };
}

async function openFutuTab(page: Page) {
  await page.goto("/portfolio");
  await page.evaluate(() =>
    window.localStorage.setItem("xenon.futu.account_env", "paper"),
  );
  await page.reload();
  await page.getByRole("button", { name: "Switch to FUTU account" }).click();
  await expect(page.getByTestId("futu-env-switch")).toBeVisible();
}

test.describe("Futu order ticket", () => {
  test.beforeEach(async ({ context }) => {
    await context.clearCookies();
  });

  test("SIMULATE: place → working order → modify → cancel (scoped brokerOrderId)", async ({
    page,
  }) => {
    const { captured } = await installOrderFlowMocks(page, {
      paper: true,
      live: false,
    });
    await openFutuTab(page);

    await page.locator(".position-order-btn").first().click();
    await expect(page.getByRole("dialog")).toContainText("SIMULATE");
    await page.locator("#position-order-price").fill("178.50");
    await page.getByRole("button", { name: "Submit close" }).click();

    await page.goto("/orders");
    await expect(page.getByRole("button", { name: "MODIFY" })).toBeVisible();

    await page.screenshot({
      path: path.join(
        SCREEN_DIR,
        "futu-order-ticket-2026-10-25-sim-ticket.png",
      ),
      fullPage: true,
    });

    await page.getByRole("button", { name: "MODIFY" }).click();
    await page.locator("#modify-price-input").fill("179.25");
    await page.getByRole("button", { name: "Modify Order" }).click();

    await page.getByRole("button", { name: "CANCEL" }).click();
    await page.getByRole("button", { name: "Cancel Order" }).click();

    await expect(page.getByRole("button", { name: "MODIFY" })).toHaveCount(0);

    const place = captured.find((c) => c.url.includes("/place"));
    const modify = captured.find((c) => c.url.includes("/modify"));
    const cancel = captured.find((c) => c.url.includes("/cancel"));
    expect(place).toBeTruthy();
    expect(modify).toBeTruthy();
    expect(cancel).toBeTruthy();
    assertFutuPaperScope(place!.url);
    expect(place!.body?.type).not.toBe("combo");
    expect(place!.body?.brokerOrderId).toBeUndefined();
    for (const req of [modify!, cancel!]) {
      assertFutuPaperScope(req.url);
      assertFutuMutationBody(req.body!);
    }
  });

  test("REAL with trading.live=false: no order action buttons", async ({
    page,
  }) => {
    await installOrderFlowMocks(page, { paper: true, live: false });
    await openFutuTab(page);
    await page.getByTestId("futu-env-real").click();

    await expect(page.locator(".position-order-btn")).toHaveCount(0);

    await page.screenshot({
      path: path.join(
        SCREEN_DIR,
        "futu-order-ticket-2026-10-25-real-readonly.png",
      ),
      fullPage: true,
    });
  });
});
