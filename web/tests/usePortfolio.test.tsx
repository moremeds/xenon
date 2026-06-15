/** @vitest-environment jsdom */
import { describe, it, expect, vi, afterEach } from "vitest";
import { renderHook, waitFor, cleanup } from "@testing-library/react";

import { usePortfolio } from "@/lib/usePortfolio";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function stubFetch() {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ last_sync: null, positions: [] }),
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("usePortfolio", () => {
  it("still reads once on mount when inactive (closed-market render)", async () => {
    const fetchMock = stubFetch();
    renderHook(() => usePortfolio(false));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock).toHaveBeenCalledWith("/api/portfolio");
  });

  it("does NOT read on mount when skipReads (operator console)", async () => {
    const fetchMock = stubFetch();
    const { result } = renderHook(() =>
      usePortfolio(false, { skipReads: true }),
    );
    // Give any mount effects a chance to run, then assert no GET fired — so
    // /api/portfolio never triggers its stale-snapshot background-sync POST.
    await new Promise((r) => setTimeout(r, 25));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.current.loading).toBe(false);
  });
});
