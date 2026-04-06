import { expect, test } from "@playwright/test";

const TODAY = "2026-03-24";

const PORTFOLIO_MOCK = {
  bankroll: 1_000_000,
  peak_value: 1_000_000,
  last_sync: "2026-03-24T14:34:25Z",
  total_deployed_pct: 0.16,
  total_deployed_dollars: -1571.92,
  remaining_capacity_pct: 99.84,
  position_count: 1,
  defined_risk_count: 0,
  undefined_risk_count: 1,
  avg_kelly_optimal: null,
  exposure: {},
  violations: [],
  account_summary: {
    net_liquidation: 1_000_000,
    daily_pnl: -188.08,
    unrealized_pnl: -188.08,
    realized_pnl: 0,
    settled_cash: 100_000,
    maintenance_margin: 0,
    excess_liquidity: 100_000,
    buying_power: 400_000,
    dividends: 0,
  },
  positions: [
    {
      id: 16,
      ticker: "PLTR",
      structure: "Risk Reversal (P$152.5/C$155.0)",
      structure_type: "Risk Reversal",
      risk_profile: "undefined",
      expiry: "2026-03-27",
      contracts: 20,
      direction: "COMBO",
      entry_cost: -1571.92,
      max_risk: null,
      market_value: -1760.0,
      market_price_is_calculated: false,
      ib_daily_pnl: null,
      entry_date: TODAY,
      kelly_optimal: null,
      target: null,
      stop: null,
      legs: [
        {
          direction: "LONG",
          contracts: 20,
          type: "Call",
          strike: 155.0,
          entry_cost: 5034.01,
          avg_cost: 251.70045,
          market_price: 2.48,
          market_value: 4960.0,
          market_price_is_calculated: false,
        },
        {
          direction: "SHORT",
          contracts: 20,
          type: "Put",
          strike: 152.5,
          entry_cost: 6605.93,
          avg_cost: 330.29626,
          market_price: 3.36,
          market_value: 6720.0,
          market_price_is_calculated: false,
        },
      ],
    },
  ],
};

const ORDERS_EMPTY = {
  last_sync: "2026-03-24T14:34:25Z",
  open_orders: [],
  executed_orders: [],
  open_count: 0,
  executed_count: 0,
};

async function freezeToTradingDay(page: import("@playwright/test").Page) {
  await page.addInitScript((iso) => {
    const fixedNow = new Date(iso).valueOf();
    const RealDate = Date;
    class MockDate extends RealDate {
      constructor(...args: ConstructorParameters<typeof Date>) {
        if (args.length === 0) {
          super(fixedNow);
          return;
        }
        super(...args);
      }
      static now() {
        return fixedNow;
      }
    }
    Object.defineProperty(window, "Date", {
      value: MockDate,
      configurable: true,
      writable: true,
    });
  }, `${TODAY}T14:34:25Z`);
}

async function installMockWebSocket(page: import("@playwright/test").Page) {
  await page.addInitScript(() => {
    class MockWebSocket {
      public static OPEN = 1;
      public url: string;
      public readyState = 0;
      public onopen: ((event: Event) => void) | null = null;
      public onmessage: ((event: MessageEvent<string>) => void) | null = null;
      public onclose: ((event: Event) => void) | null = null;
      public onerror: ((event: Event) => void) | null = null;

      constructor(url: string) {
        this.url = url;
        window.setTimeout(() => {
          this.readyState = MockWebSocket.OPEN;
          this.onopen?.(new Event("open"));
        }, 0);
        window.setTimeout(() => {
          this.onmessage?.({
            data: JSON.stringify({
              type: "status",
              ib_connected: true,
              ib_issue: null,
              ib_status_message: null,
              subscriptions: [],
            }),
          } as MessageEvent<string>);
        }, 10);
      }

      send(_message: string) {}

      close() {
        this.readyState = 3;
        this.onclose?.(new Event("close"));
      }
    }

    Object.defineProperty(window, "WebSocket", {
      configurable: true,
      writable: true,
      value: MockWebSocket,
    });
  });
}

async function setupMocks(page: import("@playwright/test").Page) {
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await freezeToTradingDay(page);
  await installMockWebSocket(page);

  await page.route("**/api/portfolio", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(PORTFOLIO_MOCK) }),
  );
  await page.route("**/api/orders", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(ORDERS_EMPTY) }),
  );
  await page.route("**/api/flex-token", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ok: true, days_until_expiry: 14 }) }),
  );
  await page.route("**/api/ib-status", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ connected: true }) }),
  );
  await page.route("**/api/regime", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        scan_time: "2026-03-24T14:34:25Z",
        market_open: true,
        date: TODAY,
        vix: 27.63,
        vvix: 122.82,
        spy: 677.69,
        vix_5d_roc: 5.66,
        vvix_vix_ratio: 4.44,
        realized_vol: 11.72,
        cor1m: 38.0,
        cor1m_5d_change: 1.0,
        spx_100d_ma: 682.05,
        spx_distance_pct: -0.64,
        spy_closes: Array.from({ length: 22 }, (_, i) => 660 + i),
        cri: { score: 24, level: "LOW", components: { vix: 6, vvix: 5, correlation: 7, momentum: 6 } },
        cta: { exposure_pct: 95, forced_reduction_pct: 0, est_selling_bn: 1.2, realized_vol: 11.72 },
        crash_trigger: { triggered: false, conditions: { spx_below_100d_ma: false, realized_vol_gt_25: false, cor1m_gt_60: false } },
        history: [],
      }),
    }),
  );
  await page.route("**/api/blotter", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ as_of: "2026-03-24T14:34:25Z", summary: { realized_pnl: 0 }, closed_trades: [], open_trades: [] }),
    }),
  );
  await page.route("**/api/prices**", (route) => route.abort());
}

test.describe("/portfolio same-day combo today pnl", () => {
  test("renders a same-day risk reversal using entry-cost-based today pnl", async ({ page }) => {
    await setupMocks(page);
    await page.goto("http://127.0.0.1:3000/portfolio");

    const row = page.locator("tr", { hasText: "PLTR" }).first();
    await expect(row).toContainText("-$188");
    await expect(row).toContainText("-$188 (-12.0%)");
    await expect(row).not.toContainText("-$14,440");
  });
});
