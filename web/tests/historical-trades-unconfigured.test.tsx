/**
 * @vitest-environment jsdom
 *
 * W2 — Historical Trades panel must render a friendly empty state with
 * setup hint when /api/blotter returns configured=false, instead of the
 * red error banner that surfaced the leaked CLI hint pre-fix.
 *
 * Plan: docs/plans/2026-04-28-postgres-migration-completion-IMPL.md § W2.1
 */

import React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { BlotterData } from "../lib/types";
import { HistoricalTradesSection } from "../components/WorkspaceSections";

vi.mock("../components/TickerLink", () => ({
  default: (props: { ticker: string }) =>
    React.createElement("span", null, props.ticker),
}));

const useBlotterMock = vi.fn();
vi.mock("../lib/useBlotter", () => ({
  useBlotter: (...args: unknown[]) => useBlotterMock(...args),
}));

const UNCONFIGURED_BLOTTER: BlotterData = {
  as_of: null,
  summary: {
    closed_trades: 0,
    open_trades: 0,
    total_commissions: 0,
    realized_pnl: 0,
  },
  closed_trades: [],
  open_trades: [],
  configured: false,
  source: "none",
  message:
    "IB Flex Query not configured. Set IB_FLEX_TOKEN and IB_FLEX_QUERY_ID in .env, then click Refresh.",
};

afterEach(() => {
  cleanup();
  useBlotterMock.mockReset();
});

describe("HistoricalTradesSection — unconfigured state", () => {
  it("renders the configured=false setup hint instead of an error", () => {
    useBlotterMock.mockReturnValue({
      data: UNCONFIGURED_BLOTTER,
      loading: false,
      syncing: false,
      error: null,
      syncNow: vi.fn(),
    });

    render(<HistoricalTradesSection />);

    // Must surface the configured=false copy with both env-var names so
    // the user knows exactly what to fix.
    expect(screen.getByText(/IB_FLEX_TOKEN/)).toBeTruthy();
    expect(screen.getByText(/IB_FLEX_QUERY_ID/)).toBeTruthy();

    // Must NOT leak the CLI setup hint that the legacy 502 path surfaced.
    expect(screen.queryByText(/Run with --setup/)).toBeNull();

    // Trade count pill should still render at zero — section header stays
    // consistent across configured and unconfigured states.
    expect(screen.getByText(/0 TRADES/)).toBeTruthy();
  });

  it("does not render the legacy 'No historical trades. Click REFRESH' copy when configured=false", () => {
    useBlotterMock.mockReturnValue({
      data: UNCONFIGURED_BLOTTER,
      loading: false,
      syncing: false,
      error: null,
      syncNow: vi.fn(),
    });

    render(<HistoricalTradesSection />);

    // The unconfigured branch supersedes the generic empty-state copy.
    expect(screen.queryByText(/Click REFRESH to fetch from IB/)).toBeNull();
  });
});
