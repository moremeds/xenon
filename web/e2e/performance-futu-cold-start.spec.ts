/**
 * E2E: Futu performance tab on a brand-new account (< 5 NAV sessions).
 *
 * The backend returns status="insufficient_history" + reason="collecting" plus
 * a hero_net_liq snapshot so the cold-start envelope can show the user where
 * they stand without metrics being possible yet.
 */
import { expect, test } from "@playwright/test";

const INSUFFICIENT_FUTU = {
  status: "insufficient_history",
  reason: "collecting",
  days_collected: 2,
  days_required_for_curve: 5,
  days_required_for_metrics: 30,
  inception_date: "2026-05-30",
  hero_net_liq: 148_237.42,
  currency: "USD",
  last_sync: "2026-06-01T20:00:00Z",
  as_of: "2026-06-01",
};

test("Futu cold-start performance renders COLLECTING envelope, not crash", async ({
  page,
}) => {
  await page.route("**/api/performance**", async (route) => {
    const url = new URL(route.request().url());
    if (url.searchParams.get("broker") === "FUTU") {
      await route.fulfill({
        status: 200,
        body: JSON.stringify(INSUFFICIENT_FUTU),
        contentType: "application/json",
      });
    } else {
      await route.continue();
    }
  });

  await page.goto("/performance");
  // Default IB tab loads first — switch to Futu.
  await page.getByRole("button", { name: /futu/i }).click();

  const panel = page.getByTestId("performance-panel-insufficient");
  await expect(panel).toBeVisible();
  await expect(panel).toContainText("2"); // days_collected
  await expect(panel).toContainText("5 sessions"); // required for curve
  await expect(panel).toContainText("$148,237.42"); // hero_net_liq formatted
  await expect(panel).toContainText("inception 2026-05-30");
  await expect(panel).toContainText("30 sessions"); // metrics unlock target
});

test("Futu Open-D unreachable surfaces a 503 detail", async ({ page }) => {
  await page.route("**/api/performance**", async (route) => {
    const url = new URL(route.request().url());
    if (url.searchParams.get("broker") === "FUTU") {
      await route.fulfill({
        status: 503,
        body: JSON.stringify({
          error: "Xenon API 503: Futu OpenD unreachable",
          detail: "Futu OpenD unreachable",
        }),
        contentType: "application/json",
      });
    } else {
      await route.continue();
    }
  });

  await page.goto("/performance");
  await page.getByRole("button", { name: /futu/i }).click();
  // Panel renders the error string returned by the hook (useSyncHook surfaces `error`)
  await expect(page.getByText(/OpenD unreachable/i)).toBeVisible();
});
