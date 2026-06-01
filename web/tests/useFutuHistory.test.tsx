/**
 * @vitest-environment jsdom
 */
// C15: file is `.tsx` for parity with sibling hook tests even though no JSX
// remains — the renderHook wrapper is no longer needed because hooks now use
// plain useState+useEffect instead of @tanstack/react-query.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import { useFutuEquityCurve, useFutuSync } from "@/lib/useFutuHistory";

beforeEach(() => {
  global.fetch = vi.fn();
});

describe("useFutuEquityCurve", () => {
  it("hits /api/futu/equity-curve with from/to query", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      json: async () => [
        {
          date: "2024-05-01",
          nav: 1000,
          source: "reconstructed",
          daily_pnl: 0,
          recon: null,
        },
      ],
    });
    const { result } = renderHook(() =>
      useFutuEquityCurve({ from: "2024-05-01", to: "2024-05-01" }),
    );
    await waitFor(() => expect(result.current.data).not.toBeNull());
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/futu/equity-curve"),
    );
    expect(result.current.data?.[0].source).toBe("reconstructed");
  });
});

describe("useFutuSync", () => {
  it("POSTs to /api/futu/history/sync", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      json: async () => ({
        status: "ok",
        fills_added: 3,
        cash_flows_added: 0,
        nav_rows_added: 1,
        warnings: [],
      }),
    });
    const { result } = renderHook(() => useFutuSync());
    await act(async () => {
      await result.current.mutateAsync({ since: "auto" });
    });
    expect(fetch).toHaveBeenCalledWith(
      "/api/futu/history/sync",
      expect.objectContaining({ method: "POST" }),
    );
  });
});
