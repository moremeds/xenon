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

// USD card: AAPL, 100 sh. market_value/market_price left null on purpose —
// the FX-badge assertions below are currency-driven, not price-driven, so no
// per-share price is fabricated. Structural fixture only.
const usdStock: PortfolioPosition = {
  id: 2,
  ticker: "AAPL",
  currency: "USD",
  exchange: "SMART",
  structure: "Stock (100 shares)",
  structure_type: "Stock",
  risk_profile: "equity",
  expiry: "N/A",
  contracts: 100,
  direction: "LONG",
  entry_cost: 0,
  market_value: null,
  max_risk: null,
  legs: [
    {
      direction: "LONG",
      contracts: 100,
      type: "Stock",
      currency: "USD",
      strike: null,
      entry_cost: 0,
      avg_cost: 0,
      market_price: null,
      market_value: null,
    },
  ],
  kelly_optimal: null,
  target: null,
  stop: null,
  entry_date: "unknown",
};

describe("PortfolioByStructure currency", () => {
  it("renders only the card's own FX pair inline in the header, never other currencies", () => {
    const { container } = render(
      <PortfolioByStructure
        positions={[jpyStock]}
        prices={
          {
            "5016": { symbol: "5016", last: 5267 },
            "USD.JPY": { symbol: "USD.JPY", last: 161.6575 },
          } as never
        }
        // Fallback carries KRW + BASE too — they must NOT leak onto a JPY card.
        fxRates={{ USD: 1, JPY: 0.006186, KRW: 0.00065, BASE: 1 }}
        activeAccount="ib"
        lastSync={new Date().toISOString()}
      />,
    );
    expect(screen.getByText(/USD\/JPY/)).toBeTruthy();
    expect(screen.queryByText(/USD\/KRW/)).toBeNull();
    expect(screen.queryByText(/USD\/BASE/)).toBeNull();
    // Exactly one FX capsule total — the inline header one, no separate row.
    expect(container.querySelectorAll(".fx-badge").length).toBe(1);
    // …and it lives inside the card header, after the ticker name.
    const header = container.querySelector(".section-header");
    expect(header?.textContent).toMatch(/USD\/JPY/);
  });

  it("renders no FX badge for a USD-denominated card", () => {
    const { container } = render(
      <PortfolioByStructure
        positions={[usdStock]}
        prices={{} as never}
        fxRates={{ USD: 1, JPY: 0.006186, KRW: 0.00065 }}
        activeAccount="ib"
        lastSync={new Date().toISOString()}
      />,
    );
    expect(container.querySelectorAll(".fx-badge").length).toBe(0);
    expect(screen.queryByText(/USD\//)).toBeNull();
  });

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
