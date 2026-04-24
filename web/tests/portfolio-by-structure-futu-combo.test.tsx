/**
 * @vitest-environment jsdom
 */

import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));
import PortfolioByStructure from "@/components/PortfolioByStructure";
import type { PortfolioPosition, PortfolioLeg } from "@/lib/types";

function mkLeg(o: Partial<PortfolioLeg>): PortfolioLeg {
  return {
    direction: "LONG",
    contracts: 10,
    type: "Put",
    strike: 400,
    entry_cost: 0,
    avg_cost: 0,
    market_price: null,
    market_value: null,
    market_price_is_calculated: false,
    ...o,
  };
}
function mkPos(
  id: number,
  leg: PortfolioLeg,
  o: Partial<PortfolioPosition> = {},
): PortfolioPosition {
  return {
    id,
    ticker: "TSLA",
    structure: `${leg.direction === "LONG" ? "Long" : "Short"} ${leg.type}`,
    structure_type: `${leg.direction === "LONG" ? "Long" : "Short"} ${leg.type}`,
    risk_profile: "",
    expiry: "2027-01-15",
    contracts: leg.contracts,
    direction: leg.direction,
    entry_cost: leg.entry_cost,
    max_risk: null,
    market_value: leg.market_value,
    legs: [leg],
    ib_daily_pnl: null,
    kelly_optimal: null,
    target: null,
    stop: null,
    entry_date: "2026-01-01",
    ...o,
  };
}

describe("PortfolioByStructure — Futu combo rendering", () => {
  // Bear Put Spread: Long $400 + Short $390 same expiry, equal contracts.
  const longPut = mkPos(
    1,
    mkLeg({
      direction: "LONG",
      type: "Put",
      strike: 400,
      contracts: 10,
      entry_cost: 4000,
      market_value: 3800,
    }),
  );
  const shortPut = mkPos(
    2,
    mkLeg({
      direction: "SHORT",
      type: "Put",
      strike: 390,
      contracts: 10,
      entry_cost: -500,
      market_value: -200,
    }),
  );

  it("does NOT repeat the pair label as a standalone text header when fused (Futu)", () => {
    render(
      <PortfolioByStructure
        positions={[longPut, shortPut]}
        activeAccount="futu"
        lastSync={new Date().toISOString()}
      />,
    );
    // When fused, the pair label is carried by the combo row itself.
    // The standalone <div class="cell-muted"> header that today prints
    // "Bear Put Spread $390/$400 · 2027-01-15" above the table should NOT appear.
    const strayHeader = screen
      .queryAllByText(/Bear Put Spread \$390\/\$400/)
      .filter(
        (el) => el.tagName === "DIV" && el.className.includes("cell-muted"),
      );
    expect(strayHeader).toHaveLength(0);
  });

  it("still renders the standalone pair-label header on the IB tab (unchanged behavior)", () => {
    render(
      <PortfolioByStructure
        positions={[longPut, shortPut]}
        activeAccount="ib"
        lastSync={new Date().toISOString()}
      />,
    );
    const header = screen
      .queryAllByText(/Bear Put Spread \$390\/\$400/)
      .filter(
        (el) => el.tagName === "DIV" && el.className.includes("cell-muted"),
      );
    expect(header.length).toBeGreaterThanOrEqual(1);
  });
});
