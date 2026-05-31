import { expect, test } from "@playwright/test";

const REGIME_CLOSE = {
  scan_time: "2026-03-12T13:03:13.251409",
  market_open: false,
  date: "2026-03-12",
  vix: 26.72,
  vvix: 130.18,
  spy: 666.06,
  vix_5d_roc: 14.6,
  vvix_vix_ratio: 5.06,
  realized_vol: 12.55,
  cor1m: 29.18,
  cor1m_previous_close: 28.87,
  cor1m_5d_change: 11.23,
  spx_100d_ma: 682.39,
  spx_distance_pct: -0.87,
  spy_closes: Array.from({ length: 40 }, (_, index) => 680 - index * 0.4),
  cri: { score: 31, level: "ELEVATED", components: { vix: 9.5, vvix: 12.4, correlation: 5.4, momentum: 3.7 } },
  crash_trigger: {
    triggered: false,
    conditions: { spx_below_100d_ma: true, realized_vol_gt_25: false, cor1m_gt_60: false },
  },
  cta: { exposure_pct: 86.9, forced_reduction_pct: 13.1, est_selling_bn: 52.4 },
  menthorq_cta: null,
  history: [],
};

const EMPTY_GEX = {
  scan_time: "",
  market_open: false,
  ticker: "SPX",
  spot: 0,
  close: null,
  day_change: null,
  day_change_pct: null,
  data_date: "",
  net_gex: 0,
  net_dex: 0,
  atm_iv: null,
  vol_pc: null,
  levels: {
    gex_flip: null,
    max_magnet: null,
    second_magnet: null,
    max_accelerator: null,
    put_wall: null,
    call_wall: null,
  },
  profile: [],
  expected_range: { low: null, high: null, iv_1d: null },
  bias: {
    direction: "NEUTRAL",
    reasons: [],
    days_above_flip: 0,
    flip_migration: [],
  },
  history: [],
  iv: null,
  mq: null,
  source_delta: null,
};

const FRESH_GEX = {
  scan_time: "2026-03-12T13:05:00.000000",
  market_open: false,
  ticker: "SPX",
  spot: 5582.69,
  close: 5575.37,
  day_change: 7.32,
  day_change_pct: 0.13,
  data_date: "2026-03-12",
  net_gex: -104044.47,
  net_dex: 37200,
  atm_iv: 19.7,
  vol_pc: 1.42,
  levels: {
    gex_flip: { strike: 5537, gamma: 0, distance: -45.69, distance_pct: -0.82 },
    max_magnet: { strike: 5700, gamma: 2955.46, distance: 117.31, distance_pct: 2.1 },
    second_magnet: { strike: 5605, gamma: 2501.8, distance: 22.31, distance_pct: 0.4 },
    max_accelerator: { strike: 5500, gamma: -13511.1, distance: -82.69, distance_pct: -1.48 },
    put_wall: { strike: 5000, gamma: -8000, distance: -582.69, distance_pct: -10.44 },
    call_wall: { strike: 5700, gamma: 3000, distance: 117.31, distance_pct: 2.1 },
  },
  profile: [
    { strike: 5500, call_gex: 100, put_gex: -60, net_gex: -13511, pct_from_spot: -1.48, tag: "MAX ACCELERATOR" },
    { strike: 5537, call_gex: 50, put_gex: -50, net_gex: 0, pct_from_spot: -0.82, tag: "GEX FLIP" },
    { strike: 5575, call_gex: 80, put_gex: -20, net_gex: 60, pct_from_spot: -0.14, tag: "SPOT" },
    { strike: 5700, call_gex: 200, put_gex: -30, net_gex: 2955, pct_from_spot: 2.1, tag: "MAX MAGNET" },
  ],
  expected_range: { low: 5500, high: 5665, iv_1d: 1.24 },
  bias: {
    direction: "CAUTIOUS_BULL",
    reasons: ["Spot above flip (5537)", "Net GEX still negative", "Magnet at 5700 above spot"],
    days_above_flip: 3,
    flip_migration: [
      { date: "2026-03-10", flip: 5433 },
      { date: "2026-03-11", flip: 5494 },
      { date: "2026-03-12", flip: 5537 },
    ],
  },
  history: [],
  iv: {
    iv30d: 19.7,
    iv_rank: 29.7,
    hv30: 16.75,
    mq_iv30d: 19.5,
    mq_iv_rank: "30%",
    source: "both",
  },
  mq: null,
  source_delta: null,
};

const PORTFOLIO_EMPTY = {
  bankroll: 100_000,
  positions: [],
  account_summary: {},
  exposure: {},
  violations: [],
};

const ORDERS_EMPTY = {
  last_sync: new Date().toISOString(),
  open_orders: [],
  executed_orders: [],
  open_count: 0,
  executed_count: 0,
};

async function setupMocks(page: import("@playwright/test").Page) {
  let gexGetCount = 0;

  await page.unrouteAll({ behavior: "ignoreErrors" });

  await page.route("**/api/regime", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(REGIME_CLOSE),
    }),
  );

  await page.route("**/api/gex", (route) => {
    gexGetCount += 1;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(gexGetCount >= 2 ? FRESH_GEX : EMPTY_GEX),
    });
  });

  await page.route("**/api/portfolio", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(PORTFOLIO_EMPTY),
    }),
  );
  await page.route("**/api/orders", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(ORDERS_EMPTY),
    }),
  );
  await page.route("**/api/prices", (route) => route.abort());
  await page.route("**/api/ib-status", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ connected: false }),
    }),
  );
  await page.route("**/api/blotter", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ as_of: new Date().toISOString(), summary: { realized_pnl: 0 }, closed_trades: [], open_trades: [] }),
    }),
  );
  await page.route("**/api/menthorq/cta", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ tables: [] }),
    }),
  );

  return {
    getGexCount: () => gexGetCount,
  };
}

test.describe("/regime page — GEX closed-session refresh", () => {
  test("hydrates the GEX panel after an empty stale first read without a manual reload", async ({ page }) => {
    const tracker = await setupMocks(page);
    await page.goto("/regime");

    await page.getByRole("button", { name: "GEX" }).click();

    await expect(page.getByText("SPX Gamma Exposure Levels")).toBeVisible({ timeout: 12_000 });
    await expect(page.getByText("DAY 3 ABOVE GEX FLIP")).toBeVisible();
    await expect(page.getByText("5,582.69")).toBeVisible();
    await expect.poll(() => tracker.getGexCount()).toBeGreaterThanOrEqual(2);
  });
});
