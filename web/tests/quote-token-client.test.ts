// @vitest-environment jsdom
import { describe, test, expect, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { useQuoteToken } from "../components/ticker-detail/useQuoteToken";

function stubFetch(token = "t.sig") {
  return vi.fn(async (_url: RequestInfo) => {
    return new Response(
      JSON.stringify({ token, bid: "500.10", ask: "500.20" }),
      { status: 200 },
    );
  });
}

describe("useQuoteToken", () => {
  test("fetches on mount with ticker + con_id", async () => {
    const spy = stubFetch();
    vi.stubGlobal("fetch", spy);
    const { result } = renderHook(() =>
      useQuoteToken({ ticker: "SPY", conId: 756733, expiry: null }),
    );
    await vi.waitFor(() => expect(result.current.token).toBe("t.sig"));
    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy.mock.calls[0][0] as string).toContain("ticker=SPY");
    vi.unstubAllGlobals();
  });

  test("refetches when ticker changes", async () => {
    const spy = stubFetch();
    vi.stubGlobal("fetch", spy);
    const { rerender } = renderHook(
      ({ ticker }) => useQuoteToken({ ticker, conId: 1, expiry: null }),
      { initialProps: { ticker: "SPY" } },
    );
    await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    rerender({ ticker: "QQQ" });
    await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(2));
    vi.unstubAllGlobals();
  });

  test("refetches when expiry changes", async () => {
    const spy = stubFetch();
    vi.stubGlobal("fetch", spy);
    const { rerender } = renderHook(
      ({ expiry }) => useQuoteToken({ ticker: "SPY", conId: 1, expiry }),
      { initialProps: { expiry: "20260620" as string | null } },
    );
    await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    rerender({ expiry: "20260718" });
    await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(2));
    vi.unstubAllGlobals();
  });

  test("does NOT refetch on unrelated state changes", async () => {
    const spy = stubFetch();
    vi.stubGlobal("fetch", spy);
    const { rerender } = renderHook(
      ({ irrelevant }) => {
        useQuoteToken({ ticker: "SPY", conId: 1, expiry: null });
        return irrelevant;
      },
      { initialProps: { irrelevant: 1 } },
    );
    await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    rerender({ irrelevant: 2 });
    expect(spy).toHaveBeenCalledTimes(1);
    vi.unstubAllGlobals();
  });
});
