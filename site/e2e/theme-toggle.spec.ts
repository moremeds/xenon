import { test, expect } from "../../web/node_modules/@playwright/test";

test.describe("site theme toggle", () => {
  test("respects a saved light theme on first load", async ({ page }) => {
    await page.addInitScript(() => {
      localStorage.setItem("theme", "light");
    });

    await page.goto("/");

    await expect(page.locator('[data-testid="site-theme-toggle"]')).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  });

  test("toggles from dark to light and persists the new theme", async ({ page }) => {
    await page.addInitScript(() => {
      localStorage.setItem("theme", "dark");
    });

    await page.goto("/");

    const toggle = page.locator('[data-testid="site-theme-toggle"]');
    await expect(toggle).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");

    await toggle.click();

    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    await expect(toggle).toHaveAttribute("title", /dark mode/i);

    const storedTheme = await page.evaluate(() => localStorage.getItem("theme"));
    expect(storedTheme).toBe("light");
  });
});
