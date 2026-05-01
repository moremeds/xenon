/**
 * @vitest-environment jsdom
 */

import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import OrderTab from "../components/ticker-detail/OrderTab";
import type { PortfolioData, PortfolioPosition } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";

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

vi.mock("@/components/ticker-detail/WizardModal", () => ({
  default: () => null,
}));

vi.mock("@/components/ticker-detail/WizardSessionStrip", () => ({
  default: () => null,
}));

vi.mock("@/lib/useWizardLauncher", () => ({
  useWizardLauncher: () => ({
    sessionId: null,
    isOpen: false,
    launch: vi.fn(),
    resume: vi.fn(),
    close: vi.fn(),
  }),
}));

vi.mock("@/lib/useWizardSession", () => ({
  useWizardSession: () => ({
    session: null,
    refresh: vi.fn(),
  }),
}));

const STOCK_PRICE: PriceData = {
  symbol: "QQQ",
  last: 450,
  lastIsCalculated: false,
  bid: 449.95,
  ask: 450.05,
  bidSize: 1,
  askSize: 1,
  volume: 10,
  high: null,
  low: null,
  open: null,
  close: 449,
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
};

const COMBO_POSITION: PortfolioPosition = {
  id: 99,
  ticker: "QQQ",
  structure: "Bull Put Spread",
  structure_type: "Bull Put Spread",
  risk_profile: "defined",
  expiry: "2026-06-19",
  contracts: 4,
  direction: "COMBO",
  entry_cost: -200,
  max_risk: 1800,
  market_value: -160,
  kelly_optimal: null,
  target: null,
  stop: null,
  entry_date: "2026-04-30",
  legs: [
    {
      direction: "LONG",
      contracts: 4,
      type: "Put",
      strike: 190,
      entry_cost: 100,
      avg_cost: 0.25,
      market_price: 0.2,
      market_value: 80,
    },
    {
      direction: "SHORT",
      contracts: 4,
      type: "Put",
      strike: 195,
      entry_cost: -300,
      avg_cost: -0.75,
      market_price: 0.6,
      market_value: -240,
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
  defined_risk_count: 1,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  positions: [COMBO_POSITION],
  exposure: {},
  violations: [],
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

const COMBO_PRICES: Record<string, PriceData> = {
  QQQ_20260619_190_P: { ...STOCK_PRICE, symbol: "QQQ_20260619_190_P", bid: 0.2, ask: 0.24 },
  QQQ_20260619_195_P: { ...STOCK_PRICE, symbol: "QQQ_20260619_195_P", bid: 0.68, ask: 0.72 },
};

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("OrderTab regime gate handling", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  it("opens the override modal for single-leg REGIME_BLOCK and retries with audit reason", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse(409, {
          detail: "TIER_1 - non-hedge entries blocked",
          reason_code: "REGIME_BLOCK",
          decision: "block",
          binding_tier: "TIER_1",
          binding_side: "cri",
          vcg_tier: "NORMAL",
          cri_tier: "TIER_1",
          override_required: true,
          override_min_reason_chars: 10,
        }),
      )
      .mockResolvedValueOnce(jsonResponse(200, { status: "ok", orderId: 1, permId: 2 }));
    vi.stubGlobal("fetch", fetchMock);

    const { container, getByRole } = render(
      <OrderTab
        ticker="QQQ"
        position={null}
        portfolio={PORTFOLIO}
        prices={{}}
        tickerPriceData={STOCK_PRICE}
      />,
    );

    fireEvent.change(container.querySelector(".order-input")!, { target: { value: "1" } });
    fireEvent.change(container.querySelector(".modify-price-input")!, { target: { value: "450" } });
    fireEvent.click(getByRole("button", { name: "Place Order" }));
    fireEvent.click(getByRole("button", { name: "Confirm Order" }));

    expect(await screen.findByRole("dialog", { name: "Order blocked by regime gate" })).toBeTruthy();
    fireEvent.change(container.querySelector("textarea")!, {
      target: { value: "risk reviewed manually" },
    });
    fireEvent.click(getByRole("button", { name: "Override and submit" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const retryBody = JSON.parse(String(fetchMock.mock.calls[1][1]?.body));
    expect(retryBody.override).toBe(true);
    expect(retryBody.override_reason).toBe("risk reviewed manually");
    expect(retryBody.client_attempt_id).toBe(JSON.parse(String(fetchMock.mock.calls[0][1]?.body)).client_attempt_id);
  });

  it("applies a combo REGIME_RESIZE_REQUIRED prompt by retrying with the suggested quantity", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse(422, {
          detail: "TIER_2 throttle: order's max loss exceeds cap",
          reason_code: "REGIME_RESIZE_REQUIRED",
          decision: "resize_required",
          binding_tier: "TIER_2",
          binding_side: "vcg",
          max_loss_usd: 400,
          max_loss_cap_usd: 125,
          cover_ratio: 1.25,
        }),
      )
      .mockResolvedValueOnce(jsonResponse(200, { status: "ok", orderId: 3, permId: 4 }));
    vi.stubGlobal("fetch", fetchMock);

    const { container, getByRole } = render(
      <OrderTab
        ticker="QQQ"
        position={COMBO_POSITION}
        portfolio={PORTFOLIO}
        prices={COMBO_PRICES}
        tickerPriceData={STOCK_PRICE}
      />,
    );

    fireEvent.click(getByRole("button", { name: /MID -0\.48/i }));
    fireEvent.click(getByRole("button", { name: "Place Combo Order" }));
    fireEvent.click(getByRole("button", { name: "Confirm Order" }));

    expect(await screen.findByRole("dialog", { name: "Order exceeds regime cap" })).toBeTruthy();
    const spinbuttons = screen.getAllByRole("spinbutton") as HTMLInputElement[];
    const resizeInput = spinbuttons[spinbuttons.length - 1];
    expect(resizeInput.value).toBe("1");
    fireEvent.click(getByRole("button", { name: "Apply resize" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const retryBody = JSON.parse(String(fetchMock.mock.calls[1][1]?.body));
    expect(retryBody.type).toBe("combo");
    expect(retryBody.quantity).toBe(1);
    expect(retryBody.client_attempt_id).toBe(JSON.parse(String(fetchMock.mock.calls[0][1]?.body)).client_attempt_id);
  });
});
