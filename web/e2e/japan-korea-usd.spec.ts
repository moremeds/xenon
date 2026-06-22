import { expect, test } from "@playwright/test";

// Real IB snapshot, 2026-06-22 (no synthetic data):
//   5016 JX Advanced Metals (TSEJ/JPY): 100 sh, prior-close ¥4,747, last ¥5,267
//   000660 SK Hynix (KRX/KRW): 1 sh, prior-close ₩2,764,000, last ₩2,885,000
//   USD.JPY 161.7 → usd_per_unit[JPY] = 0.0061845 ; USD.KRW 1537 → 0.0006507
//   (FX rates harvested live from IB ExchangeRate, verified read-only.)
const FX = { USD: 1, JPY: 0.0061845, KRW: 0.0006507 };

const PORTFOLIO_MOCK = {
  bankroll: 250_000,
  peak_value: 250_000,
  last_sync: new Date().toISOString(),
  base_currency: "USD",
  fx_rates: FX,
  fx_unconverted_count: 0,
  total_deployed_pct: 2.3,
  total_deployed_dollars: 5_734,
  remaining_capacity_pct: 97.7,
  position_count: 2,
  defined_risk_count: 0,
  undefined_risk_count: 2,
  avg_kelly_optimal: null,
  positions: [
    {
      id: 1,
      ticker: "5016",
      currency: "JPY",
      exchange: "TSEJ",
      structure: "Stock (100 shares)",
      structure_type: "Stock",
      risk_profile: "equity",
      expiry: "N/A",
      contracts: 100,
      direction: "LONG",
      entry_cost: 474_700,
      entry_cost_usd: 2_935.8,
      max_risk: null,
      market_value: 526_700,
      market_value_usd: 3_257.4,
      kelly_optimal: null,
      target: null,
      stop: null,
      entry_date: "2026-05-01",
      legs: [
        {
          direction: "LONG",
          contracts: 100,
          type: "Stock",
          currency: "JPY",
          strike: null,
          entry_cost: 474_700,
          avg_cost: 4_747,
          market_price: 5_267,
          market_value: 526_700,
          market_value_usd: 3_257.4,
        },
      ],
    },
    {
      id: 2,
      ticker: "000660",
      currency: "KRW",
      exchange: "KRX",
      structure: "Stock (1 share)",
      structure_type: "Stock",
      risk_profile: "equity",
      expiry: "N/A",
      contracts: 1,
      direction: "LONG",
      entry_cost: 2_764_000,
      entry_cost_usd: 1_798.5,
      max_risk: null,
      market_value: 2_885_000,
      market_value_usd: 1_877.3,
      kelly_optimal: null,
      target: null,
      stop: null,
      entry_date: "2026-05-01",
      legs: [
        {
          direction: "LONG",
          contracts: 1,
          type: "Stock",
          currency: "KRW",
          strike: null,
          entry_cost: 2_764_000,
          avg_cost: 2_764_000,
          market_price: 2_885_000,
          market_value: 2_885_000,
          market_value_usd: 1_877.3,
        },
      ],
    },
  ],
  exposure: {},
  violations: [],
  account_summary: {
    net_liquidation: 250_000,
    daily_pnl: 0,
    unrealized_pnl: 0,
    realized_pnl: 0,
    settled_cash: 244_266,
    maintenance_margin: 0,
    excess_liquidity: 250_000,
    buying_power: 500_000,
    dividends: null,
  },
};

const ORDERS_EMPTY = { orders: [], as_of: new Date().toISOString() };

// Live ticks the mock WS pushes on open — drives the live USD headline, the
// per-pair "live" FX dot, and the TODAY'S P&L Day Move (last vs prior close).
// Real levels (2026-06-22): 5016 prior-close ¥4,747 → +¥52,000 day move;
// 000660 prior-close ₩2,764,000 → +₩121,000. Converted: +$322 + $79 ≈ +$400.
const PRICE_TICKS: Record<string, { last: number; close: number }> = {
  "USD.JPY": { last: 161.7, close: 161.7 },
  "USD.KRW": { last: 1537, close: 1537 },
  "5016": { last: 5267, close: 4747 },
  "000660": { last: 2_885_000, close: 2_764_000 },
};

async function installMockWebSocket(page: import("@playwright/test").Page) {
  await page.addInitScript((ticks) => {
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
          this.onmessage?.({
            data: JSON.stringify({
              type: "status",
              ib_connected: true,
              ib_issue: null,
              ib_status_message: null,
              subscriptions: [],
            }),
          });
          for (const [symbol, tick] of Object.entries(
            ticks as Record<string, { last: number; close: number }>,
          )) {
            const { last, close } = tick;
            this.onmessage?.({
              data: JSON.stringify({
                type: "price",
                symbol,
                data: {
                  symbol,
                  last,
                  lastIsCalculated: false,
                  bid: last,
                  ask: last,
                  bidSize: 1,
                  askSize: 1,
                  volume: 1,
                  high: last,
                  low: last,
                  open: last,
                  close,
                  timestamp: new Date().toISOString(),
                },
              }),
            });
          }
        }, 0);
      }
      send() {}
      close() {
        this.readyState = MockWebSocket.CLOSED;
        this.onclose?.({});
      }
    }
    // @ts-expect-error test-only replacement
    window.WebSocket = MockWebSocket;
  }, PRICE_TICKS);
}

async function stubApis(page: import("@playwright/test").Page) {
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await page.route("**/api/portfolio", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(PORTFOLIO_MOCK),
    }),
  );
  await page.route("**/api/orders", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(ORDERS_EMPTY),
    }),
  );
  await page.route("**/api/regime", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ score: 15, level: "LOW", cri: { score: 15 } }),
    }),
  );
  await page.route("**/api/ib-status", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ connected: true }),
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
}

test("foreign positions render native price + live USD headline + FX badge", async ({
  page,
}) => {
  await installMockWebSocket(page);
  await stubApis(page);

  await page.goto("/portfolio");

  const jpRow = page
    .locator("table tbody tr")
    .filter({ hasText: "5016" })
    .first();
  await expect(jpRow).toBeVisible();
  const krRow = page
    .locator("table tbody tr")
    .filter({ hasText: "000660" })
    .first();
  await expect(krRow).toBeVisible();

  // Native sub-line (¥ / ₩) present on the foreign MV cells.
  await expect(
    page.locator(".fx-native-subline").filter({ hasText: "¥526,700" }).first(),
  ).toBeVisible();
  await expect(
    page
      .locator(".fx-native-subline")
      .filter({ hasText: "₩2,885,000" })
      .first(),
  ).toBeVisible();

  // USD headline ($) on the same rows (live: 526,700/161.7 ≈ $3,257).
  await expect(jpRow.getByText(/\$3,257/)).toBeVisible();
  await expect(krRow.getByText(/\$1,87/)).toBeVisible();

  // FX badge shows both pairs.
  await expect(page.getByText(/USD\/JPY/).first()).toBeVisible();
  await expect(page.getByText(/USD\/KRW/).first()).toBeVisible();

  // TODAY'S P&L Day Move headline is the USD sum (+¥52,000→$322 plus
  // +₩121,000→$79 ≈ +$400), NEVER the native magnitude (+$173,000 / +$52,000 /
  // +$121,000) — the −$552,137 native-leak this sweep fixes.
  await expect(page.getByText("+$400").first()).toBeVisible();
  await expect(page.getByText(/\$(52,000|121,000|173,000)/)).toHaveCount(0);

  await page.screenshot({
    path: "../output/playwright/japan-korea-usd-2026-06-22.png",
    fullPage: true,
  });
});
