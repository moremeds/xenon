/**
 * @vitest-environment jsdom
 *
 * PR-19 — one natural-quote implementation.
 *
 * Every surface that shows a combo's natural market must report identical
 * numbers: the position spread price bar (resolveSpreadPriceData / PositionTab),
 * the combo order form in OrderTab, the BAG order quote in ModifyOrderModal, and
 * the options-chain order builder (which already calls computeNetOptionQuote).
 *
 * Canonical contract of computeNetOptionQuote (signed instrument quote):
 *   bid = proceeds to SELL the structure at market
 *       = Σ(bid·qty over BUY legs) − Σ(ask·qty over SELL legs)
 *   ask = cost to BUY the structure at market
 *       = Σ(ask·qty over BUY legs) − Σ(bid·qty over SELL legs)
 *   mid = (bid + ask) / 2
 * Values are signed: a structure worth a net credit quotes negative, matching
 * the IB BAG limit-price convention (negative limit = net credit).
 *
 * All bid/ask numbers below are hypothetical fixtures authored for this test;
 * they are not observed market data.
 */

import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  renderHook,
  waitFor,
} from "@testing-library/react";
import {
  computeNetOptionQuote,
  normalizeComboOrder,
  type OrderLeg,
} from "../lib/optionsChainUtils";
import { resolveSpreadPriceData } from "../lib/positionUtils";
import { useOrderPrices } from "../lib/order/hooks/useOrderPrices";
import type { OrderLeg as UnifiedOrderLeg } from "../lib/order/types";
import OrderTab from "../components/ticker-detail/OrderTab";
import OptionsChainTab from "../components/ticker-detail/OptionsChainTab";
import { TickerDetailProvider } from "../lib/TickerDetailContext";
import ModifyOrderModal from "../components/ModifyOrderModal";
import type { OpenOrder, PortfolioData, PortfolioPosition } from "../lib/types";
import type { PriceData } from "../lib/pricesProtocol";

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

vi.mock("@/components/Modal", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="modal">{children}</div>
  ),
}));

// Thin WizardModal double: forwards the real OrderBuilder handlers to
// buttons so tests drive the actual submit/reprice code paths.
vi.mock("@/components/ticker-detail/WizardModal", () => ({
  default: (props: {
    open?: boolean;
    onSubmit?: () => void;
    onRepriceNatural?: () => void;
  }) => (
    <div data-testid="wizard-modal">
      {props.onSubmit && (
        <button type="button" onClick={props.onSubmit}>
          WIZ_SUBMIT
        </button>
      )}
      {props.onRepriceNatural && (
        <button type="button" onClick={props.onRepriceNatural}>
          WIZ_REPRICE_NATURAL
        </button>
      )}
    </div>
  ),
}));

/* ─── Hypothetical fixtures ─── */

const TICKER = "TEST";
const EXPIRY = "20260619";

function makePriceData(bid: number, ask: number): PriceData {
  return {
    symbol: "TEST",
    bid,
    ask,
    last: (bid + ask) / 2,
    bidSize: null,
    askSize: null,
    volume: null,
    high: null,
    low: null,
    open: null,
    close: null,
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
  } as PriceData;
}

// Hypothetical leg quotes — NOT observed market data.
const PRICES: Record<string, PriceData> = {
  TEST_20260619_200_C: makePriceData(4.5, 4.7),
  TEST_20260619_210_C: makePriceData(2.0, 2.2),
  TEST_20260619_200_P: makePriceData(4.5, 4.7),
  TEST_20260619_190_P: makePriceData(2.0, 2.2),
};

function makePosition(
  legs: PortfolioPosition["legs"],
  contracts = 10,
): PortfolioPosition {
  return {
    id: 1,
    ticker: TICKER,
    direction: "COMBO",
    structure: "TEST SPREAD",
    structure_type: "Vertical",
    expiry: "2026-06-19",
    contracts,
    entry_cost: 0,
    market_value: 0,
    legs,
  } as PortfolioPosition;
}

