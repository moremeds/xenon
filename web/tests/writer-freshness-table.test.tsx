/** @vitest-environment jsdom */
import React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { WriterFreshnessTable } from "@/components/operator/WriterFreshnessTable";
import { MarketState } from "@/lib/useMarketHours";
import type { WriterRow } from "@/lib/operatorTypes";

afterEach(() => cleanup());

const row = (over: Partial<WriterRow>): WriterRow => ({
  service: "ib_activity_poller",
  state: "ok",
  detail: null,
  last_error: null,
  last_started_at: null,
  last_finished_at: null,
  updated_at: "2026-06-15T14:00:00Z",
  age_secs: 30,
  ...over,
});

describe("WriterFreshnessTable", () => {
  it("marks fresh and stale rows", () => {
    render(
      <WriterFreshnessTable
        writers={[row({ age_secs: 30 }), row({ service: "x", age_secs: 9999 })]}
        market={MarketState.OPEN}
      />,
    );
    // Exact badge strings — /fresh/i would also match the "Freshness" header.
    expect(screen.getByText("fresh")).toBeTruthy();
    expect(screen.getByText("STALE")).toBeTruthy();
  });
  it("renders a synthesized missing writer as a fault row", () => {
    render(
      <WriterFreshnessTable
        writers={[
          row({
            service: "ib_fills_replay",
            state: "missing",
            age_secs: null,
            updated_at: null,
          }),
        ]}
        market={MarketState.OPEN}
      />,
    );
    expect(screen.getByText("missing")).toBeTruthy();
    expect(screen.getByText("STALE")).toBeTruthy();
    expect(screen.getByText("never")).toBeTruthy();
  });
  it("shows empty state", () => {
    render(<WriterFreshnessTable writers={[]} market={MarketState.OPEN} />);
    expect(screen.getByText(/no writers reported/i)).toBeTruthy();
  });
});
