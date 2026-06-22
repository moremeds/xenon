// @vitest-environment jsdom
import { render, screen, cleanup } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";

// TickerLink → useTickerNav → useRouter (next/navigation) needs the app-router
// context, which isn't mounted in a bare render. Stub it.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(),
}));

import PositionTable from "@/components/PositionTable";

// PositionTable does NOT call usePrices — it receives `prices` as a prop
// (WorkspaceShell owns the WS hook). Real IB snapshot 2026-06-22:
//   5016 JX Advanced Metals: 100 sh, last ¥5,267 → MV ¥526,700
//   USD.JPY (IDEALPRO) = 161.6575 → 526,700 / 161.6575 ≈ $3,258
const jpyPos = {
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
  entry_cost_usd: 2_936.49,
  max_risk: null,
  market_value: 526_700,
  market_value_usd: 3_258.17,
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

afterEach(() => cleanup());

describe("PositionTable currency", () => {
  it("renders native JPY value and a USD headline for a TSEJ row", () => {
    render(
      <PositionTable
        positions={[jpyPos as never]}
        prices={
          {
            "5016": { symbol: "5016", last: 5267 },
            "USD.JPY": { symbol: "USD.JPY", last: 161.6575 },
          } as never
        }
        fxRates={{ USD: 1, JPY: 0.006186 }}
      />,
    );
    // Native sub-line (¥526,700) + live USD headline ($3,258).
    expect(screen.getByText(/¥526,700|526,700/)).toBeTruthy();
    expect(screen.getByText(/\$3,258/)).toBeTruthy();
    // Per-share last price renders native, not "$5,267".
    expect(screen.getByText(/¥5,267/)).toBeTruthy();
  });

  it("shows the FX badge with USD/JPY for a foreign portfolio", () => {
    render(
      <PositionTable
        positions={[jpyPos as never]}
        prices={{ "USD.JPY": { symbol: "USD.JPY", last: 161.6575 } } as never}
        fxRates={{ USD: 1, JPY: 0.006186 }}
      />,
    );
    expect(screen.getByText(/USD\/JPY/)).toBeTruthy();
  });
});