function makePortfolio(position: PortfolioPosition): PortfolioData {
  return {
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
    positions: [position],
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
  } as PortfolioData;
}

function makeBagOrder(
  legs: {
    action: "BUY" | "SELL";
    strike: number;
    right: "C" | "P";
    symbol?: string;
  }[],
  limitPrice: number | null = null,
): OpenOrder {
  return {
    orderId: 1,
    permId: 1001,
    symbol: TICKER,
    action: "SELL",
    orderType: "LMT",
    tif: "DAY",
    totalQuantity: 1,
    limitPrice,
    auxPrice: null,
    contract: {
      symbol: TICKER,
      secType: "BAG",
      strike: null,
      right: null,
      expiry: null,
      comboLegs: legs.map((l) => ({
        action: l.action,
        symbol: l.symbol ?? TICKER,
        expiry: EXPIRY,
        strike: l.strike,
        right: l.right,
        ratio: 1,
      })),
    },
  } as unknown as OpenOrder;
}

/* ─── Shared expected values ─── */

// Bull call spread (debit): BUY 200C / SELL 210C.
//   bid = 4.50 − 2.20 = 2.30   ask = 4.70 − 2.00 = 2.70   mid = 2.50
const DEBIT_ORDER_LEGS: OrderLeg[] = [
  { id: "TEST_200_C", action: "BUY", right: "C", strike: 200, expiry: EXPIRY, quantity: 1, limitPrice: null },
  { id: "TEST_210_C", action: "SELL", right: "C", strike: 210, expiry: EXPIRY, quantity: 1, limitPrice: null },
];
const DEBIT_POSITION = makePosition([
  { type: "Call", strike: 200, direction: "LONG", contracts: 10, entry_cost: 4500, avg_cost: 450, market_price: 4.6, market_value: 4600, market_price_is_calculated: false },
  { type: "Call", strike: 210, direction: "SHORT", contracts: 10, entry_cost: -2100, avg_cost: 210, market_price: 2.1, market_value: -2100, market_price_is_calculated: false },
]);
const DEBIT_QUOTE = { bid: 2.3, ask: 2.7, mid: 2.5 };

// Credit put spread: SELL 200P / BUY 190P (the short-risk structure).
//   bid = 2.00 − 4.70 = −2.70  ask = 2.20 − 4.50 = −2.30  mid = −2.50
const CREDIT_ORDER_LEGS: OrderLeg[] = [
  { id: "TEST_200_P", action: "SELL", right: "P", strike: 200, expiry: EXPIRY, quantity: 1, limitPrice: null },
  { id: "TEST_190_P", action: "BUY", right: "P", strike: 190, expiry: EXPIRY, quantity: 1, limitPrice: null },
];
const CREDIT_POSITION = makePosition([
  { type: "Put", strike: 200, direction: "SHORT", contracts: 10, entry_cost: -4600, avg_cost: 460, market_price: 4.6, market_value: -4600, market_price_is_calculated: false },
  { type: "Put", strike: 190, direction: "LONG", contracts: 10, entry_cost: 2100, avg_cost: 210, market_price: 2.1, market_value: 2100, market_price_is_calculated: false },
]);
const CREDIT_QUOTE = { bid: -2.7, ask: -2.3, mid: -2.5 };

afterEach(() => cleanup());

function assertQuote(
  quote: { bid: number | null; ask: number | null; mid: number | null },
  expected: { bid: number; ask: number; mid: number },
) {
  expect(quote.bid).toBeCloseTo(expected.bid, 4);
  expect(quote.ask).toBeCloseTo(expected.ask, 4);
  expect(quote.mid).toBeCloseTo(expected.mid, 4);
}

