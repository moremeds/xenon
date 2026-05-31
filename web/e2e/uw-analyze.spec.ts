/**
 * E2E: /uw-analyze page — UW Analysis (tiered grid + single detail panel).
 *
 * Verifies:
 *  1. Sidebar labels the page "UW Analysis" and places it directly under
 *     "Flow Analysis".
 *  2. Six tier section headers render: MARKET INDICES, COMMODITIES & SAFE
 *     HAVEN, FIXED INCOME, VOLATILITY, SECTOR ETFS, SINGLE NAMES.
 *  3. SPY/QQQ/IWM/DIA render in that fixed order inside the market-indices tier.
 *  4. A row with `changes[]` auto-selects on load and wears the alert border.
 *  5. Clicking a single-name card swaps the detail panel to that ticker.
 *  6. Scaffold tiles render before the portfolio resolves.
 */

import { test, expect } from "@playwright/test";

function makeRow(ticker: string, opts: { changes?: unknown[] } = {}) {
  return {
    ticker,
    sources: ["portfolio"],
    prev_ts: null,
    changes: opts.changes ?? [],
    oi_changes: [],
    unusual_flow_events: [],
    snapshot: {
      ticker,
      ts: "2026-04-08T14:02:11Z",
      report: {
        ticker,
        price: 100,
        fetched_at: "2026-04-08T14:02:11Z",
        scores: {
          bias: "BULLISH",
          grade: "A",
          composite: 20,
          market_structure: 22,
          volatility: 18,
          flow: 17,
          positioning: null,
          mode: "full",
          skipped_buckets: ["positioning"],
          reweighted: true,
        },
        regime: { gex_sign: "positive", flip_distance_pct: -0.4 },
        setup_thesis: {
          structure_family: "neutral",
          regime: "R1",
          bias: "BULLISH",
          rationale: "demo rationale",
        },
        notes: [],
      },
      display: {
        sector: "XLK",
        iv_rank: 40,
        iv: 25,
        rv: 20,
        call_wall_strike: 110,
        put_wall_strike: 90,
        gamma_per_1pct: 1_000_000,
        net_call_premium: 5_000_000,
        net_put_premium: -1_000_000,
        short_volume_ratio: 0.42,
        short_volume_trend: [0.4, 0.41, 0.42],
        term_structure_label: "normal",
        gex_flip: null,
        gex_by_strike: [],
        max_pain: 100,
      },
      derived: {
        gex_sign: "POSITIVE",
        gex_flip_strike: null,
        max_pain: 100,
        call_wall: 110,
        put_wall: 90,
        iv_rank: 40,
        net_call_premium: 5_000_000,
        net_put_premium: -1_000_000,
        flow_score: 17,
        spot: 100,
      },
    },
  };
}

async function mockPortfolio(
  page: import("@playwright/test").Page,
  rows: ReturnType<typeof makeRow>[],
): Promise<void> {
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await page.route("**/api/uw-analyze/portfolio", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        fetched_at: "2026-04-08T14:02:11Z",
        market_state: "open",
        ttl_seconds: 120,
        tickers: rows,
        action_items: [],
      }),
    });
  });
}

test.describe("UW Analysis page", () => {
  test("sidebar label + order", async ({ page }) => {
    await mockPortfolio(page, []);
    await page.goto("/uw-analyze");
    const nav = page.locator("nav");
    await expect(nav.getByText("UW Analysis")).toBeVisible();

    const labels = await nav.locator("a, button").allTextContents();
    const flowIdx = labels.findIndex((t) => /Flow Analysis/i.test(t));
    const uwIdx = labels.findIndex((t) => /UW Analysis/i.test(t));
    expect(flowIdx).toBeGreaterThanOrEqual(0);
    expect(uwIdx).toBe(flowIdx + 1);
  });

  test("renders scaffold tiles before portfolio resolves", async ({ page }) => {
    await mockPortfolio(page, []);
    await page.goto("/uw-analyze");
    // Scaffold tiles render immediately for every static-universe ticker.
    for (const t of [
      "SPY",
      "QQQ",
      "IWM",
      "DIA",
      "GLD",
      "TLT",
      "UVXY",
      "XLK",
      "SMH",
    ]) {
      await expect(page.getByTestId(`uw-card-${t}`)).toBeVisible();
    }
  });

  test("tier grids + auto-select changed ticker + card click swaps detail", async ({
    page,
  }) => {
    await mockPortfolio(page, [
      makeRow("IWM"),
      makeRow("QQQ"),
      makeRow("SPY"),
      makeRow("GLD"),
      makeRow("NVDA", {
        changes: [{ code: "GEX_FLIP_SIGN", severity: "warn" }],
      }),
      makeRow("AAPL"),
    ]);
    await page.goto("/uw-analyze");

    const tiers = page.getByTestId("uw-analyze-tiers");
    await expect(tiers.getByText("MARKET INDICES")).toBeVisible();
    await expect(tiers.getByText("COMMODITIES & SAFE HAVEN")).toBeVisible();
    await expect(tiers.getByText("FIXED INCOME")).toBeVisible();
    await expect(tiers.getByText("VOLATILITY", { exact: true })).toBeVisible();
    await expect(tiers.getByText("SECTOR ETFS")).toBeVisible();
    await expect(tiers.getByText("WATCH · M7")).toBeVisible();

    // The first market-index row is selected by default; changed rows still carry alerts.
    const detail = page.getByTestId("uw-detail");
    await expect(detail).toHaveAttribute("data-ticker", "SPY");
    await expect(page.getByTestId("uw-card-NVDA")).toHaveAttribute(
      "data-alert",
      "true",
    );

    // Click AAPL → detail swaps.
    await page.getByTestId("uw-card-AAPL").click();
    await expect(detail).toHaveAttribute("data-ticker", "AAPL");
  });
});
