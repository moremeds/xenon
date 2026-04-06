import { test, expect } from '@playwright/test';

/**
 * E2E verification: AAOI option last price should not show yesterday's close
 * when bid/ask indicate a different current price.
 *
 * Bug: AAOI $105C showed Last Price = $25.26 (yesterday's close) despite
 * bid=$10.30 ask=$11.70 because IB sends frozen LAST = close via reqMarketDataType(4).
 */

test.describe('Stale option last price fix', () => {
  test('AAOI option last price should be close to bid/ask mid, not yesterday close', async ({ page }) => {
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
