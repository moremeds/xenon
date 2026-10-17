import { test, expect, type Page } from '@playwright/test';

/**
 * E2E: Futu tab empty states after the disk fallback removal.
 *
 * With no JSON fallback, a cold load whose GET /api/futu/portfolio returns
 * 404 (never synced) or 502 (FastAPI down) must NOT leave the portfolio
 * section on "LOADING / Waiting for portfolio data..." forever — it shows
 * a measurement-condition empty state with a retry action.
 *
 * The Futu POST poll gets the same failing status so the state stays
 * deterministic regardless of market hours.
 */

const IB_FIXTURE = {
  bankroll: 500_000,
  peak_value: 500_000,
  last_sync: new Date().toISOString(),
  positions: [
    {
      id: 1,
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
  total_deployed_pct: 3,
  total_deployed_dollars: 17_850,
  remaining_capacity_pct: 97,
  position_count: 1,
  defined_risk_count: 0,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  account_summary: {
    net_liquidation: 500_000, daily_pnl: 0, unrealized_pnl: 0, realized_pnl: 0,
    settled_cash: 40_000, maintenance_margin: 0, excess_liquidity: 0,
    buying_power: 80_000, dividends: 0, cash: 40_000, initial_margin: 0,
    available_funds: 0, equity_with_loan: 500_000, previous_day_ewl: 0,
    reg_t_equity: 0, sma: 0, gross_position_value: 17_850,
  },
};

async function installMocks(page: Page, futuStatus: number, futuBody: unknown) {
  await page.route('**/api/futu/portfolio', async (route) => {
    await route.fulfill({
      status: futuStatus,
      contentType: 'application/json',
      body: JSON.stringify(futuBody),
    });
  });
  await page.route('**/api/portfolio', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(IB_FIXTURE) });
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
  await page.route('**/api/prices*', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({}) });
  });
}

async function openFutuTab(page: Page) {
  await page.goto('/portfolio');
  await page.getByRole('button', { name: 'Switch to FUTU account' }).click();
}

test.describe('Futu portfolio empty state', () => {
  test.beforeEach(async ({ context }) => {
    await context.clearCookies();
  });

  test('GET 404 → "not yet synced" measurement condition + retry, no LOADING', async ({ page }) => {
    await installMocks(page, 404, {
      error: 'Futu portfolio not yet synced. Start Futu OpenD and POST /api/futu/sync.',
    });
    await openFutuTab(page);

    await expect(page.getByTestId('futu-empty-state')).toContainText(
      'Futu not yet synced — start Futu OpenD, then sync.',
    );
    await expect(page.getByText('NEVER SYNCED', { exact: true }).last()).toBeVisible();
    await expect(page.getByTestId('futu-retry')).toBeVisible();
    await expect(page.getByText('Waiting for portfolio data...')).toHaveCount(0);
  });

  test('GET 502 → "unavailable" measurement condition + retry, no LOADING', async ({ page }) => {
    await installMocks(page, 502, { error: 'Futu portfolio unavailable: upstream refused' });
    await openFutuTab(page);

    await expect(page.getByTestId('futu-empty-state')).toContainText(
      'Futu unavailable — FastAPI did not return a snapshot.',
    );
    await expect(page.getByText('UNAVAILABLE', { exact: true })).toBeVisible();
    await expect(page.getByTestId('futu-retry')).toBeVisible();
    await expect(page.getByText('Waiting for portfolio data...')).toHaveCount(0);
  });
});
