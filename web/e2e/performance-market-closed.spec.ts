import { expect, test } from "@playwright/test";

const PERFORMANCE_MOCK_CLOSED = {
  as_of: "2026-03-21",
  last_sync: "2026-03-21T20:00:00Z",
  period_start: "2026-01-01",
  period_end: "2026-03-21",
  period_label: "YTD",
  benchmark: "SPY",
  benchmark_total_return: -0.0125,
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
    ending_equity: 1_094_500,
    pnl: 44_500,
    trading_days: 46,
    total_return: 0.04238,
    annualized_return: 0.264,
    annualized_volatility: 0.118,
    downside_deviation: 0.081,
    sharpe_ratio: 1.84,
    sortino_ratio: 2.41,
    calmar_ratio: 2.87,
    max_drawdown: -0.0918,
    current_drawdown: -0.0121,
    max_drawdown_duration_days: 14,
    beta: 0.62,
    alpha: 0.038,
    correlation: 0.77,
    r_squared: 0.59,
    tracking_error: 0.094,
    information_ratio: 0.88,
    treynor_ratio: 0.43,
    upside_capture: 0.91,
    downside_capture: 0.68,
    var_95: -0.012,
    cvar_95: -0.017,
    tail_ratio: 1.12,
    ulcer_index: 0.029,
    skew: -0.2,
    kurtosis: 0.41,
    hit_rate: 0.57,
    positive_days: 26,
    negative_days: 19,
    flat_days: 1,
    best_day: 0.019,
    worst_day: -0.016,
    average_up_day: 0.005,
    average_down_day: -0.004,
    win_loss_ratio: 1.25,
  },
  warnings: [],
  contracts_missing_history: [],
  series: Array.from({ length: 5 }, (_, index) => ({
    date: `2026-03-${String(index + 17).padStart(2, "0")}`,
    equity: 1_075_000 + index * 4_500,
    daily_return: index === 0 ? null : 0.0025,
    drawdown: 0,
    benchmark_close: 570 + index * 2,
    benchmark_return: index === 0 ? 0 : 0.0014,
  })),
};

const PORTFOLIO_EMPTY = {
  bankroll: 100_000,
  peak_value: 100_000,
  last_sync: "2026-03-21T20:00:00Z",
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
  last_sync: "2026-03-21T20:00:00Z",
  open_orders: [],
  executed_orders: [],
  open_count: 0,
  executed_count: 0,
};

async function freezeToClosedWeekend(page: import("@playwright/test").Page) {
  await page.addInitScript(() => {
    const fixedNow = new Date("2026-03-22T12:00:00Z").valueOf();
    const RealDate = Date;
    class MockDate extends RealDate {
      constructor(...args: ConstructorParameters<typeof Date>) {
        if (args.length === 0) {
          super(fixedNow);
          return;
        }
        super(...args);
      }
      static now() {
        return fixedNow;
      }
    }
    Object.defineProperty(window, "Date", {
      value: MockDate,
      configurable: true,
      writable: true,
    });
  });
}

async function setupMocks(page: import("@playwright/test").Page) {
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await freezeToClosedWeekend(page);

  await page.route("**/api/performance", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(PERFORMANCE_MOCK_CLOSED),
    }),
  );
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
  await page.route("**/api/flex-token", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ok: true, days_until_expiry: 14 }),
    }),
  );
  await page.route("**/api/ib-status", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ connected: false }),
    }),
  );
  await page.route("**/api/prices**", (route) => route.abort());
  await page.route("**/api/blotter", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ as_of: "2026-03-21T20:00:00Z", summary: { realized_pnl: 0 }, closed_trades: [], open_trades: [] }),
    }),
  );
}

test.describe("/performance closed-market load", () => {
  test("renders cached performance data instead of hanging on the loading state", async ({ page }) => {
    await setupMocks(page);
    await page.goto("http://127.0.0.1:3000/performance");

    await expect(page.locator('[data-testid="performance-panel"]')).toBeVisible();
    await expect(page.getByText("Reconstructing YTD portfolio performance...")).toHaveCount(0);
    await expect(page.locator('[data-testid="performance-panel"]')).toContainText("4.24%");
    await expect(page.locator('[data-testid="performance-panel"]')).toContainText("Ending equity $1,094,500.00");
  });
});
