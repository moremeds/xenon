/**
 * E2E: Gate-4 (server-side coverage) order-entry specs — PR-03b.
 *
 * 1. A combo whose short call the server cannot cover is blocked: the 400
 *    {reason_code} body surfaces as the mapped toast copy in the ticket's
 *    error region (role=alert), never as success.
 * 2. A valid held vertical submits the correct combo payload: SELL envelope
 *    (closing), per-leg actions encoding the spread STRUCTURE
 *    (LONG leg → BUY, SHORT leg → SELL), gcd-reduced ratios, YYYYMMDD expiry.
 *
 * All API calls are intercepted via page.route() — no live backend needed.
 * The client-side naked-short guard allows the SELL-close in both cases;
 * coverage is decided server-side by Gate 4.
 */

import { test, expect } from "@playwright/test";

// ---------------------------------------------------------------------------
// Helpers (mirrors order-combo.spec.ts)
// ---------------------------------------------------------------------------

async function openSpxuOrderTab(page: import("@playwright/test").Page) {
  await page.goto("/portfolio");
  const spxuLink = page.locator('[aria-label="View details for SPXU"]').first();
  await spxuLink.waitFor({ timeout: 10_000 });
  await spxuLink.click();
  await page.waitForURL("**/SPXU**", { timeout: 5_000 });
  await page.locator(".act-ticket .order-form").waitFor({ timeout: 10_000 });
}

async function fillComboForm(page: import("@playwright/test").Page, price: string) {
  await page.locator(".combo-legs-pills").waitFor({ timeout: 10_000 });
  const limitPriceInput = page
    .locator(".act-ticket .order-form .modify-price-input")
    .first();
  await limitPriceInput.waitFor({ timeout: 5_000 });
  await limitPriceInput.fill(price);
  await page.locator("button", { hasText: "Place Combo Order" }).click();
  await page.locator("button", { hasText: /Confirm/ }).click();
}

// ---------------------------------------------------------------------------
// Mock data — held SPXU Bull Call Spread 20x LONG $53C / 20x SHORT $60C
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

test.describe("Gate-4 order entry — blocked combo toast + valid vertical payload", () => {
  test.beforeEach(async ({ page }) => {
    await page.unrouteAll({ behavior: "ignoreErrors" });
    await page.addInitScript(() => {
      window.addEventListener("error", (e) => e.preventDefault());
    });

    await page.route("**/api/portfolio", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(PORTFOLIO_MOCK),
      }),
    );
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
    await page.route("**/api/prices", (route) => route.abort());
    await page.route("**/api/regime", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ score: 0.2 }) }),
    );
    await page.route("**/api/ib-status", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ connected: false }) }),
    );
  });

  test("Gate-4 block on a combo surfaces the coverage toast, never success", async ({
    page,
  }) => {
    let placeCalls = 0;
    // Server-side Gate 4 cannot prove cover for the short leg (fills/working
    // orders state the client guard cannot see). 400 + reason_code is the
    // wire contract from _preflight_block_response.
    await page.route("**/api/orders/place", (route) => {
      placeCalls += 1;
      return route.fulfill({
        status: 400,
        contentType: "application/json",
        body: JSON.stringify({
          detail: "Uncovered short call on SPXU",
          reason_code: "ETF_CALL_UNCOVERED",
          reason_detail: "Uncovered short call on SPXU",
        }),
      });
    });

    await openSpxuOrderTab(page);
    await fillComboForm(page, "2.25");

    const errorMsg = page.locator(".order-error");
    await errorMsg.waitFor({ timeout: 5_000 });
    await expect(errorMsg).toBeVisible();
    // ORDER_REASON_CODES[ETF_CALL_UNCOVERED].copy — the mapped toast text,
    // not the raw reason_detail.
    await expect(errorMsg).toContainText(
      /Short call uncovered after accounting for working orders/i,
    );
    await expect(page.locator(".order-success")).not.toBeVisible();
    // Client guard let it through; exactly one server attempt was made.
    expect(placeCalls).toBe(1);
  });

  test("Guaranteed-BAG vertical close shows the split-required toast", async ({
    page,
  }) => {
    // Spec §2.3: with netting off, a held vertical closes as two orders —
    // Gate 4 rejects the one-BAG submission with a dedicated reason whose
    // copy says exactly how to leg out. Never auto-split, never auto-submit.
    await page.route("**/api/orders/place", (route) =>
      route.fulfill({
        status: 400,
        contentType: "application/json",
        body: JSON.stringify({
          detail:
            "Close the vertical as two orders: buy back the short leg first, then sell the long leg",
          reason_code: "VERTICAL_CLOSE_SPLIT_REQUIRED",
          reason_detail:
            "Close the vertical as two orders: buy back the short leg first, then sell the long leg",
        }),
      }),
    );

    await openSpxuOrderTab(page);
    await fillComboForm(page, "2.25");

    const errorMsg = page.locator(".order-error");
    await errorMsg.waitFor({ timeout: 5_000 });
    await expect(errorMsg).toBeVisible();
    await expect(errorMsg).toContainText(
      /Close the vertical as two orders: buy back the short leg first, then sell the long leg/i,
    );
    await expect(page.locator(".order-success")).not.toBeVisible();
  });

  test("Valid vertical submits the correct SELL-envelope BAG payload", async ({
    page,
  }) => {
    let placeBody: Record<string, unknown> | null = null;
    await page.route("**/api/orders/place", async (route) => {
      placeBody = route.request().postDataJSON() as Record<string, unknown>;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          status: "ok",
          orderId: 12345,
          permId: 67890,
          initialStatus: "Submitted",
          message: "SELL 20 SPXU @ $2.25 — Submitted",
          orders: { open_orders: [], executed_orders: [], open_count: 0, executed_count: 0 },
        }),
      });
    });

    await openSpxuOrderTab(page);
    await fillComboForm(page, "2.25");

    const successMsg = page.locator(".order-success");
    await successMsg.waitFor({ timeout: 5_000 });
    await expect(successMsg).toBeVisible();

    expect(placeBody).not.toBeNull();
    const body = placeBody as unknown as {
      type: string;
      symbol: string;
      action: string;
      quantity: number;
      limitPrice: number;
      tif: string;
      client_attempt_id: string;
      legs: Array<{
        expiry: string;
        strike: number;
        right: string;
        action: string;
        ratio: number;
      }>;
    };

    expect(body.type).toBe("combo");
    expect(body.symbol).toBe("SPXU");
    // Closing a held vertical → SELL envelope; IB reverses leg actions.
    expect(body.action).toBe("SELL");
    expect(body.quantity).toBe(20);
    expect(body.limitPrice).toBe(2.25);
    expect(body.tif).toBe("GTC");
    expect(typeof body.client_attempt_id).toBe("string");
    expect(body.client_attempt_id.length).toBeGreaterThan(0);

    // Per-leg actions encode the spread STRUCTURE — LONG leg stays BUY,
    // SHORT leg stays SELL — never flipped to match the envelope.
    // Ratios are gcd-reduced (20/20 → 1/1); expiry is YYYYMMDD.
    expect(body.legs).toEqual([
      { expiry: "20260313", strike: 53, right: "C", action: "BUY", ratio: 1 },
      { expiry: "20260313", strike: 60, right: "C", action: "SELL", ratio: 1 },
    ]);
  });
});
