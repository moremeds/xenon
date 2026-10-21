/**
 * @vitest-environment jsdom
 */
import { StrictMode, createElement, type ReactNode } from "react";
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
    expect(result.current.initialRead).toBe("never_synced");
  });

  it("treats GET 404 as never_synced, not an error (route maps FastAPI 404)", async () => {
    mockFetchOnce(
      {
        error:
          "Futu portfolio not yet synced. Start Futu OpenD and POST /api/futu/sync.",
      },
      404,
    );

    const { result } = renderHook(() => useFutuPortfolio(false));
    await waitFor(() => {
      expect(result.current.initialRead).toBe("never_synced");
    });

    expect(result.current.neverSynced).toBe(true);
    expect(result.current.data).toBeNull();
    expect(result.current.envelope).toBeNull();
    // Never-synced is a measurement condition — no error surfaced.
    expect(result.current.error).toBeNull();
    expect(result.current.lastSync).toBeNull();
  });

  it("treats GET 502 as unavailable and surfaces the error", async () => {
    mockFetchOnce({ error: "Futu portfolio unavailable: boom" }, 502);

    const { result } = renderHook(() => useFutuPortfolio(false));
    await waitFor(() => {
      expect(result.current.initialRead).toBe("unavailable");
    });

    expect(result.current.data).toBeNull();
    expect(result.current.neverSynced).toBe(false);
    expect(result.current.error).not.toBeNull();
  });

  it("recovers from unavailable when a later sync succeeds", async () => {
    mockFetchOnce({ error: "Futu portfolio unavailable: boom" }, 502);
    const { result } = renderHook(() => useFutuPortfolio(false));
    await waitFor(() => {
      expect(result.current.initialRead).toBe("unavailable");
    });

    mockFetchOnce(FIXTURE_ENVELOPE);
    await act(async () => {
      await result.current.syncNow();
    });
    await waitFor(() => {
      expect(result.current.initialRead).toBe("loaded");
    });
    expect(result.current.data).not.toBeNull();
    expect(result.current.error).toBeNull();
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
    expect(result.current.initialRead).toBe("loaded");
  });

  it("sets error on non-2xx response and leaves data untouched", async () => {
    mockFetchOnce({ error: "server exploded" }, 500);

    const { result } = renderHook(() => useFutuPortfolio(false));
    await waitFor(() => {
      expect(result.current.error).not.toBeNull();
    });

    expect(result.current.data).toBeNull();
    expect(result.current.neverSynced).toBe(false);
    expect(result.current.initialRead).toBe("unavailable");
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

describe("useFutuPortfolio first sync scheduling", () => {
  const NOW = new Date("2026-10-21T14:00:00.000Z");

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    vi.setSystemTime(NOW);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  function installFetch(cachedFetchedAt: string) {
    const calls: string[] = [];
    global.fetch = vi.fn(async (_url: unknown, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      calls.push(method);
      const fetched_at = method === "POST" ? NOW.toISOString() : cachedFetchedAt;
      return new Response(JSON.stringify({ ...FIXTURE_ENVELOPE, fetched_at }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }) as typeof global.fetch;
    return calls;
  }

  async function flush(ms: number) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  }

  it("syncs within 500 ms when the cached snapshot is older than 60 s", async () => {
    const calls = installFetch(new Date(NOW.getTime() - 61_000).toISOString());
    renderHook(() => useFutuPortfolio(true));
    await flush(0);
    expect(calls).toEqual(["GET"]);

    await flush(600);
    expect(calls).toEqual(["GET", "POST"]);
  });

  it("keeps the 30 s cadence when the cached snapshot is fresh", async () => {
    const calls = installFetch(new Date(NOW.getTime() - 10_000).toISOString());
    renderHook(() => useFutuPortfolio(true));
    await flush(0);
    await flush(5_000);
    expect(calls).toEqual(["GET"]);

    await flush(26_000);
    expect(calls).toEqual(["GET", "POST"]);
  });

  it("does not schedule any sync while disabled", async () => {
    const calls = installFetch(new Date(NOW.getTime() - 600_000).toISOString());
    renderHook(() => useFutuPortfolio(false));
    await flush(0);
    await flush(60_000);
    expect(calls).toEqual(["GET"]);
  });
});

describe("useFutuPortfolio arming order (enabled flips after mount)", () => {
  const NOW = new Date("2026-10-21T14:30:00.000Z");
  const STALE = new Date(NOW.getTime() - 600_000).toISOString();
  const FRESH = new Date(NOW.getTime() - 10_000).toISOString();

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    vi.setSystemTime(NOW);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  /** GET resolves only when `release()` is called; POST resolves at once. */
  function installGatedFetch(cachedFetchedAt: string) {
    const calls: string[] = [];
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => {
      release = r;
    });
    global.fetch = vi.fn(async (_url: unknown, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      calls.push(method);
      if (method === "GET") await gate;
      const fetched_at = method === "POST" ? NOW.toISOString() : cachedFetchedAt;
      return new Response(JSON.stringify({ ...FIXTURE_ENVELOPE, fetched_at }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }) as typeof global.fetch;
    return { calls, release: () => release() };
  }

  async function flush(ms: number) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  }

  it("enabled flips true BEFORE the GET resolves: stale cache -> POST within ~0.5 s", async () => {
    const { calls, release } = installGatedFetch(STALE);
    const { rerender } = renderHook(({ on }) => useFutuPortfolio(on), {
      initialProps: { on: false },
    });
    await flush(0);
    rerender({ on: true });
    await flush(1_000);
    expect(calls).toEqual(["GET"]); // GET still pending, nothing armed yet
    release();
    await flush(0);
    await flush(600);
    expect(calls).toEqual(["GET", "POST"]);
  });

  it("enabled true from the start: stale cache -> POST within ~0.5 s of the GET", async () => {
    const { calls, release } = installGatedFetch(STALE);
    renderHook(() => useFutuPortfolio(true));
    await flush(0);
    release();
    await flush(0);
    await flush(600);
    expect(calls).toEqual(["GET", "POST"]);
  });

  it("GET resolves first, then enabled flips -> POST follows", async () => {
    const { calls, release } = installGatedFetch(STALE);
    const { rerender } = renderHook(({ on }) => useFutuPortfolio(on), {
      initialProps: { on: false },
    });
    await flush(0);
    release();
    await flush(0);
    await flush(60_000);
    expect(calls).toEqual(["GET"]);
    rerender({ on: true });
    await flush(600);
    expect(calls).toEqual(["GET", "POST"]);
  });

  it("works under StrictMode double effects (one GET, one POST)", async () => {
    const { calls, release } = installGatedFetch(STALE);
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(StrictMode, null, children);
    const { rerender } = renderHook(({ on }) => useFutuPortfolio(on), {
      wrapper,
      initialProps: { on: false },
    });
    await flush(0);
    rerender({ on: true });
    release();
    await flush(0);
    await flush(600);
    expect(calls.filter((c) => c === "GET")).toHaveLength(1);
    expect(calls.filter((c) => c === "POST")).toHaveLength(1);
  });

  it("fresh cache and enabled flips late: first POST at 30 s, not earlier", async () => {
    const { calls, release } = installGatedFetch(FRESH);
    const { rerender } = renderHook(({ on }) => useFutuPortfolio(on), {
      initialProps: { on: false },
    });
    await flush(0);
    rerender({ on: true });
    release();
    await flush(0);
    await flush(20_000);
    expect(calls).toEqual(["GET"]);
    await flush(11_000);
    expect(calls).toEqual(["GET", "POST"]);
  });

  it("stays disabled: no POST ever", async () => {
    const { calls, release } = installGatedFetch(STALE);
    renderHook(() => useFutuPortfolio(false));
    await flush(0);
    release();
    await flush(0);
    await flush(120_000);
    expect(calls).toEqual(["GET"]);
  });
});

describe("useFutuPortfolio lifecycle (no work after disable/unmount)", () => {
  const NEVER = {
    ok: false,
    code: "never_synced",
    positions: [],
    count: 0,
    account_summary: null,
    fetched_at: null,
    data_as_of: null,
  };
  const reply = () => new Response(JSON.stringify(NEVER));

  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  async function flush(ms = 0) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  }

  function gatedFirstPost(failFirst = false) {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    const state = { posts: 0 };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_u: unknown, init?: RequestInit) => {
        if (init?.method === "POST") {
          state.posts++;
          if (state.posts === 1) {
            await gate;
            if (failFirst) throw new Error("boom");
          }
        }
        return reply();
      }),
    );
    return { state, release: () => release() };
  }

  it("does not POST after disable while a POST is in flight", async () => {
    const { state, release } = gatedFirstPost();
    const { rerender } = renderHook(({ on }) => useFutuPortfolio(on), {
      initialProps: { on: true },
    });
    await flush();
    await flush(500);
    expect(state.posts).toBe(1);
    rerender({ on: false });
    release();
    await flush();
    await flush(30_000);
    expect(state.posts).toBe(1);
  });

  it("does not rearm after unmount while a POST is in flight", async () => {
    const { state, release } = gatedFirstPost();
    const { unmount } = renderHook(() => useFutuPortfolio(true));
    await flush();
    await flush(500);
    unmount();
    release();
    await flush();
    await flush(60_000);
    expect(state.posts).toBe(1);
  });

  it("does not back off and retry after unmount when the in-flight POST fails", async () => {
    const { state, release } = gatedFirstPost(true);
    const { unmount } = renderHook(() => useFutuPortfolio(true));
    await flush();
    await flush(500);
    unmount();
    release();
    await flush();
    await flush(600_000);
    expect(state.posts).toBe(1);
  });

  it("cleans a visibility timer on unmount before the GET settles", async () => {
    let posts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_u: unknown, init?: RequestInit) => {
        if (init?.method === "POST") {
          posts++;
          return reply();
        }
        return new Promise<Response>(() => {});
      }),
    );
    const { unmount } = renderHook(() => useFutuPortfolio(true));
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "visible",
    });
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    unmount();
    await flush(500);
    expect(posts).toBe(0);
  });

  it("drops a visibility timer when disabled before the GET settles", async () => {
    let posts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_u: unknown, init?: RequestInit) => {
        if (init?.method === "POST") {
          posts++;
          return reply();
        }
        return new Promise<Response>(() => {});
      }),
    );
    const { rerender } = renderHook(({ on }) => useFutuPortfolio(on), {
      initialProps: { on: true },
    });
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    rerender({ on: false });
    await flush(1_000);
    expect(posts).toBe(0);
  });
});
