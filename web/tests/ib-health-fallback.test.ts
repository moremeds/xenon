/**
 * @vitest-environment jsdom
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import {
  fetchIbConnectedFromHealth,
  useIbHealthFallback,
} from "@/lib/ibHealthFallback";

afterEach(() => vi.restoreAllMocks());

function mockFetch(body: unknown, ok = true) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok, json: async () => body }) as unknown as Response),
  );
}

describe("fetchIbConnectedFromHealth", () => {
  it("returns true when any IB pool is connected", async () => {
    mockFetch({
      ib_pool: { sync: { connected: true }, orders: { connected: false } },
    });
    expect(await fetchIbConnectedFromHealth()).toBe(true);
  });

  it("returns false when no IB pool is connected", async () => {
    mockFetch({
      ib_pool: { sync: { connected: false }, orders: { connected: false } },
    });
    expect(await fetchIbConnectedFromHealth()).toBe(false);
  });

  it("returns null on a non-ok response", async () => {
    mockFetch({ error: "down" }, false);
    expect(await fetchIbConnectedFromHealth()).toBeNull();
  });

  it("returns null when fetch throws", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("network");
      }),
    );
    expect(await fetchIbConnectedFromHealth()).toBeNull();
  });
});

describe("useIbHealthFallback (state preservation)", () => {
  it("preserves the last reading when active flips false", async () => {
    mockFetch({ ib_pool: { sync: { connected: true } } });
    const { result, rerender } = renderHook(
      ({ active }: { active: boolean }) => useIbHealthFallback(active, 1_000),
      { initialProps: { active: true } },
    );
    await waitFor(() => expect(result.current).toBe(true));
    rerender({ active: false });
    expect(result.current).toBe(true);
  });

  it("preserves the last reading when /health returns null mid-poll", async () => {
    mockFetch({ ib_pool: { sync: { connected: true } } });
    const { result } = renderHook(() => useIbHealthFallback(true, 1_000));
    await waitFor(() => expect(result.current).toBe(true));
    mockFetch({ error: "down" }, false);
    await new Promise((r) => setTimeout(r, 1_100));
    expect(result.current).toBe(true);
  });
});
