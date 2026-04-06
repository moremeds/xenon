import { expect, test } from "@playwright/test";

const PERFORMANCE_MOCK = {
  as_of: "2026-03-10",
  last_sync: "2026-03-10T18:55:00Z",
  period_start: "2026-01-01",
  period_end: "2026-03-10",
  period_label: "YTD",
  benchmark: "SPY",
  benchmark_total_return: -0.0071,
  trades_source: "ib_flex",
  price_sources: {
    stocks: "ib_with_uw_yahoo_fallback",
    options: "unusual_whales_option_contract_historic",
  },
  methodology: {
    curve_type: "reconstructed_net_liquidation",
    return_basis: "daily_close_to_close",
    risk_free_rate: 0,
    library_strategy: "in_repo_formulas_aligned_to_empyrical_quantstats_conventions",
  },
  summary: {
    starting_equity: 1_050_000,
    ending_equity: 1_319_477.93,
    pnl: 269_477.93,
    trading_days: 47,
    total_return: 0.2566,
    annualized_return: 0.84,
    annualized_volatility: 0.152,
    downside_deviation: 0.099,
    sharpe_ratio: 1.79,
    sortino_ratio: 2.21,
    calmar_ratio: 2.81,
    max_drawdown: -0.0473,
    current_drawdown: -0.0121,
    max_drawdown_duration_days: 13,
    beta: 0.23,
    alpha: 0.041,
    correlation: 0.23,
    r_squared: 0.19,
    tracking_error: 0.093,
    information_ratio: 0.88,
    treynor_ratio: 0.43,
    upside_capture: 8.59,
    downside_capture: 3.42,
    var_95: -0.0473,
    cvar_95: -0.1738,
    tail_ratio: 1.79,
    ulcer_index: 0.34,
    skew: -0.2,
    kurtosis: 0.41,
    hit_rate: 0.5,
    positive_days: 23,
    negative_days: 23,
    flat_days: 1,
    best_day: 0.019,
    worst_day: -0.016,
    average_up_day: 0.005,
    average_down_day: -0.004,
    win_loss_ratio: 1.25,
  },
  warnings: [],
  contracts_missing_history: [],
  series: Array.from({ length: 12 }, (_, index) => ({
    date: `2026-01-${String(index + 2).padStart(2, "0")}`,
    equity: index < 4 ? 1_080_000 + index * 1_000 : 1_250_000 - index * 6_000,
    daily_return: index === 0 ? null : 0.0025,
    drawdown: index < 8 ? 0 : -0.01,
    benchmark_close: 670 + index * (index % 2 === 0 ? 4 : -3),
    benchmark_return: index === 0 ? 0 : 0.0014,
  })),
};

const PORTFOLIO_EMPTY = {
  bankroll: 100_000,
  peak_value: 100_000,
  last_sync: "2026-03-10T18:55:00Z",
  positions: [],
  total_deployed_pct: 0,
  total_deployed_dollars: 0,
  remaining_capacity_pct: 100,
  position_count: 0,
  defined_risk_count: 0,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  account_summary: {
    net_liquidation: 100_000,
    daily_pnl: 0,
    unrealized_pnl: 0,
    realized_pnl: 0,
    settled_cash: 100_000,
    maintenance_margin: 0,
    excess_liquidity: 100_000,
    buying_power: 100_000,
    dividends: 0,
  },
};

const ORDERS_EMPTY = {
  last_sync: "2026-03-10T18:55:00Z",
  open_orders: [],
  executed_orders: [],
  open_count: 0,
  executed_count: 0,
};

async function setupMocks(page: import("@playwright/test").Page) {
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await page.route("**/api/performance", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(PERFORMANCE_MOCK) }),
  );
  await page.route("**/api/portfolio", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(PORTFOLIO_EMPTY) }),
  );
  await page.route("**/api/orders", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(ORDERS_EMPTY) }),
  );
  await page.route("**/api/blotter", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        as_of: "2026-03-10T18:55:00Z",
        summary: { closed_trades: 0, open_trades: 0, total_commissions: 0, realized_pnl: 0 },
        closed_trades: [],
        open_trades: [],
      }),
    }),
  );
}

test.describe("/performance page — chart theme", () => {
  test("equity chart and chart meta cards switch to light-mode surfaces", async ({ page }) => {
    await setupMocks(page);
    await page.addInitScript(() => {
      localStorage.setItem("theme", "dark");
    });

    await page.goto("/performance");

    const chart = page.getByTestId("performance-equity-chart");
    const chartMetaItem = page.locator(".performance-chart-meta .performance-meta-item").first();

    await expect(chart).toBeVisible({ timeout: 10_000 });
    await expect(chartMetaItem).toBeVisible();

    const darkChartBackground = await chart.evaluate((node) => getComputedStyle(node).backgroundImage);

    await page.getByLabel("Toggle theme").click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");

    const lightChartBackground = await chart.evaluate((node) => getComputedStyle(node).backgroundImage);
    const lightMetaBackground = await chartMetaItem.evaluate((node) => getComputedStyle(node).backgroundColor);

    expect(lightChartBackground).not.toBe(darkChartBackground);
    expect(lightChartBackground).toContain("241, 245, 249");
    expect(lightChartBackground).toContain("255, 255, 255");
    expect(lightMetaBackground).toContain("241, 245, 249");
  });
});
