// @vitest-environment jsdom
import { describe, it, expect, afterEach, beforeEach } from "vitest";
import { render, screen, cleanup, act } from "@testing-library/react";
import { OrderBook } from "@/components/ticker-detail/OrderBook";

// Wrap cleanup in act() so React's concurrent scheduler flushes all pending
// setImmediate callbacks before JSDOM tears down — prevents "window is not
// defined" errors bleeding into adjacent test files' environments.
afterEach(() => act(cleanup));
beforeEach(() => {
  try {
    window.localStorage.clear();
  } catch {
    /* ignore */
  }
});

const baseProps = {
  depth: null,
  trades: [],
  last: 5.78,
  bid: null,
  ask: null,
  l1Fallback: <div data-testid="l1" />,
};

describe("OrderBook head — kind tag", () => {
  it("does NOT render the OPTION tag in an option head (spec is self-evident)", () => {
    const { container } = render(
      <OrderBook
        {...baseProps}
        symbolLabel="QQQ $692P 07/17/26"
        kind="option"
      />,
    );
    expect(container.querySelector(".book-kind")).toBeNull();
  });

  it("still renders the STOCK tag in a stock head", () => {
    const { container } = render(
      <OrderBook {...baseProps} symbolLabel="QQQ" kind="stock" last={733.7} />,
    );
    const tag = container.querySelector(".book-kind");
    expect(tag?.textContent).toBe("STOCK");
  });
});

describe("OrderBook tape — default visibility", () => {
  it("defaults the tape COLLAPSED for an option (options have no tape)", () => {
    const { container } = render(
      <OrderBook
        {...baseProps}
        symbolLabel="QQQ $692P 07/17/26"
        kind="option"
      />,
    );
    const toggle = screen.getByRole("switch", {
      name: /toggle time and sales/i,
    });
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    expect(container.querySelector(".book-body-grid.tape-hidden")).toBeTruthy();
  });

  it("defaults the tape SHOWN for a stock (no stored preference)", () => {
    const { container } = render(
      <OrderBook {...baseProps} symbolLabel="QQQ" kind="stock" last={733.7} />,
    );
    const toggle = screen.getByRole("switch", {
      name: /toggle time and sales/i,
    });
    expect(toggle.getAttribute("aria-checked")).toBe("true");
    expect(container.querySelector(".book-body-grid.tape-hidden")).toBeNull();
  });
});
