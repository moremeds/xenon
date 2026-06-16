// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import CompanyTab from "@/components/ticker-detail/CompanyTab";

// CompanyTab fetches /api/ticker/info and reads data.uw_info / stock_state /
// profile / stats (verified CompanyTab.tsx:86–94; issueType = uw_info.issue_type).
// Mock the REAL shape — a {info:{...}} mock fails on a missing-uw_info crash, not
// on the gating logic.
function mockInfo(issueType: string) {
  global.fetch = vi.fn(
    async () =>
      new Response(
        JSON.stringify({
          uw_info: { issue_type: issueType },
          stock_state: {},
          profile: {},
          stats: {},
        }),
        { status: 200 },
      ),
  ) as unknown as typeof fetch;
}

describe("CompanyTab ETF/index gate", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("hides P/E, EPS, Next Earnings for an index", async () => {
    mockInfo("INDEX");
    render(
      <CompanyTab ticker="SPX" active priceData={null} fundamentals={null} />,
    );
    // Wait until the stats grid renders (Beta is never gated), then assert the
    // equity-only stats are gated out.
    await waitFor(() => expect(screen.getByText("Beta")).toBeTruthy());
    expect(screen.queryByText("P/E Ratio")).toBeNull();
    expect(screen.queryByText("EPS")).toBeNull();
    expect(screen.queryByText("Next Earnings")).toBeNull();
  });

  it("shows P/E for a common stock", async () => {
    mockInfo("Common Stock");
    render(
      <CompanyTab ticker="AAPL" active priceData={null} fundamentals={null} />,
    );
    await waitFor(() => expect(screen.getByText("P/E Ratio")).toBeTruthy());
  });
});
