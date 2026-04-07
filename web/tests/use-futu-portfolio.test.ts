/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { renderHook, waitFor, cleanup, act } from "@testing-library/react";
import { useFutuPortfolio } from "@/lib/useFutuPortfolio";

const originalFetch = global.fetch;

afterEach(() => {
  cleanup();
  global.fetch = originalFetch;
  vi.restoreAllMocks();
});

function mockFetchOnce(body: unknown, status = 200) {
  const res = new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
  global.fetch = vi.fn().mockResolvedValue(res) as typeof global.fetch;
}

const FIXTURE_ENVELOPE = {
  ok: true,
  fetched_at: "2026-04-07T12:00:00.000Z",
  data_as_of: "2026-04-07T12:00:00.000Z",
  account_id: "12345",
  source: "futu" as const,
  is_stale: false,
  warnings: [],
  positions: [],
  count: 0,
  account_summary: {
    net_liquidation: 148000,
    equity_with_loan: 148000,
    cash: -14585,
    settled_cash: -14585,
    buying_power: 29917,
    available_funds: 29917,
    initial_margin: 130962,
    maintenance_margin: 114285,
    excess_liquidity: 33715,
    gross_position_value: 561030,
    unrealized_pnl: 51021,
    daily_pnl: 51021,
    realized_pnl: 0,
    dividends: null,
    previous_day_ewl: null,
    reg_t_equity: null,
    sma: null,
  },
};

describe("useFutuPortfolio", () => {
  it("branches on never_synced envelope without crashing the adapter", async () => {
    mockFetchOnce({
      ok: false,
      code: "never_synced",
      positions: [],
      count: 0,
      account_summary: null,
      fetched_at: null,
      data_as_of: null,
    });

    const { result } = renderHook(() => useFutuPortfolio(false));
    await waitFor(() => {
      expect(result.current.neverSynced).toBe(true);
    });

    expect(result.current.data).toBeNull();
    expect(result.current.envelope).toBeNull();
    expect(result.current.error).toBeNull();
    expect(result.current.lastSync).toBeNull();
  });

  it("adapts a normal envelope into PortfolioData", async () => {
    mockFetchOnce(FIXTURE_ENVELOPE);

    const { result } = renderHook(() => useFutuPortfolio(false));
    await waitFor(() => {
      expect(result.current.data).not.toBeNull();
    });

    expect(result.current.neverSynced).toBe(false);
    expect(result.current.error).toBeNull();
    expect(result.current.envelope).toEqual(FIXTURE_ENVELOPE);
    expect(result.current.lastSync).toBe("2026-04-07T12:00:00.000Z");
    expect(result.current.data?.bankroll).toBe(148000);
  });

  it("sets error on non-2xx response and leaves data untouched", async () => {
    mockFetchOnce({ error: "server exploded" }, 500);

    const { result } = renderHook(() => useFutuPortfolio(false));
    await waitFor(() => {
      expect(result.current.error).not.toBeNull();
    });

    expect(result.current.data).toBeNull();
    expect(result.current.neverSynced).toBe(false);
  });

  it("flips neverSynced back to false when a real envelope arrives after a never_synced response", async () => {
    // First fetch: never_synced
    mockFetchOnce({
      ok: false,
      code: "never_synced",
      positions: [],
      count: 0,
      account_summary: null,
      fetched_at: null,
      data_as_of: null,
    });

    const { result, rerender } = renderHook(
      ({ enabled }) => useFutuPortfolio(enabled),
      { initialProps: { enabled: false } },
    );
    await waitFor(() => {
      expect(result.current.neverSynced).toBe(true);
    });

    // Second fetch via syncNow: real envelope
    mockFetchOnce(FIXTURE_ENVELOPE);
    await act(async () => {
      await result.current.syncNow();
    });

    await waitFor(() => {
      expect(result.current.neverSynced).toBe(false);
    });
    expect(result.current.data).not.toBeNull();
  });
});
