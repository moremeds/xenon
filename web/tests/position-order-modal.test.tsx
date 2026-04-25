/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, afterEach, vi } from "vitest";
import { render, cleanup, fireEvent, waitFor } from "@testing-library/react";
import PositionOrderModal from "@/components/PositionOrderModal";
import type { PortfolioPosition } from "@/lib/types";

vi.mock("@/components/Modal", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="modal">{children}</div>
  ),
}));

const markSubmitted = vi.fn();
const markTerminal = vi.fn();
const onFieldEdit = vi.fn();
vi.mock("@/components/ticker-detail/useClientAttemptId", () => ({
  useClientAttemptId: () => ({
    id: "test-attempt-id-123",
    markSubmitted,
    markTerminal,
    onFieldEdit,
  }),
}));

afterEach(() => {
  cleanup();
  markSubmitted.mockReset();
  markTerminal.mockReset();
  onFieldEdit.mockReset();
});

const stockPos: PortfolioPosition = {
  id: 1,
  ticker: "TSLA",
  structure: "Stock",
  structure_type: "Stock",
  risk_profile: "equity",
  expiry: "",
  contracts: 300,
  direction: "LONG",
  entry_cost: 96000,
  max_risk: null,
  market_value: 105000,
  legs: [
    {
      direction: "LONG",
      contracts: 300,
      type: "Stock",
      strike: null,
      entry_cost: 96000,
      avg_cost: 320,
      market_price: 350,
      market_value: 105000,
      market_price_is_calculated: false,
    },
  ],
  ib_daily_pnl: null,
  kelly_optimal: null,
  target: null,
  stop: null,
  entry_date: "",
};

describe("PositionOrderModal — Close/Add toggle", () => {
  it("defaults to Close intent for a LONG stock → action SELL", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ orderId: "abc", status: "ok" }),
    });
    (global as any).fetch = fetchMock;
    const { getByRole } = render(
      <PositionOrderModal
        position={stockPos}
        prices={{ TSLA: { last: 350, bid: 349.9, ask: 350.1 } as any }}
        onClose={() => {}}
      />,
    );
    fireEvent.click(getByRole("button", { name: /^Submit/i }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const body = JSON.parse((fetchMock.mock.calls[0] as any)[1].body);
    expect(body.action).toBe("SELL");
    expect(body.client_attempt_id).toBe("test-attempt-id-123");
  });

  it("switching to Add toggles action to BUY for the same LONG stock", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ orderId: "abc", status: "ok" }),
    });
    (global as any).fetch = fetchMock;
    const { getByRole } = render(
      <PositionOrderModal
        position={stockPos}
        prices={{ TSLA: { last: 350, bid: 349.9, ask: 350.1 } as any }}
        onClose={() => {}}
      />,
    );
    fireEvent.click(getByRole("button", { name: /^Add$/i }));
    fireEvent.click(getByRole("button", { name: /^Submit/i }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const body = JSON.parse((fetchMock.mock.calls[0] as any)[1].body);
    expect(body.action).toBe("BUY");
  });
});

describe("PositionOrderModal — time in force", () => {
  it("submits selected GTC time-in-force", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ orderId: "abc", status: "ok" }),
    });
    (global as any).fetch = fetchMock;
    const { getByRole } = render(
      <PositionOrderModal
        position={stockPos}
        prices={{ TSLA: { last: 350, bid: 349.9, ask: 350.1 } as any }}
        onClose={() => {}}
      />,
    );

    fireEvent.click(getByRole("button", { name: "GTC" }));
    fireEvent.click(getByRole("button", { name: /^Submit/i }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const body = JSON.parse((fetchMock.mock.calls[0] as any)[1].body);
    expect(body.tif).toBe("GTC");
  });
});

