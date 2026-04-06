import { test, expect } from "@playwright/test";

const PORTFOLIO = {
  bankroll: 1_098_051.01,
  peak_value: 1_098_051.01,
  last_sync: new Date().toISOString(),
  total_deployed_pct: 3.65,
  total_deployed_dollars: 40_076.51,
  remaining_capacity_pct: 96.35,
  position_count: 1,
  defined_risk_count: 1,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  exposure: {},
  violations: [],
  positions: [
    {
      id: 23,
      ticker: "WULF",
      structure: "Long Call",
      structure_type: "Long Call",
      risk_profile: "defined",
      expiry: "2027-01-15",
      contracts: 77,
      direction: "LONG",
      entry_cost: 40_076.51,
      max_risk: 40_076.51,
      market_value: 34_650.0,
      market_price_is_calculated: false,
      ib_daily_pnl: -3_688.02,
      legs: [
        {
          direction: "LONG",
          contracts: 77,
          type: "Call",
          strike: 17,
          entry_cost: 40_076.51,
          avg_cost: 520.4741844,
          market_price: 4.5,
          market_value: 34_650.0,
          market_price_is_calculated: false,
        },
      ],
      kelly_optimal: null,
      target: null,
      stop: null,
      entry_date: "2026-03-19",
    },
  ],
  account_summary: {
    net_liquidation: 1_098_051.01,
    daily_pnl: -3_688.02,
    unrealized_pnl: -5_426.51,
    realized_pnl: 0,
    settled_cash: 206_956.63,
    maintenance_margin: 247_662.16,
    excess_liquidity: 476_727.23,
    buying_power: 1_906_908.93,
    dividends: 0,
  },
};

const ORDERS = {
  last_sync: new Date().toISOString(),
  open_orders: [],
  executed_orders: [],
  open_count: 0,
  executed_count: 0,
};

const PRICE_FIXTURES = {
  WULF: {
    symbol: "WULF",
    last: 12.4,
    lastIsCalculated: false,
    bid: 12.35,
    ask: 12.45,
    bidSize: 10,
    askSize: 10,
    volume: 1000,
    high: null,
    low: null,
    open: null,
    close: 12.1,
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
  WULF_20270115_17_C: {
    symbol: "WULF_20270115_17_C",
    last: 4.5,
    lastIsCalculated: false,
    bid: 4.45,
    ask: 4.55,
    bidSize: 12,
    askSize: 14,
    volume: 180,
    high: null,
    low: null,
    open: null,
    close: 4.41,
    week52High: null,
    week52Low: null,
    avgVolume: null,
    delta: 0.52,
    gamma: null,
    theta: null,
    vega: null,
    impliedVol: 0.81,
    undPrice: 12.4,
    timestamp: new Date().toISOString(),
  },
};

function installMockWebSocket(page: import("@playwright/test").Page) {
  return page.addInitScript((priceFixtures) => {
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

      constructor(url: string) {
        this.url = url;
        setTimeout(() => {
          this.readyState = MockWebSocket.OPEN;
          this.onopen?.({});
          this.emit({
            type: "status",
            ib_connected: true,
            ib_issue: null,
            ib_status_message: null,
            subscriptions: [],
          });
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
          if (priceFixtures[symbol]) updates[symbol] = priceFixtures[symbol];
        }
        for (const contract of message.contracts ?? []) {
          const expiry = String(contract.expiry).replace(/-/g, "");
          const key = `${String(contract.symbol).toUpperCase()}_${expiry}_${Number(contract.strike)}_${contract.right}`;
          if (priceFixtures[key]) updates[key] = priceFixtures[key];
        }

        if (Object.keys(updates).length > 0) {
          this.emit({ type: "batch", updates });
        }
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
  }, PRICE_FIXTURES);
}

function stubApis(page: import("@playwright/test").Page) {
  page.route("**/api/portfolio", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(PORTFOLIO) }),
  );
  page.route("**/api/orders", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(ORDERS) }),
  );
  page.route("**/api/regime", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ score: 15, cri: { score: 15 } }) }),
  );
  page.route("**/api/ib-status", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ connected: true }) }),
  );
  page.route("**/api/blotter", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ as_of: new Date().toISOString(), summary: { realized_pnl: 0 }, closed_trades: [], open_trades: [] }),
    }),
  );
}

test("portfolio day move prefers IB daily P&L over positive mark-to-close math for same-day WULF position", async ({ page }) => {
  await installMockWebSocket(page);
  stubApis(page);

  await page.goto("http://127.0.0.1:3000/portfolio");

  const todayPnlRow = page.locator(".metrics-grid-3").filter({ hasText: "Day Move" }).first();
  await expect(todayPnlRow).toContainText("Day Move");
  await expect(todayPnlRow).toContainText("-$3,688");
  await expect(todayPnlRow).toContainText("Total");
  await expect(todayPnlRow).toContainText("-$3,688");

  await page.locator(".metric-card", { hasText: "Day Move" }).first().click();
  const modal = page.locator(".modal-content");
  await expect(modal).toBeVisible();
  await expect(modal).toContainText("IB reqPnLSingle");
  await expect(modal).toContainText("WULF");
  await expect(modal).toContainText("$4.41");
  await expect(modal).toContainText("$4.50");
  await expect(modal).toContainText("-$3,688.02");
});
