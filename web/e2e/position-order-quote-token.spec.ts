/**
 * E2E: PositionOrderModal quote-token wiring
 *
 * Verifies that when the user clicks the position-row "Create order"
 * (Zap) button on /portfolio and submits a close, the outgoing
 * /api/orders/place POST body carries quote tokens:
 *   - single-leg/stock → body.quote_token: string
 *   - combo            → body.quote_tokens: { [conId]: string }
 *
 * Task 4 already has Vitest coverage for the modal's payload shape
 * (web/tests/position-order-modal-quote-tokens.test.tsx). This spec
 * is the belt-and-suspenders integration check that the wiring
 * survives the real page shell (PositionTable → PositionOrderModal →
 * fetch → /api/orders/quote + /api/orders/place).
 */

import { test, expect } from "@playwright/test";

// ── Mock data ────────────────────────────────────────────────────────────────

const NOW = new Date().toISOString();

const COMBO_PORTFOLIO = {
  bankroll: 100_000,
  peak_value: 100_000,
  last_sync: NOW,
  total_deployed_pct: 1.2,
  total_deployed_dollars: 1_200,
  remaining_capacity_pct: 98.8,
  position_count: 1,
  defined_risk_count: 1,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  exposure: {},
  violations: [],
  positions: [
    {
      id: 42,
      ticker: "SPY",
      structure: "Bull Call Spread",
      structure_type: "vertical_call_debit",
      risk_profile: "defined_risk",
      expiry: "2026-05-16",
      contracts: 1,
      direction: "LONG",
      entry_cost: 300,
      market_value: 320,
      market_price_is_calculated: false,
      ib_daily_pnl: null,
      legs: [
        {
          conId: 111,
          direction: "LONG",
          contracts: 1,
          type: "Call",
          strike: 500,
          entry_cost: 500,
          avg_cost: 5.0,
          market_price: 5.2,
          market_value: 520,
          market_price_is_calculated: false,
        },
        {
          conId: 222,
          direction: "SHORT",
          contracts: 1,
          type: "Call",
          strike: 510,
          entry_cost: -200,
          avg_cost: 2.0,
          market_price: 2.0,
          market_value: -200,
          market_price_is_calculated: false,
        },
      ],
      kelly_optimal: null,
      target: null,
      stop: null,
      entry_date: "2026-04-01",
    },
  ],
  account_summary: {
    net_liquidation: 100_000,
    daily_pnl: 0,
    unrealized_pnl: 0,
    realized_pnl: 0,
    settled_cash: 100_000,
    maintenance_margin: 0,
    excess_liquidity: 100_000,
    buying_power: 200_000,
    dividends: 0,
  },
};

const ORDERS_EMPTY = {
  last_sync: NOW,
  open_orders: [],
  executed_orders: [],
  open_count: 0,
  executed_count: 0,
};

const PRICE_FIXTURES: Record<string, Record<string, unknown>> = {
  SPY: {
    symbol: "SPY",
    last: 500.5,
    lastIsCalculated: false,
    bid: 500.4,
    ask: 500.6,
    bidSize: 10,
    askSize: 10,
    volume: 10,
    high: null,
    low: null,
    open: null,
    close: 499,
    week52High: null,
    week52Low: null,
    avgVolume: null,
    delta: null,
    gamma: null,
    theta: null,
    vega: null,
    impliedVol: null,
    undPrice: null,
    timestamp: NOW,
  },
  SPY_20260516_500_C: {
    symbol: "SPY_20260516_500_C",
    last: 5.2,
    lastIsCalculated: false,
    bid: 5.1,
    ask: 5.3,
    bidSize: 10,
    askSize: 10,
    volume: 10,
    high: null,
    low: null,
    open: null,
    close: 5.0,
    week52High: null,
    week52Low: null,
    avgVolume: null,
    delta: null,
    gamma: null,
    theta: null,
    vega: null,
    impliedVol: null,
    undPrice: 500.5,
    timestamp: NOW,
  },
  SPY_20260516_510_C: {
    symbol: "SPY_20260516_510_C",
    last: 2.0,
    lastIsCalculated: false,
    bid: 1.9,
    ask: 2.1,
    bidSize: 10,
    askSize: 10,
    volume: 10,
    high: null,
    low: null,
    open: null,
    close: 2.0,
    week52High: null,
    week52Low: null,
    avgVolume: null,
    delta: null,
    gamma: null,
    theta: null,
    vega: null,
    impliedVol: null,
    undPrice: 500.5,
    timestamp: NOW,
  },
};

