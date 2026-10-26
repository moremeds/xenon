import { expect, test } from "@playwright/test";

// X7b browser E2E (contract-x7 / CLAUDE.md rule 2) — no real relay, no IB, no
// network: a MockWebSocket replays clearly SYNTHETIC Futu-shaped price rows
// ({source:"futu", stale:boolean, ...}) and records the subscribe frames the
// page sends so the held/view tier tagging can be asserted.
//
//   SYNQ  — stock position, stale:true  → row shows .price-stale-dot
//   FRSQ  — stock position, stale:false → no dot
//   SPRD  — Bull Call Spread $100/$110: LONG leg quote stale:true (dot on the
//           leg row + the spread row), SHORT leg quote stale:false (no dot)
//
// All fixture values are synthetic, chosen only to be unambiguous in rendered
// text. The leg `last` values sit inside their bid/ask so resolveRealtimePrice
// serves `last` rather than the mid.

const PORTFOLIO_MOCK = {
  bankroll: 1_000_000,
  peak_value: 1_000_000,
  last_sync: new Date().toISOString(),
  total_deployed_pct: 5,
  total_deployed_dollars: 50_000,
  remaining_capacity_pct: 95,
  position_count: 3,
  defined_risk_count: 1,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  positions: [
    {
      id: 1,
      ticker: "SYNQ",
      structure: "Stock",
      structure_type: "Stock",
      risk_profile: "equity",
      expiry: "",
      contracts: 100,
      direction: "LONG",
      entry_cost: 11_000,
      max_risk: null,
      market_value: 12_340,
      market_price_is_calculated: false,
      ib_daily_pnl: null,
      legs: [
        {
          direction: "LONG",
          contracts: 100,
          type: "Stock",
          strike: null,
          conId: 1,
          entry_cost: 11_000,
          avg_cost: 110,
          market_price: 123.4,
          market_value: 12_340,
          market_price_is_calculated: false,
        },
      ],
      kelly_optimal: null,
      target: null,
      stop: null,
      entry_date: "",
    },
    {
      id: 2,
      ticker: "FRSQ",
      structure: "Stock",
      structure_type: "Stock",
      risk_profile: "equity",
      expiry: "",
      contracts: 50,
      direction: "LONG",
      entry_cost: 2_500,
      max_risk: null,
      market_value: 2_839,
      market_price_is_calculated: false,
      ib_daily_pnl: null,
      legs: [
        {
          direction: "LONG",
          contracts: 50,
          type: "Stock",
          strike: null,
          conId: 2,
          entry_cost: 2_500,
          avg_cost: 50,
          market_price: 56.78,
          market_value: 2_839,
          market_price_is_calculated: false,
        },
      ],
      kelly_optimal: null,
      target: null,
      stop: null,
      entry_date: "",
    },
    {
      id: 3,
      ticker: "SPRD",
      structure: "Bull Call Spread $100/$110",
      structure_type: "Vertical",
      risk_profile: "defined",
      expiry: "2026-12-18",
      contracts: 5,
      direction: "DEBIT",
      entry_cost: 6_500,
      max_risk: 6_500,
      market_value: 6_500,
      market_price_is_calculated: false,
      ib_daily_pnl: null,
      legs: [
        {
          direction: "LONG",
          contracts: 5,
          type: "Call",
          strike: 100,
          entry_cost: 9_500,
          avg_cost: 19,
          market_price: 1.85,
          market_value: 925,
          market_price_is_calculated: false,
        },
        {
          direction: "SHORT",
          contracts: 5,
          type: "Call",
          strike: 110,
          entry_cost: 3_000,
          avg_cost: 6,
          market_price: 0.55,
          market_value: 275,
          market_price_is_calculated: false,
        },
      ],
      kelly_optimal: null,
      target: null,
      stop: null,
      entry_date: "",
    },
  ],
  exposure: {},
  violations: [],
  account_summary: {
    net_liquidation: 1_000_000,
    daily_pnl: 0,
    unrealized_pnl: 0,
    realized_pnl: 0,
    settled_cash: 500_000,
    maintenance_margin: 0,
    excess_liquidity: 500_000,
    buying_power: 1_000_000,
    dividends: 0,
  },
};

