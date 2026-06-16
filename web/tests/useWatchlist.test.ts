/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useWatchlist } from "@/lib/useWatchlist";

describe("useWatchlist", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    // Stateful mock so the hook's post-write re-sync (await loadWatchlist())
    // reflects POST/DELETE — mirroring the real FastAPI watchlist surface.
    const symbols = new Set<string>(["AAPL"]);
    global.fetch = vi.fn(async (url: string, opts?: RequestInit) => {
      const method = opts?.method ?? "GET";
      if (method === "POST") {
        const body = JSON.parse(String(opts?.body ?? "{}")) as {
          symbol?: string;
        };
        if (body.symbol) symbols.add(body.symbol.toUpperCase());
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      }
      if (method === "DELETE") {
        const sym = decodeURIComponent(
          url.split("/").pop() ?? "",
        ).toUpperCase();
        symbols.delete(sym);
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      }
      return new Response(
        JSON.stringify({
          watchlist: [...symbols].map((symbol) => ({
            id: symbol,
            symbol,
            sector: null,
            added_at: "",
          })),
        }),
        { status: 200 },
      );
    }) as unknown as typeof fetch;
  });

  it("loads and reports isWatched", async () => {
    const { result } = renderHook(() => useWatchlist());
    await waitFor(() => expect(result.current.isWatched("AAPL")).toBe(true));
    expect(result.current.isWatched("TSLA")).toBe(false);
  });

  it("toggleWatch optimistically adds then persists", async () => {
    const { result } = renderHook(() => useWatchlist());
    await waitFor(() => expect(result.current.isWatched("AAPL")).toBe(true));
    await act(async () => {
      await result.current.toggleWatch("TSLA");
    });
    expect(result.current.isWatched("TSLA")).toBe(true);
  });
});
