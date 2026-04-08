/**
 * @vitest-environment jsdom
 *
 * UwAnalyzeSections — tiered card grid + single detail panel.
 *
 * Covers the 2026-04-08 layout overhaul:
 *   - Three tier sections: INDEX ETFs (SPY/QQQ/IWM fixed order),
 *     COMMODITY/MACRO (GLD), SINGLE NAMES (changed-first).
 *   - Auto-selects the first changed ticker on load.
 *   - Clicking a card swaps the single detail panel.
 *   - Mid-poll removal of the selected ticker falls back safely.
 *   - Empty state when no tickers present.
 */

import React from "react";
import {
  render,
  screen,
  fireEvent,
  cleanup,
  within,
  act,
} from "@testing-library/react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

import WorkspaceSections from "../components/WorkspaceSections";
import type { UwPortfolioResponse, UwTickerRow } from "../lib/uwAnalyzeTypes";

// Stub unrelated hooks pulled in by WorkspaceSections.
vi.mock("@/lib/OrderActionsContext", () => ({
  useOrderActions: () => ({}),
}));
vi.mock("@/lib/useJournal", () => ({ useJournal: () => ({}) }));
vi.mock("@/lib/useDiscover", () => ({ useDiscover: () => ({}) }));
vi.mock("@/lib/useFlowAnalysis", () => ({ useFlowAnalysis: () => ({}) }));
vi.mock("@/lib/useScanner", () => ({ useScanner: () => ({}) }));
vi.mock("@/lib/useBlotter", () => ({ useBlotter: () => ({}) }));

// Mock GexProfileChart to avoid importing canvas/chart libs under jsdom.
vi.mock("@/components/charts/GexProfileChart", () => ({
  GexProfileChart: () => <div data-testid="gex-chart-stub" />,
}));

// Mutable portfolio state driven per-test.
let currentPortfolio: UwPortfolioResponse | null = null;
const refreshOne = vi.fn();
const refreshAll = vi.fn();
const addAdhoc = vi.fn();

vi.mock("@/lib/useUwPortfolio", () => ({
  useUwPortfolio: () => ({
    data: currentPortfolio,
    loading: false,
    error: null,
    lastFetchedAt: "2026-04-08T14:02:11Z",
    refreshAll,
    refreshOne,
    addAdhoc,
  }),
}));

function makeRow(
  ticker: string,
  overrides: Partial<UwTickerRow> = {},
): UwTickerRow {
  const base: UwTickerRow = {
    ticker,
    sources: ["portfolio"],
    prev_ts: null,
    changes: [],
    oi_changes: [],
    unusual_flow_events: [],
    snapshot: {
      ticker,
      ts: "2026-04-08T14:02:11Z",
      report: {
        ticker,
        price: 100,
        fetched_at: "2026-04-08T14:02:11Z",
        scores: {
          bias: "BULLISH",
          grade: "A",
          composite: 20,
          market_structure: 22,
          volatility: 18,
          flow: 17,
          positioning: 0,
          mode: "full",
          skipped_buckets: ["positioning"],
          reweighted: true,
        },
        regime: { gex_sign: "positive", flip_distance_pct: -0.4 },
        setup_thesis: {
          structure_family: "neutral",
          regime: "R1",
          bias: "BULLISH",
          rationale: "demo rationale",
        },
        notes: [],
      },
      display: {
        sector: "XLK",
        iv_rank: 40,
        iv: 25,
        rv: 20,
        call_wall_strike: 110,
        put_wall_strike: 90,
        gamma_per_1pct: 1_000_000,
        net_call_premium: 5_000_000,
        net_put_premium: -1_000_000,
        short_volume_ratio: 0.42,
        short_volume_trend: [0.4, 0.41, 0.42],
        term_structure_label: "normal",
        gex_flip: null,
        gex_by_strike: [],
        max_pain: 100,
      },
      derived: {
        gex_sign: "POSITIVE",
        gex_flip_strike: null,
        max_pain: 100,
        call_wall: 110,
        put_wall: 90,
        iv_rank: 40,
        net_call_premium: 5_000_000,
        net_put_premium: -1_000_000,
        flow_score: 17,
        spot: 100,
      },
    },
  };
  return { ...base, ...overrides } as UwTickerRow;
}