const ORDERS_EMPTY = {
  last_sync: new Date().toISOString(),
  open_orders: [],
  executed_orders: [],
  open_count: 0,
  executed_count: 0,
};

// Synthetic Futu-shaped rows; `stale` is the relay-stamped flag under test.
const PRICE_FIXTURES: Record<string, Record<string, unknown>> = {
  SYNQ: {
    symbol: "SYNQ",
    source: "futu",
    stale: true,
    last: 12.34,
    lastIsCalculated: false,
    bid: 12.3,
    ask: 12.38,
    bidSize: 10,
    askSize: 10,
    volume: 1_000,
    close: 12.0,
    timestamp: new Date().toISOString(),
  },
  FRSQ: {
    symbol: "FRSQ",
    source: "futu",
    stale: false,
    last: 56.78,
    lastIsCalculated: false,
    bid: 56.7,
    ask: 56.86,
    bidSize: 10,
    askSize: 10,
    volume: 1_000,
    close: 56.0,
    timestamp: new Date().toISOString(),
  },
  SPRD: {
    symbol: "SPRD",
    source: "futu",
    stale: false,
    last: 44.0,
    lastIsCalculated: false,
    bid: 43.9,
    ask: 44.1,
    bidSize: 10,
    askSize: 10,
    volume: 1_000,
    close: 43.0,
    timestamp: new Date().toISOString(),
  },
  "SPRD_20261218_100_C": {
    symbol: "SPRD_20261218_100_C",
    source: "futu",
    stale: true,
    last: 1.85,
    lastIsCalculated: false,
    bid: 1.8,
    ask: 1.9,
    bidSize: 40,
    askSize: 40,
    volume: 0,
    close: 1.8,
    undPrice: 44.0,
    timestamp: new Date().toISOString(),
  },
  "SPRD_20261218_110_C": {
    symbol: "SPRD_20261218_110_C",
    source: "futu",
    stale: false,
    last: 0.55,
    lastIsCalculated: false,
    bid: 0.5,
    ask: 0.6,
    bidSize: 40,
    askSize: 40,
    volume: 0,
    close: 0.5,
    undPrice: 44.0,
    timestamp: new Date().toISOString(),
  },
};

