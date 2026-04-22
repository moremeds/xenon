/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, afterEach, vi } from "vitest";
import { render, cleanup } from "@testing-library/react";
import PositionOrderModal from "@/components/PositionOrderModal";
import type { PortfolioPosition } from "@/lib/types";

vi.mock("@/components/Modal", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="modal">{children}</div>
  ),
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
