import { test, expect } from '@playwright/test';

/**
 * E2E verification: AAOI option last price should not show yesterday's close
 * when bid/ask indicate a different current price.
 *
 * Bug: AAOI $105C showed Last Price = $25.26 (yesterday's close) despite
 * bid=$10.30 ask=$11.70 because IB sends frozen LAST = close via reqMarketDataType(4).
 */

const PORTFOLIO = {
  bankroll: 100_000,
  peak_value: 100_000,
  last_sync: new Date().toISOString(),
  total_deployed_pct: 2.5,
  total_deployed_dollars: 2_500,
  remaining_capacity_pct: 97.5,
  position_count: 1,
  defined_risk_count: 1,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  exposure: {},
  violations: [],
  positions: [
    {
      id: 105,
      ticker: 'AAOI',
      structure: 'Long Call $105.0',
      structure_type: 'Long Call',
      risk_profile: 'defined',
      direction: 'LONG',
      contracts: 1,
      expiry: '2026-03-20',
      entry_cost: 1030,
      market_value: 1170,
      market_price: 11.7,
      market_price_is_calculated: false,
      avg_cost: 1030,
      legs: [
        {
          direction: 'LONG',
          contracts: 1,
          type: 'Call',
          strike: 105,
          avg_cost: 1030,
          entry_cost: 1030,
          market_price: 11.7,
          market_price_is_calculated: false,
          market_value: 1170,
        },
      ],
    },
  ],
};

async function stubApis(page: import('@playwright/test').Page) {
  await page.route('**/api/portfolio', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PORTFOLIO) }),
  );
  await page.route('**/api/orders', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ last_sync: new Date().toISOString(), open_orders: [], executed_orders: [], open_count: 0, executed_count: 0 }),
    }),
  );
  await page.route('**/api/regime', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ score: 15, cri: { score: 15 } }) }),
  );
  await page.route('**/api/ib-status', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ connected: true }) }),
  );
  await page.route('**/api/blotter', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ as_of: new Date().toISOString(), summary: { realized_pnl: 0 }, closed_trades: [], open_trades: [] }),
    }),
  );
  await page.route('**/api/prices', (route) => route.abort());
}

test.describe('Stale option last price fix', () => {
  test('AAOI option last price should be close to bid/ask mid, not yesterday close', async ({ page }) => {
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    await stubApis(page);

    // Navigate to portfolio page
    await page.goto('/portfolio');

    // Wait for the position table to load
    await page.waitForSelector('table tbody tr', { timeout: 15000 });

    // Find the AAOI row
    const aaoi = page.locator('table tbody tr').filter({ hasText: 'AAOI' }).first();
    await expect(aaoi).toBeVisible({ timeout: 10000 });

    // The portfolio table can render one last-price cell for the underlying and
    // a second last-price cell for the option itself. Use the rightmost cell so
    // the assertion always targets the option contract price.
    const lastPriceCell = aaoi.locator('td.last-price-cell').last();
    await expect(lastPriceCell).not.toHaveText(/^\s*[—-]\s*$/, { timeout: 10000 });
    const lastPriceText = await lastPriceCell.textContent();

    // Extract numeric value
    const match = lastPriceText?.match(/\$?([\d,.]+)/);
    expect(match).not.toBeNull();
    const lastPrice = parseFloat(match![1].replace(',', ''));

    // The last price should NOT be $25.26 (yesterday's close)
    // It should be near the current bid/ask mid (~$10-$13 range)
    expect(lastPrice).toBeLessThan(20); // Definitely not $25.26
    expect(lastPrice).toBeGreaterThan(5); // Sanity: should be a reasonable option price
  });
});
