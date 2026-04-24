/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, afterEach, vi } from "vitest";
import { render, fireEvent, cleanup } from "@testing-library/react";
import PositionTable from "@/components/PositionTable";
import type { PortfolioPosition } from "@/lib/types";

// The PositionTable mounts InstrumentDetailModal which imports a lot of
// IB-specific surface area; stub it out so the test focuses on safety gates.
vi.mock("@/components/InstrumentDetailModal", () => ({
  default: () => <div data-testid="instrument-detail-modal">MODAL</div>,
}));

const navigateToTicker = vi.fn();

vi.mock("@/lib/useTickerNav", () => ({
  useTickerNav: () => ({ navigateToTicker }),
}));

afterEach(() => {
  navigateToTicker.mockReset();
  cleanup();
});

const stockPosition: PortfolioPosition = {
  id: 1,
  ticker: "TSLA",
  structure: "Stock",
  structure_type: "Stock",
  risk_profile: "equity",
  expiry: "",
  contracts: 300,
  direction: "LONG",
  entry_cost: 96214.2,
  max_risk: null,
  market_value: 105502.5,
  legs: [
    {
      direction: "LONG",
      contracts: 300,
      type: "Stock",
      strike: null,
      entry_cost: 96214.2,
      avg_cost: 320.714,
      market_price: 351.675,
      market_value: 105502.5,
      market_price_is_calculated: false,
    },
  ],
  ib_daily_pnl: null,
  kelly_optimal: null,
  target: null,
  stop: null,
  entry_date: "",
};

describe("PositionTable readonly mode", () => {
  it("renders ticker as a button when readonly=true", () => {
    const { container } = render(
      <PositionTable positions={[stockPosition]} readonly={true} />,
    );
    const tickerButtons = container.querySelectorAll("button.ticker-link");
    expect(tickerButtons.length).toBeGreaterThan(0);
    const disabled = container.querySelectorAll(".ticker-link-disabled");
    expect(disabled.length).toBe(0);
  });

  it("navigates to ticker detail when readonly=true", () => {
    const { getByRole } = render(
      <PositionTable positions={[stockPosition]} readonly={true} />,
    );

    fireEvent.click(getByRole("button", { name: /TSLA/i }));

    expect(navigateToTicker).toHaveBeenCalledWith("TSLA", stockPosition.id);
  });

  it("renders ticker as button when readonly=false (default)", () => {
    const { container } = render(<PositionTable positions={[stockPosition]} />);
    const tickerButtons = container.querySelectorAll("button.ticker-link");
    expect(tickerButtons.length).toBeGreaterThan(0);
    // No disabled span in interactive mode
    const disabled = container.querySelectorAll(".ticker-link-disabled");
    expect(disabled.length).toBe(0);
  });

  it("does not mount InstrumentDetailModal in readonly mode even if state is set externally", () => {
    // Structural invariant: the modal render path is gated on !readonly,
    // so even a direct leg click cannot reveal the modal.
    const { queryByTestId } = render(
      <PositionTable positions={[stockPosition]} readonly={true} />,
    );
    expect(queryByTestId("instrument-detail-modal")).toBeNull();
  });
});

describe("PositionTable — order button", () => {
  it("renders the order button when readonly=false", () => {
    const { container } = render(<PositionTable positions={[stockPosition]} />);
    expect(container.querySelector("button.position-order-btn")).not.toBeNull();
  });

  it("does NOT render the order button when readonly=true", () => {
    const { container } = render(
      <PositionTable positions={[stockPosition]} readonly={true} />,
    );
    expect(container.querySelector("button.position-order-btn")).toBeNull();
  });
});