// ── Helpers ──────────────────────────────────────────────────────────────────

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
          symbols?: string[];
          contracts?: Array<{
            symbol: string;
            expiry: string;
            strike: number;
            right: "C" | "P";
          }>;
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
        if (Object.keys(updates).length > 0)
          this.emit({ type: "batch", updates });
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

async function stubCommonApis(
  page: import("@playwright/test").Page,
  portfolio: unknown,
) {
  await page.unrouteAll({ behavior: "ignoreErrors" });

  await page.route("**/api/portfolio", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(portfolio),
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
        as_of: NOW,
        summary: { realized_pnl: 0 },
        closed_trades: [],
        open_trades: [],
      }),
    }),
  );
  await page.route("**/api/prices", (route) => route.abort());
  await page.route("**/api/ticker/**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({}),
    }),
  );
}

// ── Tests ────────────────────────────────────────────────────────────────────

test.describe("PositionOrderModal — quote_token wiring", () => {
  test("vertical combo close sends quote_tokens map keyed by conId", async ({
    page,
  }) => {
    await installMockWebSocket(page);
    await stubCommonApis(page, COMBO_PORTFOLIO);

    let placeBody: Record<string, unknown> | null = null;

    await page.route("**/api/orders/quote*", async (route) => {
      const u = new URL(route.request().url());
      const conId = u.searchParams.get("con_id");
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ token: `tok-${conId}` }),
      });
    });

    await page.route("**/api/orders/place", async (route) => {
      try {
        placeBody = route.request().postDataJSON() as Record<string, unknown>;
      } catch {
        placeBody = null;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ orderId: "O1" }),
      });
    });

    await page.goto("/portfolio");

    // The position-row order button has no test-id today; it is a <button>
    // with aria-label="Create order for SPY position" (PositionTable.tsx).
    const orderBtn = page
      .getByRole("button", { name: /Create order for SPY position/i })
      .first();
    await orderBtn.waitFor({ timeout: 15_000 });
    await orderBtn.click();

    const submit = page.getByRole("button", { name: /^Submit close$/i });
    await submit.waitFor({ timeout: 10_000 });
    // Wait until the modal enables the submit button, which only happens
    // after both quote tokens have resolved (useQuoteTokens → tokensReady).
    await expect(submit).toBeEnabled({ timeout: 10_000 });
    await submit.click();

    await expect.poll(() => placeBody !== null, { timeout: 10_000 }).toBe(true);

    const body = placeBody as Record<string, unknown>;
    expect(body.type).toBe("combo");
    const quoteTokens = body.quote_tokens as Record<string, string> | undefined;
    expect(quoteTokens).toBeDefined();
    expect(Object.keys(quoteTokens ?? {}).sort()).toEqual(["111", "222"]);
    expect(quoteTokens!["111"]).toBe("tok-111");
    expect(quoteTokens!["222"]).toBe("tok-222");
    expect(body.quote_token).toBeUndefined();
  });

  // TODO(quote-token E2E): add a single-leg / stock variant that asserts
  // body.quote_token is a string (not quote_tokens map). Blocked on building
  // a matching single-leg portfolio fixture + confirming the Zap button is
  // exposed for single-leg rows (PositionTable currently shows it for both
  // single and multi-leg positions, so this should be straightforward once
  // the combo path is green in CI).
  test.fixme("single-leg option close sends quote_token string", async () => {});
});