function setPortfolio(rows: UwTickerRow[]): void {
  currentPortfolio = {
    tickers: rows,
    fetched_at: "2026-04-08T14:02:11Z",
    market_state: "open",
    ttl_seconds: 120,
    action_items: [],
  };
}

function renderSection() {
  return render(<WorkspaceSections section="uw-analyze" />);
}

describe("UwAnalyzeSections — tiered layout", () => {
  beforeEach(() => {
    currentPortfolio = null;
    refreshOne.mockClear();
    refreshAll.mockClear();
    addAdhoc.mockClear();
  });
  afterEach(() => {
    cleanup();
  });

  it("renders the scaffold tile grid even when the portfolio is empty", () => {
    setPortfolio([]);
    renderSection();
    // Every static-universe ticker renders on first paint.
    for (const t of [
      "SPY",
      "QQQ",
      "IWM",
      "DIA",
      "GLD",
      "TLT",
      "UVXY",
      "XLK",
      "SMH",
    ]) {
      expect(screen.getByTestId(`uw-card-${t}`)).toBeTruthy();
    }
    // Scaffold cards are tagged so downstream styling can react.
    expect(
      screen.getByTestId("uw-card-SPY").getAttribute("data-scaffold"),
    ).toBe("true");
    // SPY is the default selection; the detail panel is always mounted.
    const detail = screen.getByTestId("uw-detail");
    expect(detail.getAttribute("data-ticker")).toBe("SPY");
  });

  it("renders tier grids with SPY/QQQ/IWM/DIA in fixed order", () => {
    setPortfolio([
      makeRow("IWM"),
      makeRow("QQQ"),
      makeRow("SPY"),
      makeRow("GLD"),
      makeRow("NVDA"),
      makeRow("AAPL"),
    ]);
    renderSection();

    // Cards exist for every live ticker.
    for (const t of ["SPY", "QQQ", "IWM", "GLD", "NVDA", "AAPL"]) {
      expect(screen.getByTestId(`uw-card-${t}`)).toBeTruthy();
    }

    // Index tier renders in the exact SPY/QQQ/IWM/DIA order.
    const tiers = screen.getByTestId("uw-analyze-tiers");
    const indexCards = within(tiers)
      .getAllByRole("button")
      .filter((el) =>
        /uw-card-(SPY|QQQ|IWM|DIA)/.test(el.getAttribute("data-testid") ?? ""),
      );
    expect(indexCards.map((el) => el.getAttribute("data-testid"))).toEqual([
      "uw-card-SPY",
      "uw-card-QQQ",
      "uw-card-IWM",
      "uw-card-DIA",
    ]);
  });

  it("scaffold cards are replaced by live data when the portfolio resolves", () => {
    // First paint with nothing live.
    setPortfolio([]);
    const { rerender } = renderSection();
    expect(
      screen.getByTestId("uw-card-SPY").getAttribute("data-scaffold"),
    ).toBe("true");

    // Live portfolio lands.
    setPortfolio([makeRow("SPY")]);
    rerender(<WorkspaceSections section="uw-analyze" />);
    expect(
      screen.getByTestId("uw-card-SPY").getAttribute("data-scaffold"),
    ).toBe("false");
  });

  it("auto-selects the first changed ticker on load", () => {
    setPortfolio([
      makeRow("SPY"),
      makeRow("NVDA", {
        changes: [{ code: "GEX_FLIP_SIGN", severity: "warn" } as any],
      }),
      makeRow("AAPL"),
    ]);
    renderSection();

    const nvdaCard = screen.getByTestId("uw-card-NVDA");
    expect(nvdaCard.getAttribute("aria-pressed")).toBe("true");
    expect(nvdaCard.getAttribute("data-alert")).toBe("true");

    // Detail panel shows NVDA.
    const detail = screen.getByTestId("uw-detail");
    expect(detail.getAttribute("data-ticker")).toBe("NVDA");
  });

  it("falls back to SPY when no rows are changed", () => {
    setPortfolio([makeRow("AAPL"), makeRow("SPY"), makeRow("NVDA")]);
    renderSection();
    const spyCard = screen.getByTestId("uw-card-SPY");
    expect(spyCard.getAttribute("aria-pressed")).toBe("true");
  });

  it("clicking a card swaps the detail panel", () => {
    setPortfolio([makeRow("SPY"), makeRow("NVDA")]);
    renderSection();
    fireEvent.click(screen.getByTestId("uw-card-NVDA"));
    const detail = screen.getByTestId("uw-detail");
    expect(detail.getAttribute("data-ticker")).toBe("NVDA");
    expect(
      screen.getByTestId("uw-card-NVDA").getAttribute("aria-pressed"),
    ).toBe("true");
    expect(screen.getByTestId("uw-card-SPY").getAttribute("aria-pressed")).toBe(
      "false",
    );
  });

  it("selection recovers when the selected ticker disappears on refresh", () => {
    setPortfolio([makeRow("SPY"), makeRow("NVDA")]);
    const { rerender } = renderSection();

    fireEvent.click(screen.getByTestId("uw-card-NVDA"));
    expect(screen.getByTestId("uw-detail").getAttribute("data-ticker")).toBe(
      "NVDA",
    );

    // NVDA drops off the portfolio mid-poll.
    setPortfolio([makeRow("SPY")]);
    rerender(<WorkspaceSections section="uw-analyze" />);

    // Falls back to SPY without crashing.
    expect(screen.getByTestId("uw-detail").getAttribute("data-ticker")).toBe(
      "SPY",
    );
    expect(screen.queryByTestId("uw-card-NVDA")).toBeNull();
  });

  it("detail header exposes source pills and per-ticker refresh", () => {
    setPortfolio([makeRow("SPY", { sources: ["portfolio", "watchlist"] })]);
    renderSection();
    const refreshBtn = screen.getByTestId("uw-refresh-SPY");
    fireEvent.click(refreshBtn);
    expect(refreshOne).toHaveBeenCalledWith("SPY");
  });

  it("scaffold-first paint promotes to the first-changed live ticker when data arrives", () => {
    // First paint: no live data — fallback selects scaffold SPY.
    setPortfolio([]);
    const { rerender } = renderSection();
    expect(screen.getByTestId("uw-detail").getAttribute("data-ticker")).toBe(
      "SPY",
    );

    // Live data arrives with NVDA as the only changed ticker. Selection
    // must promote away from the scaffold-SPY stub to the alerting row.
    setPortfolio([
      makeRow("SPY"),
      makeRow("NVDA", {
        changes: [{ code: "GEX_FLIP_SIGN", severity: "warn" } as any],
      }),
      makeRow("AAPL"),
    ]);
    rerender(<WorkspaceSections section="uw-analyze" />);
    expect(screen.getByTestId("uw-detail").getAttribute("data-ticker")).toBe(
      "NVDA",
    );
  });

  it("pending ad-hoc selection reclaims the detail pane after the 5s timeout", async () => {
    vi.useFakeTimers();
    try {
      // Start with SPY in the portfolio; SPY is the default selection.
      setPortfolio([makeRow("SPY")]);
      renderSection();
      expect(screen.getByTestId("uw-detail").getAttribute("data-ticker")).toBe(
        "SPY",
      );

      // User submits a bogus ad-hoc ticker that will never arrive in
      // `data.tickers`. The detail pane should immediately switch to it.
      const input = screen.getByTestId(
        "uw-analyze-adhoc-input",
      ) as HTMLInputElement;
      fireEvent.change(input, { target: { value: "ZZZZ" } });
      fireEvent.submit(input.closest("form")!);
      expect(addAdhoc).toHaveBeenCalledWith("ZZZZ");

      // Advance past the 5s pending suppression window. The fallback
      // effect must re-run (via the pendingTick state bump) and reclaim
      // the pane back to SPY instead of leaving it stranded.
      act(() => {
        vi.advanceTimersByTime(5001);
      });
      expect(screen.getByTestId("uw-detail").getAttribute("data-ticker")).toBe(
        "SPY",
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("preserves the positioning n/a fallback tile when positioning is null", () => {
    const row = makeRow("SPY");
    // Force skipped positioning bucket
    (
      row.snapshot.report.scores as { positioning?: number | null }
    ).positioning = null;
    setPortfolio([row]);
    renderSection();
    const positioning = screen.getByTestId("uw-detail-positioning");
    expect(positioning.textContent ?? "").toContain("reweighted out");
  });
});
