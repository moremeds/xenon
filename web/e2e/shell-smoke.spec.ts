import { test, expect } from "@playwright/test";

// Replaces the standalone prices-performance.test.js launcher: same checks
// (WorkspaceShell renders on `/`, under a 15s load bound), on the shared webServer.
test("workspace shell renders on / within 15s", async ({ page }) => {
  const start = Date.now();
  await page.goto("/");
  await expect(page.locator(".app-shell")).toBeVisible({ timeout: 15_000 });
  expect(Date.now() - start).toBeLessThan(15_000);
});
