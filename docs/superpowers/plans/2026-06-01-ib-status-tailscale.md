# IB Status Down Over Tailscale — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the IB connection badge read correctly from any host (Mac mini _and_ remote Tailscale device) by fixing the realtime-WS URL host resolution, and stop a dropped realtime stream from being shown as "IB Gateway down."

**Architecture:** `/api/ib/ws-config` currently derives the WS host from `request.nextUrl`, which under `HOSTNAME=0.0.0.0` returns `ws://0.0.0.0:8765` to every client — unreachable from a remote browser. We switch it to derive the host from the request's `Host`/`X-Forwarded-Host` header (scheme from `X-Forwarded-Proto`). Separately, the IB-connected signal — currently sourced only from realtime-WS `status` messages — gains a `/api/health` fallback so it reflects the real IB Gateway state when the stream is down.

**Tech Stack:** Next.js App Router (route handlers), React hooks, TypeScript, Vitest (unit), Playwright/chrome-cdp (browser). Deployment: Docker Compose on a Mac mini (`/opt/xenon/compose.yml`).

**Spec:** `docs/superpowers/specs/2026-06-01-ib-status-tailscale-design.md`

> **⚠️ Symbol collision — read before editing.** There are **two** functions named `resolveBrowserIbRealtimeWsUrl` in this repo:
>
> 1. `web/lib/server/ibRealtimeRuntime.ts` — **server-side** resolver, takes options object, returns `string`. Called by `web/app/api/ib/ws-config/route.ts`. **This is the one Task 1 modifies.**
> 2. `web/lib/ibRealtimeWsClient.ts` — **client-side** wrapper, no args, returns `Promise<string>`, calls `/api/ib/ws-config`. Used by `usePrices.ts`, `IBStatusContext.tsx`, `TickerSearch.tsx`. **Do not touch.**
>
> Always grep the import path before editing — same name, different files, different contracts.

---

## File Structure

| File                                           | Change                                 | Responsibility                                                                  |
| ---------------------------------------------- | -------------------------------------- | ------------------------------------------------------------------------------- |
| `web/lib/server/ibRealtimeRuntime.ts`          | Modify `resolveBrowserIbRealtimeWsUrl` | Build the browser WS URL from request host + forwarded-proto (not `requestUrl`) |
| `web/app/api/ib/ws-config/route.ts`            | Modify                                 | Pass request headers to the resolver                                            |
| `web/tests/ib-realtime-runtime-config.test.ts` | Modify                                 | Update + extend host-derivation cases                                           |
| `web/lib/ibHealthFallback.ts`                  | Create                                 | `/api/health` IB-connected probe + polling hook                                 |
| `web/tests/ib-health-fallback.test.ts`         | Create                                 | Unit tests for the probe + hook                                                 |
| `web/lib/usePrices.ts`                         | Modify                                 | Source `ibConnected` from health when the WS is down                            |
| `web/lib/IBStatusContext.tsx`                  | Modify                                 | Remove force-false on WS close; use health fallback                             |
| `web/lib/ibConnectionAlert.ts`                 | Modify                                 | Add "live data stream offline" banner state                                     |
| `web/tests/connection-banner-state.test.ts`    | Modify                                 | Test the new banner state                                                       |
| `/opt/xenon/web.env` (deploy host)             | Modify                                 | `IB_REALTIME_WS_URL=ws://realtime:8765` (server-side path)                      |

> Local dev keeps `IB_REALTIME_WS_URL=ws://localhost:8765` (`web/.env.example`, `web/.env.local.example`) — correct when everything runs on localhost. Only the **production** env file changes.

---

## Task 1: Header-derived realtime WS host (the core fix)

**Files:**

- Modify: `web/lib/server/ibRealtimeRuntime.ts` (`resolveBrowserIbRealtimeWsUrl`, ~line 51)
- Modify: `web/app/api/ib/ws-config/route.ts`
- Test: `web/tests/ib-realtime-runtime-config.test.ts`

- [ ] **Step 1: Rewrite the resolver test cases (failing)**

Replace the `resolveBrowserIbRealtimeWsUrl` test block (the `"builds a browser-safe websocket URL from the runtime file and request host"` case) and add new cases. New test code:

