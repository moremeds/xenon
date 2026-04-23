/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { useQuoteTokens } from "@/components/ticker-detail/useQuoteToken";

describe("useQuoteTokens", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    global.fetch = vi.fn(async (url: string) => {
      const m = url.match(/con_id=(\d+)/);
      const conId = m?.[1];
      return {
        ok: true,
        json: async () => ({ token: `tok-${conId}` }),
      } as Response;
    });
  });

  it("mints one token per leg in parallel, keyed by conId", async () => {
    const { result } = renderHook(() =>
      useQuoteTokens({
        legs: [
          { ticker: "SPY", conId: 111, expiry: "2026-05-16" },
          { ticker: "SPY", conId: 222, expiry: "2026-05-16" },
        ],
      }),
    );
    await waitFor(() => expect(result.current.tokens).not.toBeNull());
    expect(result.current.tokens).toEqual({
      "111": "tok-111",
      "222": "tok-222",
    });
    expect(result.current.error).toBeNull();
    expect(global.fetch).toHaveBeenCalledTimes(2);
  });

  it("returns error if any leg fails", async () => {
    global.fetch = vi.fn(async (url: string) =>
      url.includes("con_id=222")
        ? ({ ok: false, status: 500 } as Response)
        : ({ ok: true, json: async () => ({ token: "tok-111" }) } as Response),
    );
    const { result } = renderHook(() =>
      useQuoteTokens({
        legs: [
          { ticker: "SPY", conId: 111, expiry: "2026-05-16" },
          { ticker: "SPY", conId: 222, expiry: "2026-05-16" },
        ],
      }),
    );
    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.tokens).toBeNull();
  });
});
