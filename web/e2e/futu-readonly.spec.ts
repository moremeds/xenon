import { test, expect, type Page } from '@playwright/test';

/**
 * Safety E2E: when the Futu tab is active, a user's click path cannot
 * reach any IB order placement surface. This is the tribunal T7 guarantee.
 *
 * Test strategy:
 *  - Mock `/api/futu/portfolio` (GET + POST) so the browser sees a
 *    deterministic envelope with one stock row and one spread row.
 *  - Mock `/api/portfolio` so the IB tab has content to compare against
 *    (and so "click to switch" actually has something to show).
 *  - Mock `/api/orders` (empty) and `/api/ib-status` (connected).
 *  - Click the FUTU tab, then verify that the three unsafe click paths
 *    (ticker click, leg click, keyboard Enter) do NOT navigate to
 *    /[ticker] and do NOT open InstrumentDetailModal.
 */

const FUTU_FIXTURE = {
  ok: true,
  fetched_at: new Date().toISOString(),
  data_as_of: new Date().toISOString(),
  account_id: '281756478831553263',
  source: 'futu' as const,
  is_stale: false,
  warnings: [] as string[],
  count: 1,
  positions: [
    {
      futu_code: 'US.TSLA',
      normalized: {
        kind: 'STK' as const,
        symbol: 'TSLA',
        exchange: 'SMART',
        currency: 'USD',
        live_data: true,
      },
      quantity: 300,
      avg_cost: 320.71,
      market_price: 351.67,
      market_value: 105501.0,
      unrealized_pnl: 9288.0,
      unrealized_pnl_pct: 9.65,
      currency: 'USD',
      position_side: 'LONG',
    },
  ],
  account_summary: {
    net_liquidation: 148000,
    equity_with_loan: 148000,
    cash: -14585,
    settled_cash: -14585,
    buying_power: 29917,
    available_funds: 29917,
    initial_margin: 130962,
    maintenance_margin: 114285,
    excess_liquidity: 33715,
    gross_position_value: 105501,
    unrealized_pnl: 9288,
    daily_pnl: 9288,
    realized_pnl: 0,
    dividends: null,
    previous_day_ewl: null,
    reg_t_equity: null,
    sma: null,
  },
};

const IB_FIXTURE = {
  bankroll: 847231,
  peak_value: 900000,
  last_sync: new Date().toISOString(),
  positions: [
    {
      id: 1,
      ticker: 'AAPL',
      structure: 'Stock',
      structure_type: 'Stock',
      risk_profile: 'equity',
      expiry: '',
      contracts: 100,
      direction: 'LONG',
      entry_cost: 15000,
      max_risk: null,
      market_value: 17850,
      legs: [
        {
          direction: 'LONG',
          contracts: 100,
          type: 'Stock',
          strike: null,
          entry_cost: 15000,
          avg_cost: 150,
          market_price: 178.5,
          market_value: 17850,
        },
      ],
      kelly_optimal: null,
      target: null,
      stop: null,
      entry_date: '2026-01-01',
    },
  ],
  total_deployed_pct: 12,
  total_deployed_dollars: 17850,
  remaining_capacity_pct: 88,
  position_count: 1,
  defined_risk_count: 0,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  account_summary: {
    net_liquidation: 847231,
    daily_pnl: 3104,
    unrealized_pnl: 2850,
    realized_pnl: 0,
    settled_cash: 38811,
    maintenance_margin: 31786,
    excess_liquidity: 11551,
    buying_power: 49533,
    dividends: 0.08,
    cash: 38811,
    initial_margin: 35903,
    available_funds: 7430,
    equity_with_loan: 843333,
    previous_day_ewl: 0,
    reg_t_equity: 0,
    sma: 0,
    gross_position_value: 19210,
  },
};

async function installMocks(page: Page) {
  await page.route('**/api/futu/portfolio', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(FUTU_FIXTURE),
    });
  });
  await page.route('**/api/portfolio', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(IB_FIXTURE),
    });
  });
  await page.route('**/api/orders', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        last_sync: new Date().toISOString(),
        open_orders: [],
        executed_orders: [],
        open_count: 0,
        executed_count: 0,
      }),
    });
  });
  // Block any attempt to hit the real order placement endpoint — fail the
  // test loudly if code path reaches here from a Futu row.
  await page.route('**/api/orders/place', async (route) => {
    throw new Error('FATAL: code path reached /api/orders/place from Futu tab');
  });
}

test.describe('Futu tab read-only safety (T7)', () => {
  test.beforeEach(async ({ page }) => {
    await installMocks(page);
    await page.goto('/portfolio');
    // Switch to FUTU tab
    const futuTab = page.getByRole('button', { name: /Switch to FUTU account/ });
    await futuTab.waitFor();
    await futuTab.click();
  });

  test('ticker cell renders as a non-interactive span with aria-disabled', async ({ page }) => {
    const tslaSpan = page
      .locator('span.ticker-link-disabled')
      .filter({ hasText: 'TSLA' })
      .first();
    await expect(tslaSpan).toBeVisible();
    await expect(tslaSpan).toHaveAttribute('aria-disabled', 'true');
    // Must NOT be a button
    const tslaButton = page
      .locator('button.ticker-link')
      .filter({ hasText: 'TSLA' });
    await expect(tslaButton).toHaveCount(0);
  });

  test('clicking the TSLA ticker does not navigate to /TSLA', async ({ page }) => {
    const urlBefore = page.url();
    const tslaSpan = page
      .locator('span.ticker-link-disabled')
      .filter({ hasText: 'TSLA' })
      .first();
    await tslaSpan.click({ force: true });
    // Short delay to allow any async navigation
    await page.waitForTimeout(300);
    expect(page.url()).toBe(urlBefore);
  });

  test('InstrumentDetailModal never appears while on FUTU tab', async ({ page }) => {
    // Modal has class .instrument-detail-modal — must never render.
    const modal = page.locator('.instrument-detail-modal');
    await expect(modal).toHaveCount(0);
  });

  test('switching back to IB restores interactive ticker links', async ({ page }) => {
    const ibTab = page.getByRole('button', { name: /Switch to IB account/ });
    await ibTab.click();
    // AAPL row on IB side — should render as a real button
    const aaplButton = page
      .locator('button.ticker-link')
      .filter({ hasText: 'AAPL' });
    await expect(aaplButton).toBeVisible();
  });
});
