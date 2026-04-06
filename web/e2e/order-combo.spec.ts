/**
 * E2E: SPXU combo order entry — verifies that IB rejection states surface as
 * errors in the UI (red path) and that a valid placement shows the order (green path).
 *
 * All IB API calls are intercepted via page.route() — no live IB connection needed.
 */

import { test, expect } from "@playwright/test";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Open the SPXU ticker detail modal and navigate to the Order tab. */
async function openSpxuOrderTab(page: import("@playwright/test").Page) {
  // Navigate to portfolio page
  await page.goto("/portfolio");

  // Wait for the SPXU ticker link to appear
  const spxuLink = page.locator('[aria-label="View details for SPXU"]').first();
  await spxuLink.waitFor({ timeout: 10_000 });
  await spxuLink.click();

  // Click the Order tab in the modal
  const orderTab = page.locator(".ticker-tab", { hasText: /^Order/ }).first();
  await orderTab.waitFor({ timeout: 5_000 });
  await orderTab.click();
}

/** Fill in the combo order form and click Place Combo Order (first click). */
async function fillComboForm(page: import("@playwright/test").Page, price: string) {
  // Wait for the combo order form to be visible
  const limitPriceInput = page
    .locator(".modify-price-input")
    .first();
  await limitPriceInput.waitFor({ timeout: 5_000 });
  await limitPriceInput.fill(price);

  // Click Place Combo Order
  await page.locator("button", { hasText: "Place Combo Order" }).click();

  // Confirm step
  await page.locator("button", { hasText: /Confirm/ }).click();
}

// ---------------------------------------------------------------------------
// Mock data the portfolio endpoint returns (SPXU Bull Call Spread)
// ---------------------------------------------------------------------------

