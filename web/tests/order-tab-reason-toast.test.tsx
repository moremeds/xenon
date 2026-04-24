/**
 * @vitest-environment jsdom
 */

import React from "react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import OrderTab from "../components/ticker-detail/OrderTab";
import type { PortfolioData, PortfolioPosition } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import { ORDER_REASON_CODES } from "@/lib/orderReasonCodes";

vi.mock("@/lib/OrderActionsContext", () => ({
  useOrderActions: () => ({
    pendingCancels: new Map(),
    pendingModifies: new Map(),
    cancelledOrders: [],
    requestCancel: vi.fn(),
    requestModify: vi.fn(),
    drainNotifications: vi.fn(() => []),
    setOrdersUpdater: vi.fn(),
  }),
}));

vi.mock("@/components/ModifyOrderModal", () => ({
  default: () => null,
}));

// Default quote-token fetch so useQuoteToken resolves before place fetch.
const quoteOk = () =>
  Promise.resolve({
    ok: true,
    status: 200,
    json: async () => ({ token: "tok-test" }),
  }) as unknown as Promise<Response>;

const STOCK_POSITION: PortfolioPosition = {
  id: 1,
  ticker: "AAPL",
  structure: "Stock",
  structure_type: "Stock",
  risk_profile: "undefined",
  expiry: "",
  contracts: 100,
  direction: "LONG",
  entry_cost: 15000,
  max_risk: null,
  market_value: 16000,
  kelly_optimal: null,
  target: null,
  stop: null,
  entry_date: "2026-03-19",
  legs: [
    {
      conId: null,
      direction: "LONG",
      contracts: 100,
      type: "Stock",
      strike: null,
      entry_cost: 15000,
      avg_cost: 150,
      market_price: 160,
      market_value: 16000,
      market_price_is_calculated: false,
    },
  ],
};

const PORTFOLIO: PortfolioData = {
  bankroll: 100_000,
  peak_value: 100_000,
  last_sync: new Date().toISOString(),
  total_deployed_pct: 1,
  total_deployed_dollars: 1_000,
  remaining_capacity_pct: 99,
  position_count: 1,
  defined_risk_count: 0,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  positions: [STOCK_POSITION],
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

const PRICES: Record<string, PriceData> = {
  AAPL: {
    symbol: "AAPL",
    last: 160,
    lastIsCalculated: false,
    bid: 159.9,
    ask: 160.1,
    bidSize: 1,
    askSize: 1,
    volume: 10,
    high: null,
    low: null,
    open: null,
    close: 159,
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
};

describe("OrderTab reason-code toast", () => {
  beforeEach(() => {
    // Stub crypto.randomUUID for useClientAttemptId.
    if (!globalThis.crypto?.randomUUID) {
      // @ts-expect-error — jsdom polyfill shim
      globalThis.crypto = {
        randomUUID: () => "00000000-0000-0000-0000-000000000000",
      };
    }
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("test_renders_stale_quote_copy", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(((
      input: RequestInfo | URL,
    ) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/api/orders/quote")) return quoteOk();
      if (url.includes("/api/orders/place")) {
        return Promise.resolve({
          ok: false,
          status: 400,
          json: async () => ({
            reason_code: "STALE_QUOTE",
            detail: "quote expired (server-side)",
          }),
        }) as unknown as Promise<Response>;
      }
      return quoteOk();
    }) as typeof fetch);

    const { container, getByRole, findByText } = render(
      React.createElement(OrderTab, {
        ticker: "AAPL",
        position: STOCK_POSITION,
        portfolio: PORTFOLIO,
        prices: PRICES,
        openOrders: [],
        tickerPriceData: PRICES.AAPL,
      }),
    );

    // Fill limit price (quantity is pre-populated from position.contracts)
    const limitInput = container.querySelector(
      ".modify-price-input",
    ) as HTMLInputElement;
    fireEvent.change(limitInput, { target: { value: "160.00" } });

    // First click advances to confirm step
    fireEvent.click(getByRole("button", { name: "Place Order" }));
    // Second click performs the POST
    fireEvent.click(getByRole("button", { name: "Confirm Order" }));

    const expectedCopy = ORDER_REASON_CODES.STALE_QUOTE.copy;
    const banner = await findByText(expectedCopy);
    expect(banner).toBeTruthy();
    expect(fetchSpy).toHaveBeenCalled();
  });

  it("test_no_hardcoded_reason_strings_in_order_tab", () => {
    const orderTabPath = resolve(
      __dirname,
      "../components/ticker-detail/OrderTab.tsx",
    );
    const source = readFileSync(orderTabPath, "utf8");

    // Every reason-code copy must NOT appear as a literal in OrderTab.
    // These phrases are specific enough that a false positive is unlikely.
    const failures: string[] = [];
    for (const [code, entry] of Object.entries(ORDER_REASON_CODES)) {
      if (source.includes(entry.copy)) {
        failures.push(`${code}: "${entry.copy}"`);
      }
    }
    expect(failures).toEqual([]);
  });
});
