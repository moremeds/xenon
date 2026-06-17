// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { DepthMontage } from "@/components/ticker-detail/DepthMontage";
import type { DepthBook } from "@/lib/pricesProtocol";

afterEach(cleanup);

const book: DepthBook = {
  symbol: "QQQ",
  kind: "stock",
  isSmartDepth: true,
  feed: "SMART",
  entitled: true,
  timestamp: "t",
  bid: [{ price: 500.1, size: 300, marketMaker: "ARCA", exchange: "ARCA" }],
  ask: [{ price: 500.2, size: 200, marketMaker: "NSDQ", exchange: "NSDQ" }],
};

describe("DepthMontage", () => {
  it("renders bid and ask rows", () => {
    render(<DepthMontage book={book} onPriceClick={() => {}} />);
    expect(screen.getByText("500.10")).toBeTruthy();
    expect(screen.getByText("500.20")).toBeTruthy();
  });

  it("clicking a bid level fills SELL at that price", () => {
    const onClick = vi.fn();
    render(<DepthMontage book={book} onPriceClick={onClick} />);
    fireEvent.click(screen.getByText("500.10"));
    expect(onClick).toHaveBeenCalledWith(
      expect.objectContaining({
        price: 500.1,
        action: "SELL",
        source: "montage",
      }),
    );
  });

  it("clicking an ask level fills BUY at that price", () => {
    const onClick = vi.fn();
    render(<DepthMontage book={book} onPriceClick={onClick} />);
    fireEvent.click(screen.getByText("500.20"));
    expect(onClick).toHaveBeenCalledWith(
      expect.objectContaining({
        price: 500.2,
        action: "BUY",
        source: "montage",
      }),
    );
  });

  it("places the NBBO tag LEFT of the price on bid, RIGHT of the price on ask", () => {
    const optionBook: DepthBook = {
      symbol: "QQQ",
      kind: "option",
      isSmartDepth: true,
      feed: "OPRA",
      entitled: true,
      timestamp: "t",
      bid: [{ price: 6.66, size: 9, exchange: "CBOE2", nbbo: true }],
      ask: [{ price: 6.72, size: 1, exchange: "BATS", nbbo: true }],
    };
    const { container } = render(<DepthMontage book={optionBook} />);
    const bidPx = container.querySelector(".book-side.bid .book-px");
    const askPx = container.querySelector(".book-side.ask .book-px");
    // bid: tag is the FIRST child (before the price text); ask: the LAST child.
    expect(bidPx?.firstElementChild?.className).toContain("book-nbbo-tag");
    expect(askPx?.lastElementChild?.className).toContain("book-nbbo-tag");
  });
});