const PORTFOLIO_MOCK = {
  bankroll: 50_000,
  peak_value: 52_000,
  last_sync: new Date().toISOString(),
  total_deployed_pct: 5.78,
  total_deployed_dollars: 2891.57,
  remaining_capacity_pct: 94.22,
  position_count: 1,
  defined_risk_count: 1,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  positions: [
    {
      id: 23,
      ticker: "SPXU",
      structure: "Bull Call Spread $53.0/$60.0",
      structure_type: "Bull Call Spread",
      risk_profile: "defined",
      expiry: "2026-03-13",
      contracts: 20,
      direction: "DEBIT",
      entry_cost: 2891.57,
      max_risk: 2891.57,
      market_value: 3950.0,
      market_price_is_calculated: false,
      legs: [
        {
          direction: "LONG",
          contracts: 20,
          type: "Call",
          strike: 53.0,
          entry_cost: 4079.75,
          avg_cost: 203.99,
          market_price: 2.875,
          market_value: 5750.0,
          market_price_is_calculated: false,
        },
        {
          direction: "SHORT",
          contracts: 20,
          type: "Call",
          strike: 60.0,
          entry_cost: 1188.18,
          avg_cost: 59.41,
          market_price: 0.9,
          market_value: 1800.0,
          market_price_is_calculated: false,
        },
      ],
      kelly_optimal: null,
      target: null,
      stop: null,
      entry_date: "2026-03-09",
    },
  ],
  exposure: {},
  violations: [],
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test.describe("SPXU combo order — rejection surfaces as error (RED → GREEN)", () => {
  test.beforeEach(async ({ page }) => {
    // Clear all previous route handlers so tests don't bleed into each other
    await page.unrouteAll({ behavior: "ignoreErrors" });

    // Dismiss any Next.js error overlays before the test starts
    await page.addInitScript(() => {
      window.addEventListener("error", (e) => e.preventDefault());
    });

    // Mock portfolio API
    await page.route("**/api/portfolio", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(PORTFOLIO_MOCK),
      }),
    );

    // Mock orders API (no open orders)
    await page.route("**/api/orders", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          last_sync: new Date().toISOString(),
          open_orders: [],
          executed_orders: [],
          open_count: 0,
          executed_count: 0,
        }),
      }),
    );

    // Mock prices WebSocket handshake (avoid connection error noise)
    await page.route("**/api/prices", (route) => route.abort());

    // Mock ancillary routes that the page may call
    await page.route("**/api/regime", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ score: 0.2 }) }),
    );
    await page.route("**/api/ib-status", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ connected: false }) }),
    );
  });

  test("RED: IB silent cancellation shows error instead of success", async ({ page }) => {
    // Mock placement — IB returns ok=true but initialStatus=Cancelled
    await page.route("**/api/orders/place", (route) =>
      route.fulfill({
        status: 502,
        contentType: "application/json",
        body: JSON.stringify({
          error: "Order rejected by IB: Cancelled",
          detail: { status: "ok", orderId: 12345, initialStatus: "Cancelled" },
        }),
      }),
    );

    await openSpxuOrderTab(page);
    await fillComboForm(page, "2.25");

    // Should show an error, not a success
    const errorMsg = page.locator(".order-error");
    await errorMsg.waitFor({ timeout: 5_000 });
    await expect(errorMsg).toBeVisible();
    await expect(errorMsg).toContainText(/rejected|Cancelled/i);

    // Success message should NOT appear
    await expect(page.locator(".order-success")).not.toBeVisible();
  });

  test("GREEN: IB acceptance shows success message", async ({ page }) => {
    // Mock placement — IB accepts the order
    await page.route("**/api/orders/place", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          status: "ok",
          orderId: 12345,
          permId: 67890,
          initialStatus: "Submitted",
          message: "SELL 20 SPXU @ $2.25 — Submitted",
          orders: {
            open_orders: [
              {
                orderId: 12345,
                permId: 67890,
                symbol: "SPXU Spread",
                contract: { symbol: "SPXU", secType: "BAG" },
                action: "SELL",
                orderType: "LMT",
                totalQuantity: 20,
                limitPrice: 2.25,
                status: "Submitted",
                filled: 0,
                remaining: 20,
                tif: "GTC",
              },
            ],
            executed_orders: [],
            open_count: 1,
            executed_count: 0,
          },
        }),
      }),
    );

    await openSpxuOrderTab(page);
    await fillComboForm(page, "2.25");

    // Should show success
    const successMsg = page.locator(".order-success");
    await successMsg.waitFor({ timeout: 5_000 });
    await expect(successMsg).toBeVisible();
    await expect(successMsg).toContainText(/Combo order placed.*\$2\.25/i);

    // Error should NOT appear
    await expect(page.locator(".order-error")).not.toBeVisible();
  });

  test("GREEN: Unknown status (no IB ack) shows error not success", async ({ page }) => {
    await page.route("**/api/orders/place", (route) =>
      route.fulfill({
        status: 502,
        contentType: "application/json",
        body: JSON.stringify({
          error: "Order rejected by IB: no acknowledgement (Unknown)",
          detail: { status: "ok", orderId: 0, initialStatus: "Unknown" },
        }),
      }),
    );

    await openSpxuOrderTab(page);
    await fillComboForm(page, "2.25");

    const errorMsg = page.locator(".order-error");
    await errorMsg.waitFor({ timeout: 5_000 });
    await expect(errorMsg).toBeVisible();
    await expect(page.locator(".order-success")).not.toBeVisible();
  });

  test("GREEN: noisy upstream margin rejection is rendered as concise operator copy", async ({ page }) => {
    await page.route("**/api/orders/place", (route) =>
      route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({
          error:
            "Radon API 502: IB error 201: Order rejected - reason:YOUR ORDER IS NOT ACCEPTED. IN ORDER TO OBTAIN THE DESIRED POSITION YOUR PREVIOUS DAY EQUITY WITH LOAN VALUE <E> [644770.54 USD] MUST EXCEED THE INITIAL MARGIN [677243.00 USD].",
        }),
      }),
    );

    await openSpxuOrderTab(page);
    await fillComboForm(page, "2.25");

    const errorMsg = page.locator(".order-error");
    await errorMsg.waitFor({ timeout: 5_000 });
    await expect(errorMsg).toBeVisible();
    await expect(errorMsg).toContainText("Order rejected by IB: insufficient margin.");
    await expect(errorMsg).toContainText("$644,770.54");
    await expect(errorMsg).toContainText("$677,243.00");
    await expect(errorMsg).not.toContainText("Radon API 502");
    await expect(errorMsg).not.toContainText("YOUR ORDER IS NOT ACCEPTED");
  });
});
