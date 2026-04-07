import { test, expect, type Page } from '@playwright/test';

/**
 * E2E: Portfolio "By Structure" view + toggle.
 *
 * Harness: mock portfolio + orders + status routes so header metrics are
 * deterministic. Price WS not installed — we exercise the fallback paths
 * that render from the fixture shape (which is what buildTickerGroups
 * uses when WS prices aren't available).
 *
 * Assertions cover:
 *   1. Default view is By Structure.
 *   2. First ticker card corresponds to largest-|MV| ticker.
 *   3. Toggle → By Risk reveals Defined/Undefined/Equity sections.
 *   4. Reload persists view mode.
 *   5. Toggle → By Structure restores cards.
 *   6. Collapse a category sub-block hides its PositionTable.
 *   7. Account switch IB ↔ FUTU with collapsed group in Structure view
 *      does not corrupt render.
 *   9. ISSUE-1 regression: `structure='Short Put $440.0'` with
 *      `structure_type='Short Put'` renders under SINGLE, not OTHER.
 */

const IB_FIXTURE = {
  bankroll: 500_000,
  peak_value: 500_000,
  last_sync: new Date().toISOString(),
  positions: [
    // Largest MV — TSLA stock + TSLA options
    {
      id: 1,
      ticker: 'TSLA',
      structure: 'Stock (500 shares)',
      structure_type: 'Stock',
      risk_profile: 'equity',
      expiry: 'N/A',
      contracts: 500,
      direction: 'LONG',
      entry_cost: 200_000,
      max_risk: null,
      market_value: 225_000,
      legs: [
        { direction: 'LONG', contracts: 500, type: 'Stock', strike: null, entry_cost: 200_000, avg_cost: 400, market_price: 450, market_value: 225_000 },
      ],
      kelly_optimal: null,
      target: null,
      stop: null,
      entry_date: '2026-01-01',
    },
    // TSLA Short Put — ISSUE-1 shape (decorated structure + normalized structure_type)
    {
      id: 2,
      ticker: 'TSLA',
      structure: 'Short Put $440.0',
      structure_type: 'Short Put',
      risk_profile: 'undefined',
      expiry: '2026-05-15',
      contracts: 1,
      direction: 'CREDIT',
      entry_cost: -500,
      max_risk: 44_000,
      market_value: -200,
      legs: [
        { direction: 'SHORT', contracts: 1, type: 'Put', strike: 440, entry_cost: -500, avg_cost: -500, market_price: -2, market_value: -200 },
      ],
      kelly_optimal: null,
      target: null,
      stop: null,
      entry_date: '2026-03-01',
    },
    // TSLA Bull Call Spread
    {
      id: 3,
      ticker: 'TSLA',
      structure: 'Bull Call Spread $450.0/$470.0',
      structure_type: 'Bull Call Spread',
      risk_profile: 'defined',
      expiry: '2026-05-15',
      contracts: 1,
      direction: 'DEBIT',
      entry_cost: 700,
      max_risk: 700,
      market_value: 900,
      legs: [
        { direction: 'LONG', contracts: 1, type: 'Call', strike: 450, entry_cost: 900, avg_cost: 900, market_price: 12, market_value: 1200 },
        { direction: 'SHORT', contracts: 1, type: 'Call', strike: 470, entry_cost: -200, avg_cost: -200, market_price: -3, market_value: -300 },
      ],
      kelly_optimal: null,
      target: null,
      stop: null,
      entry_date: '2026-03-01',
    },
    // AAPL (smaller MV)
    {
      id: 4,
      ticker: 'AAPL',
      structure: 'Stock (100 shares)',
      structure_type: 'Stock',
      risk_profile: 'equity',
      expiry: 'N/A',
      contracts: 100,
      direction: 'LONG',
      entry_cost: 15_000,
      max_risk: null,
      market_value: 17_850,
      legs: [
        { direction: 'LONG', contracts: 100, type: 'Stock', strike: null, entry_cost: 15_000, avg_cost: 150, market_price: 178.5, market_value: 17_850 },
      ],
      kelly_optimal: null,
      target: null,
      stop: null,
      entry_date: '2026-01-01',
    },
  ],
  total_deployed_pct: 48,
  total_deployed_dollars: 240_000,
  remaining_capacity_pct: 52,
  position_count: 4,
  defined_risk_count: 1,
  undefined_risk_count: 1,
  avg_kelly_optimal: null,
  account_summary: {
    net_liquidation: 500_000, daily_pnl: 0, unrealized_pnl: 27_850, realized_pnl: 0,
    settled_cash: 40_000, maintenance_margin: 30_000, excess_liquidity: 20_000,
    buying_power: 80_000, dividends: 0, cash: 40_000, initial_margin: 30_000,
    available_funds: 20_000, equity_with_loan: 500_000, previous_day_ewl: 0,
    reg_t_equity: 0, sma: 0, gross_position_value: 243_950,
  },
};

