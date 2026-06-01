/**
 * @vitest-environment jsdom
 */
import { render, screen, cleanup } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { FutuTradeHistoryTable } from "@/components/futu/FutuTradeHistoryTable";

afterEach(cleanup);

beforeEach(() => {
  global.fetch = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({
      rows: [
        {
          futu_deal_id: "1",
          ticker: "AAPL",
          action: "BUY",
          quantity: 100,
          price: 150,
          fees: 1,
          realized_pnl: null,
          filled_at: "2024-05-01T10:00:00Z",
        },
        {
          futu_deal_id: "2",
          ticker: "AAPL",
          action: "SELL",
          quantity: 100,
          price: 160,
          fees: 1,
          realized_pnl: 998,
          filled_at: "2024-05-02T10:00:00Z",
        },
      ],
      next_cursor: null,
    }),
  });
});

describe("<FutuTradeHistoryTable />", () => {
  it("renders header cells", async () => {
    render(<FutuTradeHistoryTable from="2024-05-01" to="2024-05-02" />);
    expect(await screen.findByText("Ticker")).toBeTruthy();
    expect(screen.getByText("Realized P&L")).toBeTruthy();
  });

  it("renders fetched rows", async () => {
    render(<FutuTradeHistoryTable from="2024-05-01" to="2024-05-02" />);
    // Round-trip fixture: two AAPL rows (BUY + SELL).
    const aapl = await screen.findAllByText("AAPL");
    expect(aapl.length).toBe(2);
    expect(screen.getByText("$998.00")).toBeTruthy();
  });
});
