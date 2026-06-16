// @vitest-environment jsdom

import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";
import { TickerDetailProvider } from "../lib/TickerDetailContext";
import { OrderActionsProvider } from "../lib/OrderActionsContext";
import TickerDetailContent from "@/components/TickerDetailContent";

vi.mock("@/lib/useWatchlist", () => ({
  useWatchlist: () => ({ isWatched: () => false, toggleWatch: vi.fn() }),
}));

describe("TickerDetailContent cockpit adapter", () => {
  it("renders the cockpit shell from the activeTab(deck) prop", () => {
    const onTabChange = vi.fn();
    const { container } = render(
      <TickerDetailProvider>
        <OrderActionsProvider>
          <TickerDetailContent
            ticker="SPX"
            positionId={null}
            activeTab="c" // deck key carried in the existing activeTab prop
            onTabChange={onTabChange}
            prices={{}}
            fundamentals={{}}
            portfolio={null}
            orders={null}
            theme="dark"
          />
        </OrderActionsProvider>
      </TickerDetailProvider>,
    );
    expect(container.querySelector(".cockpit")).toBeTruthy();
    // local-only decks (":"/"o") set internal state and must NOT call onTabChange;
    // full keyboard behavior is covered by AssetDeck.test.tsx.
  });
});
