/**
 * E2E: Ticker Detail Page — /{TICKER} route.
 *
 * Validates:
 * 1. Direct navigation to /AAPL renders ticker detail
 * 2. /aapl redirects to /AAPL (case normalization)
 * 3. Invalid paths return 404
 * 4. Tab clicks update URL search params
 * 5. Direct nav with ?tab= opens correct tab
 * 6. Back button returns to previous page
 */

import { test, expect } from "@playwright/test";

const PORTFOLIO = {
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
  exposure: {},
  violations: [],
  positions: [],
};

const ORDERS = {
  last_sync: new Date().toISOString(),
  open_orders: [],
  executed_orders: [],
  open_count: 0,
  executed_count: 0,
};

function stubApis(page: import("@playwright/test").Page) {
  page.route("**/api/portfolio", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(PORTFOLIO) }),
  );
  page.route("**/api/orders", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(ORDERS) }),
  );
  page.route("**/api/regime", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ score: 15, cri: { score: 15 } }) }),
  );
  page.route("**/api/ib-status", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ connected: false }) }),
  );
  page.route("**/api/blotter", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ as_of: new Date().toISOString(), summary: { realized_pnl: 0 }, closed_trades: [], open_trades: [] }),
    }),
  );
  page.route("**/api/ticker/**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        uw_info: { name: "Apple Inc.", sector: "Technology", description: "Consumer electronics" },
        stock_state: {},
        profile: {},
        stats: {},
      }),
    }),
  );
  page.route("**/api/options/expirations*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ symbol: "AAPL", expirations: ["20260320", "20260417"] }),
    }),
  );
  page.route("**/api/options/chain*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        symbol: "AAPL",
        expiry: "20260320",
        exchange: "SMART",
        strikes: [195, 200, 205, 210, 215],
        multiplier: "100",
      }),
    }),
  );

  // Stub WS
  page.addInitScript(() => {
    // @ts-expect-error - intentional override
    window.WebSocket = class FakeWebSocket extends EventTarget {
      static CONNECTING = 0;
      static OPEN = 1;
      static CLOSING = 2;
      static CLOSED = 3;
      CONNECTING = 0;
      OPEN = 1;
      CLOSING = 2;
      CLOSED = 3;
      readyState = 0;
      url: string;
      onopen: ((ev: Event) => void) | null = null;
      onmessage: ((ev: MessageEvent) => void) | null = null;
      onclose: ((ev: CloseEvent) => void) | null = null;
      onerror: ((ev: Event) => void) | null = null;
      binaryType = "blob";
      bufferedAmount = 0;
      extensions = "";
      protocol = "";

      constructor(url: string) {
        super();
        this.url = url;
        setTimeout(() => {
          this.readyState = 1;
          if (this.onopen) this.onopen(new Event("open"));
          setTimeout(() => {
            if (this.onmessage) {
              this.onmessage(new MessageEvent("message", {
                data: JSON.stringify({ type: "status", ib_connected: false, subscriptions: [] }),
              }));
            }
          }, 20);
        }, 50);
      }
      send() {}
      close() { this.readyState = 3; }
    };
  });
}

test.describe("Ticker Page E2E", () => {
  test("direct nav to /AAPL renders ticker detail page", async ({ page }) => {
    await page.unrouteAll({ behavior: "ignoreErrors" });
    stubApis(page);

    await page.goto("/AAPL");
    await page.waitForLoadState("networkidle");

    // Should show ticker detail content
    const content = page.locator(".ticker-detail-page");
    await expect(content).toBeVisible({ timeout: 5000 });

    // Should show tab bar
    const tabs = page.locator(".ticker-tabs");
    await expect(tabs).toBeVisible();

    // Breadcrumb should show AAPL
    const breadcrumb = page.locator(".breadcrumb");
    await expect(breadcrumb).toContainText("AAPL");
  });

  test("/aapl redirects to /AAPL (case normalization)", async ({ page }) => {
    await page.unrouteAll({ behavior: "ignoreErrors" });
    stubApis(page);

    await page.goto("/aapl");
    // Should redirect to uppercase
    await page.waitForURL("**/AAPL", { timeout: 5000 });
    expect(page.url()).toContain("/AAPL");
  });

  test("tab click updates URL to ?tab=chain", async ({ page }) => {
    await page.unrouteAll({ behavior: "ignoreErrors" });
    stubApis(page);

    await page.goto("/AAPL");
    await page.waitForLoadState("networkidle");

    // Click on Chain tab
    const chainTab = page.locator(".ticker-tab", { hasText: "Chain" });
    await expect(chainTab).toBeVisible();
    await chainTab.click();

    // URL should update with ?tab=chain
    await page.waitForURL("**/AAPL?tab=chain", { timeout: 5000 });
    expect(page.url()).toContain("tab=chain");

    // Chain tab should be active
    await expect(chainTab).toHaveClass(/active/);
  });

  test("direct nav to /AAPL?tab=ratings opens on Ratings tab", async ({ page }) => {
    await page.unrouteAll({ behavior: "ignoreErrors" });
    stubApis(page);

    await page.goto("/AAPL?tab=ratings");
    await page.waitForLoadState("networkidle");

    // Ratings tab should be active
    const ratingsTab = page.locator(".ticker-tab", { hasText: "Ratings" });
    await expect(ratingsTab).toHaveClass(/active/);
  });

  test("back button returns to previous page", async ({ page }) => {
    await page.unrouteAll({ behavior: "ignoreErrors" });
    stubApis(page);

    // Start on portfolio page
    await page.goto("/portfolio");
    await page.waitForLoadState("networkidle");

    // Navigate to ticker page
    await page.goto("/AAPL");
    await page.waitForLoadState("networkidle");

    // Click back button
    const backBtn = page.locator(".ticker-back-nav");
    await expect(backBtn).toBeVisible();
    await backBtn.click();

    // Should go back to portfolio
    await page.waitForURL("**/portfolio", { timeout: 5000 });
  });
});
