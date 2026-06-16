// @vitest-environment jsdom
import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import BookTab from "@/components/ticker-detail/BookTab";
import type { DepthBook } from "@/lib/pricesProtocol";

afterEach(cleanup);

const baseProps = {
  ticker: "SPX",
  position: null,
  prices: {},
  openOrders: [],
  tickerPriceData: null,
  bookKey: "SPX",
  bookKind: "stock" as const,
};

const entitledBook: DepthBook = {
  symbol: "SPX",
  kind: "stock",
  bid: [{ price: 1, size: 10, marketMaker: "ARCA", exchange: "ARCA" }],
  ask: [{ price: 2, size: 20, marketMaker: "NSDQ", exchange: "NSDQ" }],
  isSmartDepth: true,
  feed: "SMART DEPTH",
  entitled: true,
  timestamp: "t",
};

describe("BookTab bookOnly", () => {
  it("renders the L1 book region without the position/order summary chrome when bookOnly", () => {
    const { container } = render(
      <BookTab
        ticker="SPX"
        position={null}
        prices={{}}
        openOrders={[]}
        tickerPriceData={null}
        bookOnly
      />,
    );
    // bookOnly wraps content in .book-tab-only (cockpit book-region styling)
    expect(container.querySelector(".book-tab-only")).toBeTruthy();
  });

  it("bookOnly renders the L2 OrderBook montage when an entitled depth book is present", () => {
    render(
      <BookTab
        {...baseProps}
        bookOnly
        depths={{ [baseProps.bookKey]: entitledBook }}
      />,
    );
    expect(screen.getByTestId("order-book")).toBeTruthy();
    // Montage (not the L1 fallback) renders — the L1 "ORDER BOOK" header is absent.
    expect(screen.queryByText("ORDER BOOK")).toBeNull();
  });

  it("bookOnly falls back to L1 when depth is unentitled", () => {
    render(
      <BookTab
        {...baseProps}
        bookOnly
        depths={{ [baseProps.bookKey]: { ...entitledBook, entitled: false } }}
      />,
    );
    expect(screen.getByText("ORDER BOOK")).toBeTruthy(); // existing L1 header
  });
});
