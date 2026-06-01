/**
 * @vitest-environment jsdom
 */
import { render, screen, cleanup } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import { FutuEquityCurve } from "@/components/futu/FutuEquityCurve";

afterEach(cleanup);

beforeEach(() => {
  global.fetch = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => [
      {
        date: "2024-05-01",
        nav: 10000,
        source: "reconstructed",
        daily_pnl: 0,
        recon: null,
      },
      {
        date: "2024-05-02",
        nav: 10100,
        source: "reported",
        daily_pnl: 100,
        recon: { delta: 50, exceeded: false, notes: null },
      },
    ],
  });
});

describe("<FutuEquityCurve />", () => {
  it("renders the chart container", async () => {
    render(<FutuEquityCurve from="2024-05-01" to="2024-05-02" />);
    expect(await screen.findByTestId("futu-equity-curve")).toBeTruthy();
  });

  it("renders a reconstructed-segment badge in legend", async () => {
    render(<FutuEquityCurve from="2024-05-01" to="2024-05-02" />);
    expect(await screen.findByText(/reconstructed/i)).toBeTruthy();
  });
});
