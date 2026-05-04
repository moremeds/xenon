import { expect, test } from "@playwright/test";

test("workspace shell renders within the performance budget", async ({ page }) => {
  const startTime = Date.now();
  await page.goto("/", { waitUntil: "networkidle", timeout: 30_000 });
  const loadTime = Date.now() - startTime;

  await expect(page.locator(".app-shell")).toBeVisible();
  expect(loadTime).toBeLessThanOrEqual(15_000);
});
