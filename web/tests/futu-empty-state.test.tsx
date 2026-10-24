/**
 * @vitest-environment jsdom
 *
 * Regression: after the futu disk fallback was removed, a cold load whose
 * GET /api/futu/portfolio returns 404/502 left PortfolioSections spinning
 * "LOADING / Waiting for portfolio data..." forever. The hook now exposes
 * `initialRead` and the section renders a measurement-condition empty
 * state (with retry) instead. Covers both HTTP outcomes + the IB tab
 * regression (IB must keep the old LOADING panel).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { cleanup, render, screen, fireEvent } from "@testing-library/react";
import React from "react";
import WorkspaceSections from "../components/workspace/WorkspaceSections";
import type { TradeEntry } from "@/lib/types";

vi.mock("../components/TickerLink", () => ({
  default: (props: { ticker: string }) =>
    React.createElement("span", null, props.ticker),
}));

const useJournalMock = vi.fn();
vi.mock("@/lib/useJournal", () => ({
  useJournal: () => useJournalMock(),
}));

const memStore = new Map<string, string>();
const fakeLocalStorage: Storage = {
  get length() {
    return memStore.size;
  },
  clear: () => memStore.clear(),
  getItem: (k) => memStore.get(k) ?? null,
  key: (i) => Array.from(memStore.keys())[i] ?? null,
  removeItem: (k) => {
    memStore.delete(k);
  },
  setItem: (k, v) => {
    memStore.set(k, String(v));
  },
};
Object.defineProperty(window, "localStorage", {
  value: fakeLocalStorage,
  writable: true,
  configurable: true,
});

beforeEach(() => {
  memStore.clear();
  useJournalMock.mockReturnValue({
    data: { trades: [] as TradeEntry[] },
    loading: false,
    error: null,
    syncWithIB: vi.fn(),
    syncing: false,
    lastSyncResult: null,
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderFutuPortfolio(props: Record<string, unknown> = {}) {
  return render(
    React.createElement(WorkspaceSections, {
      section: "portfolio",
      portfolio: null,
      activeAccount: "futu",
      ...props,
    }),
  );
}

describe("Futu portfolio empty state (initial read resolved, no data)", () => {
  it("not_configured renders one panel, no retry, no alert-item marker", () => {
    renderFutuPortfolio({ futuRead: "not_configured", onFutuRetry: vi.fn() });

    expect(screen.getByTestId("futu-not-configured-panel")).toBeTruthy();
    expect(screen.queryByTestId("portfolio-view-toggle")).toBeNull();
    expect(screen.queryByTestId("futu-retry")).toBeNull();
    const msg = screen.getByTestId("futu-empty-state").textContent ?? "";
    expect(msg).toBe("No REAL Futu account configured for this environment.");
    expect(msg.startsWith("›")).toBe(false);
    expect(screen.getByText("NOT CONFIGURED", { exact: true })).toBeTruthy();
  });

  it("404/never_synced renders the not-yet-synced condition + retry", () => {
    renderFutuPortfolio({ futuRead: "never_synced", onFutuRetry: vi.fn() });

    expect(screen.getByTestId("futu-empty-state").textContent).toContain(
      "Futu not yet synced — start Futu OpenD, then sync.",
    );
    expect(screen.getByText("NEVER SYNCED")).toBeTruthy();
    expect(screen.getByTestId("futu-retry")).toBeTruthy();
    expect(screen.queryByText("Waiting for portfolio data...")).toBeNull();
  });

  it("502/unavailable renders the unavailable condition + retry", () => {
    renderFutuPortfolio({ futuRead: "unavailable", onFutuRetry: vi.fn() });

    expect(screen.getByTestId("futu-empty-state").textContent).toContain(
      "Futu unavailable — FastAPI did not return a snapshot.",
    );
    expect(screen.getByText("UNAVAILABLE")).toBeTruthy();
    expect(screen.getByTestId("futu-retry")).toBeTruthy();
    expect(screen.queryByText("Waiting for portfolio data...")).toBeNull();
  });

  it("retry button invokes the sync action", () => {
    const onRetry = vi.fn();
    renderFutuPortfolio({ futuRead: "unavailable", onFutuRetry: onRetry });

    fireEvent.click(screen.getByTestId("futu-retry"));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("pending read keeps the LOADING panel (no premature empty state)", () => {
    renderFutuPortfolio({ futuRead: "pending" });

    expect(screen.getByText("LOADING")).toBeTruthy();
    expect(screen.getByText("Waiting for portfolio data...")).toBeTruthy();
    expect(screen.queryByTestId("futu-empty-state")).toBeNull();
  });

  it("IB tab is unaffected — null portfolio still shows LOADING", () => {
    render(
      React.createElement(WorkspaceSections, {
        section: "portfolio",
        portfolio: null,
        activeAccount: "ib",
        // Even if a futu read resolved, the IB tab must not consume it.
        futuRead: "unavailable",
      }),
    );

    expect(screen.getByText("LOADING")).toBeTruthy();
    expect(screen.getByText("Waiting for portfolio data...")).toBeTruthy();
    expect(screen.queryByTestId("futu-empty-state")).toBeNull();
  });
});
