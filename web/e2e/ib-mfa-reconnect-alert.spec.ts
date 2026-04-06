import { expect, test } from "@playwright/test";

const CRI_MOCK_OPEN = {
  scan_time: "2026-03-12T10:00:00",
  market_open: true,
  date: "2026-03-12",
  vix: 24.0,
  vvix: 115.0,
  spy: 555.0,
  vix_5d_roc: 5.2,
  vvix_vix_ratio: 4.79,
  realized_vol: 12.5,
  cor1m: 29.31,
  cor1m_previous_close: 28.97,
  cor1m_5d_change: 1.48,
  spx_100d_ma: 560.0,
  spx_distance_pct: -0.89,
  spy_closes: Array.from({ length: 22 }, (_, index) => 550 + index * 0.5),
  cri: { score: 20, level: "LOW", components: { vix: 5, vvix: 4, correlation: 6, momentum: 5 } },
  crash_trigger: {
    triggered: false,
    conditions: { spx_below_100d_ma: false, realized_vol_gt_25: false, cor1m_gt_60: false },
  },
  cta: { exposure_pct: 95, forced_reduction_pct: 0, est_selling_bn: 0 },
  menthorq_cta: null,
  history: [],
};

async function setupMfaStatusMocks(page: import("@playwright/test").Page) {
  await page.unrouteAll({ behavior: "ignoreErrors" });

  await page.route("**/api/regime", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(CRI_MOCK_OPEN) }),
  );
  await page.route("**/api/portfolio", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ bankroll: 100_000, positions: [], account_summary: {}, exposure: {}, violations: [] }) }),
  );
  await page.route("**/api/orders", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ last_sync: new Date().toISOString(), open_orders: [], executed_orders: [], open_count: 0, executed_count: 0 }) }),
  );
  await page.route("**/api/ib-status", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ connected: false }) }),
  );
  await page.route("**/api/blotter", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ as_of: new Date().toISOString(), summary: { realized_pnl: 0 }, closed_trades: [], open_trades: [] }) }),
  );
  await page.route("**/api/menthorq/cta", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ tables: [] }) }),
  );
  await page.route("**/api/previous-close", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ closes: {} }) }),
  );

  await page.addInitScript(() => {
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
        window.setTimeout(() => {
          this.onmessage?.({
            data: JSON.stringify({
              type: "status",
              ib_connected: false,
              ib_issue: "ibc_mfa_required",
              ib_status_message:
                "Interactive Brokers Gateway is reconnecting. Check the push notification from Interactive Brokers on your phone to approve MFA.",
              subscriptions: ["SPY", "VIX", "VVIX", "COR1M"],
            }),
          } as MessageEvent<string>);
        }, 10);
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
  });
}

async function setupGenericReconnectMocks(page: import("@playwright/test").Page) {
  await page.unrouteAll({ behavior: "ignoreErrors" });

  await page.route("**/api/regime", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(CRI_MOCK_OPEN) }),
  );
  await page.route("**/api/portfolio", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ bankroll: 100_000, positions: [], account_summary: {}, exposure: {}, violations: [] }) }),
  );
  await page.route("**/api/orders", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ last_sync: new Date().toISOString(), open_orders: [], executed_orders: [], open_count: 0, executed_count: 0 }) }),
  );
  await page.route("**/api/ib-status", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ connected: false }) }),
  );
  await page.route("**/api/blotter", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ as_of: new Date().toISOString(), summary: { realized_pnl: 0 }, closed_trades: [], open_trades: [] }) }),
  );
  await page.route("**/api/menthorq/cta", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ tables: [] }) }),
  );
  await page.route("**/api/previous-close", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ closes: {} }) }),
  );

  await page.addInitScript(() => {
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
        window.setTimeout(() => {
          this.onmessage?.({
            data: JSON.stringify({
              type: "status",
              ib_connected: false,
              ib_issue: null,
              ib_status_message: null,
              subscriptions: ["SPY", "VIX", "VVIX", "COR1M"],
            }),
          } as MessageEvent<string>);
        }, 10);
        window.setTimeout(() => {
          this.onmessage?.({
            data: JSON.stringify({
              type: "status",
              ib_connected: true,
              ib_issue: null,
              ib_status_message: null,
              subscriptions: ["SPY", "VIX", "VVIX", "COR1M"],
            }),
          } as MessageEvent<string>);
        }, 2300);
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
  });
}

async function setupGenericDisconnectMocks(page: import("@playwright/test").Page) {
  await page.unrouteAll({ behavior: "ignoreErrors" });

  await page.route("**/api/regime", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(CRI_MOCK_OPEN) }),
  );
  await page.route("**/api/portfolio", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ bankroll: 100_000, positions: [], account_summary: {}, exposure: {}, violations: [] }) }),
  );
  await page.route("**/api/orders", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ last_sync: new Date().toISOString(), open_orders: [], executed_orders: [], open_count: 0, executed_count: 0 }) }),
  );
  await page.route("**/api/ib-status", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ connected: false }) }),
  );
  await page.route("**/api/blotter", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ as_of: new Date().toISOString(), summary: { realized_pnl: 0 }, closed_trades: [], open_trades: [] }) }),
  );
  await page.route("**/api/menthorq/cta", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ tables: [] }) }),
  );
  await page.route("**/api/previous-close", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ closes: {} }) }),
  );

  await page.addInitScript(() => {
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
        window.setTimeout(() => {
          this.onmessage?.({
            data: JSON.stringify({
              type: "status",
              ib_connected: false,
              ib_issue: null,
              ib_status_message: null,
              subscriptions: ["SPY", "VIX", "VVIX", "COR1M"],
            }),
          } as MessageEvent<string>);
        }, 10);
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
  });
}

test.describe("IB MFA reconnect alert", () => {
  test("shows phone approval guidance when the realtime server reports the local gateway reconnect issue", async ({ page }) => {
    await setupMfaStatusMocks(page);
    await page.goto("/regime");

    const banner = page.locator('[data-testid="ib-connection-banner"]');
    await expect(banner).toBeVisible({ timeout: 10_000 });
    await expect(banner).toContainText("Interactive Brokers");
    await expect(banner).toContainText("push notification");
    await expect(banner).toContainText("phone");
    await expect(banner).toContainText("approve MFA");
  });

  test("does not render the upper shell banner for a generic disconnect without an MFA issue", async ({ page }) => {
    await setupGenericDisconnectMocks(page);
    await page.goto("/regime");

    await page.waitForTimeout(2300);
    await expect(page.locator('[data-testid="ib-connection-banner"]')).toHaveCount(0);
  });

  test("does not render the upper shell banner for a generic reconnect success", async ({ page }) => {
    await setupGenericReconnectMocks(page);
    await page.goto("/regime");

    await page.waitForTimeout(2800);
    await expect(page.locator('[data-testid="ib-connection-banner"]')).toHaveCount(0);
    await expect(page.locator(".toast-container")).toContainText("IB Gateway reconnected");
  });
});
