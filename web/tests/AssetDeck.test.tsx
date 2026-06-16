// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import type { ReactNode } from "react";
import AssetDeck from "@/components/ticker-detail/AssetDeck";
import { TickerDetailProvider } from "@/lib/TickerDetailContext";

const baseProps = {
  ticker: "SPX",
  prices: {},
  fundamentals: {},
  portfolio: null,
  position: null,
  quotePriceData: null,
};

// AssetDeck eagerly renders the active deck body; the chain deck ("c") mounts
// OptionsChainTab which consumes TickerDetailContext. Wrap so the keyboard
// contract can be exercised with an open deck.
function withProvider(node: ReactNode) {
  return <TickerDetailProvider>{node}</TickerDetailProvider>;
}

describe("AssetDeck keyboard", () => {
  it("single key opens the matching deck", () => {
    const onDeckChange = vi.fn();
    render(
      withProvider(
        <AssetDeck
          activeDeck={null}
          onDeckChange={onDeckChange}
          {...baseProps}
        />,
      ),
    );
    fireEvent.keyDown(document, { key: "c" });
    expect(onDeckChange).toHaveBeenCalledWith("c");
  });

  it("Esc closes an open deck", () => {
    const onDeckChange = vi.fn();
    render(
      withProvider(
        <AssetDeck activeDeck="c" onDeckChange={onDeckChange} {...baseProps} />,
      ),
    );
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onDeckChange).toHaveBeenCalledWith(null);
  });

  it("ignores keys while typing in an input", () => {
    const onDeckChange = vi.fn();
    render(
      withProvider(
        <>
          <input data-testid="qty" />
          <AssetDeck
            activeDeck={null}
            onDeckChange={onDeckChange}
            {...baseProps}
          />
        </>,
      ),
    );
    const input = document.querySelector("input")!;
    input.focus();
    fireEvent.keyDown(document, { key: "c" });
    expect(onDeckChange).not.toHaveBeenCalled();
  });

  it("ignores modified keys", () => {
    const onDeckChange = vi.fn();
    render(
      withProvider(
        <AssetDeck
          activeDeck={null}
          onDeckChange={onDeckChange}
          {...baseProps}
        />,
      ),
    );
    fireEvent.keyDown(document, { key: "c", metaKey: true });
    expect(onDeckChange).not.toHaveBeenCalled();
  });
});
