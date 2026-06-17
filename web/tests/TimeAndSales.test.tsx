// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { TimeAndSales } from "@/components/ticker-detail/TimeAndSales";
import type { Trade } from "@/lib/pricesProtocol";

afterEach(cleanup);

// Oldest-first (relay ring order). classifyTicks: 10=flat (first), 11=up.
const trades: Trade[] = [
  { price: 10, size: 1, exchange: "X", time: "1" },
  { price: 11, size: 2, exchange: "Y", time: "2" },
];

describe("TimeAndSales", () => {
  it("applies the tick-test tone class (uptick = up)", () => {
    render(<TimeAndSales trades={trades} visible />);
    expect(screen.getByText("11.00").className).toContain("up");
    expect(screen.getByText("10.00").className).toContain("flat");
  });

  it("clicking an uptick print fills BUY at that price, qty, source tape", () => {
    const onClick = vi.fn();
    render(<TimeAndSales trades={trades} visible onPriceClick={onClick} />);
    fireEvent.click(screen.getByText("11.00"));
    expect(onClick).toHaveBeenCalledWith(
      expect.objectContaining({
        price: 11,
        action: "BUY",
        quantity: 2,
        source: "tape",
      }),
    );
  });

  it("shows an empty-state (not blank rows) when there are no prints", () => {
    const { container } = render(<TimeAndSales trades={[]} visible />);
    expect(container.querySelector(".book-tape-empty")).toBeTruthy();
    expect(screen.getByText(/No prints/)).toBeTruthy();
  });
});
