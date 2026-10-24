/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { renderHook, waitFor, cleanup, act } from "@testing-library/react";
import {
  getFutuAccountEnv,
  setFutuAccountEnv,
  futuScopeQuery,
} from "@/lib/futuAccountEnv";
import { useOrders } from "@/lib/useOrders";

const originalFetch = global.fetch;

const fakeLocalStorage = (() => {
  const store = new Map<string, string>();
  return {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => {
      store.set(k, v);
    },
    removeItem: (k: string) => {
      store.delete(k);
    },
    clear: () => store.clear(),
  };
})();

Object.defineProperty(window, "localStorage", {
  value: fakeLocalStorage,
  writable: true,
  configurable: true,
});

beforeEach(() => {
  fakeLocalStorage.clear();
  setFutuAccountEnv("paper");
});

afterEach(() => {
  cleanup();
  global.fetch = originalFetch;
  vi.restoreAllMocks();
  fakeLocalStorage.clear();
});

function jsonRes(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("futuAccountEnv helpers", () => {
  it("futuScopeQuery encodes paper and live", () => {
    expect(futuScopeQuery("paper")).toBe("?broker=FUTU&account_env=paper");
    expect(futuScopeQuery("live")).toBe("?broker=FUTU&account_env=live");
  });

  it("persists env in localStorage", () => {
    setFutuAccountEnv("live");
    expect(getFutuAccountEnv()).toBe("live");
  });
});

describe("useOrders — Futu account_env cache isolation", () => {
  it("uses account_env in fetch URLs for FUTU broker", async () => {
    const urls: string[] = [];
    global.fetch = vi.fn((url: string | URL) => {
      urls.push(String(url));
      return Promise.resolve(
        jsonRes({
          open_orders: [],
          executed_orders: [],
          last_sync: String(url),
        }),
      );
    }) as typeof fetch;

    renderHook(() => useOrders(false, "FUTU"));
    await waitFor(() => expect(urls.length).toBeGreaterThan(0));
    expect(urls[0]).toContain("account_env=paper");
  });

  it("never shows the other env's data after SIMULATE→REAL switch", async () => {
    let resolvePaper!: (r: Response) => void;
    const paperPending = new Promise<Response>((r) => {
      resolvePaper = r;
    });

    global.fetch = vi.fn((url: string | URL) => {
      const s = String(url);
      if (s.includes("account_env=live")) {
        return Promise.resolve(
          jsonRes({
            open_orders: [],
            executed_orders: [],
            last_sync: "live-sync",
          }),
        );
      }
      if (s.includes("account_env=paper")) {
        return paperPending;
      }
      return Promise.resolve(jsonRes({ last_sync: "other" }));
    }) as typeof fetch;

    const { result } = renderHook(() => useOrders(false, "FUTU"));

    act(() => setFutuAccountEnv("live"));
    await waitFor(() =>
      expect(result.current.data?.last_sync).toBe("live-sync"),
    );

    await act(async () => {
      resolvePaper(
        jsonRes({
          open_orders: [],
          executed_orders: [],
          last_sync: "paper-sync",
        }),
      );
      await Promise.resolve();
    });

    expect(result.current.data?.last_sync).toBe("live-sync");
  });
});
