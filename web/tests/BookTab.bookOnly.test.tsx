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

  it("shows the option contract spec ($strike·right·expiry) in the head, not just the underlying", () => {
    const { container } = render(
      <BookTab
        ticker="QQQ"
        position={null}
        prices={{}}
        openOrders={[]}
        tickerPriceData={null}
        bookOnly
        bookKey="QQQ_20260717_692_P"
        bookKind="option"
      />,
    );
    const sym = container.querySelector(".book-sym");
    expect(sym?.textContent).toContain("$692P");
    expect(sym?.textContent).toContain("07/17/26");
  });

  it("option book does NOT borrow the underlying L1 when the option quote is absent", () => {
    // tickerPriceData (the book subject's quote) is null; prices has only the
    // underlying. The option book must show "---", never the stock's bid/ask —
    // web/CLAUDE.md forbids showing underlying price where an option is expected.
    const { container } = render(
      <BookTab
        ticker="QQQ"
        position={null}
        prices={{
          QQQ: {
            symbol: "QQQ",
            last: 733.7,
            bid: 733.65,
            ask: 733.75,
          } as never,
        }}
        openOrders={[]}
        tickerPriceData={null}
        bookOnly
        bookKey="QQQ_20260717_692_P"
        bookKind="option"
      />,
    );
    // L1 fallback renders (no entitled depth), but none of the underlying's
    // 733.x scalars leak into the option head or the L1 panel.
    expect(screen.getByText("ORDER BOOK")).toBeTruthy();
    expect(container.textContent).not.toContain("733");
    // The option spec head is still shown.
    expect(container.querySelector(".book-sym")?.textContent).toContain(
      "$692P",
    );
  });

  it("links the underlying symbol in the option head to its stock page", () => {
    const { container } = render(
      <BookTab
        ticker="QQQ"
        position={null}
        prices={{}}
        openOrders={[]}
        tickerPriceData={null}
        bookOnly
        bookKey="QQQ_20260717_692_P"
        bookKind="option"
      />,
    );
    const link = container.querySelector(".book-sym .book-sym-link");
    expect(link).toBeTruthy();
    expect(link?.tagName).toBe("A");
    expect(link?.getAttribute("href")).toBe("/QQQ");
    expect(link?.textContent).toBe("QQQ");
  });
});