```ts
describe("resolveBrowserIbRealtimeWsUrl (header-derived)", () => {
  it("builds the URL from the runtime-file port and the Host header", () => {
    const runtimeFile = writeRuntimeFile(8876);
    expect(
      resolveBrowserIbRealtimeWsUrl({
        envUrl: undefined,
        runtimeFile,
        host: "localhost:3000",
      }),
    ).toBe("ws://localhost:8876");
  });

  it("uses the default port and the Tailscale host", () => {
    expect(
      resolveBrowserIbRealtimeWsUrl({
        envUrl: undefined,
        host: "mini.tailnet.ts.net:3000",
      }),
    ).toBe("ws://mini.tailnet.ts.net:8765");
  });

  it("prefers x-forwarded-host over host and uses wss for https", () => {
    expect(
      resolveBrowserIbRealtimeWsUrl({
        envUrl: undefined,
        host: "internal:3000",
        forwardedHost: "edge.example.com",
        forwardedProto: "https",
      }),
    ).toBe("wss://edge.example.com:8765");
  });

  it("preserves IPv6 host literals", () => {
    expect(
      resolveBrowserIbRealtimeWsUrl({ envUrl: undefined, host: "[::1]:3000" }),
    ).toBe("ws://[::1]:8765");
  });

  it("falls back to loopback when no host header is present", () => {
    expect(
      resolveBrowserIbRealtimeWsUrl({ envUrl: undefined, host: null }),
    ).toBe("ws://127.0.0.1:8765");
  });

  it("rejects literal 0.0.0.0 and falls back to loopback", () => {
    // Defensive — prevents a misconfigured proxy forwarding x-forwarded-host: 0.0.0.0
    // from reintroducing the exact bug this PR fixes.
    expect(
      resolveBrowserIbRealtimeWsUrl({
        envUrl: undefined,
        host: "0.0.0.0:3000",
      }),
    ).toBe("ws://127.0.0.1:8765");
    // When forwardedHost is 0.0.0.0 but host is a real value, the rejected
    // forwardedHost falls through the `??` chain to host. Codex's vote: prefer
    // recovering via host over emitting loopback when a real Host header exists.
    expect(
      resolveBrowserIbRealtimeWsUrl({
        envUrl: undefined,
        host: "trusted:3000",
        forwardedHost: "0.0.0.0",
      }),
    ).toBe("ws://trusted:8765");
    // Both forwardedHost and host literal 0.0.0.0 → loopback.
    expect(
      resolveBrowserIbRealtimeWsUrl({
        envUrl: undefined,
        host: "0.0.0.0:3000",
        forwardedHost: "0.0.0.0",
      }),
    ).toBe("ws://127.0.0.1:8765");
  });

  it("treats an empty host string as missing (loopback fallback)", () => {
    // Spec says "if the host header is missing/blank, fall back". stripHostPort's
    // !h.trim() branch must be pinned by a test — otherwise a future simplification
    // could silently emit ws://:8765 (port-only, unreachable).
    expect(resolveBrowserIbRealtimeWsUrl({ envUrl: undefined, host: "" })).toBe(
      "ws://127.0.0.1:8765",
    );
    expect(
      resolveBrowserIbRealtimeWsUrl({ envUrl: undefined, host: "   " }),
    ).toBe("ws://127.0.0.1:8765");
  });

  it("lets an explicit env URL override everything", () => {
    expect(
      resolveBrowserIbRealtimeWsUrl({
        envUrl: "ws://quotes.internal:9001",
        host: "x:3000",
      }),
    ).toBe("ws://quotes.internal:9001");
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd web && npx vitest run tests/ib-realtime-runtime-config.test.ts`
Expected: FAIL — `resolveBrowserIbRealtimeWsUrl` does not accept `host`/`forwardedHost`/`forwardedProto`.

- [ ] **Step 3: Implement the header-derived resolver**

In `web/lib/server/ibRealtimeRuntime.ts`, add a host helper above the function:

```ts
function stripHostPort(host: string | null | undefined): string | null {
  if (!host) return null;
  const h = host.trim();
  if (!h) return null;
  let bare: string;
  if (h.startsWith("[")) {
    const end = h.indexOf("]");
    bare = end === -1 ? h : h.slice(0, end + 1); // keep [::1]
  } else {
    const colon = h.indexOf(":");
    bare = colon === -1 ? h : h.slice(0, colon);
  }
  // Defensive: never serialize literal `0.0.0.0` (or `::`) as a connect-to
  // address, even if a misconfigured proxy forwards it. The whole bug class
  // this PR fixes was caused by exactly this string flowing to the browser.
  if (bare === "0.0.0.0" || bare === "::" || bare === "[::]") return null;
  return bare;
}
```

Replace `resolveBrowserIbRealtimeWsUrl` with:

