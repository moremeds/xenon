import { expect, test } from "@playwright/test";

test.describe("Performance period selector", () => {
  test("defaults to YTD and switches to 1M", async ({ page }) => {
    await page.goto("/performance");
    await page.waitForLoadState("networkidle");

    // Selector is mounted.
    const selector = page.getByTestId("performance-period-selector");
    await expect(selector).toBeVisible();

    // YTD active by default.
    await expect(page.getByTestId("performance-period-YTD")).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    // Click 1M, confirm pressed-state and that a fresh /api/performance fires.
    const apiCall = page.waitForRequest(
      (req) =>
        req.url().includes("/api/performance") &&
        req.url().includes("period=1M"),
    );
    await page.getByTestId("performance-period-1M").click();
    await apiCall;
    await expect(page.getByTestId("performance-period-1M")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await expect(page.getByTestId("performance-period-YTD")).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  test("headline tooltip reveals TWR / IRR / net deposits", async ({
    page,
  }) => {
    await page.goto("/performance");
    await page.waitForLoadState("networkidle");

    const info = page.getByTestId("performance-headline-info");
    await expect(info).toBeVisible();

    await info.hover();
    const tooltip = page.getByTestId("performance-headline-tooltip");
    await expect(tooltip).toBeVisible();
    await expect(tooltip).toContainText("Simple (flow-adj)");
    await expect(tooltip).toContainText("Time-Weighted");
    await expect(tooltip).toContainText("Money-Weighted");
    await expect(tooltip).toContainText("Net deposits");
  });

  test("freshness subtitle shows source mix", async ({ page }) => {
    await page.goto("/performance");
    await page.waitForLoadState("networkidle");

    const freshness = page.getByTestId("performance-freshness");
    await expect(freshness).toBeVisible();
    // At least one of intraday / close / no data must appear.
    await expect(freshness).toContainText(/intraday|close|no data/);
  });
});
