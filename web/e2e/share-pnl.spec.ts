import { test, expect } from "@playwright/test";

test.describe("Share PnL", () => {
  // --- API route tests ---

  test("API route returns valid PNG for positive P&L", async ({ request }) => {
    const res = await request.get("/api/share/pnl?description=Long+AAOI+2026-04-17+Call+%2445.00&pnl=1234.56&pnlPct=47.5&commission=2.60&fillPrice=12.50&time=2026-03-10");
    expect(res.status()).toBe(200);
    expect(res.headers()["content-type"]).toContain("image/png");
    const body = await res.body();
    expect(body.length).toBeGreaterThan(1000);
    // PNG magic bytes
    expect(body[0]).toBe(0x89);
    expect(body[1]).toBe(0x50); // P
    expect(body[2]).toBe(0x4e); // N
    expect(body[3]).toBe(0x47); // G
  });

  test("API route returns valid PNG for negative P&L", async ({ request }) => {
    const res = await request.get("/api/share/pnl?description=Short+TSLA+Put&pnl=-500&pnlPct=-10");
    expect(res.status()).toBe(200);
    expect(res.headers()["content-type"]).toContain("image/png");
  });

  test("API route returns 400 when description missing", async ({ request }) => {
    const res = await request.get("/api/share/pnl?pnl=100");
    expect(res.status()).toBe(400);
  });

  test("API route handles pnl-only (no pnlPct)", async ({ request }) => {
    const res = await request.get("/api/share/pnl?description=Long+AAPL&pnl=100");
    expect(res.status()).toBe(200);
    expect(res.headers()["content-type"]).toContain("image/png");
  });

  test("API route handles pnlPct-only (no pnl)", async ({ request }) => {
    const res = await request.get("/api/share/pnl?description=Long+AAPL&pnlPct=25.5");
    expect(res.status()).toBe(200);
    expect(res.headers()["content-type"]).toContain("image/png");
  });

  // --- Share popover UI tests ---

  test("clicking share button opens popover with checkboxes", async ({ page }) => {
    await page.goto("http://127.0.0.1:3000/orders");
    await page.locator("text=Today's Executed Orders").waitFor({ timeout: 10000 });
    const shareBtn = page.locator(".share-pnl-button").first();
    if (await shareBtn.count() === 0) {
      test.skip();
      return;
    }
    await shareBtn.click();
    const popover = page.locator(".share-pnl-popover");
    await expect(popover).toBeVisible({ timeout: 3000 });
    // Should have two checkboxes
    const checkboxes = popover.locator("input[type='checkbox']");
    await expect(checkboxes).toHaveCount(2);
    // P&L $ should be off, P&L % on by default
    await expect(checkboxes.nth(0)).not.toBeChecked();
    await expect(checkboxes.nth(1)).toBeChecked();
  });

  test("popover has Copy & Tweet and Copy buttons", async ({ page }) => {
    await page.goto("http://127.0.0.1:3000/orders");
    await page.locator("text=Today's Executed Orders").waitFor({ timeout: 10000 });
    const shareBtn = page.locator(".share-pnl-button").first();
    if (await shareBtn.count() === 0) {
      test.skip();
      return;
    }
    await shareBtn.click();
    const popover = page.locator(".share-pnl-popover");
    await expect(popover).toBeVisible({ timeout: 3000 });
    // Should have a "Copy & Tweet" button and a "Copy" button
    await expect(popover.locator("button", { hasText: "Copy & Tweet" })).toBeVisible();
    await expect(popover.locator("button", { hasText: /^Copy$/ })).toBeVisible();
  });

  test("unchecking P&L $ disables it but keeps % checked", async ({ page }) => {
    await page.goto("/orders");
    await page.locator("text=Today's Executed Orders").waitFor({ timeout: 10000 });
    const shareBtn = page.locator(".share-pnl-button").first();
    if (await shareBtn.count() === 0) {
      test.skip();
      return;
    }
    await shareBtn.click();
    const popover = page.locator(".share-pnl-popover");
    await expect(popover).toBeVisible({ timeout: 3000 });
    const dollarCheckbox = popover.locator("input[type='checkbox']").nth(0);
    const pctCheckbox = popover.locator("input[type='checkbox']").nth(1);
    // Toggle states to verify % remains enabled
    await dollarCheckbox.uncheck();
    await expect(dollarCheckbox).not.toBeChecked();
    await expect(pctCheckbox).toBeChecked();
    await dollarCheckbox.check();
    await expect(dollarCheckbox).toBeChecked();
    await dollarCheckbox.uncheck();
    await expect(dollarCheckbox).not.toBeChecked();
  });

  test("popover closes when clicking outside", async ({ page }) => {
    await page.goto("/orders");
    await page.locator("text=Today's Executed Orders").waitFor({ timeout: 10000 });
    const shareBtn = page.locator(".share-pnl-button").first();
    if (await shareBtn.count() === 0) {
      test.skip();
      return;
    }
    await shareBtn.click();
    const popover = page.locator(".share-pnl-popover");
    await expect(popover).toBeVisible({ timeout: 3000 });
    // Click outside
    await page.locator("body").click({ position: { x: 10, y: 10 } });
    await expect(popover).not.toBeVisible({ timeout: 2000 });
  });

  // --- Historical trades ---

  test("share button appears on historical trades for closed trades", async ({ page }) => {
    await page.goto("/orders");
    const section = page.locator("text=Historical Trades");
    await expect(section).toBeVisible({ timeout: 15000 });
    const shareButtons = page.locator(".share-pnl-button");
    const count = await shareButtons.count();
    if (count > 0) {
      await expect(shareButtons.first()).toBeVisible();
    }
  });
});