```ts
export function resolveBrowserIbRealtimeWsUrl({
  envUrl = process.env.NEXT_PUBLIC_IB_REALTIME_WS_URL,
  runtimeFile,
  host,
  forwardedHost,
  forwardedProto,
  defaultPort = DEFAULT_IB_REALTIME_PORT,
}: {
  envUrl?: string;
  runtimeFile?: string;
  host?: string | null;
  forwardedHost?: string | null;
  forwardedProto?: string | null;
  defaultPort?: number;
}): string {
  if (envUrl) return envUrl;
  const runtime = readIbRealtimeRuntimeFile(runtimeFile);
  const port = runtime?.port ?? defaultPort;
  const hostNoPort = stripHostPort(forwardedHost) ?? stripHostPort(host);
  if (!hostNoPort) return `ws://127.0.0.1:${port}`;
  const protocol = forwardedProto === "https" ? "wss:" : "ws:";
  return `${protocol}//${hostNoPort}:${port}`;
}
```

- [ ] **Step 4: Update the route to pass headers**

Replace `web/app/api/ib/ws-config/route.ts` body:

```ts
import { NextRequest, NextResponse } from "next/server";
import { resolveBrowserIbRealtimeWsUrl } from "@/lib/server/ibRealtimeRuntime";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const h = request.headers;
  return NextResponse.json({
    url: resolveBrowserIbRealtimeWsUrl({
      host: h.get("host"),
      forwardedHost: h.get("x-forwarded-host"),
      forwardedProto: h.get("x-forwarded-proto"),
    }),
  });
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd web && npx vitest run tests/ib-realtime-runtime-config.test.ts`
Expected: PASS (all cases, including the unchanged `resolveServerIbRealtimeWsUrl` cases).

- [ ] **Step 6: Typecheck**

Run: `cd web && npm run typecheck`
Expected: no errors (confirms no other caller relied on the removed `requestUrl` param).

- [ ] **Step 7: Live route smoke-test (optional but recommended)**

If `npm run dev` is up (or the deployed `xenon-web-1` is locally reachable), curl the route with a spoofed `Host` header to prove the fix end-to-end:

```bash
curl -sS -H 'Host: macmini.tail20094b.ts.net:3000' \
     http://localhost:3000/api/ib/ws-config
```

Expected (post-fix): `{"url":"ws://macmini.tail20094b.ts.net:8765"}`
Pre-fix this returned `{"url":"ws://0.0.0.0:8765"}` regardless of the `Host` header.

- [ ] **Step 8: Commit**

```bash
git add web/lib/server/ibRealtimeRuntime.ts web/app/api/ib/ws-config/route.ts web/tests/ib-realtime-runtime-config.test.ts
git commit -m "fix(web): derive realtime WS host from request Host header, not nextUrl"
```

---

## Task 2: `/api/health` IB-connected fallback (probe + hook)

**Files:**

- Create: `web/lib/ibHealthFallback.ts`
- Test: `web/tests/ib-health-fallback.test.ts`

- [ ] **Step 1: Write the failing test**

```ts
import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchIbConnectedFromHealth } from "@/lib/ibHealthFallback";

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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd web && npx vitest run tests/ib-health-fallback.test.ts`
Expected: FAIL — module `@/lib/ibHealthFallback` not found.

- [ ] **Step 3: Implement the probe + hook**

Create `web/lib/ibHealthFallback.ts`:

```ts
"use client";

import { useEffect, useState } from "react";

/**
 * Probe the real IB Gateway state via the server-side /api/health proxy.
 * Returns true/false for connected, or null when health itself is unreachable
 * (caller treats null as "unknown", not "down").
 */
export async function fetchIbConnectedFromHealth(
  signal?: AbortSignal,
): Promise<boolean | null> {
  try {
    const res = await fetch("/api/health", { cache: "no-store", signal });
    if (!res.ok) return null;
    const data = (await res.json()) as {
      ib_pool?: Record<string, { connected?: boolean }>;
    };
    const pool = data?.ib_pool;
    if (pool && typeof pool === "object") {
      // Iterate every pool role rather than hardcode {sync,orders,data} —
      // if FastAPI adds a new role tomorrow (e.g. `quotes`), this still works.
      // Adversarial Pass-3 finding: hardcoded set is brittle.
      return Object.values(pool).some(
        (role): role is { connected: true } =>
          !!role &&
          typeof role === "object" &&
          (role as { connected?: unknown }).connected === true,
      );
    }
    return null;
  } catch {
    return null;
  }
}

/**
 * While `active`, poll /api/health for the IB-connected state.
 * Returns the last KNOWN reading: null only before any successful poll.
 * Preserves the previous value on:
 *   - active flipping false (don't wipe — caller may flip back to true shortly)
 *   - fetch returning null (transient health-route failure — keep last good)
 */