describe("PositionOrderModal — BID / MID / ASK quick buttons", () => {
  it("clicking BID sets limit price to bid", () => {
    const { getByRole, getByLabelText } = render(
      <PositionOrderModal
        position={stockPos}
        prices={{ TSLA: { last: 350, bid: 349.5, ask: 350.5 } as any }}
        onClose={() => {}}
      />,
    );
    fireEvent.click(getByRole("button", { name: /^BID/i }));
    const price = getByLabelText(/Limit Price/i) as HTMLInputElement;
    expect(price.value).toBe("349.50");
  });

  it("clicking ASK sets limit price to ask", () => {
    const { getByRole, getByLabelText } = render(
      <PositionOrderModal
        position={stockPos}
        prices={{ TSLA: { last: 350, bid: 349.5, ask: 350.5 } as any }}
        onClose={() => {}}
      />,
    );
    fireEvent.click(getByRole("button", { name: /^ASK/i }));
    const price = getByLabelText(/Limit Price/i) as HTMLInputElement;
    expect(price.value).toBe("350.50");
  });
});

describe("PositionOrderModal — input UX", () => {
  it("user can clear the qty input via backspace and retype", () => {
    const { getByLabelText } = render(
      <PositionOrderModal
        position={stockPos}
        prices={{ TSLA: { last: 350, bid: 349.9, ask: 350.1 } as any }}
        onClose={() => {}}
      />,
    );
    const qty = getByLabelText(/Quantity/i) as HTMLInputElement;
    fireEvent.change(qty, { target: { value: "" } });
    expect(qty.value).toBe("");
    fireEvent.change(qty, { target: { value: "42" } });
    expect(qty.value).toBe("42");
  });

  it("user can type a minus sign in limit price (combo credit spread)", () => {
    const { getByLabelText } = render(
      <PositionOrderModal
        position={stockPos}
        prices={{ TSLA: { last: 350, bid: 349.9, ask: 350.1 } as any }}
        onClose={() => {}}
      />,
    );
    const price = getByLabelText(/Limit Price/i) as HTMLInputElement;
    fireEvent.change(price, { target: { value: "-" } });
    expect(price.value).toBe("-");
  });

  it("editing qty after a submit attempt rolls the client_attempt_id (calls onFieldEdit)", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: false, json: async () => ({ error: "bad" }) });
    (global as any).fetch = fetchMock;
    const { getByRole, getByLabelText } = render(
      <PositionOrderModal
        position={stockPos}
        prices={{ TSLA: { last: 350, bid: 349.9, ask: 350.1 } as any }}
        onClose={() => {}}
      />,
    );
    fireEvent.click(getByRole("button", { name: /^Submit/i }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    fireEvent.change(getByLabelText(/Quantity/i), { target: { value: "100" } });
    expect(onFieldEdit).toHaveBeenCalledWith("quantity");
  });
});

describe("PositionOrderModal — leg pills (combo)", () => {
  const bullCallSpread: PortfolioPosition = {
    id: 2,
    ticker: "SPY",
    structure: "Bull Call Spread",
    structure_type: "BullCallSpread",
    risk_profile: "defined",
    expiry: "2026-06-19",
    contracts: 4,
    direction: "LONG",
    entry_cost: 1200,
    max_risk: 1200,
    market_value: 1400,
    legs: [
      {
        direction: "LONG",
        contracts: 4,
        type: "Call",
        strike: 200,
        entry_cost: 1800,
        avg_cost: 4.5,
        market_price: 5,
        market_value: 2000,
        market_price_is_calculated: false,
      },
      {
        direction: "SHORT",
        contracts: 4,
        type: "Call",
        strike: 210,
        entry_cost: -600,
        avg_cost: 1.5,
        market_price: 1.5,
        market_value: -600,
        market_price_is_calculated: false,
      },
    ],
    ib_daily_pnl: null,
    kelly_optimal: null,
    target: null,
    stop: null,
    entry_date: "",
  };

  it("renders the OrderLegPills strip for a combo position", () => {
    const { container } = render(
      <PositionOrderModal
        position={bullCallSpread}
        prices={{}}
        onClose={() => {}}
      />,
    );
    expect(container.querySelector(".order-leg-pills")).toBeTruthy();
  });
});