function assertSpreadPriceData(
  position: PortfolioPosition,
  expected: { bid: number; ask: number; mid: number },
) {
  const pd = resolveSpreadPriceData(TICKER, position, PRICES);
  expect(pd).not.toBeNull();
  expect(pd!.bid).toBeCloseTo(expected.bid, 4);
  expect(pd!.ask).toBeCloseTo(expected.ask, 4);
  // The synthetic mark is the natural-quote mid.
  expect(pd!.last).toBeCloseTo(expected.mid, 4);
}

function assertStripValues(
  strip: Element | null,
  expected: { bid: number; ask: number; mid: number },
) {
  const fmt = (n: number) =>
    n < 0 ? `-$${Math.abs(n).toFixed(2)}` : `$${n.toFixed(2)}`;
  const items = strip?.querySelectorAll(".spread-price-item") ?? [];
  const text = (label: string) =>
    Array.from(items)
      .find((el) => el.querySelector(".spread-price-label")?.textContent === label)
      ?.querySelector(".spread-price-value")?.textContent ?? null;
  expect(text("BID")).toBe(fmt(expected.bid));
  expect(text("MID")).toBe(fmt(expected.mid));
  expect(text("ASK")).toBe(fmt(expected.ask));
}

function assertOrderTabStrip(
  position: PortfolioPosition,
  expected: { bid: number; ask: number; mid: number },
) {
  const { container, getByRole } = render(
    React.createElement(OrderTab, {
      ticker: TICKER,
      position,
      portfolio: makePortfolio(position),
      prices: PRICES,
      openOrders: [],
    }),
  );

  const strip = container.querySelector(".spread-price-strip");
  assertStripValues(strip, expected);

  // The quote describes the instrument, not the envelope direction: toggling
  // BUY/SELL must not change the displayed natural market.
  fireEvent.click(getByRole("button", { name: "BUY" }));
  assertStripValues(
    container.querySelector(".spread-price-strip"),
    expected,
  );
}

function assertModifyModalStrip(
  order: OpenOrder,
  expected: { bid: number; ask: number; mid: number },
) {
  const { container } = render(
    React.createElement(ModifyOrderModal, {
      order,
      loading: false,
      prices: PRICES,
      portfolio: undefined,
      onConfirm: () => {},
      onClose: () => {},
    }),
  );

  const strip = container.querySelector(".order-price-strip-compact");
  const fmt = (n: number) => `$${n.toFixed(2)}`;
  const items = strip?.querySelectorAll(".order-price-item") ?? [];
  const text = (label: string) =>
    Array.from(items)
      .find((el) => el.querySelector(".order-price-label")?.textContent === label)
      ?.querySelector(".order-price-value")?.textContent ?? null;
  expect(text("BID")).toBe(fmt(expected.bid));
  expect(text("MID")).toBe(fmt(expected.mid));
  expect(text("ASK")).toBe(fmt(expected.ask));
}

/* ─── Tests ─── */

