import { expect, test } from "@playwright/test";

function parsePrice(text: string | null): number {
  return Number((text ?? "").replace(/[$,]/g, ""));
}

function formatUsd(value: number): string {
  return `$${value.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

const PORTFOLIO_WITH_SINGLE_CALL = {
  bankroll: 100_000,
  peak_value: 100_000,
  last_sync: new Date().toISOString(),
  total_deployed_pct: 22.78,
  total_deployed_dollars: 22_775,
  remaining_capacity_pct: 77.22,
  position_count: 1,
  defined_risk_count: 1,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  exposure: {},
  violations: [],
  positions: [
    {
      id: 1,
      ticker: "AAOI",
      structure: "Long Call $105",
      structure_type: "Long Call",
      risk_profile: "defined",
      expiry: "2026-03-20",
      contracts: 25,
      direction: "LONG",
      entry_cost: 22_775,
      max_risk: 22_775,
      market_value: 36_675,
      market_price: 14.67,
      market_price_is_calculated: false,
      legs: [
        {
          direction: "LONG",
          contracts: 25,
          type: "Call",
          strike: 105,
          entry_cost: 22_775,
          avg_cost: 911,
          market_price: 14.67,
          market_price_is_calculated: false,
          market_value: 36_675,
        },
      ],
      kelly_optimal: null,
      target: null,
      stop: null,
      entry_date: "2026-03-05",
    },
  ],
};

const ORDERS = {
  last_sync: new Date().toISOString(),
  open_orders: [
    {
      orderId: 101,
      permId: 202,
      symbol: "AAOI",
      contract: {
        conId: 123456,
        symbol: "AAOI",
        secType: "OPT",
        strike: 105,
        right: "C",
        expiry: "2026-03-20",
      },
      action: "BUY",
      orderType: "LMT",
      totalQuantity: 25,
      limitPrice: 11.95,
      auxPrice: null,
      status: "Submitted",
      filled: 0,
      remaining: 25,
      avgFillPrice: null,
      tif: "DAY",
    },
  ],
  executed_orders: [],
  open_count: 1,
  executed_count: 0,
};

const PRICES = {
  AAOI: {
    symbol: "AAOI",
    last: 19.6,
    lastIsCalculated: false,
    bid: 19.55,
    ask: 19.65,
    bidSize: 20,
    askSize: 15,
    volume: 500_000,
    high: 20.3,
    low: 19.1,
    open: 19.25,
    close: 19.0,
    week52High: null,
    week52Low: null,
    avgVolume: null,
    delta: null,
    gamma: null,
    theta: null,
    vega: null,
    impliedVol: null,
    undPrice: null,
    timestamp: new Date().toISOString(),
  },
  AAOI_20260320_105_C: {
    symbol: "AAOI_20260320_105_C",
    last: 13.6,
    lastIsCalculated: false,
    bid: 12.8,
    ask: 14.4,
    bidSize: 10,
    askSize: 12,
    volume: 232,
    high: 15.89,
    low: 12.4,
    open: 15.1,
    close: 15.16,
    week52High: null,
    week52Low: null,
    avgVolume: null,
    delta: 0.48,
    gamma: 0.04,
    theta: -0.19,
    vega: 0.11,
    impliedVol: 0.54,
    undPrice: 19.6,
    timestamp: new Date().toISOString(),
  },
};

async function installMockWebSocket(page: import("@playwright/test").Page) {
  await page.addInitScript((prices) => {
    class MockWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      static CLOSING = 2;
      static CLOSED = 3;

      url: string;
      readyState = MockWebSocket.CONNECTING;
      onopen: ((event?: unknown) => void) | null = null;
      onmessage: ((event: { data: string }) => void) | null = null;
      onclose: ((event?: unknown) => void) | null = null;
      onerror: ((event?: unknown) => void) | null = null;

      addEventListener(type: string, listener: (event?: unknown) => void) {
        if (type === "open") this.onopen = listener;
        if (type === "message") this.onmessage = listener as (event: { data: string }) => void;
        if (type === "close") this.onclose = listener;
        if (type === "error") this.onerror = listener;
      }

      removeEventListener() {}

      constructor(url: string) {
        this.url = url;
        setTimeout(() => {
          this.readyState = MockWebSocket.OPEN;
          this.onopen?.({});
          this.emit({ type: "status", ib_connected: true, ib_issue: null, ib_status_message: null, subscriptions: [] });
        }, 0);
      }

      send(raw: string) {
        const message = JSON.parse(raw) as {
          action?: string;
          symbols?: string[];
          contracts?: Array<{ symbol: string; expiry: string; strike: number; right: "C" | "P" }>;
        };
        if (message.action !== "subscribe") return;

        const updates: Record<string, unknown> = {};
        for (const symbol of message.symbols ?? []) {
          if (prices[symbol]) updates[symbol] = prices[symbol];
        }
        for (const contract of message.contracts ?? []) {
          const expiry = String(contract.expiry).replace(/-/g, "");
          const key = `${String(contract.symbol).toUpperCase()}_${expiry}_${Number(contract.strike)}_${contract.right}`;
          if (prices[key]) updates[key] = prices[key];
        }
        if (Object.keys(updates).length > 0) this.emit({ type: "batch", updates });
      }

      close() {
        this.readyState = MockWebSocket.CLOSED;
        this.onclose?.({});
      }

      emit(payload: unknown) {
        this.onmessage?.({ data: JSON.stringify(payload) });
      }
    }

    // @ts-expect-error test-only replacement
    window.WebSocket = MockWebSocket;
  }, PRICES);
}

async function stubApis(page: import("@playwright/test").Page) {
  await page.route("**/api/portfolio", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(PORTFOLIO_WITH_SINGLE_CALL),
    }),
  );

  await page.route("**/api/orders", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(ORDERS),
    }),
  );

  await page.route("**/api/regime", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ score: 15, cri: { score: 15 } }),
    }),
  );

  await page.route("**/api/ib-status", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ connected: false }),
    }),
  );

  await page.route("**/api/blotter", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        as_of: new Date().toISOString(),
        summary: { realized_pnl: 0 },
        closed_trades: [],
        open_trades: [],
      }),
    }),
  );

  await page.route("**/api/ticker/**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        uw_info: { name: "Applied Optoelectronics, Inc.", sector: "Technology", description: "Test" },
        stock_state: {},
        profile: {},
        stats: {},
      }),
    }),
  );

  await page.route("**/api/prices", (route) => route.abort());
}

test.describe("Modify-order spread telemetry", () => {
  test("shows raw spread dollars and midpoint percent in the modify modal", async ({ page }) => {
    await page.unrouteAll({ behavior: "ignoreErrors" });
    await installMockWebSocket(page);
    await stubApis(page);

    await page.goto("/portfolio");

    const detailLink = page.locator('[aria-label="View details for AAOI"]').first();
    await detailLink.waitFor({ timeout: 10_000 });
    await detailLink.click();
    await page.waitForURL("**/AAOI**", { timeout: 5_000 });

    const tickerPage = page.locator(".ticker-detail-page");
    await tickerPage.waitFor({ timeout: 5_000 });

    const orderTab = tickerPage.locator(".ticker-tab", { hasText: /^Order/ }).first();
    await orderTab.click();

    const modifyButton = tickerPage.locator(".btn-modify").first();
    await modifyButton.waitFor({ timeout: 5_000 });
    await modifyButton.click();

    const modifyModal = page.locator(".modify-dialog");
    await modifyModal.waitFor({ timeout: 5_000 });

    const bidText = await modifyModal
      .locator(".modify-market-row")
      .filter({ hasText: "BID" })
      .locator(".modify-market-value")
      .textContent();
    const askText = await modifyModal
      .locator(".modify-market-row")
      .filter({ hasText: "ASK" })
      .locator(".modify-market-value")
      .textContent();
    const spreadValue = modifyModal
      .locator(".modify-market-row")
      .filter({ hasText: "SPREAD" })
      .locator(".modify-market-value");

    const bid = parsePrice(bidText);
    const ask = parsePrice(askText);
    const fullSpread = ask - bid;
    const mid = (bid + ask) / 2;
    const expectedSpread = `${formatUsd(fullSpread)} / ${((fullSpread / mid) * 100).toFixed(2)}%`;

    await expect(spreadValue).toHaveText(expectedSpread);
  });
});
