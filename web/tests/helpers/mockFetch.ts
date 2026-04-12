/**
 * Configurable fetch mock for Vitest.
 *
 * Usage:
 *   const restore = installFetchMock({ "/api/stats": { body: {...} } });
 *   // ... test ...
 *   restore();
 */
import { vi } from "vitest";

type MockRoute = {
  body?: unknown;
  status?: number;
  headers?: Record<string, string>;
  error?: Error;
  delay?: number;
};

export function installFetchMock(routes: Record<string, MockRoute>) {
  const original = globalThis.fetch;
  const mockFn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
    const pathname = new URL(url, "http://localhost").pathname;

    const route = routes[pathname];
    if (!route) {
      return new Response(JSON.stringify({ error: "not mocked" }), {
        status: 404,
      });
    }
    if (route.error) throw route.error;
    if (route.delay) await new Promise((r) => setTimeout(r, route.delay));

    return new Response(JSON.stringify(route.body ?? {}), {
      status: route.status ?? 200,
      headers: { "Content-Type": "application/json", ...route.headers },
    });
  });

  globalThis.fetch = mockFn as typeof fetch;
  return () => {
    globalThis.fetch = original;
  };
}

export { type MockRoute };