describe("natural quote — one implementation", () => {
  it("canonical: debit bull call spread", () => {
    assertQuote(
      computeNetOptionQuote(DEBIT_ORDER_LEGS, PRICES, TICKER),
      DEBIT_QUOTE,
    );
  });

  it("canonical: credit put spread", () => {
    assertQuote(
      computeNetOptionQuote(CREDIT_ORDER_LEGS, PRICES, TICKER),
      CREDIT_QUOTE,
    );
  });

  it("chain order-builder path (ratio-normalized legs) agrees", () => {
    // OptionsChainTab calls computeNetOptionQuote(normalizeComboOrder(legs).legs).
    for (const [legs, expected] of [
      [DEBIT_ORDER_LEGS, DEBIT_QUOTE],
      [CREDIT_ORDER_LEGS, CREDIT_QUOTE],
    ] as const) {
      const doubled = legs.map((l) => ({ ...l, quantity: 2 }));
      assertQuote(
        computeNetOptionQuote(normalizeComboOrder(doubled).legs, PRICES, TICKER),
        expected,
      );
    }
  });

  it("position spread price bar agrees (debit)", () => {
    assertSpreadPriceData(DEBIT_POSITION, DEBIT_QUOTE);
  });

  it("position spread price bar agrees and stays signed (credit)", () => {
    // A held credit structure must keep a negative mark — PositionTab MV is
    // last × contracts × 100 and a negative mark means net liability.
    assertSpreadPriceData(CREDIT_POSITION, CREDIT_QUOTE);
  });

  it("OrderTab combo form agrees, independent of envelope action (debit)", () => {
    assertOrderTabStrip(DEBIT_POSITION, DEBIT_QUOTE);
  });

  it("OrderTab combo form agrees, independent of envelope action (credit)", () => {
    assertOrderTabStrip(CREDIT_POSITION, CREDIT_QUOTE);
  });

  it("ModifyOrderModal BAG quote agrees (debit)", () => {
    assertModifyModalStrip(
      makeBagOrder([
        { action: "BUY", strike: 200, right: "C" },
        { action: "SELL", strike: 210, right: "C" },
      ]),
      DEBIT_QUOTE,
    );
  });

  it("ModifyOrderModal BAG quote agrees (credit)", () => {
    assertModifyModalStrip(
      makeBagOrder([
        { action: "SELL", strike: 200, right: "P" },
        { action: "BUY", strike: 190, right: "P" },
      ]),
      CREDIT_QUOTE,
    );
  });
});

/* ─── useOrderPrices (unified order system hook) ─── */

function toUnifiedLegs(legs: OrderLeg[]): UnifiedOrderLeg[] {
  return legs.map((l, i) => ({
    id: `u${i}`,
    action: l.action,
    direction: l.action === "BUY" ? "LONG" : "SHORT",
    strike: l.strike,
    type: l.right === "C" ? "Call" : "Put",
    expiry: "2026-06-19",
    quantity: 1,
  }));
}

describe("useOrderPrices — same natural quote", () => {
  it("combo debit quote is signed and identical for BUY/SELL envelopes", () => {
    for (const action of ["BUY", "SELL"] as const) {
      const { result } = renderHook(() =>
        useOrderPrices({
          ticker: TICKER,
          type: "combo",
          action,
          legs: toUnifiedLegs(DEBIT_ORDER_LEGS),
          prices: PRICES,
        }),
      );
      expect(result.current.available).toBe(true);
      assertQuote(result.current, DEBIT_QUOTE);
    }
  });

  it("combo credit quote stays negative for BUY/SELL envelopes", () => {
    for (const action of ["BUY", "SELL"] as const) {
      const { result } = renderHook(() =>
        useOrderPrices({
          ticker: TICKER,
          type: "combo",
          action,
          legs: toUnifiedLegs(CREDIT_ORDER_LEGS),
          prices: PRICES,
        }),
      );
      expect(result.current.available).toBe(true);
      assertQuote(result.current, CREDIT_QUOTE);
    }
  });

  it("unavailable when a leg quote is missing", () => {
    const { result } = renderHook(() =>
      useOrderPrices({
        ticker: TICKER,
        type: "combo",
        action: "BUY",
        legs: toUnifiedLegs(DEBIT_ORDER_LEGS),
        prices: {},
      }),
    );
    expect(result.current.available).toBe(false);
    expect(result.current.bid).toBeNull();
  });
});

/* ─── ModifyOrderModal signed limit price ─── */

function makeSingleLegOrder(): OpenOrder {
  return {
    orderId: 2,
    permId: 2002,
    symbol: TICKER,
    action: "BUY",
    orderType: "LMT",
    tif: "DAY",
    totalQuantity: 1,
    limitPrice: 5.0,
    auxPrice: null,
    contract: {
      symbol: TICKER,
      secType: "OPT",
      strike: 200,
      right: "C",
      expiry: EXPIRY,
      comboLegs: [],
    },
  } as unknown as OpenOrder;
}