const PNG_1X1 = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wn0p1sAAAAASUVORK5CYII=",
  "base64",
);

const PORTFOLIO_MOCK = {
  bankroll: 100_000,
  peak_value: 100_000,
  last_sync: new Date().toISOString(),
  total_deployed_pct: 0,
  total_deployed_dollars: 0,
  remaining_capacity_pct: 100,
  position_count: 0,
  defined_risk_count: 0,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  positions: [],
  exposure: {},
  violations: [],
  account_summary: {
    net_liquidation: 100_000,
    daily_pnl: null,
    unrealized_pnl: 0,
    realized_pnl: 0,
    settled_cash: 100_000,
    maintenance_margin: 0,
    excess_liquidity: 100_000,
    buying_power: 200_000,
    dividends: 0,
  },
};

const ORDERS_MOCK = {
  last_sync: new Date().toISOString(),
  open_orders: [],
  executed_orders: [
    {
      execId: "bag-unrelated",
      symbol: "AAOI",
      contract: { conId: 2001, symbol: "AAOI", secType: "BAG", strike: 0, right: "?", expiry: null },
      side: "BOT",
      quantity: 25,
      avgPrice: 0.25,
      commission: 0,
      realizedPNL: null,
      time: "2026-03-17T14:01:00+00:00",
      exchange: "SMART",
    },
    {
      execId: "call-unrelated",
      symbol: "AAOI",
      contract: { conId: 1901, symbol: "AAOI", secType: "OPT", strike: 92, right: "C", expiry: "2026-03-27" },
      side: "BOT",
      quantity: 25,
      avgPrice: 5.1,
      commission: -0.61,
      realizedPNL: 0,
      time: "2026-03-17T14:01:00+00:00",
      exchange: "SMART",
    },
    {
      execId: "put-unrelated",
      symbol: "AAOI",
      contract: { conId: 1902, symbol: "AAOI", secType: "OPT", strike: 88, right: "P", expiry: "2026-03-27" },
      side: "SLD",
      quantity: 25,
      avgPrice: 5.35,
      commission: -0.64,
      realizedPNL: 0,
      time: "2026-03-17T14:01:00+00:00",
      exchange: "SMART",
    },
    {
      execId: "open-call-1",
      symbol: "AAOI",
      contract: { conId: 861001, symbol: "AAOI", secType: "OPT", strike: 90, right: "C", expiry: "2026-03-27" },
      side: "BOT",
      quantity: 12,
      avgPrice: 5.59,
      commission: -8.40,
      realizedPNL: 0,
      time: "2026-03-17T14:14:16+00:00",
      exchange: "SMART",
    },
    {
      execId: "open-call-2",
      symbol: "AAOI",
      contract: { conId: 861001, symbol: "AAOI", secType: "OPT", strike: 90, right: "C", expiry: "2026-03-27" },
      side: "BOT",
      quantity: 13,
      avgPrice: 5.59,
      commission: -9.11,
      realizedPNL: 0,
      time: "2026-03-17T14:14:16+00:00",
      exchange: "SMART",
    },
    {
      execId: "open-put-1",
      symbol: "AAOI",
      contract: { conId: 858539, symbol: "AAOI", secType: "OPT", strike: 85, right: "P", expiry: "2026-03-27" },
      side: "SLD",
      quantity: 13,
      avgPrice: 6.34,
      commission: -9.12,
      realizedPNL: 0,
      time: "2026-03-17T14:12:25+00:00",
      exchange: "SMART",
    },
    {
      execId: "open-put-2",
      symbol: "AAOI",
      contract: { conId: 858539, symbol: "AAOI", secType: "OPT", strike: 85, right: "P", expiry: "2026-03-27" },
      side: "SLD",
      quantity: 12,
      avgPrice: 6.34,
      commission: -8.41,
      realizedPNL: 0,
      time: "2026-03-17T14:12:25+00:00",
      exchange: "SMART",
    },
    {
      execId: "close-bag",
      symbol: "AAOI",
      contract: { conId: 2002, symbol: "AAOI", secType: "BAG", strike: 0, right: "?", expiry: null },
      side: "BOT",
      quantity: 25,
      avgPrice: 1.0,
      commission: 0,
      realizedPNL: null,
      time: "2026-03-17T15:16:13+00:00",
      exchange: "SMART",
    },
    {
      execId: "close-call",
      symbol: "AAOI",
      contract: { conId: 861001, symbol: "AAOI", secType: "OPT", strike: 90, right: "C", expiry: "2026-03-27" },
      side: "SLD",
      quantity: 25,
      avgPrice: 5.33,
      commission: -1.03,
      realizedPNL: 2200,
      time: "2026-03-17T15:16:13+00:00",
      exchange: "SMART",
    },
    {
      execId: "close-put",
      symbol: "AAOI",
      contract: { conId: 858539, symbol: "AAOI", secType: "OPT", strike: 85, right: "P", expiry: "2026-03-27" },
      side: "BOT",
      quantity: 25,
      avgPrice: 7.83,
      commission: -1.03,
      realizedPNL: 2137.9,
      time: "2026-03-17T15:16:13+00:00",
      exchange: "SMART",
    },
  ],
  open_count: 0,
  executed_count: 9,
};

