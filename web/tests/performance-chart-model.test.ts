import { expect, test } from "vitest";
import {
  DEFAULT_PERFORMANCE_CHART_HEIGHT,
  DEFAULT_PERFORMANCE_CHART_MARGINS,
  DEFAULT_PERFORMANCE_CHART_WIDTH,
  buildPerformanceChartModel,
} from "../lib/performanceChart";
import type { PerformanceOk } from "../lib/types";

const PERFORMANCE_MOCK: PerformanceOk = {
  status: "ok",
  as_of: "2026-03-10",
  last_sync: "2026-03-10T18:55:00Z",
  period_start: "2026-01-01",
  period_end: "2026-03-10",
  period_label: "YTD NAV Change",
  scope: { broker: "IB", account_env: "paper", broker_account: "DU0000000" },
  currency: "USD",
  benchmark: "SPY",
  benchmark_total_return: -0.0225,
  trades_source: "nav_history",
  price_sources: { primary: "nav_history", benchmark: "ib_historical_daily" },
  methodology: { basis: "NAV change", annualization_periods: 252 },
  summary: {
    starting_equity: 1_050_000,
    ending_equity: 1_094_500,
    pnl: 44_500,
    trading_days: 46,
    total_return: 0.04238,
    max_drawdown: -0.0918,
    current_drawdown: -0.0121,
    max_drawdown_duration_days: 14,
    low_confidence: true,
    sharpe_se: 2.34,
    sortino_se: 2.34,
    annualized_return: 0.264,
    annualized_volatility: 0.118,
    downside_deviation: 0.081,
    sharpe_ratio: 1.84,
    sortino_ratio: 2.41,
    calmar_ratio: 2.87,
    var_95: -0.012,
    cvar_95: -0.017,
    tail_ratio: 1.12,
    ulcer_index: 0.029,
    beta: 0.62,
    alpha: 0.038,
    correlation: 0.77,
    r_squared: 0.59,
    tracking_error: 0.094,
    information_ratio: 0.88,
    treynor_ratio: 0.43,
    upside_capture: 0.91,
    downside_capture: 0.68,
    hit_rate: 0.57,
    positive_days: 26,
    negative_days: 19,
    flat_days: 1,
    best_day: 0.019,
    worst_day: -0.016,
    average_up_day: 0.005,
    average_down_day: -0.004,
    win_loss_ratio: 1.25,
    skew: -0.2,
    kurtosis: 0.41,
  },
  warnings: [],
  contracts_missing_history: [],
  series: Array.from({ length: 10 }, (_, index) => ({
    date: `2026-01-${String(index + 2).padStart(2, "0")}`,
    equity: 1_050_000 + index * 4_500,
    daily_return: index === 0 ? null : 0.0025,
    drawdown: index < 6 ? 0 : -0.01,
    benchmark_close: 670 + index * 2,
    benchmark_return: index === 0 ? null : 0.0014,
  })),
};

test("performance chart model builds visible x/y axis ticks", () => {
  const model = buildPerformanceChartModel(
    PERFORMANCE_MOCK,
    DEFAULT_PERFORMANCE_CHART_WIDTH,
    DEFAULT_PERFORMANCE_CHART_HEIGHT,
    DEFAULT_PERFORMANCE_CHART_MARGINS,
  );

  expect(model.yAxisTicks.length).toBeGreaterThanOrEqual(4);
  expect(model.xAxisTicks.length).toBeGreaterThanOrEqual(3);
  expect(model.yAxisTicks.every((tick) => tick.label.startsWith("$"))).toBe(
    true,
  );
  expect(model.xAxisTicks[0]?.label).toMatch(/^[A-Z][a-z]{2} \d{1,2}$/);
});

test("performance chart model uses one shared domain for portfolio and rebased benchmark", () => {
  const model = buildPerformanceChartModel(
    PERFORMANCE_MOCK,
    DEFAULT_PERFORMANCE_CHART_WIDTH,
    DEFAULT_PERFORMANCE_CHART_HEIGHT,
    DEFAULT_PERFORMANCE_CHART_MARGINS,
  );
  const combined = [
    ...PERFORMANCE_MOCK.series.map((point) => point.equity),
    ...model.rebasedBenchmarkValues,
  ];

  expect(model.domainMin).toBeLessThanOrEqual(Math.min(...combined));
  expect(model.domainMax).toBeGreaterThanOrEqual(Math.max(...combined));
  expect(model.equityPath.startsWith("M ")).toBe(true);
  expect(model.benchmarkPath.startsWith("M ")).toBe(true);
});

test("performance chart model carries forward through null benchmark gaps", () => {
  const withGaps: PerformanceOk = {
    ...PERFORMANCE_MOCK,
    series: PERFORMANCE_MOCK.series.map((p, i) => ({
      ...p,
      benchmark_close: i % 2 === 0 ? p.benchmark_close : null,
      benchmark_return: i % 2 === 0 ? p.benchmark_return : null,
    })),
  };
  const model = buildPerformanceChartModel(withGaps);
  // No NaN values from null arithmetic
  expect(model.rebasedBenchmarkValues.every((v) => Number.isFinite(v))).toBe(
    true,
  );
});
