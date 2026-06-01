/**
 * @vitest-environment jsdom
 */
import { render, screen, cleanup } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { FutuPerformanceStats } from "@/components/futu/FutuPerformanceStats";

afterEach(cleanup);

beforeEach(() => {
  global.fetch = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({
      total_return_twr: 0.082,
      cagr: 0.165,
      max_drawdown: -0.07,
      win_rate: 0.55,
      avg_holding_days: 12,
      best_trade: null,
      worst_trade: null,
      total_commissions: 145.5,
      monthly_pnl: {},
      external_cash_flows: { deposits: 1000, withdrawals: 0 },
    }),
  });
});

describe("<FutuPerformanceStats />", () => {
  it("renders TWR card", async () => {
    render(<FutuPerformanceStats from="2024-05-01" to="2024-06-01" />);
    expect(await screen.findByText("TWR")).toBeTruthy();
    expect(screen.getByText(/8\.20%/)).toBeTruthy();
  });
});