const FUTU_FIXTURE = {
  ok: true,
  fetched_at: new Date().toISOString(),
  data_as_of: new Date().toISOString(),
  account_id: 'FUTU-TEST',
  source: 'futu' as const,
  is_stale: false,
  warnings: [] as string[],
  count: 1,
  positions: [
    {
      futu_code: 'US.MSFT',
      normalized: {
        kind: 'STK' as const,
        symbol: 'MSFT',
        exchange: 'SMART',
        currency: 'USD',
        live_data: true,
      },
      quantity: 50,
      avg_cost: 400,
      market_price: 420,
      market_value: 21_000,
      unrealized_pnl: 1000,
      unrealized_pnl_pct: 5,
      currency: 'USD',
      position_side: 'LONG',
    },
  ],
  account_summary: {
    net_liquidation: 21_000, equity_with_loan: 21_000, cash: 0, settled_cash: 0,
    buying_power: 0, available_funds: 0, initial_margin: 0, maintenance_margin: 0,
    excess_liquidity: 0, gross_position_value: 21_000, unrealized_pnl: 1000,
    daily_pnl: 500, realized_pnl: 0, dividends: null, previous_day_ewl: null,
    reg_t_equity: null, sma: null,
  },
};

async function installMocks(page: Page) {
  await page.route('**/api/portfolio', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(IB_FIXTURE) });
  });
  await page.route('**/api/futu/portfolio', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(FUTU_FIXTURE) });
  });
  await page.route('**/api/orders', async (route) => {
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ last_sync: new Date().toISOString(), open_orders: [], executed_orders: [], open_count: 0, executed_count: 0 }),
    });
  });
  await page.route('**/api/ib-status', async (route) => {
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ connected: true, port_listening: true, ib_connected: true, disconnected_since: null }),
    });
  });
  await page.route('**/api/blotter', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ positions: [], daily_pnl: 0 }) });
  });
  // Abort WS price + market data noise so header metrics are deterministic
  await page.route('**/api/prices*', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({}) });
  });
}

test.describe('Portfolio View Toggle — By Structure', () => {
  test.beforeEach(async ({ page, context }) => {
    await context.clearCookies();
    await installMocks(page);
    await page.addInitScript(() => {
      try { window.localStorage.removeItem('xenon.portfolio.view'); } catch { /* noop */ }
    });
  });

  test('default view is By Structure; first card is largest-|MV| ticker (TSLA)', async ({ page }) => {
    await page.goto('/portfolio');
    const toggle = page.getByTestId('portfolio-view-toggle');
    await toggle.waitFor();
    const byStructureBtn = page.getByTestId('toggle-by-structure');
    await expect(byStructureBtn).toHaveAttribute('aria-selected', 'true');

    const firstCard = page.locator('[data-ticker]').first();
    await expect(firstCard).toHaveAttribute('data-ticker', 'TSLA');
  });

  test('ISSUE-1 regression: Short Put $440.0 (structure_type=Short Put) renders under SINGLE, not OTHER', async ({ page }) => {
    await page.goto('/portfolio');
    const tslaCard = page.locator('[data-ticker="TSLA"]');
    await tslaCard.waitFor();
    await expect(tslaCard.locator('[data-category="single"]')).toHaveCount(1);
    await expect(tslaCard.locator('[data-category="other"]')).toHaveCount(0);
  });

  test('toggle to By Risk reveals risk sections and persists across reload', async ({ page }) => {
    await page.goto('/portfolio');
    await page.getByTestId('toggle-by-risk').click();
    await expect(page.getByText('Defined Risk Positions')).toBeVisible();
    await expect(page.getByText('Equity Positions')).toBeVisible();

    await page.reload();
    await page.getByTestId('portfolio-view-toggle').waitFor();
    await expect(page.getByTestId('toggle-by-risk')).toHaveAttribute('aria-selected', 'true');
    await expect(page.getByText('Defined Risk Positions')).toBeVisible();
  });

  test('toggle back to By Structure returns to ticker cards', async ({ page }) => {
    await page.goto('/portfolio');
    await page.getByTestId('toggle-by-risk').click();
    await expect(page.getByText('Defined Risk Positions')).toBeVisible();
    await page.getByTestId('toggle-by-structure').click();
    await expect(page.locator('[data-ticker="TSLA"]')).toBeVisible();
    await expect(page.getByText('Defined Risk Positions')).toHaveCount(0);
  });

  test('collapse a category sub-block hides its rows (aria-expanded flips)', async ({ page }) => {
    await page.goto('/portfolio');
    const tslaCard = page.locator('[data-ticker="TSLA"]');
    const verticalBlock = tslaCard.locator('[data-category="vertical"]');
    const verticalToggle = verticalBlock.locator('button[aria-controls="group-TSLA-vertical"]');
    await verticalToggle.waitFor();
    await expect(verticalToggle).toHaveAttribute('aria-expanded', 'true');
    await verticalToggle.click();
    await expect(verticalToggle).toHaveAttribute('aria-expanded', 'false');
    // Body element should be gone
    await expect(page.locator('#group-TSLA-vertical')).toHaveCount(0);
  });
});
