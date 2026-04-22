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

vi.mock("@/components/ticker-detail/useClientAttemptId", () => ({
  useClientAttemptId: () => ({
    id: "test-attempt-id-123",
    markSubmitted: vi.fn(),
    markTerminal: vi.fn(),
    onFieldEdit: vi.fn(),
  }),
}));

afterEach(() => cleanup());

const pos: PortfolioPosition = {
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

describe("PositionOrderModal — Close form", () => {
  it("shows default qty equal to full position.contracts", () => {
    const { getByLabelText } = render(
      <PositionOrderModal
        position={pos}
        prices={{ TSLA: { last: 350 } as any }}
        onClose={() => {}}
      />,
    );
    const qty = getByLabelText(/Quantity/i) as HTMLInputElement;
    expect(qty.value).toBe("300");
  });

  it("50% chip halves qty and shows partial-close note", () => {
    const { getByRole, getByText } = render(
      <PositionOrderModal
        position={pos}
        prices={{ TSLA: { last: 350 } as any }}
        onClose={() => {}}
      />,
    );
    fireEvent.click(getByRole("button", { name: /^50%$/ }));
    expect(getByText(/Partial close — 150 of 300/)).toBeTruthy();
  });

  it("submits POST /api/orders/place with close payload and closes modal on 200", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ orderId: "abc123", status: "ok" }),
    });
    (global as any).fetch = fetchMock;

    const onClose = vi.fn();
    const onSubmitted = vi.fn();
    const { getByRole } = render(
      <PositionOrderModal
        position={pos}
        prices={{ TSLA: { last: 350 } as any }}
        onClose={onClose}
        onSubmitted={onSubmitted}
      />,
    );
    fireEvent.click(getByRole("button", { name: /Submit close/i }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/orders/place");
    const body = JSON.parse((init as any).body);
    expect(body).toMatchObject({
      type: "stock",
      symbol: "TSLA",
      action: "SELL",
      quantity: 300,
      tif: "DAY",
    });
    expect(typeof body.client_attempt_id).toBe("string");
    expect(body.client_attempt_id.length).toBeGreaterThan(0);
    await waitFor(() => expect(onSubmitted).toHaveBeenCalledWith("abc123"));
    expect(onClose).toHaveBeenCalled();
  });
});

describe("PositionOrderModal — preset tiles", () => {
  it("renders four preset tiles: Close active, others disabled", () => {
    const { getByRole } = render(
      <PositionOrderModal
        position={pos}
        prices={{ TSLA: { last: 350 } as any }}
        onClose={() => {}}
      />,
    );
    const close = getByRole("button", { name: /^Close$/ });
    const tsl = getByRole("button", { name: /Trailing Stop Loss/i });
    const ttp = getByRole("button", { name: /Trailing Take Profit/i });
    const roll = getByRole("button", { name: /^Roll$/ });
    expect(close.getAttribute("aria-pressed")).toBe("true");
    expect(tsl.hasAttribute("disabled")).toBe(true);
    expect(ttp.hasAttribute("disabled")).toBe(true);
    expect(roll.hasAttribute("disabled")).toBe(true);
    expect(tsl.getAttribute("title")).toMatch(/coming soon/i);
    expect(roll.getAttribute("title")).toMatch(/coming soon/i);
  });
});
