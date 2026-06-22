/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(),
}));

import PortfolioByStructure from "@/components/PortfolioByStructure";
import type { PortfolioPosition } from "@/lib/types";

afterEach(() => cleanup());

// Real IB snapshot 2026-06-22: 5016 JX Advanced Metals, TSEJ/JPY, 100 sh.
//   MV 100 @ ¥5,267 = ¥526,700 ; USD.JPY 161.6575 → ≈ $3,258
const jpyStock: PortfolioPosition = {
  id: 1,
  ticker: "5016",
  currency: "JPY",
  exchange: "TSEJ",
  structure: "Stock (100 shares)",
  structure_type: "Stock",
  risk_profile: "equity",
  expiry: "N/A",
  contracts: 100,
  direction: "LONG",
  entry_cost: 474_700,
  market_value: 526_700,
  max_risk: null,
  legs: [
    {
      direction: "LONG",
      contracts: 100,
      type: "Stock",
      currency: "JPY",
      strike: null,
      entry_cost: 474_700,
      avg_cost: 4_747,
      market_price: 5_267,
      market_value: 526_700,
    },
  ],
  kelly_optimal: null,
  target: null,
  stop: null,
  entry_date: "unknown",
};

describe("PortfolioByStructure currency", () => {
  it("shows the foreign stock card aggregate MV as a USD headline, not ¥ with $", () => {
    render(
      <PortfolioByStructure
        positions={[jpyStock]}
        prices={
          {
            "5016": { symbol: "5016", last: 5267 },
            "USD.JPY": { symbol: "USD.JPY", last: 161.6575 },
          } as never
        }
        fxRates={{ USD: 1, JPY: 0.006186 }}
        activeAccount="ib"
        lastSync={new Date().toISOString()}
      />,
    );
    // Card header MV should read the USD figure ($3,258), never "$526,700".
    expect(screen.getByText(/MV \$3,258/)).toBeTruthy();
    expect(screen.queryByText(/\$526,700/)).toBeNull();
  });
});