export function useIbHealthFallback(
  active: boolean,
  intervalMs = 15_000,
): boolean | null {
  const [ibConnected, setIbConnected] = useState<boolean | null>(null);

  useEffect(() => {
    if (!active) return; // stop polling; keep last reading

    let cancelled = false;
    const controller = new AbortController();
    const poll = async () => {
      const reading = await fetchIbConnectedFromHealth(controller.signal);
      // Only overwrite when we got a real reading; preserve last value on null.
      if (!cancelled && reading !== null) setIbConnected(reading);
    };
    void poll();
    const id = setInterval(() => void poll(), intervalMs);
    return () => {
      cancelled = true;
      controller.abort();
      clearInterval(id);
    };
  }, [active, intervalMs]);

  return ibConnected;
}
```

Add these test cases to `web/tests/ib-health-fallback.test.ts` to pin the new behavior:

```ts
import { renderHook, waitFor } from "@testing-library/react";
import { useIbHealthFallback } from "@/lib/ibHealthFallback";

describe("useIbHealthFallback (state preservation)", () => {
  it("preserves the last reading when active flips false", async () => {
    mockFetch({ ib_pool: { sync: { connected: true } } });
    const { result, rerender } = renderHook(
      ({ active }: { active: boolean }) => useIbHealthFallback(active, 1_000),
      { initialProps: { active: true } },
    );
    await waitFor(() => expect(result.current).toBe(true));
    rerender({ active: false });
    // WS reconnected — we stop polling but keep the last known value.
    expect(result.current).toBe(true);
  });

  it("preserves the last reading when /health returns null mid-poll", async () => {
    mockFetch({ ib_pool: { sync: { connected: true } } });
    const { result } = renderHook(() => useIbHealthFallback(true, 1_000));
    await waitFor(() => expect(result.current).toBe(true));
    mockFetch({ error: "down" }, false); // next poll returns null
    // After another poll cycle, previous reading still holds.
    await new Promise((r) => setTimeout(r, 1_100));
    expect(result.current).toBe(true);
  });
});
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd web && npx vitest run tests/ib-health-fallback.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/lib/ibHealthFallback.ts web/tests/ib-health-fallback.test.ts
git commit -m "feat(web): add /api/health IB-connected fallback probe and polling hook"
```

---

## Task 3: Source `usePrices` `ibConnected` from health when the WS is down

**Files:**

- Modify: `web/lib/usePrices.ts` (return block ~line 620; hook body)
- Test: `web/tests/use-prices-health-fallback.test.ts` (create)

- [ ] **Step 1: Write the failing test**

Create `web/tests/use-prices-health-fallback.test.ts`. Use the existing MockWebSocket pattern from `use-prices-ws-stability.test.ts` (a WS that never reaches OPEN), and mock `/api/health` to report IB connected:

```ts
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { usePrices } from "@/lib/usePrices";

class DeadWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  readyState = DeadWebSocket.CONNECTING;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  constructor() {
    setTimeout(() => {
      this.readyState = DeadWebSocket.CLOSED;
      this.onclose?.();
    }, 0);
  }
  send() {}
  close() {
    this.readyState = DeadWebSocket.CLOSED;
  }
}

beforeEach(() => {
  vi.stubGlobal("WebSocket", DeadWebSocket as unknown as typeof WebSocket);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      url.includes("/api/health")
        ? {
            ok: true,
            json: async () => ({ ib_pool: { sync: { connected: true } } }),
          }
        : { ok: true, json: async () => ({ url: "ws://localhost:8765" }) },
    ) as unknown as typeof fetch,
  );
});
afterEach(() => vi.restoreAllMocks());

