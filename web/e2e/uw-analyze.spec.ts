/**
 * E2E: /uw-analyze page — per-ticker UW signal analyse
 *
 * Verifies (modular-mapping-simon plan, Phase 3):
 *  1. Empty state renders before any analyse run
 *  2. Typing AAPL + Analyse renders identity / thesis / buckets / GEX / notes
 *  3. Positioning bucket renders "n/a" tile (skipped_buckets contains "positioning")
 *  4. Unknown ticker (404) shows error alert
 */

import { test, expect } from "@playwright/test";

const FIXTURE = {
  report: {
    ticker: "AAPL",
    price: 184.22,
    fetched_at: "2026-04-08T14:02:11",
    data_freshness: { gex: "live", volatility: "live", earnings: "live", benchmark_spy: "live" },
    scores: {
      market_structure: 24,
      volatility: 19,
      flow: 17,
      positioning: 0,
      composite: 15,
      grade: "B",
      bias: "MIXED",
      mode: "full",
      reweighted: true,
      skipped_buckets: ["positioning"],
    },
    notes: ["positioning bucket unavailable — composite reweighted"],
    setup_thesis: {
      bias: "MIXED",
      regime: "R1",
      structure_family: "neutral",
      rationale: "demo rationale",
    },
    regime: { gex_sign: "positive", flip_distance_pct: -0.011 },
  },
  display: {
    sector: "XLK",
    iv_rank: 38,
    iv: 22,
    rv: 18.6,
    call_wall_strike: 190,
    put_wall_strike: 175,
    gamma_per_1pct: 42_000_000,
    net_call_premium: 12_400_000,
    net_put_premium: -3_100_000,
    short_volume_ratio: 0.41,
    short_volume_trend: [0.4, 0.41, 0.42],
    term_structure_label: "normal",
    gex_flip: null,
    gex_by_strike: [
      { strike: 195, call_gamma: 21.0, put_gamma: -2.6, net_gamma: 18.4, distance_pct: 0.059, is_call_wall: false, is_put_wall: false },
      { strike: 190, call_gamma: 44.8, put_gamma: -2.7, net_gamma: 42.1, distance_pct: 0.031, is_call_wall: true, is_put_wall: false },
      { strike: 185, call_gamma: 14.2, put_gamma: -4.5, net_gamma: 9.7, distance_pct: 0.004, is_call_wall: false, is_put_wall: false },
      { strike: 180, call_gamma: 3.1, put_gamma: -9.4, net_gamma: -6.3, distance_pct: -0.023, is_call_wall: false, is_put_wall: false },
      { strike: 175, call_gamma: 1.9, put_gamma: -26.7, net_gamma: -24.8, distance_pct: -0.05, is_call_wall: false, is_put_wall: true },
    ],
  },
  generated_at: "2026-04-08T18:00:00Z",
};

async function setupMocks(page: import("@playwright/test").Page) {
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await page.route("**/api/uw-analyze", async (route) => {
    const req = route.request();
    let body: { ticker?: string } = {};
    try {
      body = JSON.parse(req.postData() ?? "{}");
    } catch {
      body = {};
    }
    if (body.ticker?.toUpperCase() === "ZZZZ") {
      await route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({ error: "ticker not found: ZZZZ" }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(FIXTURE),
    });
  });
  // Stub the other panels so the page boots cleanly.
  await page.route("**/api/portfolio", (r) =>
    r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ positions: [] }) }),
  );
  await page.route("**/api/orders", (r) =>
    r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ open_orders: [], executed_orders: [], open_count: 0, executed_count: 0, last_sync: new Date().toISOString() }) }),
  );
  await page.route("**/api/prices", (r) => r.abort());
  await page.route("**/api/ib-status", (r) =>
    r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ connected: false }) }),
  );
}

test.describe("/uw-analyze", () => {
  test("nav item exists in sidebar", async ({ page }) => {
    await setupMocks(page);
    await page.goto("/uw-analyze");
    const nav = page.locator("nav, aside").first();
    await expect(nav.locator("a[href='/uw-analyze']")).toBeVisible({ timeout: 10_000 });
  });

  test("renders empty state then full report after Analyse", async ({ page }) => {
    await setupMocks(page);
    await page.goto("/uw-analyze");
    await expect(page.getByTestId("uw-analyze-empty")).toBeVisible({ timeout: 10_000 });

    await page.getByTestId("uw-analyze-input").fill("AAPL");
    await page.getByTestId("uw-analyze-submit").click();

    await expect(page.getByTestId("uw-analyze-identity")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId("uw-analyze-thesis")).toBeVisible();
    await expect(page.getByTestId("uw-analyze-buckets")).toBeVisible();
    await expect(page.getByTestId("uw-analyze-gex-table")).toBeVisible();
    await expect(page.getByTestId("uw-analyze-notes")).toBeVisible();

    // Positioning n/a tile
    const positioning = page.getByTestId("uw-analyze-positioning");
    await expect(positioning).toContainText("not available");
  });

  test("shows error alert on 404 unknown ticker", async ({ page }) => {
    await setupMocks(page);
    await page.goto("/uw-analyze");
    await page.getByTestId("uw-analyze-input").fill("ZZZZ");
    await page.getByTestId("uw-analyze-submit").click();
    await expect(page.getByTestId("uw-analyze-error")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId("uw-analyze-error")).toContainText("ticker not found");
  });
});
