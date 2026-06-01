import { test, expect } from "@playwright/test";

test.describe("Futu history surfaces", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await page.getByRole("tab", { name: /Futu/i }).click();
  });

  test("renders equity curve + trade table + perf stats", async ({ page }) => {
    await expect(page.getByTestId("futu-equity-curve")).toBeVisible({
      timeout: 10_000,
    });
    await expect(page.getByText("TWR")).toBeVisible();
    await expect(page.getByText("Ticker")).toBeVisible();
  });

  test("sync-now button refreshes data", async ({ page }) => {
    const syncBtn = page.getByRole("button", { name: /sync now/i });
    await syncBtn.click();
    await expect(syncBtn).toHaveText(/syncing/i);
    // Returns to "Sync now" within 30s once the request settles.
    await expect(syncBtn).toHaveText(/sync now/i, { timeout: 30_000 });
  });

  test("cross-broker chart toggles combined", async ({ page }) => {
    await page.goto("/"); // workspace position above tabs
    const chart = page.getByTestId("equity-curve-cross-broker");
    await expect(chart).toBeVisible();
    const toggle = page.getByLabel(/combined/i);
    await toggle.check();
    await expect(toggle).toBeChecked();
  });
});
