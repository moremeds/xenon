import { expect, test } from "@playwright/test";

const PORTFOLIO_MOCK = {
  bankroll: 100_000,
  peak_value: 100_000,
  last_sync: "2026-03-12T10:00:00Z",
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
  account_summary: {},
};

const ORDERS_EMPTY = {
  last_sync: "2026-03-12T10:00:00Z",
  open_orders: [],
  executed_orders: [],
  open_count: 0,
  executed_count: 0,
};

const BLOTTER_EMPTY = {
  as_of: "2026-03-12T10:00:00Z",
  summary: { realized_pnl: 0 },
  closed_trades: [],
  open_trades: [],
};

type Page = import("@playwright/test").Page;

async function routeWorkspaceApis(page: Page) {
  await page.unrouteAll({ behavior: "ignoreErrors" });

  await page.route("**/api/portfolio", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(PORTFOLIO_MOCK) }),
  );
  await page.route("**/api/orders", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(ORDERS_EMPTY) }),
  );
  await page.route("**/api/ib-status", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ connected: false }) }),
  );
  await page.route("**/api/blotter", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(BLOTTER_EMPTY) }),
  );
  await page.route("**/api/flex-token", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ok: true, days_until_expiry: 14 }) }),
  );
  await page.route("**/api/previous-close", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ closes: {} }) }),
  );
  await page.route("**/api/prices**", (route) => route.abort());
}

// Installs a WebSocket stub that pushes the given realtime `status` messages on
// a timer. The WorkspaceShell ConnectionBanner + reconnect toast are driven
// entirely by ib_connected / ib_issue / ib_status_message in those frames.
async function installMockWebSocket(
  page: Page,
  frames: Array<{ delayMs: number; status: Record<string, unknown> }>,
) {
  await page.addInitScript((pendingFrames) => {
    class MockWebSocket {
      public static OPEN = 1;
      public url: string;
      public readyState = 0;
      public onopen: ((event: Event) => void) | null = null;
      public onmessage: ((event: MessageEvent<string>) => void) | null = null;
      public onclose: ((event: Event) => void) | null = null;
      public onerror: ((event: Event) => void) | null = null;

      constructor(url: string) {
        this.url = url;
        window.setTimeout(() => {
          this.readyState = MockWebSocket.OPEN;
          this.onopen?.(new Event("open"));
        }, 0);
        for (const frame of pendingFrames) {
          window.setTimeout(() => {
            this.onmessage?.({
              data: JSON.stringify({ type: "status", ...frame.status }),
            } as MessageEvent<string>);
          }, frame.delayMs);
        }
      }

      send(_message: string) {}

      close() {
        this.readyState = 3;
        this.onclose?.(new Event("close"));
      }
    }

    Object.defineProperty(window, "WebSocket", {
      configurable: true,
      writable: true,
      value: MockWebSocket,
    });
  }, frames);
}

async function setupMfaStatusMocks(page: Page) {
  await routeWorkspaceApis(page);
  await installMockWebSocket(page, [
    {
      delayMs: 10,
      status: {
        ib_connected: false,
        ib_issue: "ibc_mfa_required",
        ib_status_message:
          "Interactive Brokers Gateway is reconnecting. Check the push notification from Interactive Brokers on your phone to approve MFA.",
        subscriptions: [],
      },
    },
  ]);
}

async function setupGenericDisconnectMocks(page: Page) {
  await routeWorkspaceApis(page);
  await installMockWebSocket(page, [
    {
      delayMs: 10,
      status: {
        ib_connected: false,
        ib_issue: null,
        ib_status_message: null,
        subscriptions: [],
      },
    },
  ]);
}

async function setupGenericReconnectMocks(page: Page) {
  await routeWorkspaceApis(page);
  await installMockWebSocket(page, [
    {
      delayMs: 10,
      status: {
        ib_connected: false,
        ib_issue: null,
        ib_status_message: null,
        subscriptions: [],
      },
    },
    {
      delayMs: 2300,
      status: {
        ib_connected: true,
        ib_issue: null,
        ib_status_message: null,
        subscriptions: [],
      },
    },
  ]);
}

test.describe("IB MFA reconnect alert", () => {
  test("shows phone approval guidance when the realtime server reports the local gateway reconnect issue", async ({ page }) => {
    await setupMfaStatusMocks(page);
    await page.goto("/portfolio");

    const banner = page.locator('[data-testid="ib-connection-banner"]');
    await expect(banner).toBeVisible({ timeout: 10_000 });
    await expect(banner).toContainText("Interactive Brokers");
    await expect(banner).toContainText("push notification");
    await expect(banner).toContainText("phone");
    await expect(banner).toContainText("approve MFA");
  });

  test("does not render the upper shell banner for a generic disconnect without an MFA issue", async ({ page }) => {
    await setupGenericDisconnectMocks(page);
    await page.goto("/portfolio");

    await page.waitForTimeout(2300);
    await expect(page.locator('[data-testid="ib-connection-banner"]')).toHaveCount(0);
  });

  test("does not render the upper shell banner for a generic reconnect success", async ({ page }) => {
    await setupGenericReconnectMocks(page);
    await page.goto("/portfolio");

    await page.waitForTimeout(2800);
    await expect(page.locator('[data-testid="ib-connection-banner"]')).toHaveCount(0);
    await expect(page.locator(".toast-container")).toContainText("IB Gateway reconnected");
  });
});
