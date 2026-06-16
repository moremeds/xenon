// @vitest-environment jsdom
import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import OrderTab from "@/components/ticker-detail/OrderTab";
import {
  TickerDetailProvider,
  useTickerDetail,
} from "@/lib/TickerDetailContext";

// OrderTab reaches OrderActionsContext + ModifyOrderModal; stub them so the
// ticket renders standalone (same pattern as order-tab-combo-sign.test.ts).
vi.mock("@/lib/OrderActionsContext", () => ({
  useOrderActions: () => ({
    pendingCancels: new Map(),
    pendingModifies: new Map(),
    cancelledOrders: [],
    requestCancel: vi.fn(),
    requestModify: vi.fn(),
    drainNotifications: vi.fn(() => []),
    setOrdersUpdater: vi.fn(),
  }),
}));
vi.mock("@/components/ModifyOrderModal", () => ({ default: () => null }));

afterEach(cleanup);

function FillButton({
  price,
  action,
}: {
  price: number;
  action: "BUY" | "SELL";
}) {
  const { setOrderPrefill } = useTickerDetail();
  return (
    <button
      onClick={() => setOrderPrefill({ price, action, source: "ladder" })}
    >
      fill
    </button>
  );
}

describe("OrderTab click-to-fill prefill", () => {
  it("prefills the limit price and toggles the action from a book click", () => {
    const { container } = render(
      <TickerDetailProvider>
        <FillButton price={123.45} action="SELL" />
        <OrderTab
          ticker="QQQ"
          position={null}
          portfolio={null}
          prices={{}}
          openOrders={[]}
          tickerPriceData={null}
        />
      </TickerDetailProvider>,
    );

    // Default action for a flat ticker is BUY; the prefill should flip it to SELL.
    const sellBtn = screen.getByRole("button", { name: "SELL" });
    expect(sellBtn.className).not.toContain("order-action-active");

    fireEvent.click(screen.getByText("fill"));

    const input = container.querySelector(
      ".modify-price-input",
    ) as HTMLInputElement;
    expect(input.value).toBe("123.45");
    expect(sellBtn.className).toContain("order-action-active");
  });
});
