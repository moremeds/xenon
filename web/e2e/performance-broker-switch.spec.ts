/**
 * E2E: Performance tab swaps payload when the broker tab switches.
 *
 * Spec: docs/superpowers/specs/2026-05-31-performance-rebuild-design.md
 * Contract: GET /api/performance?broker=<IB|FUTU> returns a scope-tagged
 * payload; the hook keeps a separate cache entry per broker.
 */
import { expect, test } from "@playwright/test";

const OK_PAYLOAD = (
  broker: "IB" | "FUTU",
  env: "paper" | "live",
  account: string,
  returnPct: number,
) => ({
  status: "ok",
  as_of: "2026-06-01",
  last_sync: "2026-06-01T20:00:00Z",
  period_start: "2026-01-01",
  period_end: "2026-06-01",
  period_label: "YTD NAV Change",
  scope: { broker, account_env: env, broker_account: account },
  currency: "USD",
  benchmark: "SPY",
  benchmark_total_return: 0.0612,
  trades_source: "nav_history",
  price_sources: { primary: "nav_history", benchmark: "ib_historical_daily" },
  methodology: { basis: "NAV change", annualization_periods: 252 },
  summary: {
    starting_equity: 100_000,
    ending_equity: 100_000 * (1 + returnPct),
    pnl: 100_000 * returnPct,
    trading_days: 105,
    total_return: returnPct,
    max_drawdown: -0.045,
    current_drawdown: -0.012,
    max_drawdown_duration_days: 8,
    low_confidence: true,
    sharpe_se: 1.55,
    sortino_se: 1.55,
    annualized_return: 0.18,
    annualized_volatility: 0.12,
    downside_deviation: 0.08,
    sharpe_ratio: 1.5,
    sortino_ratio: 2.0,
    calmar_ratio: 4.0,
    var_95: -0.015,
    cvar_95: -0.022,
    tail_ratio: 1.1,
    ulcer_index: 0.02,
    beta: 0.65,
    alpha: 0.03,
    correlation: 0.74,
    r_squared: 0.55,
    tracking_error: 0.09,
    information_ratio: 0.6,
    treynor_ratio: 0.28,
    upside_capture: 0.88,
    downside_capture: 0.62,
    hit_rate: 0.56,
    positive_days: 58,
    negative_days: 45,
    flat_days: 2,
    best_day: 0.018,
    worst_day: -0.015,
    average_up_day: 0.005,
    average_down_day: -0.004,
    win_loss_ratio: 1.2,
    skew: -0.15,
    kurtosis: 0.4,
  },
  warnings:
    broker === "FUTU"
      ? [
          "FUTU NAV-change returns include external cash flows (deposits, withdrawals, dividends). True Time-Weighted Return requires cash-flow tracking — follow-up.",
        ]
      : [],
  contracts_missing_history: [],
  series: Array.from({ length: 12 }, (_, i) => ({
    date: `2026-05-${String(i + 1).padStart(2, "0")}`,
    equity: 100_000 + i * 200,
    daily_return: i === 0 ? null : 0.002,
    drawdown: i < 5 ? 0 : -0.01,
    benchmark_close: 500 + i,
    benchmark_return: i === 0 ? null : 0.0015,
  })),
});

test.describe("Performance broker switch", () => {
  test.beforeEach(async ({ page }) => {
    await page.route("**/api/performance**", async (route) => {
      const url = new URL(route.request().url());
      const broker = (url.searchParams.get("broker") ?? "IB") as "IB" | "FUTU";
      const payload =
        broker === "FUTU"
          ? OK_PAYLOAD("FUTU", "live", "999777", 0.0512)
          : OK_PAYLOAD("IB", "paper", "DU0000000", 0.0834);
      await route.fulfill({
        status: 200,
        body: JSON.stringify(payload),
        contentType: "application/json",
      });
    });
  });

  test("IB tab shows IB scope; Futu tab swaps to FUTU scope", async ({
    page,
  }) => {
    await page.goto("/performance");
    await expect(page.getByTestId("performance-panel")).toBeVisible();
    // IB scope chip in the hero pills (e.g. "IB PAPER")
    await expect(page.locator(".performance-hero-pills")).toContainText(
      /IB\s+PAPER/i,
    );

    // Switch to Futu tab
    await page.getByRole("button", { name: /futu/i }).click();
    await expect(page.locator(".performance-hero-pills")).toContainText(
      /FUTU\s+LIVE/i,
    );
  });

  test("low_confidence badge surfaces with Sharpe SE tooltip", async ({
    page,
  }) => {
    await page.goto("/performance");
    const badge = page.getByTestId("performance-low-confidence-badge");
    await expect(badge).toBeVisible();
    await expect(badge).toHaveAttribute("title", /Sharpe SE\s+≈\s+1\.55/);
  });
});