describe("ModifyOrderModal — signed limit price", () => {
  it("credit vertical: negative limit passes validation and submits signed newPrice", async () => {
    const onConfirm = vi.fn();
    const { getByLabelText, getByRole } = render(
      <ModifyOrderModal
        order={makeBagOrder(
          [
            { action: "SELL", strike: 200, right: "P" },
            { action: "BUY", strike: 190, right: "P" },
          ],
          -2.45,
        )}
        loading={false}
        prices={PRICES}
        onConfirm={onConfirm}
        onClose={() => {}}
      />,
    );

    const priceInput = getByLabelText(
      /New Net Price|New Limit Price/i,
    ) as HTMLInputElement;
    fireEvent.change(priceInput, { target: { value: "-2.50" } });

    const submit = getByRole("button", {
      name: /Modify Order/i,
    }) as HTMLButtonElement;
    expect(submit.disabled).toBe(false);
    fireEvent.click(submit);

    await waitFor(() => expect(onConfirm).toHaveBeenCalled());
    const req = onConfirm.mock.calls[0][0];
    expect(req.replaceOrder).toBeUndefined();
    expect(req.newPrice).toBeCloseTo(-2.5, 4);
  });

  it("BAG order: zero limit is rejected", () => {
    const onConfirm = vi.fn();
    const { getByLabelText, getByRole } = render(
      <ModifyOrderModal
        order={makeBagOrder(
          [
            { action: "SELL", strike: 200, right: "P" },
            { action: "BUY", strike: 190, right: "P" },
          ],
          -2.45,
        )}
        loading={false}
        prices={PRICES}
        onConfirm={onConfirm}
        onClose={() => {}}
      />,
    );

    fireEvent.change(
      getByLabelText(/New Net Price|New Limit Price/i) as HTMLInputElement,
      { target: { value: "0" } },
    );
    const submit = getByRole("button", {
      name: /Modify Order/i,
    }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    fireEvent.click(submit);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("single-leg order: negative limit is still rejected", () => {
    const onConfirm = vi.fn();
    const { getByLabelText, getByRole } = render(
      <ModifyOrderModal
        order={makeSingleLegOrder()}
        loading={false}
        prices={PRICES}
        onConfirm={onConfirm}
        onClose={() => {}}
      />,
    );

    fireEvent.change(
      getByLabelText(/New Net Price|New Limit Price/i) as HTMLInputElement,
      { target: { value: "-2.50" } },
    );
    const submit = getByRole("button", {
      name: /Modify Order/i,
    }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    fireEvent.click(submit);
    expect(onConfirm).not.toHaveBeenCalled();
  });
});

/* ─── OrderTab: one normalized leg set for quote + guard + submit ─── */

describe("OrderTab — unequal-ratio position", () => {
  // Held structure: LONG 2× TEST 200C / SHORT 1× TEST 210C.
  // Per-unit quote (ratio 2:1): bid = 2×4.50−2.20 = 6.80,
  // ask = 2×4.70−2.00 = 7.40, mid = 7.10.
  const RATIO_POSITION = makePosition(
    [
      { type: "Call", strike: 200, direction: "LONG", contracts: 2, entry_cost: 900, avg_cost: 450, market_price: 4.6, market_value: 920, market_price_is_calculated: false },
      { type: "Call", strike: 210, direction: "SHORT", contracts: 1, entry_cost: -210, avg_cost: 210, market_price: 2.1, market_value: -210, market_price_is_calculated: false },
    ],
    2,
  );
  const RATIO_QUOTE = { bid: 6.8, ask: 7.4, mid: 7.1 };

  it("submits gcd-reduced leg ratios + unit quantity matching the displayed quote", async () => {
    const fetchMock = vi.fn<typeof fetch>();
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const { container, getByPlaceholderText, getByRole } = render(
      <OrderTab
        ticker={TICKER}
        position={RATIO_POSITION}
        portfolio={makePortfolio(RATIO_POSITION)}
        prices={PRICES}
        openOrders={[]}
      />,
    );

    // The quote strip shows the per-unit 2:1 structure price.
    assertStripValues(
      container.querySelector(".spread-price-strip"),
      RATIO_QUOTE,
    );

    // Default quantity is the normalized structure-unit count (gcd), not
    // the raw per-leg contract count.
    const qtyInput = getByPlaceholderText("Contracts") as HTMLInputElement;
    expect(qtyInput.value).toBe("1");

    // Set the limit price to the displayed mid.
    fireEvent.click(getByRole("button", { name: /^MID 7\.10/ }));
    const priceInput = container.querySelector(
      ".modify-price-input",
    ) as HTMLInputElement;
    expect(priceInput.value).toBe("7.10");

    fireEvent.click(getByRole("button", { name: /Place Combo Order/i }));
    fireEvent.click(
      await waitFor(() => getByRole("button", { name: /Confirm Order/i })),
    );

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some((c) =>
          String(
            typeof c[0] === "string" ? c[0] : (c[0] as Request).url,
          ).includes("/api/orders/place"),
        ),
      ).toBe(true),
    );

    const placeCall = fetchMock.mock.calls.find((c) =>
      String(
        typeof c[0] === "string" ? c[0] : (c[0] as Request).url,
      ).includes("/api/orders/place"),
    )!;
    const body = JSON.parse(String(placeCall[1]?.body));

    expect(body.type).toBe("combo");
    expect(body.limitPrice).toBeCloseTo(7.1, 4);
    expect(body.quantity).toBe(1);
    expect(body.legs).toEqual([
      expect.objectContaining({ strike: 200, action: "BUY", ratio: 2 }),
      expect.objectContaining({ strike: 210, action: "SELL", ratio: 1 }),
    ]);
  });
});

/* ─── OptionsChainTab: zero-crossing quote keeps its sign end-to-end ─── */

describe("OptionsChainTab — zero-crossing combo quote", () => {
  // Hypothetical quotes chosen so the natural market straddles zero:
  //   BUY 200C 4.50/4.70, SELL 210C 4.55/4.75
  //   bid = 4.50 − 4.75 = −0.25   ask = 4.70 − 4.55 = +0.15   mid = −0.05
  const ZX_PRICES: Record<string, PriceData> = {
    TEST_20260619_200_C: makePriceData(4.5, 4.7),
    TEST_20260619_210_C: makePriceData(4.55, 4.75),
  };

  const fetchMock = vi.fn<typeof fetch>();
  let placeBody: Record<string, unknown> | null = null;
  let planBody: Record<string, unknown> | null = null;
  let submitBody: Record<string, unknown> | null = null;
  let repriceBody: Record<string, unknown> | null = null;
  // Wizard session state is pushed over a live SSE ReadableStream so the
  // test can advance PLANNED → WORKING deterministically.
  const sseEncoder = new TextEncoder();
  let streamController: ReadableStreamDefaultController<Uint8Array> | null =
    null;
  const pushSession = (state: Record<string, unknown>) => {
    streamController?.enqueue(
      sseEncoder.encode(
        `data: ${JSON.stringify({ session_id: "s1", ...state })}\n\n`,
      ),
    );
  };

  beforeEach(() => {
    placeBody = null;
    planBody = null;
    submitBody = null;
    repriceBody = null;
    streamController = null;
    vi.stubGlobal("fetch", fetchMock);
    if (!("scrollIntoView" in HTMLElement.prototype)) {
      Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
        configurable: true,
        value: vi.fn(),
      });
    } else {
      vi.spyOn(HTMLElement.prototype, "scrollIntoView").mockImplementation(
        () => {},
      );
    }

    fetchMock.mockImplementation(async (input, init) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : String((input as Request).url);
      if (url.includes("/api/orders/place")) {
        placeBody = JSON.parse(String(init?.body ?? "{}"));
        return new Response(JSON.stringify({ status: "ok" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/api/wizard/plan")) {
        planBody = JSON.parse(String(init?.body ?? "{}"));
        return new Response(JSON.stringify({ session_id: "s1" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/api/wizard/stream")) {
        const stream = new ReadableStream<Uint8Array>({
          start(c) {
            streamController = c;
          },
        });
        queueMicrotask(() => pushSession({ state: "PLANNED" }));
        return new Response(stream, {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      }
      if (
        url.includes("/api/wizard/sessions/") &&
        url.endsWith("/submit")
      ) {
        submitBody = JSON.parse(String(init?.body ?? "{}"));
        return new Response("{}", {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (
        url.includes("/api/wizard/sessions/") &&
        url.endsWith("/reprice")
      ) {
        repriceBody = JSON.parse(String(init?.body ?? "{}"));
        return new Response("{}", {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/api/options/expirations")) {
        return new Response(
          JSON.stringify({ symbol: TICKER, expirations: [EXPIRY] }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url.includes("/api/options/chain")) {
        return new Response(
          JSON.stringify({
            symbol: TICKER,
            expiry: EXPIRY,
            strikes: [200, 210],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url.includes("/api/previous-close")) {
        return new Response(JSON.stringify({ closes: { TEST: 205 } }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("{}", {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function strikeRow(label: string): HTMLElement {
    const cell = Array.from(
      document.querySelectorAll<HTMLElement>(".chain-strike"),
    ).find((el) => el.textContent === label);
    expect(cell, `strike row ${label}`).toBeTruthy();
    return cell!.closest("tr")!;
  }

  it("clicking ASK 0.15 submits +0.15 (not sign-rewritten by mid direction)", async () => {
    render(
      React.createElement(
        TickerDetailProvider,
        null,
        React.createElement(OptionsChainTab, {
          ticker: TICKER,
          prices: ZX_PRICES,
          tickerPriceData: null,
        }),
      ),
    );

    // Wait for the chain grid, then build BUY 200C / SELL 210C by clicking
    // the real cells (call ask/mid cells = BUY, call bid cell = SELL).
    await waitFor(() =>
      expect(
        document.querySelectorAll(".chain-strike").length,
      ).toBeGreaterThanOrEqual(2),
    );
    fireEvent.click(
      strikeRow("$200.00").querySelector('[title="Buy call"]')!,
    );
    fireEvent.click(
      strikeRow("$210.00").querySelector('[title="Sell call"]')!,
    );

    // The quick-order ASK button shows the signed natural ask (+0.15).
    const askBtn = await waitFor(() => {
      const b = Array.from(
        document.querySelectorAll<HTMLButtonElement>(".btn-quick"),
      ).find((x) => /^ASK/.test(x.textContent ?? ""));
      expect(b, "ASK quick button").toBeTruthy();
      return b!;
    });
    expect(askBtn.textContent).toContain("0.15");
    fireEvent.click(askBtn);

    const priceInput = await waitFor(() => {
      const el = Array.from(
        document.querySelectorAll<HTMLInputElement>(".modify-price-input"),
      ).find((x) => x.value === "0.15");
      expect(el, "limit price input = 0.15").toBeTruthy();
      return el!;
    });
    expect(priceInput.value).toBe("0.15");

    // Place → confirm → capture the payload.
    const placeBtn = Array.from(
      document.querySelectorAll<HTMLButtonElement>(".order-submit button"),
    ).find((b) => b.textContent?.startsWith("Place "));
    expect(placeBtn, "Place button").toBeTruthy();
    fireEvent.click(placeBtn!);
    await waitFor(() => {
      const confirm = Array.from(
        document.querySelectorAll<HTMLButtonElement>("button"),
      ).find((b) => b.textContent === "Confirm Order");
      expect(confirm, "Confirm Order button").toBeTruthy();
      fireEvent.click(confirm!);
    });

    await waitFor(() => expect(placeBody).not.toBeNull());
    // The selected signed limit goes through verbatim — the displayed
    // +0.15 must NOT be rewritten to −0.15 by the debit/credit mid.
    expect(placeBody!.type).toBe("combo");
    expect(placeBody!.limitPrice).toBeCloseTo(0.15, 4);

    // Wizard initial order_payload carries the same signed limit.
    fireEvent.click(
      Array.from(document.querySelectorAll("button")).find(
        (b) => b.textContent === "Open Wizard",
      )!,
    );
    await waitFor(() => expect(planBody).not.toBeNull());
    expect(
      (planBody!.order_payload as { limitPrice: number }).limitPrice,
    ).toBeCloseTo(0.15, 4);

    // Wizard submit target (session PLANNED → WIZ_SUBMIT button live).
    await waitFor(() => {
      const btn = Array.from(
        document.querySelectorAll<HTMLButtonElement>("button"),
      ).find((b) => b.textContent === "WIZ_SUBMIT");
      expect(btn, "WIZ_SUBMIT button").toBeTruthy();
      fireEvent.click(btn!);
    });
    await waitFor(() => expect(submitBody).not.toBeNull());
    expect(submitBody!.target_price).toBeCloseTo(0.15, 4);

    // Natural reprice keeps the signed ask (+0.15, not −0.15). Submit's
    // refresh swaps in a new SSE stream, so re-push WORKING on each poll
    // until the live stream picks it up.
    await waitFor(() => {
      pushSession({ state: "WORKING", current_attempt_id: "a1" });
      const btn = Array.from(
        document.querySelectorAll<HTMLButtonElement>("button"),
      ).find((b) => b.textContent === "WIZ_REPRICE_NATURAL");
      expect(btn, "WIZ_REPRICE_NATURAL button").toBeTruthy();
      fireEvent.click(btn!);
    });
    await waitFor(() => expect(repriceBody).not.toBeNull());
    expect(repriceBody!.target_price).toBeCloseTo(0.15, 4);
  });
});

/* ─── ModifyOrderModal: per-leg symbol identity ─── */

describe("ModifyOrderModal — mixed-symbol BAG", () => {
  // Two symbols colliding on strike/right/expiry: AAA 200C vs BBB 200C.
  // Correct per-leg pricing: bid = 4.50 − 2.20 = 2.30, ask = 4.70 − 2.00 = 2.70.
  // If legs were priced under a single symbol, both legs would quote under
  // AAA (or the envelope symbol) — bid = 4.50 − 4.70, ask = 4.70 − 4.50.
  const MIXED_PRICES: Record<string, PriceData> = {
    AAA_20260619_200_C: makePriceData(4.5, 4.7),
    BBB_20260619_200_C: makePriceData(2.0, 2.2),
  };

  it("prices each combo leg under its own symbol", () => {
    const { container } = render(
      <ModifyOrderModal
        order={makeBagOrder([
          { action: "BUY", strike: 200, right: "C", symbol: "AAA" },
          { action: "SELL", strike: 200, right: "C", symbol: "BBB" },
        ])}
        loading={false}
        prices={MIXED_PRICES}
        onConfirm={() => {}}
        onClose={() => {}}
      />,
    );

    const strip = container.querySelector(".order-price-strip-compact");
    const fmt = (n: number) => `$${n.toFixed(2)}`;
    const items = strip?.querySelectorAll(".order-price-item") ?? [];
    const text = (label: string) =>
      Array.from(items)
        .find(
          (el) => el.querySelector(".order-price-label")?.textContent === label,
        )
        ?.querySelector(".order-price-value")?.textContent ?? null;
    expect(text("BID")).toBe(fmt(2.3));
    expect(text("MID")).toBe(fmt(2.5));
    expect(text("ASK")).toBe(fmt(2.7));
  });
});
