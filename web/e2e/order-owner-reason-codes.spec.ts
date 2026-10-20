import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { expect, test } from "@playwright/test";

// PR-10 single order owner: the three owner refusals reach the toast with
// their mapped copy (web/lib/orderReasonCodes.ts) and the order stays listed.
// The vitest suite (web/tests/order-owner-reason-codes.test.tsx) pins these
// strings to ORDER_REASON_CODES; this spec proves the rendered toast.

// Must match MOCK_BACKEND_PORT in playwright.config.ts — the webServer hands
// it to Next as XENON_API_URL so /api/orders/cancel proxies here.
const MOCK_BACKEND_PORT = 8325;

const PORTFOLIO_MOCK = {
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
  positions: [],
  exposure: {},
  violations: [],
  account_summary: {
    net_liquidation: 100_000,
    daily_pnl: null,
    unrealized_pnl: 0,
    realized_pnl: 0,
    settled_cash: 100_000,
    maintenance_margin: 0,
    excess_liquidity: 100_000,
    buying_power: 200_000,
    dividends: 0,
  },
};

const ORDERS_MOCK = {
  last_sync: new Date().toISOString(),
  open_orders: [
    {
      orderId: 101,
      permId: 9001,
      symbol: "TSLL",
      contract: {
        conId: 5001,
        symbol: "TSLL",
        secType: "STK",
        strike: null,
        right: null,
        expiry: null,
      },
      action: "SELL",
      orderType: "LMT",
      totalQuantity: 500,
      limitPrice: 12.34,
      auxPrice: null,
      status: "Submitted",
      filled: 0,
      remaining: 500,
      avgFillPrice: null,
      tif: "GTC",
    },
  ],
  executed_orders: [],
  open_count: 1,
  executed_count: 0,
};

function respondJson(res: ServerResponse, status: number, body: unknown) {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json");
  res.end(JSON.stringify(body));
}

let backendServer: Server | null = null;
let ownerReply = { status: 409, code: "ORDER_NOT_OWNED" };

test.beforeAll(async () => {
  backendServer = createServer((req: IncomingMessage, res: ServerResponse) => {
    if (req.method === "POST" && req.url === "/orders/cancel") {
      // FastAPI order_error() shape: top-level reason_code + detail dict.
      respondJson(res, ownerReply.status, {
        reason_code: ownerReply.code,
        detail: { reason_code: ownerReply.code, message: `${ownerReply.code} from the order owner` },
      });
      return;
    }

    if (req.method === "POST" && req.url === "/orders/refresh") {
      respondJson(res, 200, { status: "ok" });
      return;
    }

    respondJson(res, 404, { detail: "Not found" });
  });

  await new Promise<void>((resolve, reject) => {
    backendServer?.once("error", reject);
    backendServer?.listen(MOCK_BACKEND_PORT, "127.0.0.1", () => resolve());
  });
});

test.afterAll(async () => {
  if (!backendServer) return;
  await new Promise<void>((resolve, reject) => {
    backendServer?.close((error) => {
      if (error) reject(error);
      else resolve();
    });
  });
  backendServer = null;
});

async function stubOrdersPageApis(page: import("@playwright/test").Page) {
  await page.unrouteAll({ behavior: "ignoreErrors" });

  await page.route("**/api/portfolio", (route) => {
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(PORTFOLIO_MOCK),
    });
  });

  await page.route("**/api/orders", (route) => {
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(ORDERS_MOCK),
    });
  });

  await page.route("**/api/blotter", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        as_of: new Date().toISOString(),
        summary: {
          closed_trades: 0,
          open_trades: 0,
          total_commissions: 0,
          realized_pnl: 0,
        },
        closed_trades: [],
        open_trades: [],
      }),
    }),
  );

  await page.route("**/api/ib-status", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ connected: true }),
    }),
  );

  await page.route("**/api/prices", (route) => route.abort());
}

const CASES = [
  {
    status: 409,
    code: "ORDER_NOT_OWNED",
    copy: "This order was not placed by Xenon's order connection. Cancel or modify it in TWS / IBKR Mobile.",
  },
  {
    status: 503,
    code: "IB_BROKER_DISCONNECTED",
    copy: "IB is not connected to its servers. Nothing was sent — retry when IB reconnects.",
  },
  {
    status: 503,
    code: "ORDERS_HALTED",
    copy: "Order actions are halted after an unexplained IB disconnect. Nothing was sent — the operator must restart the API.",
  },
];

test.describe("Order owner reason codes", () => {
  for (const c of CASES) {
    test(`cancel ${c.status} ${c.code} shows the mapped toast`, async ({ page }) => {
      ownerReply = { status: c.status, code: c.code };
      await stubOrdersPageApis(page);
      await page.goto("/orders");

      const openOrderRow = page.locator("tbody tr").filter({ hasText: "TSLL" });
      await expect(openOrderRow).toBeVisible({ timeout: 10_000 });

      await openOrderRow.getByRole("button", { name: "CANCEL" }).click();
      await expect(page.getByRole("dialog", { name: "Cancel Order" })).toBeVisible();

      const cancelResponse = page.waitForResponse(
        (response) => response.url().includes("/api/orders/cancel") && response.request().method() === "POST",
      );
      await page.getByRole("button", { name: "Cancel Order" }).click();

      const response = await cancelResponse;
      expect(response.status()).toBe(c.status);
      expect((await response.json()).reason_code).toBe(c.code);
      await expect(page.locator(".toast-message").filter({ hasText: c.copy })).toBeVisible();
      await expect(openOrderRow).toBeVisible();
      if (c.code === "ORDER_NOT_OWNED") {
        await page.screenshot({ path: "../output/playwright/order-owner-not-owned-2026-10-20.png" });
      }
    });
  }
});