async function stubOrdersShareApis(page: import("@playwright/test").Page) {
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { write: async () => undefined },
    });
    // @ts-expect-error test shim
    window.ClipboardItem = class ClipboardItem {
      items: Record<string, Blob>;

      constructor(items: Record<string, Blob>) {
        this.items = items;
      }
    };
  });

  await page.route("**/api/portfolio", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(PORTFOLIO_MOCK) }),
  );
  await page.route("**/api/orders", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(ORDERS_MOCK) }),
  );
  await page.route("**/api/blotter", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        as_of: new Date().toISOString(),
        summary: { closed_trades: 0, open_trades: 0, total_commissions: 0, realized_pnl: 0 },
        closed_trades: [],
        open_trades: [],
      }),
    }),
  );
  await page.route("**/api/ib-status", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ connected: true }) }),
  );
  await page.route("**/api/prices", (route) => route.abort());
}

test.describe("Share PnL signed combo basis", () => {
  test("uses the matching opening legs for signed risk-reversal entry basis", async ({ page }) => {
    await stubOrdersShareApis(page);

    let shareRequestUrl: string | null = null;
    await page.route("**/api/share/pnl?*", (route) => {
      shareRequestUrl = route.request().url();
      return route.fulfill({ status: 200, contentType: "image/png", body: PNG_1X1 });
    });

    await page.goto("http://127.0.0.1:3000/orders");

    const shareButton = page.locator(".share-pnl-button").first();
    await expect(shareButton).toBeVisible({ timeout: 10_000 });

    const closedRow = shareButton.locator("xpath=ancestor::tr[1]");
    await expect(closedRow).toContainText("Risk Reversal (Short $85 Put / Long $90 Call)");
    await expect(closedRow).toContainText("$1.00");
    await shareButton.click();
    const popover = page.locator(".share-pnl-popover");
    await expect(popover).toBeVisible();
    await popover.getByRole("button", { name: /^Copy$/ }).click();

    await expect.poll(() => shareRequestUrl).not.toBeNull();

    const params = new URL(shareRequestUrl ?? "http://localhost").searchParams;
    expect(params.get("entryPrice")).toBe("-0.75");
    expect(params.get("exitPrice")).toBe("1");
    expect(Number(params.get("pnlPct"))).toBeCloseTo(231.35, 2);
  });
});
