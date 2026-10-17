import { expect, test } from "@playwright/test";

test("portfolio uses the resolved realtime websocket config without the old localhost:8765 403 failure", async ({
  page,
}) => {
  const consoleErrors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") {
      consoleErrors.push(msg.text());
    }
  });

  await page.goto("/portfolio");

  await expect(page.getByText("CONNECTED", { exact: true })).toBeVisible({
    timeout: 20_000,
  });
  await expect(
    page.getByRole("button", { name: /Switch to IB account/i }),
  ).not.toContainText("DOWN");
  await expect(page.getByText("Waiting for portfolio data...")).toHaveCount(0);

  const wsConfig = await page.evaluate(async () => {
    const res = await fetch("/api/ib/ws-config", { cache: "no-store" });
    return (await res.json()) as { url: string };
  });

  expect(wsConfig.url).toMatch(/^ws:\/\/(localhost|127\.0\.0\.1):\d+$/);
  expect(
    consoleErrors.some(
      (msg) => msg.includes("ws://localhost:8765") && msg.includes("403"),
    ),
  ).toBe(false);
});