async function installMockWebSocket(page: import("@playwright/test").Page) {
  await page.addInitScript((priceFixtures) => {
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
          symbols?: Array<string | { symbol?: string }>;
          contracts?: Array<{
            symbol: string;
            expiry: string;
            strike: number;
            right: "C" | "P";
          }>;
        };
        if (message.action !== "subscribe") return;

        // Record the raw frame so the spec can assert tier tags.
        (
          window as unknown as { __subscribeFrames?: unknown[] }
        ).__subscribeFrames ??= [];
        (
          window as unknown as { __subscribeFrames: unknown[] }
        ).__subscribeFrames.push(message);

        const updates: Record<string, unknown> = {};
        for (const entry of message.symbols ?? []) {
          const symbol = typeof entry === "string" ? entry : entry?.symbol;
          if (symbol && priceFixtures[symbol])
            updates[symbol] = priceFixtures[symbol];
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
  // WS auth ticket — the real route proxies FastAPI; return a synthetic one
  // so connect() proceeds to the MockWebSocket (whose URL it ignores).
  await page.route("**/api/ib/ws-ticket", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ticket: "x7b-synthetic-ticket" }),
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

test("futu rows: held tier tags on subscribe, .price-stale-dot on stale rows, prices still render", async ({
  page,
}) => {
  await installMockWebSocket(page);
  await stubApis(page);

  await page.goto("/portfolio");

  const synqRow = page
    .locator("table tbody tr")
    .filter({ hasText: "SYNQ" })
    .first();
  const frsqRow = page
    .locator("table tbody tr")
    .filter({ hasText: "FRSQ" })
    .first();
  const sprdRow = page
    .locator("table tbody tr")
    .filter({ hasText: "Bull Call Spread" })
    .first();
  await expect(synqRow).toBeVisible();
  await expect(frsqRow).toBeVisible();
  await expect(sprdRow).toBeVisible();

  // (a) subscribe frame: portfolio stock/option legs tagged held.
  await expect
    .poll(async () =>
      page.evaluate(
        () =>
          (window as unknown as { __subscribeFrames?: unknown[] })
            .__subscribeFrames?.length ?? 0,
      ),
    )
    .toBeGreaterThan(0);
  const frames = await page.evaluate(
    () =>
      (window as unknown as { __subscribeFrames: unknown[] })
        .__subscribeFrames,
  );
  type SubFrame = {
    action?: string;
    symbols?: Array<string | { symbol?: string; tier?: string }>;
    contracts?: Array<{
      symbol: string;
      expiry: string;
      strike: number;
      right: string;
      tier?: string;
    }>;
  };
  const heldSymbols = new Set<string>();
  const heldContracts = new Set<string>();
  for (const raw of frames as SubFrame[]) {
    for (const entry of raw.symbols ?? []) {
      if (typeof entry === "object" && entry?.symbol && entry?.tier === "held")
        heldSymbols.add(entry.symbol);
    }
    for (const c of raw.contracts ?? []) {
      if (c.tier === "held")
        heldContracts.add(
          `${c.symbol}_${String(c.expiry).replace(/-/g, "")}_${Number(c.strike)}_${c.right}`,
        );
    }
  }
  expect(heldSymbols.has("SYNQ")).toBe(true);
  expect(heldSymbols.has("FRSQ")).toBe(true);
  expect(heldSymbols.has("SPRD")).toBe(true);
  expect(heldContracts.has("SPRD_20261218_100_C")).toBe(true);
  expect(heldContracts.has("SPRD_20261218_110_C")).toBe(true);

  // (b) stale:true rows carry the dot; fresh rows do not.
  await expect(synqRow.locator(".price-stale-dot")).toHaveCount(1);
  await expect(frsqRow.locator(".price-stale-dot")).toHaveCount(0);
  // Spread row flags stale because one leg quote is stale.
  await expect(sprdRow.locator(".price-stale-dot")).toHaveCount(1);

  await page.getByLabel("Expand legs for SPRD").click();
  const longLegRow = page
    .locator("table tbody tr")
    .filter({ hasText: "LONG 5x Call $100" });
  const shortLegRow = page
    .locator("table tbody tr")
    .filter({ hasText: "SHORT 5x Call $110" });
  await expect(longLegRow.locator(".price-stale-dot")).toHaveCount(1);
  await expect(shortLegRow.locator(".price-stale-dot")).toHaveCount(0);

  // (c) prices still render from the synthetic Futu rows. Position rows carry
  // two last-price cells (underlying + position last) — the trailing one is
  // the row price.
  await expect(synqRow.locator("td.last-price-cell").last()).toContainText(
    "$12.34",
  );
  await expect(frsqRow.locator("td.last-price-cell").last()).toContainText(
    "$56.78",
  );
  // Leg rows serve the Futu `last`; the spread row net price is computed from
  // the leg bids/asks (1.80-0.60 / 1.90-0.50 → mid $1.30).
  await expect(longLegRow.locator("td.last-price-cell")).toContainText("$1.85");
  await expect(shortLegRow.locator("td.last-price-cell")).toContainText(
    "$0.55",
  );
  await expect(sprdRow.locator("td.last-price-cell").last()).toContainText(
    "$1.30",
  );

  await page.screenshot({
    path: "../output/playwright/x7b-futu-stale-2026-10-26.png",
    fullPage: true,
  });
});