describe("usePrices health fallback", () => {
  it("reports ibConnected=true from /api/health when the WS never opens", async () => {
    const { result } = renderHook(() =>
      usePrices({ symbols: ["AAPL"], contracts: [], indexes: [] }),
    );
    await waitFor(() => expect(result.current.connected).toBe(false));
    await waitFor(() => expect(result.current.ibConnected).toBe(true));
  });

  it("reports ibConnected=false when WS is down AND /api/health reports IB down", async () => {
    // Spec test matrix lines 106-109: WS down + health down → both false.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        url.includes("/api/health")
          ? {
              ok: true,
              json: async () => ({
                ib_pool: {
                  sync: { connected: false },
                  orders: { connected: false },
                  data: { connected: false },
                },
              }),
            }
          : { ok: true, json: async () => ({ url: "ws://localhost:8765" }) },
      ) as unknown as typeof fetch,
    );
    const { result } = renderHook(() =>
      usePrices({ symbols: ["AAPL"], contracts: [], indexes: [] }),
    );
    await waitFor(() => expect(result.current.connected).toBe(false));
    await waitFor(() => expect(result.current.ibConnected).toBe(false));
  });

  it("does not poll /api/health when hook is disabled or has no subscriptions", async () => {
    // Codex review: gate health polling on `enabled && hasSubscriptions && !connected`.
    // Without that gate, a disabled or empty hook would still hammer /api/health.
    const fetchSpy = vi.fn(async () => ({
      ok: true,
      json: async () => ({ ib_pool: { sync: { connected: true } } }),
    }));
    vi.stubGlobal("fetch", fetchSpy as unknown as typeof fetch);
    renderHook(() =>
      usePrices({
        symbols: [],
        contracts: [],
        indexes: [],
        enabled: false,
      }),
    );
    // Give the effect a few microtasks; expect zero /api/health hits.
    await new Promise((r) => setTimeout(r, 50));
    const healthCalls = fetchSpy.mock.calls.filter(([url]) =>
      String(url).includes("/api/health"),
    );
    expect(healthCalls).toHaveLength(0);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd web && npx vitest run tests/use-prices-health-fallback.test.ts`
Expected: FAIL — `ibConnected` stays `false` (no health fallback yet).

- [ ] **Step 3: Implement the fallback in `usePrices`**

At the top of `usePrices.ts`, add the import (next to the existing `resolveBrowserIbRealtimeWsUrl` import at line 17):

```ts
import { useIbHealthFallback } from "./ibHealthFallback";
```

The WS-sourced state at line 94 (`const [ibConnected, setIbConnected] = useState(false);`) stays as the **WS** value. Rename its _exposed_ form. Just before the `return {` block (~line 620), add:

```ts
// When the realtime WS is down, the WS `status` message can't tell us the IB
// state. Fall back to the authoritative /api/health probe so a dead stream
// is not reported as "IB Gateway down".
//
// Gate the poll: only when (a) the hook is enabled by the caller, (b) there's
// at least one subscription so the WS would actually try to open, and (c) the
// WS is not currently connected. Otherwise polling wastes /api/health calls.
// Mirrors usePrices.ts:325-327's own no-connect early-return.
const hasSubscriptions =
  symbols.length > 0 || contracts.length > 0 || indexes.length > 0;
const healthIbConnected = useIbHealthFallback(
  enabled && hasSubscriptions && !connected,
);
const effectiveIbConnected = connected
  ? ibConnected
  : healthIbConnected === true;
```

Then change the returned field:

```ts
return {
  prices,
  fundamentals,
  connected,
  ibConnected: effectiveIbConnected,
  ibIssue,
  ibStatusMessage,
  error,
  reconnect,
  getSnapshot,
};
```

- [ ] **Step 4: Run the new test + the existing WS-stability test**

Run: `cd web && npx vitest run tests/use-prices-health-fallback.test.ts tests/use-prices-ws-stability.test.ts`
Expected: PASS (new fallback works; existing stability behavior unbroken).

- [ ] **Step 5: Commit**

```bash
git add web/lib/usePrices.ts web/tests/use-prices-health-fallback.test.ts
git commit -m "fix(web): usePrices reports IB state from /api/health when realtime WS is down"
```

---

## Task 4: Stop `IBStatusContext` forcing IB offline on WS close

**Files:**

- Modify: `web/lib/IBStatusContext.tsx` (`ws.onclose` ~line 169-188; provider body ~line 214)
- Test: `web/tests/ib-status-context.test.ts` (extend)

> **Why this task ships even though `IBStatusContext` has no current production consumers.**
> Grep confirms `useIBStatus()` / `useIBStatusContext()` is not called outside the file's own re-export and tests. But `IBStatusProvider` IS mounted unconditionally via `web/components/Providers.tsx`, so its WS runs on every page load and its (currently unread) `ibConnected` value would be `false` whenever the WS is down. A future PR adding a real consumer would inherit broken semantics. Fixing now (defense-in-depth) is cheaper than rediscovering the same bug class later. Spec line 80-83 also requires "apply to both consumers" without conditioning on whether the second is currently live.

- [ ] **Step 1: Write the failing test**

Add to `web/tests/ib-status-context.test.ts` a case that mocks `/api/health` connected and a WS that closes, asserting `ibConnected` stays true. Match the file's existing MockWebSocket + render harness. New case:

```ts
it("keeps ibConnected from /api/health after the WS closes", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      url.includes("/api/health")
        ? {
            ok: true,
            json: async () => ({ ib_pool: { sync: { connected: true } } }),
          }
        : { ok: true, json: async () => ({ url: "ws://localhost:8765" }) },
    ) as unknown as typeof fetch,
  );
  const { result } = renderHook(() => useIBStatusContext(), { wrapper });
  // CRITICAL: flush async WS construction before touching latestWs(). The
  // provider awaits resolveBrowserIbRealtimeWsUrl() inside connect(); without
  // this flush, latestWs() returns undefined and the test is racy. Mirrors
  // every other test in this file (e.g. line 82, line 102).
  await act(async () => {});
  act(() => latestWs().simulateOpen()); // make the WS open first so onclose is a real drop
  act(() => latestWs().simulateClose());
  await waitFor(() => expect(result.current.wsConnected).toBe(false));
  await waitFor(() => expect(result.current.ibConnected).toBe(true));
});
```

Note: this test uses the file's existing `wrapper` helper (line 59), the `latestWs()` accessor (line 55), and the `MockWebSocket.simulateOpen()`/`simulateClose()` methods (lines 41-51) — all already defined in the file. The earlier `IBStatusProvider`-as-wrapper form was a mistake; use `wrapper`.

The existing test file (`web/tests/ib-status-context.test.ts`) already imports `vi`, `renderHook`, `act`, `IBStatusProvider`, `useIBStatusContext`. **Add `waitFor`** to the existing `@testing-library/react` import — change:

```ts
import { renderHook, act } from "@testing-library/react";
```

to:

```ts
import { renderHook, act, waitFor } from "@testing-library/react";
```

No other imports need to change.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd web && npx vitest run tests/ib-status-context.test.ts`
Expected: FAIL — on close the provider currently force-sets `ibConnected=false`.

