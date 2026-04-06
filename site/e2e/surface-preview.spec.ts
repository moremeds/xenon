import { expect, test } from "../../web/node_modules/@playwright/test";

test.describe("site surface preview metrics", () => {
  test("keeps the Radon Performance metric value inside its own tile", async ({ page }) => {
    await page.setViewportSize({ width: 2048, height: 900 });
    await page.goto("/");

    const performanceCard = page.locator("article").filter({
      has: page.getByText("Radon Performance"),
    }).first();
    await expect(performanceCard).toBeVisible();

    const metricsGrid = performanceCard.locator("div.grid").first();
    const metricTiles = metricsGrid.locator(":scope > div");
    await expect(metricTiles).toHaveCount(2);

    const metricsTile = metricTiles.nth(0);
    const anchorTile = metricTiles.nth(1);
    const metricsValue = metricsTile.locator(".mono-metric-value");

    const [metricsTileBox, anchorTileBox, metricsValueBox] = await Promise.all([
      metricsTile.boundingBox(),
      anchorTile.boundingBox(),
      metricsValue.boundingBox(),
    ]);

    expect(metricsTileBox).not.toBeNull();
    expect(anchorTileBox).not.toBeNull();
    expect(metricsValueBox).not.toBeNull();

    const metricsRightEdge = metricsValueBox!.x + metricsValueBox!.width;
    const tileRightEdge = metricsTileBox!.x + metricsTileBox!.width;
    const tileDividerGap = tileRightEdge - metricsRightEdge;
    const neighborDividerGap = anchorTileBox!.x - metricsRightEdge;

    expect(tileDividerGap).toBeGreaterThanOrEqual(12);
    expect(neighborDividerGap).toBeGreaterThanOrEqual(12);
  });
});
