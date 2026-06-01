# IB Status Down Over Tailscale — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the IB connection badge read correctly from any host (Mac mini _and_ remote Tailscale device) by fixing the realtime-WS URL host resolution, and stop a dropped realtime stream from being shown as "IB Gateway down."

**Architecture:** `/api/ib/ws-config` currently derives the WS host from `request.nextUrl`, which under `HOSTNAME=0.0.0.0` returns `ws://0.0.0.0:8765` to every client — unreachable from a remote browser. We switch it to derive the host from the request's `Host`/`X-Forwarded-Host` header (scheme from `X-Forwarded-Proto`). Separately, the IB-connected signal — currently sourced only from realtime-WS `status` messages — gains a `/api/health` fallback so it reflects the real IB Gateway state when the stream is down.

**Tech Stack:** Next.js App Router (route handlers), React hooks, TypeScript, Vitest (unit), Playwright/chrome-cdp (browser). Deployment: Docker Compose on a Mac mini (`/opt/xenon/compose.yml`).

**Spec:** `docs/superpowers/specs/2026-06-01-ib-status-tailscale-design.md`

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
  if (h.startsWith("[")) {
    const end = h.indexOf("]");
    return end === -1 ? h : h.slice(0, end + 1); // keep [::1]
  }
  const colon = h.indexOf(":");
  return colon === -1 ? h : h.slice(0, colon);
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

- [ ] **Step 7: Commit**

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
      return Boolean(
        pool.sync?.connected || pool.orders?.connected || pool.data?.connected,
      );
    }
    return null;
  } catch {
    return null;
  }
}

/**
 * While `active`, poll /api/health for the IB-connected state.
 * Returns the last reading (null = unknown / not yet polled / health unreachable).
 */
export function useIbHealthFallback(
  active: boolean,
  intervalMs = 15_000,
): boolean | null {
  const [ibConnected, setIbConnected] = useState<boolean | null>(null);

  useEffect(() => {
    if (!active) {
      setIbConnected(null);
      return;
    }
    let cancelled = false;
    const controller = new AbortController();
    const poll = async () => {
      const reading = await fetchIbConnectedFromHealth(controller.signal);
      if (!cancelled) setIbConnected(reading);
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
const healthIbConnected = useIbHealthFallback(!connected);
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
  const { result } = renderHook(() => useIBStatusContext(), {
    wrapper: IBStatusProvider,
  });
  const ws = latestWs();
  await act(async () => {
    ws.readyState = MockWebSocket.CLOSED;
    ws.onclose?.();
  });
  await waitFor(() => expect(result.current.wsConnected).toBe(false));
  await waitFor(() => expect(result.current.ibConnected).toBe(true));
});
```

(Imports at top of the test file: ensure `useIBStatusContext`, `IBStatusProvider`, `act`, `waitFor`, `renderHook`, `vi` are imported — add any missing.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd web && npx vitest run tests/ib-status-context.test.ts`
Expected: FAIL — on close the provider currently force-sets `ibConnected=false`.

- [ ] **Step 3: Implement — remove force-false, add health fallback**

In `web/lib/IBStatusContext.tsx`, import the hook (next to line 14):

```ts
import { useIbHealthFallback } from "./ibHealthFallback";
```

In `ws.onclose` (lines ~169-188), delete the force-false block so it only clears the WS flag:

```ts
ws.onclose = () => {
  if (!mountedRef.current) return;
  setWsConnected(false);
  clearStalenessTimer();

  // Schedule reconnect with backoff
  if (strategyRef.current.canRetry()) {
    const delay = strategyRef.current.nextDelay();
    reconnectTimerRef.current = setTimeout(() => {
      if (mountedRef.current) connect();
    }, delay);
  }
};
```

(Removed: the `if (prevConnectedRef.current !== false) { setIbConnected(false); setDisconnectedSince(...); prevConnectedRef.current = false; }` block.)

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

- [ ] **Step 3: Browser-verify the badge shows live (chrome-cdp/Playwright)**

With the stack up (`xenon-web-1` + `xenon-api-1` + `xenon-realtime-1`), open `http://localhost:3000`, sign in if needed, and confirm:

- The IB account tab status reads **live** (green), not down.
- DevTools → Network → WS shows an open connection to `ws://localhost:8765` (no `0.0.0.0`).

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
- **Non-goals respected:** no reverse proxy, no auth changes, no Futu changes.