- [ ] **Step 3: Implement — remove force-false, add health fallback**

In `web/lib/IBStatusContext.tsx`, import the hook (next to line 14):

```ts
import { useIbHealthFallback } from "./ibHealthFallback";
```

In `ws.onclose` (lines ~169-188), delete ONLY the `setIbConnected(false)` line. Keep `setDisconnectedSince` and the `prevConnectedRef` guard — `disconnectedSince` is a separate signal (how long the WS has been down) that downstream tests + UI still depend on. Removing the whole block would break `ib-status-context.test.ts:208` ("sets wsConnected false and disconnectedSince when WS drops"), which we are NOT modifying.

```ts
ws.onclose = () => {
  if (!mountedRef.current) return;
  setWsConnected(false);
  clearStalenessTimer();

  // WS just dropped — track when, but do NOT force ibConnected=false.
  // The /api/health fallback below is authoritative for IB state.
  if (prevConnectedRef.current !== false) {
    setDisconnectedSince((prev) => prev ?? Date.now());
    prevConnectedRef.current = false;
  }

  // Schedule reconnect with backoff
  if (strategyRef.current.canRetry()) {
    const delay = strategyRef.current.nextDelay();
    reconnectTimerRef.current = setTimeout(() => {
      if (mountedRef.current) connect();
    }, delay);
  }
};
```

