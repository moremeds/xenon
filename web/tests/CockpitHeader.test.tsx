// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import CockpitHeader from "@/components/ticker-detail/CockpitHeader";

vi.mock("@/lib/useWatchlist", () => ({
  useWatchlist: () => ({ isWatched: () => false, toggleWatch: vi.fn() }),
}));

describe("CockpitHeader", () => {
  it("renders ticker, kind, last and delta from quotePriceData", () => {
    render(
      <CockpitHeader
        ticker="SPX"
        kind="stock"
        quotePriceData={
          {
            symbol: "SPX",
            last: 5500,
            close: 5450,
            bid: 5499,
            ask: 5501,
          } as never
        }
        position={null}
        live
        onDeckChange={vi.fn()}
      />,
    );
    expect(screen.getByText("SPX")).toBeTruthy();
    expect(screen.getByText("STOCK")).toBeTruthy();
    expect(screen.getByText(/LIVE/)).toBeTruthy();
    expect(screen.getByText("FLAT")).toBeTruthy(); // no position chip
  });
});
