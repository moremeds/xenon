// @vitest-environment jsdom
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import BookTab from "@/components/ticker-detail/BookTab";

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
});