(Removed: only the `setIbConnected(false)` line. The `setDisconnectedSince` + `prevConnectedRef.current = false` lines stay — they're unrelated to IB state.)

Before the `connectionState` derivation (~line 214), add the fallback and an effective value:

```ts
const healthIbConnected = useIbHealthFallback(!wsConnected);
const effectiveIbConnected = wsConnected
  ? ibConnected
  : healthIbConnected === true;
```

Change the `connectionState` derivation and the provider value to use `effectiveIbConnected`:

```ts
  const connectionState: ConnectionState =
    wsConnected && effectiveIbConnected
      ? "connected"
      : wsConnected && !effectiveIbConnected
        ? "ib_offline"
        : "relay_offline";

  return (
    <IBStatusContext.Provider
      value={{ wsConnected, ibConnected: effectiveIbConnected, disconnectedSince, connectionState }}
    >
      {children}
    </IBStatusContext.Provider>
  );
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd web && npx vitest run tests/ib-status-context.test.ts`
Expected: PASS (including the existing "cleans up WebSocket on unmount" and shared-connection cases).

- [ ] **Step 5: Commit**

```bash
git add web/lib/IBStatusContext.tsx web/tests/ib-status-context.test.ts
git commit -m "fix(web): IBStatusContext sources IB state from health instead of forcing offline on WS close"
```

---

## Task 5: "Live data stream offline" banner (IB up, WS down)

**Files:**

- Modify: `web/lib/ibConnectionAlert.ts` (`getConnectionBannerState`)
- Test: `web/tests/connection-banner-state.test.ts` (extend)

- [ ] **Step 1: Write the failing test**

Add to `web/tests/connection-banner-state.test.ts`:

```ts
it("warns when IB is up but the realtime data stream is offline", () => {
  const banner = getConnectionBannerState({
    reconnected: false,
    wsConnected: false,
    ibConnected: true,
    ibIssue: null,
    ibStatusMessage: null,
  });
  expect(banner).toEqual({
    tone: "warning",
    message:
      "Live data stream offline — IB Gateway is still connected; prices may be delayed.",
  });
});

it("does not warn when both WS and IB are connected", () => {
  expect(
    getConnectionBannerState({
      reconnected: false,
      wsConnected: true,
      ibConnected: true,
      ibIssue: null,
      ibStatusMessage: null,
    }),
  ).toBeNull();
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd web && npx vitest run tests/connection-banner-state.test.ts`
Expected: FAIL — current `getConnectionBannerState` returns `null` for the non-MFA path.

- [ ] **Step 3: Implement the banner case**

In `web/lib/ibConnectionAlert.ts`, inside `getConnectionBannerState`, after the existing `ibc_mfa_required` block and before `return null;`:

```ts
if (input.ibConnected && !input.wsConnected) {
  return {
    tone: "warning",
    message:
      "Live data stream offline — IB Gateway is still connected; prices may be delayed.",
  };
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd web && npx vitest run tests/connection-banner-state.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/lib/ibConnectionAlert.ts web/tests/connection-banner-state.test.ts
git commit -m "feat(web): distinguish 'data stream offline' from 'IB Gateway down' in connection banner"
```

---

## Task 6: Production deployment env fix (server-side WS host)

**Files:**

- Modify: `/opt/xenon/web.env` (deploy host — Mac mini)

> Repo `.env.example` files stay `ws://localhost:8765` (correct for local `npm run dev`). This is a **production-only** change. `IB_REALTIME_WS_URL` is the server-side path used by `resolveServerIbRealtimeWsUrl` (e.g. `web/app/api/previous-close/route.ts`); inside the container `localhost` is the web container itself, not the relay.

> **All Step commands here run on the deploy host (Mac mini), not the dev workspace.** Either ssh in or run them in a Tailscale terminal session on the Mac mini directly. The path `/opt/xenon/web.env` does not exist on the dev Mac.

> **Sequencing — atomic with the code deploy.** Do NOT do this AFTER the code deploy as a separate operation: recreating the web container without the new env would just restart old code, then a second recreate to pick up the env would cause a second downtime. Edit `/opt/xenon/web.env` FIRST (Steps 1-2 below), THEN run the standard image-pull + `docker-compose up -d web` for the new release — that single recreate picks up both the new image AND the new env value. If you've already deployed the new image without the env fix, run Steps 1-3 in this task immediately as a corrective.

- [ ] **Step 1: Confirm the current (wrong) value**

Run: `grep IB_REALTIME_WS_URL /opt/xenon/web.env`
Expected: `IB_REALTIME_WS_URL=ws://localhost:8765`

- [ ] **Step 2: Set it to the compose service name**

Edit `/opt/xenon/web.env`: change to `IB_REALTIME_WS_URL=ws://realtime:8765`.

- [ ] **Step 3: Recreate the web container to pick up the env**

Run: `docker-compose -f /opt/xenon/compose.yml -p xenon up -d web`
(Note: use the standalone `docker-compose` binary — the RTK hook mangles `docker compose -f`.)
Expected: `xenon-web-1` recreated, `Up … (healthy)`.

- [ ] **Step 4: Verify**

Run: `docker exec xenon-web-1 printenv IB_REALTIME_WS_URL`
Expected: `ws://realtime:8765`

- [ ] **Step 5: Confirm `NEXT_PUBLIC_IB_REALTIME_WS_URL` is NOT set (or is set to a Tailscale-reachable value)**

The browser-side resolver short-circuits to `NEXT_PUBLIC_IB_REALTIME_WS_URL` if set (see `web/lib/server/ibRealtimeRuntime.ts:62` and `web/lib/ibRealtimeWsClient.ts:17`). If this env is accidentally set to `ws://localhost:8765` or `ws://0.0.0.0:8765`, the entire header-derivation fix is bypassed and the bug returns.

Run: `docker exec xenon-web-1 printenv NEXT_PUBLIC_IB_REALTIME_WS_URL || echo UNSET`
Expected: `UNSET` (preferred — let the resolver compute per-request) — OR a Tailscale-reachable URL like `ws://macmini.tail20094b.ts.net:8765`. **NEVER** `ws://localhost:8765` or `ws://0.0.0.0:8765`.

If misconfigured: edit `/opt/xenon/web.env` to remove the line (or fix it), then re-run Step 3.

> No commit — this file is not in the repo.

---

## Task 7: Browser verification + full gates

**Files:** none (verification only)

- [ ] **Step 1: Run the full web unit suite**

Run: `cd web && npm test`
Expected: all green (includes the new tests from Tasks 1-5).

- [ ] **Step 2: Typecheck + lint**

Run: `cd web && npm run typecheck && npm run lint`
Expected: no errors.

- [ ] **Step 3: Browser-verify the badge shows live (chrome-cdp/Playwright) — from a Tailscale host**

The bug is **invisible from `localhost`** (where `0.0.0.0` resolves locally and the WS opens anyway). Verification MUST hit the deployed stack via its Tailscale hostname. With `xenon-web-1` + `xenon-api-1` + `xenon-realtime-1` up on the Mac mini, open `http://macmini.tail20094b.ts.net:3000` (substitute your tailnet hostname) from a different Tailscale device and confirm:

- The IB account tab status reads **live** (green), not down.
- DevTools → Network → WS shows an open connection to `ws://macmini.tail20094b.ts.net:8765` — **not** `ws://0.0.0.0:8765` and **not** `ws://localhost:8765`.

A pure-localhost smoke test passes pre-fix too, so it does **not** count as verification.

- [ ] **Step 4: Browser-verify the stream-offline distinction**

In DevTools, simulate the relay being unreachable (e.g. block `:8765` / kill `xenon-realtime-1` briefly with `docker stop xenon-realtime-1`). Confirm:

- The IB tab still reads **live** (sourced from `/api/health`), and
- The connection banner shows **"Live data stream offline — IB Gateway is still connected…"** (not "IB Gateway down").
- Restart: `docker-compose -f /opt/xenon/compose.yml -p xenon up -d realtime`.

- [ ] **Step 5: MANUAL post-deploy smoke test (cannot be automated)**

After deploying the new `xenon-web-1` image, open the app from a **remote Tailscale device** and confirm the IB badge reads live and the WS connects to `ws://<tailscale-host>:8765`. The tailnet cannot be reproduced in CI — this step must be done by hand and **must not be skipped or silently assumed**.

- [ ] **Step 6: Open the PR**

```bash
git push -u origin fix/ib-status-tailscale-ws-host
gh pr create --fill --base master
```

Let CI run; merge via PR per the repo's PR-first rule.

---

## Self-Review

- **Spec coverage:** Part 1 (header host) → Task 1. Part 2 (decouple) → Tasks 2-5 (probe/hook, usePrices, IBStatusContext, banner). Part 3 (deploy env) → Task 6. Test matrix → Tasks 1-5 unit + Task 7 browser/manual. All spec sections mapped.
- **Placeholders:** none — every code step shows full code; every run step shows command + expected result.
- **Type consistency:** `resolveBrowserIbRealtimeWsUrl` new params (`host`/`forwardedHost`/`forwardedProto`) used identically in Task 1 route + tests. `useIbHealthFallback(active)`/`fetchIbConnectedFromHealth()` defined in Task 2, consumed unchanged in Tasks 3-4. `effectiveIbConnected` is local to each consumer; the returned/Provider field name `ibConnected` is unchanged, so `WorkspaceShell`/`Sidebar`/`AccountTabBar`/`ConnectionBanner` consumers need no edits. Banner message string identical in Task 5 impl + test.
- **Default-asymmetry note (intentional):** `usePrices` defaults its WS-sourced `ibConnected` to `false` (line 94); `IBStatusContext` defaults to `true` (line 59, "assume connected until told otherwise"). Tasks 3/4 leave both defaults in place — the effective value is recomputed per-render from `connected ? wsValue : healthValue === true`, so the WS-sourced defaults are only visible for ~one-poll-latency (≤200ms) on first paint. Not a regression vs current behavior (which already shows red on initial-load before the first status message).
- **Test-suite drift:** Task 4's `ws.onclose` patch removes ONLY `setIbConnected(false)` — `setDisconnectedSince` and `prevConnectedRef` updates are preserved, so the existing `ib-status-context.test.ts:208-220` ("sets wsConnected false and disconnectedSince when WS drops") still passes unchanged. Codex Pass-2 flagged this as a near-miss; verified by re-reading the test.
- **Symbol-collision check:** the front-of-plan note distinguishes the two same-named `resolveBrowserIbRealtimeWsUrl` functions. Task 1 only touches `web/lib/server/ibRealtimeRuntime.ts`. The client wrapper in `web/lib/ibRealtimeWsClient.ts` and its callers (`usePrices`, `IBStatusContext`, `TickerSearch`) are untouched.
- **Imports drift-check:** Task 4 explicitly adds `waitFor` to the existing `@testing-library/react` import in `ib-status-context.test.ts` (the only `waitFor` the file needs that isn't already imported). No other test file needs new imports beyond what its task already creates.
- **Verification realism:** Task 7 Step 3 targets a Tailscale hostname (not `localhost`), because the bug is invisible from `localhost`. Task 1 Step 7 adds a route-level curl with a spoofed `Host` header that mirrors the spec's re-verified live evidence — a fix that doesn't change this curl's output hasn't fixed anything.
- **Non-goals respected:** no reverse proxy, no auth changes, no Futu changes.
