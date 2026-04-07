/**
 * @vitest-environment jsdom
 */

import React from "react";
import { render, screen, fireEvent, waitFor, act, cleanup } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

import WorkspaceSections from "../components/WorkspaceSections";
import type { UwAnalyzeResponse } from "../lib/types/uwAnalyze";

// Stub all hooks pulled in by WorkspaceSections that aren't relevant.
vi.mock("@/lib/OrderActionsContext", () => ({
  useOrderActions: () => ({}),
}));
vi.mock("@/lib/useJournal", () => ({ useJournal: () => ({}) }));
vi.mock("@/lib/useDiscover", () => ({ useDiscover: () => ({}) }));
vi.mock("@/lib/useFlowAnalysis", () => ({ useFlowAnalysis: () => ({}) }));
vi.mock("@/lib/useScanner", () => ({ useScanner: () => ({}) }));
vi.mock("@/lib/useBlotter", () => ({ useBlotter: () => ({}) }));

const FIXTURE: UwAnalyzeResponse = {
  report: {
    ticker: "AAPL",
    price: 184.22,
    fetched_at: "2026-04-08T14:02:11",
    data_freshness: { gex: "live" },
    scores: {
      market_structure: 24,
      volatility: 19,
      flow: 17,
      positioning: 0,
      composite: 15,
      grade: "B",
      bias: "MIXED",
      mode: "full",
      reweighted: true,
      skipped_buckets: ["positioning"],
    },
    notes: ["positioning bucket unavailable — composite reweighted"],
    setup_thesis: {
      bias: "MIXED",
      regime: "R1",
      structure_family: "neutral",
      rationale: "demo rationale",
    },
    regime: { gex_sign: "positive", flip_distance_pct: -0.011 },
  },
  display: {
    sector: "XLK",
    iv_rank: 38,
    iv: 22,
    rv: 18.6,
    call_wall_strike: 190,
    put_wall_strike: 175,
    gamma_per_1pct: 42_000_000,
    net_call_premium: 12_400_000,
    net_put_premium: -3_100_000,
    short_volume_ratio: 0.41,
    short_volume_trend: [0.4, 0.41, 0.42],
    term_structure_label: "normal",
    gex_flip: null,
    gex_by_strike: [
      {
        strike: 190,
        call_gamma: 44.8,
        put_gamma: -2.7,
        net_gamma: 42.1,
        distance_pct: 0.031,
        is_call_wall: true,
        is_put_wall: false,
      },
    ],
  },
  generated_at: "2026-04-08T18:00:00Z",
};

describe("UwAnalyzeSections", () => {
  beforeEach(() => {
    // @ts-expect-error fetch mock
    global.fetch = vi.fn();
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  function renderSection() {
    return render(<WorkspaceSections section="uw-analyze" />);
  }

  it("renders the empty state before any analyse run", () => {
    renderSection();
    expect(screen.getByTestId("uw-analyze-empty")).toBeTruthy();
    const submit = screen.getByTestId("uw-analyze-submit") as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
  });

  it("renders the success state with bucket grid + GEX table + n/a positioning", async () => {
    (global.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true,
      json: async () => FIXTURE,
    });
    renderSection();
    const input = screen.getByTestId("uw-analyze-input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "AAPL" } });
    const submit = screen.getByTestId("uw-analyze-submit");
    await act(async () => {
      fireEvent.click(submit);
    });
    await waitFor(() => screen.getByTestId("uw-analyze-identity"));

    expect(screen.getByTestId("uw-analyze-identity")).toBeTruthy();
    expect(screen.getByTestId("uw-analyze-thesis")).toBeTruthy();
    expect(screen.getByTestId("uw-analyze-buckets")).toBeTruthy();
    expect(screen.getByTestId("uw-analyze-gex-table")).toBeTruthy();
    // n/a positioning tile (skipped_buckets contains "positioning")
    const positioning = screen.getByTestId("uw-analyze-positioning");
    expect(positioning.textContent ?? "").toContain("not available");
  });

  it("renders the error state when the API returns non-OK", async () => {
    (global.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: false,
      status: 404,
      json: async () => ({ error: "ticker not found: ZZZZ" }),
    });
    renderSection();
    fireEvent.change(screen.getByTestId("uw-analyze-input"), {
      target: { value: "ZZZZ" },
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId("uw-analyze-submit"));
    });
    await waitFor(() => screen.getByTestId("uw-analyze-error"));
    expect(screen.getByTestId("uw-analyze-error").textContent).toContain("ticker not found");
  });
});
