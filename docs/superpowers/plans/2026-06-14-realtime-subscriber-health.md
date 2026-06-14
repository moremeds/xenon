# Realtime Subscriber Health Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show per-subscriber connection health for the external WS price-stream clients on :8765 in the web health sidebar.

**Architecture:** The realtime server (Node, :8765) keeps an in-memory registry of identified subscribers (`?id=` in the WS URL) and exposes it via a loopback-only `GET /status`. FastAPI `/health` fetches that and adds a silent-degrading `realtime_subscribers` block. A dedicated web hook polls `/api/health` and feeds rows into `Sidebar.tsx`.

**Tech Stack:** Node (ESM) WebSocket server, FastAPI (Python 3.13, stdlib `urllib`), Next.js/React + Vitest, `node --test` not used (registry unit-tested via Vitest).

**Spec:** `docs/superpowers/specs/2026-06-14-realtime-subscriber-health-design.md`

---

## File Structure

- Create `scripts/infra/ib_realtime/subscriber_registry.js` — pure ESM registry (connect/pong/disconnect/snapshot + TTL prune). No socket/IB deps.
- Create `web/tests/subscriber-registry.test.ts` — Vitest unit tests importing the registry module.
- Modify `scripts/infra/ib_realtime/ib_realtime_server.js` — wire registry, parse `?id=`, add `GET /status`.
- Create `web/lib/subscriberHealth.ts` — types, `classifySubscriber`, `formatAge`, `DOT_CLASS`, `parseRealtimeSubscribers`.
- Create `web/tests/subscriber-health.test.ts` — Vitest unit tests for the pure helpers.
- Modify `src/xenon/api/server.py` — `_resolve_realtime_port`, `_fetch_realtime_status_json`, `_realtime_subscribers_health`, add block to `/health`.
- Create `src/xenon/api/tests/test_realtime_subscribers_health.py` — pytest.
- Create `web/lib/useSubscriberHealth.ts` — interval poll hook (thin; uses `parseRealtimeSubscribers`).
- Modify `web/app/globals.css` — add `.status-dot-stale`.
- Modify `web/components/Sidebar.tsx` — Subscribers section.
- Create `web/tests/sidebar-subscribers.test.tsx` — Vitest render tests.
- Modify `web/components/WorkspaceShell.tsx` — call hook, pass props to both `<Sidebar>` usages.

---

## Task 1: Subscriber registry module (pure logic)

**Files:**

- Create: `scripts/infra/ib_realtime/subscriber_registry.js`
- Test: `web/tests/subscriber-registry.test.ts`

- [ ] **Step 1: Write the failing test**

`web/tests/subscriber-registry.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import { createSubscriberRegistry } from "../../scripts/infra/ib_realtime/subscriber_registry.js";

describe("subscriberRegistry", () => {
  it("reports a connected subscriber with small last_pong age", () => {
    const r = createSubscriberRegistry({ ttlMs: 1000 });
    r.onConnect("alpha", 1000);
    const snap = r.snapshot(1200);
    expect(snap).toEqual([
      {
        id: "alpha",
        connected: true,
        connected_at_ms: 1000,
        last_pong_ms_ago: 200,
      },
    ]);
  });

  it("advances last-seen on pong", () => {
    const r = createSubscriberRegistry({ ttlMs: 10_000 });
    r.onConnect("alpha", 1000);
    r.onPong("alpha", 5000);
    expect(r.snapshot(5200)[0].last_pong_ms_ago).toBe(200);
  });

  it("keeps a disconnected subscriber as offline with offline_for_ms", () => {
    const r = createSubscriberRegistry({ ttlMs: 10_000 });
    r.onConnect("alpha", 1000);
    r.onDisconnect("alpha", 2000);
    const s = r.snapshot(5000)[0];
    expect(s.connected).toBe(false);
    expect(s.offline_for_ms).toBe(3000);
  });

  it("stays connected when one of two same-id connections drops", () => {
    const r = createSubscriberRegistry({ ttlMs: 10_000 });
    r.onConnect("alpha", 1000);
    r.onConnect("alpha", 1100);
    r.onDisconnect("alpha", 2000);
    expect(r.snapshot(2100)[0].connected).toBe(true);
  });

  it("prunes entries older than ttl", () => {
    const r = createSubscriberRegistry({ ttlMs: 1000 });
    r.onConnect("alpha", 1000);
    r.onDisconnect("alpha", 1000);
    expect(r.snapshot(2500)).toEqual([]);
  });

  it("sorts subscribers by id", () => {
    const r = createSubscriberRegistry({ ttlMs: 10_000 });
    r.onConnect("zeta", 1000);
    r.onConnect("alpha", 1000);
    expect(r.snapshot(1000).map((s) => s.id)).toEqual(["alpha", "zeta"]);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run ../web/tests/subscriber-registry.test.ts`
Expected: FAIL — `Failed to resolve import ".../subscriber_registry.js"`.

- [ ] **Step 3: Write minimal implementation**

`scripts/infra/ib_realtime/subscriber_registry.js`:

```js
// In-memory registry of identified WS subscribers, keyed by `id`.
// Pure (no socket/IB deps) so it is unit-testable. All times are epoch ms
// passed in by the caller — the module never reads the clock itself.

export function createSubscriberRegistry({ ttlMs = 900_000 } = {}) {
  // id -> { id, connectedAt, lastSeenAt, wsCount, connectedNow, disconnectedAt }
  const byId = new Map();

  function onConnect(id, nowMs) {
    let e = byId.get(id);
    if (!e) {
      e = {
        id,
        connectedAt: nowMs,
        lastSeenAt: nowMs,
        wsCount: 0,
        connectedNow: false,
        disconnectedAt: null,
      };
      byId.set(id, e);
    }
    e.wsCount += 1;
    e.connectedNow = true;
    e.lastSeenAt = nowMs;
    e.disconnectedAt = null;
  }

  function onPong(id, nowMs) {
    const e = byId.get(id);
    if (e) e.lastSeenAt = nowMs;
  }

  function onDisconnect(id, nowMs) {
    const e = byId.get(id);
    if (!e) return;
    e.wsCount = Math.max(0, e.wsCount - 1);
    e.lastSeenAt = nowMs;
    if (e.wsCount === 0) {
      e.connectedNow = false;
      e.disconnectedAt = nowMs;
    }
  }

  function snapshot(nowMs) {
    const subs = [];
    for (const e of [...byId.values()]) {
      if (nowMs - e.lastSeenAt > ttlMs) {
        byId.delete(e.id);
        continue;
      }
      if (e.connectedNow) {
        subs.push({
          id: e.id,
          connected: true,
          connected_at_ms: e.connectedAt,
          last_pong_ms_ago: nowMs - e.lastSeenAt,
        });
      } else {
        subs.push({
          id: e.id,
          connected: false,
          last_seen_ms_ago: nowMs - e.lastSeenAt,
          offline_for_ms:
            e.disconnectedAt == null ? null : nowMs - e.disconnectedAt,
        });
      }
    }
    subs.sort((a, b) => a.id.localeCompare(b.id));
    return subs;
  }

  return { onConnect, onPong, onDisconnect, snapshot };
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx vitest run ../web/tests/subscriber-registry.test.ts`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/infra/ib_realtime/subscriber_registry.js web/tests/subscriber-registry.test.ts
git commit -m "feat(realtime): subscriber registry module + tests"
```

---

## Task 2: Wire registry + `GET /status` into the realtime server

**Files:**

- Modify: `scripts/infra/ib_realtime/ib_realtime_server.js`

> No unit harness exists for the monolithic server (the existing `test_ib_realtime.py` is a live-server integration script). The registry logic is covered by Task 1; this task is glue, verified by a manual integration check (Step 5).

- [ ] **Step 1: Import the registry and declare state**

Near the other connection maps (just after `const clientLastPong = new Map();`, ~line 500), add:

```js
import { createSubscriberRegistry } from "./subscriber_registry.js";

const SUBSCRIBER_TTL_MS =
  Number(process.env.IB_REALTIME_SUBSCRIBER_TTL_MS) || 900_000;
const subscriberRegistry = createSubscriberRegistry({
  ttlMs: SUBSCRIBER_TTL_MS,
});
const clientId = new Map(); // ws -> id (only for identified subscribers)
```

> Move the `import` to the top of the file with the other imports (ESM imports must be top-level); the three `const`s stay next to the maps.

- [ ] **Step 2: Add the `GET /status` route**

Replace the `http.createServer((_req, res) => {...})` handler (~line 352) with:

```js
const httpServer = http.createServer((req, res) => {
  if (
    req.method === "GET" &&
    (req.url === "/status" || req.url.startsWith("/status?"))
  ) {
    const remote = req.socket.remoteAddress || "";
    const isLoopback =
      remote === "127.0.0.1" ||
      remote === "::1" ||
      remote === "::ffff:127.0.0.1";
    if (!isLoopback) {
      res.writeHead(403, { "Content-Type": "text/plain" });
      res.end("Forbidden");
      return;
    }
    const now = Date.now();
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        ib_connected: ibConnected,
        now_ms: now,
        ttl_ms: SUBSCRIBER_TTL_MS,
        subscribers: subscriberRegistry.snapshot(now),
        anonymous_count: Math.max(0, clients.size - clientId.size),
      }),
    );
    return;
  }
  res.writeHead(426, { "Content-Type": "text/plain" });
  res.end("WebSocket upgrade required");
});
```

> `ibConnected`, `clients`, `clientId`, `subscriberRegistry`, `SUBSCRIBER_TTL_MS` are module-level and initialized before any request arrives, so the closure resolves them at request time.

- [ ] **Step 3: Capture `?id=` on connect, update registry on pong/close**

Change the connection handler signature and body (~line 1475) to accept `req` and register identity:

```js
wss.on("connection", (client, req) => {
  clients.add(client);
  clientLastPong.set(client, Date.now());

  let subId = null;
  try {
    subId = new URL(
      req.url || "/",
      `http://${req.headers.host || "localhost"}`,
    ).searchParams.get("id");
  } catch {
    subId = null;
  }
  if (subId) {
    clientId.set(client, subId);
    subscriberRegistry.onConnect(subId, Date.now());
  }

  verbose(
    `WS client connected (total: ${clients.size}${subId ? `, id=${subId}` : ""})`,
  );
  sendStatus(client);
  // ... existing message handler unchanged ...

  client.on("close", () => {
    verbose(`WS client disconnected (remaining: ${clients.size - 1})`);
    const sid = clientId.get(client);
    if (sid) {
      subscriberRegistry.onDisconnect(sid, Date.now());
      clientId.delete(client);
    }
    disconnectClient(client);
    clients.delete(client);
  });

  client.on("error", () => {
    const sid = clientId.get(client);
    if (sid) {
      subscriberRegistry.onDisconnect(sid, Date.now());
      clientId.delete(client);
    }
    disconnectClient(client);
    clients.delete(client);
  });
});
```

In the `pong` case of `handleClientMessage` (~line 1176, right after `clientLastPong.set(client, Date.now())`), add:

```js
const sid = clientId.get(client);
if (sid) subscriberRegistry.onPong(sid, Date.now());
```

- [ ] **Step 4: Lint / typecheck**

Run: `cd web && npm run lint`
Expected: PASS (no new errors in the realtime server file).

- [ ] **Step 5: Manual integration verification**

```bash
# Terminal A: start the realtime server (from web/, or directly)
node scripts/infra/ib_realtime/ib_realtime_server.js --port 8765 &
# Terminal B:
# anonymous connection raises anonymous_count; identified raises subscribers
npx wscat -c 'ws://127.0.0.1:8765/?id=hedge-bot' &   # or any WS client
curl -s http://127.0.0.1:8765/status | python3 -m json.tool
# Expect: subscribers[0].id == "hedge-bot", connected true; kill the client → connected false
# Expect: 403 from a non-loopback address (skip if no second host handy)
```

- [ ] **Step 6: Commit**

```bash
git add scripts/infra/ib_realtime/ib_realtime_server.js
git commit -m "feat(realtime): track ?id= subscribers + loopback GET /status"
```

---

## Task 3: Web pure helpers (classify / format / parse)

**Files:**

- Create: `web/lib/subscriberHealth.ts`
- Test: `web/tests/subscriber-health.test.ts`

- [ ] **Step 1: Write the failing test**

`web/tests/subscriber-health.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import {
  classifySubscriber,
  formatAge,
  parseRealtimeSubscribers,
  DOT_CLASS,
} from "../lib/subscriberHealth";

describe("classifySubscriber", () => {
  it("live under 35s", () => {
    expect(
      classifySubscriber({ id: "a", connected: true, lastPongMsAgo: 3000 }),
    ).toBe("live");
  });
  it("stale between 35s and 65s", () => {
    expect(
      classifySubscriber({ id: "a", connected: true, lastPongMsAgo: 40_000 }),
    ).toBe("stale");
  });
  it("offline when not connected", () => {
    expect(
      classifySubscriber({ id: "a", connected: false, offlineForMs: 1000 }),
    ).toBe("offline");
  });
});

describe("formatAge", () => {
  it("seconds, minutes, hours", () => {
    expect(formatAge(3000)).toBe("3s");
    expect(formatAge(120_000)).toBe("2m");
    expect(formatAge(7_200_000)).toBe("2h");
    expect(formatAge(undefined)).toBe("—");
  });
});

describe("parseRealtimeSubscribers", () => {
  it("maps the /api/health block to camelCase", () => {
    const out = parseRealtimeSubscribers({
      realtime_subscribers: {
        reachable: true,
        anonymous_count: 2,
        subscribers: [
          { id: "a", connected: true, last_pong_ms_ago: 3000 },
          {
            id: "b",
            connected: false,
            offline_for_ms: 9000,
            last_seen_ms_ago: 9000,
          },
        ],
      },
    });
    expect(out.reachable).toBe(true);
    expect(out.anonymousCount).toBe(2);
    expect(out.subscribers[0]).toEqual({
      id: "a",
      connected: true,
      lastPongMsAgo: 3000,
    });
    expect(out.subscribers[1]).toEqual({
      id: "b",
      connected: false,
      offlineForMs: 9000,
      lastSeenMsAgo: 9000,
    });
  });
  it("returns an unreachable empty shape when the block is missing", () => {
    expect(parseRealtimeSubscribers({})).toEqual({
      reachable: false,
      subscribers: [],
      anonymousCount: 0,
    });
  });
});

describe("DOT_CLASS", () => {
  it("maps liveness to dot classes", () => {
    expect(DOT_CLASS.live).toBe("status-dot-live");
    expect(DOT_CLASS.stale).toBe("status-dot-stale");
    expect(DOT_CLASS.offline).toBe("status-dot-dead");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run ../web/tests/subscriber-health.test.ts`
Expected: FAIL — cannot resolve `../lib/subscriberHealth`.

- [ ] **Step 3: Write minimal implementation**

`web/lib/subscriberHealth.ts`:

```ts
export type SubscriberHealth = {
  id: string;
  connected: boolean;
  lastPongMsAgo?: number;
  offlineForMs?: number;
  lastSeenMsAgo?: number;
};

export type RealtimeSubscribers = {
  reachable: boolean;
  subscribers: SubscriberHealth[];
  anonymousCount: number;
};

export type SubscriberLiveness = "live" | "stale" | "offline";

const LIVE_MAX_MS = 35_000;

export function classifySubscriber(s: SubscriberHealth): SubscriberLiveness {
  if (!s.connected) return "offline";
  return (s.lastPongMsAgo ?? 0) < LIVE_MAX_MS ? "live" : "stale";
}

export function formatAge(ms: number | undefined): string {
  if (ms == null) return "—";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m`;
  return `${Math.round(m / 60)}h`;
}

export const DOT_CLASS: Record<SubscriberLiveness, string> = {
  live: "status-dot-live",
  stale: "status-dot-stale",
  offline: "status-dot-dead",
};

type RawRow = {
  id: string;
  connected: boolean;
  last_pong_ms_ago?: number;
  offline_for_ms?: number | null;
  last_seen_ms_ago?: number;
};
type RawBlock = {
  reachable?: boolean;
  subscribers?: RawRow[];
  anonymous_count?: number;
};

export function parseRealtimeSubscribers(
  health: { realtime_subscribers?: RawBlock } | null | undefined,
): RealtimeSubscribers {
  const block = health?.realtime_subscribers;
  if (!block) return { reachable: false, subscribers: [], anonymousCount: 0 };
  return {
    reachable: Boolean(block.reachable),
    anonymousCount: block.anonymous_count ?? 0,
    subscribers: (block.subscribers ?? []).map((r) => {
      const row: SubscriberHealth = { id: r.id, connected: r.connected };
      if (r.last_pong_ms_ago != null) row.lastPongMsAgo = r.last_pong_ms_ago;
      if (r.offline_for_ms != null) row.offlineForMs = r.offline_for_ms;
      if (r.last_seen_ms_ago != null) row.lastSeenMsAgo = r.last_seen_ms_ago;
      return row;
    }),
  };
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx vitest run ../web/tests/subscriber-health.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/lib/subscriberHealth.ts web/tests/subscriber-health.test.ts
git commit -m "feat(web): subscriber health classify/format/parse helpers + tests"
```

---

## Task 4: FastAPI `/health` block

**Files:**

- Modify: `src/xenon/api/server.py` (add helpers near `_snapshotter_health`; add key in `health()`)
- Test: `src/xenon/api/tests/test_realtime_subscribers_health.py`

- [ ] **Step 1: Write the failing test**

`src/xenon/api/tests/test_realtime_subscribers_health.py`:

```python
from xenon.api import server


def test_reachable_passthrough(monkeypatch):
    payload = {
        "ib_connected": True,
        "subscribers": [{"id": "alpha", "connected": True, "last_pong_ms_ago": 1000}],
        "anonymous_count": 2,
        "ttl_ms": 900000,
    }
    monkeypatch.setattr(server, "_fetch_realtime_status_json", lambda port, timeout=0.5: payload)
    monkeypatch.setattr(server, "_resolve_realtime_port", lambda: 8765)

    out = server._realtime_subscribers_health()
    assert out["reachable"] is True
    assert out["subscribers"] == payload["subscribers"]
    assert out["anonymous_count"] == 2


def test_silent_degrade_when_unreachable(monkeypatch):
    def boom(port, timeout=0.5):
        raise OSError("connection refused")

    monkeypatch.setattr(server, "_fetch_realtime_status_json", boom)
    monkeypatch.setattr(server, "_resolve_realtime_port", lambda: 8765)

    out = server._realtime_subscribers_health()
    assert out == {"reachable": False, "subscribers": [], "anonymous_count": 0}


def test_resolve_realtime_port_falls_back_to_default(monkeypatch, tmp_path):
    monkeypatch.setenv("IB_REALTIME_RUNTIME_FILE", str(tmp_path / "absent.json"))
    assert server._resolve_realtime_port() == 8765
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest src/xenon/api/tests/test_realtime_subscribers_health.py -v`
Expected: FAIL — `AttributeError: module 'xenon.api.server' has no attribute '_resolve_realtime_port'`.

- [ ] **Step 3: Write minimal implementation**

Add near `_snapshotter_health` in `src/xenon/api/server.py` (ensure `import asyncio` exists at top of file — add it if missing):

```python
def _resolve_realtime_port() -> int:
    """Resolve the IB realtime WS port from the runtime file, else 8765.

    Mirror of web/lib/server/ibRealtimeRuntime.ts: IB_REALTIME_RUNTIME_FILE env,
    else <tmpdir>/xenon-ib-realtime.json; fall back to 8765 when absent/invalid.
    """
    import os
    import tempfile
    from pathlib import Path

    runtime_file = os.environ.get("IB_REALTIME_RUNTIME_FILE") or str(
        Path(tempfile.gettempdir()) / "xenon-ib-realtime.json"
    )
    try:
        data = json.loads(Path(runtime_file).read_text())
        port = int(data.get("port"))
        if port > 0:
            return port
    except Exception:
        pass
    return 8765


def _fetch_realtime_status_json(port: int, timeout: float = 0.5) -> dict:
    import urllib.request

    url = f"http://127.0.0.1:{port}/status"
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _realtime_subscribers_health() -> dict:
    """Realtime WS subscriber health, silent-degrade when the server is down."""
    try:
        payload = _fetch_realtime_status_json(_resolve_realtime_port())
    except Exception:
        logger.warning("[health] realtime /status unreachable", exc_info=True)
        return {"reachable": False, "subscribers": [], "anonymous_count": 0}
    return {
        "reachable": True,
        "ib_connected": payload.get("ib_connected"),
        "subscribers": payload.get("subscribers", []),
        "anonymous_count": payload.get("anonymous_count", 0),
        "ttl_ms": payload.get("ttl_ms"),
    }
```

Then add to the `health()` return dict (after `"flex_divergence": _flex_divergence_health(),`):

```python
        "realtime_subscribers": await asyncio.to_thread(_realtime_subscribers_health),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest src/xenon/api/tests/test_realtime_subscribers_health.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/xenon/api/server.py src/xenon/api/tests/test_realtime_subscribers_health.py
git commit -m "feat(api): realtime_subscribers block in /health (silent-degrade)"
```

---

## Task 5: `useSubscriberHealth` poll hook

**Files:**

- Create: `web/lib/useSubscriberHealth.ts`

> The parse logic is already covered by Task 3 (`parseRealtimeSubscribers`); this hook is thin glue (fetch on an interval + setState), so no separate hook test — it composes a tested pure function.

- [ ] **Step 1: Write the implementation**

`web/lib/useSubscriberHealth.ts`:

```ts
"use client";

import { useEffect, useState } from "react";
import {
  parseRealtimeSubscribers,
  type RealtimeSubscribers,
} from "./subscriberHealth";

const EMPTY: RealtimeSubscribers = {
  reachable: false,
  subscribers: [],
  anonymousCount: 0,
};

export function useSubscriberHealth(intervalMs = 10_000): RealtimeSubscribers {
  const [state, setState] = useState<RealtimeSubscribers>(EMPTY);

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        const res = await fetch("/api/health", { cache: "no-store" });
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled) setState(parseRealtimeSubscribers(data));
      } catch {
        // Silent: backend may be down during frontend-only dev sessions.
      }
    }
    poll();
    const timer = setInterval(poll, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [intervalMs]);

  return state;
}
```

- [ ] **Step 2: Typecheck**

Run: `cd web && npm run typecheck`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add web/lib/useSubscriberHealth.ts
git commit -m "feat(web): useSubscriberHealth poll hook"
```

---

## Task 6: Amber stale dot token

**Files:**

- Modify: `web/app/globals.css` (after the `.status-dot-dead` block, ~line 287)

- [ ] **Step 1: Add the CSS**

```css
.status-dot-stale {
  background: var(--warning);
  animation: none;
}
```

- [ ] **Step 2: Verify lint**

Run: `cd web && npm run lint`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add web/app/globals.css
git commit -m "style(web): status-dot-stale amber token"
```

---

## Task 7: Sidebar Subscribers section

**Files:**

- Modify: `web/components/Sidebar.tsx`
- Test: `web/tests/sidebar-subscribers.test.tsx`

- [ ] **Step 1: Write the failing test**

`web/tests/sidebar-subscribers.test.tsx`:

```tsx
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import Sidebar from "../components/Sidebar";

const base = { activeSection: "portfolio" as const, actionTone: "#fff" };

describe("Sidebar subscribers", () => {
  it("renders a live subscriber row with its id and age", () => {
    render(
      <Sidebar
        {...base}
        subscribersReachable
        subscribers={[
          { id: "hedge-bot", connected: true, lastPongMsAgo: 3000 },
        ]}
        anonymousCount={0}
      />,
    );
    expect(screen.getByText("hedge-bot")).toBeInTheDocument();
    expect(screen.getByText("3s")).toBeInTheDocument();
  });

  it("renders an offline subscriber row", () => {
    render(
      <Sidebar
        {...base}
        subscribersReachable
        subscribers={[
          { id: "scalper", connected: false, offlineForMs: 120_000 },
        ]}
        anonymousCount={0}
      />,
    );
    expect(screen.getByText("offline 2m")).toBeInTheDocument();
  });

  it("shows stream offline when unreachable", () => {
    render(
      <Sidebar
        {...base}
        subscribersReachable={false}
        subscribers={[]}
        anonymousCount={0}
      />,
    );
    expect(screen.getByText("stream offline")).toBeInTheDocument();
  });

  it("shows none when reachable with no subscribers", () => {
    render(
      <Sidebar
        {...base}
        subscribersReachable
        subscribers={[]}
        anonymousCount={0}
      />,
    );
    expect(screen.getByText("none")).toBeInTheDocument();
  });

  it("shows the anonymous app-client count", () => {
    render(
      <Sidebar
        {...base}
        subscribersReachable
        subscribers={[]}
        anonymousCount={2}
      />,
    );
    expect(screen.getByText("+2 app clients")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run ../web/tests/sidebar-subscribers.test.tsx`
Expected: FAIL — Sidebar does not accept `subscribers` / renders nothing matching.

- [ ] **Step 3: Implement the section**

In `web/components/Sidebar.tsx`, extend the imports and props, then add the section at the end of `sidebar-footer`.

Add import:

```tsx
import {
  classifySubscriber,
  formatAge,
  DOT_CLASS,
  type SubscriberHealth,
} from "@/lib/subscriberHealth";
```

Extend `SidebarProps`:

```tsx
type SidebarProps = {
  activeSection: WorkspaceSection;
  actionTone: string;
  ibConnected?: boolean;
  lastSync?: string | null;
  subscribers?: SubscriberHealth[];
  subscribersReachable?: boolean;
  anonymousCount?: number;
};
```

Extend the destructure with defaults:

```tsx
export default function Sidebar({
  activeSection,
  actionTone,
  ibConnected = true,
  lastSync,
  subscribers = [],
  subscribersReachable = false,
  anonymousCount = 0,
}: SidebarProps) {
```

Insert this block immediately before the closing `</div>` of `sidebar-footer` (after the Version `status-row`):

```tsx
<div className="status-row status-row-header">
  <span>Subscribers</span>
  <span />
</div>;
{
  !subscribersReachable ? (
    <div className="status-row">
      <span className="status-muted">stream offline</span>
    </div>
  ) : subscribers.length === 0 ? (
    <div className="status-row">
      <span className="status-muted">none</span>
    </div>
  ) : (
    subscribers.map((s) => {
      const liveness = classifySubscriber(s);
      const age =
        liveness === "offline"
          ? `offline ${formatAge(s.offlineForMs)}`
          : formatAge(s.lastPongMsAgo);
      return (
        <div className="status-row" key={s.id}>
          <span className="status-sub-id">{s.id}</span>
          <span className="status-dot-wrap">
            <span className={`status-dot ${DOT_CLASS[liveness]}`} />
            {age}
          </span>
        </div>
      );
    })
  );
}
{
  anonymousCount > 0 && (
    <div className="status-row">
      <span className="status-muted">+{anonymousCount} app clients</span>
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx vitest run ../web/tests/sidebar-subscribers.test.tsx`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add web/components/Sidebar.tsx web/tests/sidebar-subscribers.test.tsx
git commit -m "feat(web): subscriber rows in health sidebar + tests"
```

---

## Task 8: Wire the hook through WorkspaceShell + browser verification

**Files:**

- Modify: `web/components/WorkspaceShell.tsx` (both `<Sidebar>` usages, ~lines 396 and 430)

- [ ] **Step 1: Call the hook and pass props**

Add the import:

```tsx
import { useSubscriberHealth } from "@/lib/useSubscriberHealth";
```

Inside the component body (near the existing `ibConnected` derivation, ~line 229):

```tsx
const subscriberHealth = useSubscriberHealth();
```

On **both** `<Sidebar ... />` usages, add:

```tsx
        subscribers={subscriberHealth.subscribers}
        subscribersReachable={subscriberHealth.reachable}
        anonymousCount={subscriberHealth.anonymousCount}
```

- [ ] **Step 2: Typecheck + unit suite**

Run: `cd web && npm run typecheck && npx vitest run ../web/tests/subscriber-registry.test.ts ../web/tests/subscriber-health.test.ts ../web/tests/sidebar-subscribers.test.tsx`
Expected: PASS.

- [ ] **Step 3: Browser verification (MANDATORY per root CLAUDE.md §2)**

```bash
scripts/infra/dev.sh paper   # brings up next + realtime + FastAPI
```

Then with `chrome-cdp` (or Playwright fallback):

1. Open `http://localhost:3000`, open the health sidebar.
2. Connect a test subscriber: `npx wscat -c 'ws://127.0.0.1:8765/?id=hedge-bot'`.
3. Verify a **Subscribers** section shows `hedge-bot` with a green dot + age within ~10s (poll interval).
4. Kill the subscriber → within the poll interval the row flips to a red `offline <age>` dot (and ages out after the TTL).
5. Confirm `+N app clients` reflects the browser's own WS connection(s).
   Capture a screenshot as evidence.

- [ ] **Step 4: Full affected suites**

Run: `uv run python scripts/infra/dev/run_pytest_affected.py` and `cd web && npm test`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/components/WorkspaceShell.tsx
git commit -m "feat(web): wire subscriber health into the sidebar"
```

---

## Done-when

- `GET 127.0.0.1:8765/status` returns identified subscribers + `anonymous_count`; non-loopback → 403.
- `/health.realtime_subscribers` reflects it and silent-degrades to `reachable:false` when the realtime server is down.
- Sidebar shows per-subscriber rows (live/stale/offline dot + age), `none`/`stream offline` empty states, and `+N app clients`; verified in-browser.
- All unit suites green (registry, helpers, FastAPI, Sidebar); `npm run typecheck` + `npm run lint` clean.

## Non-goals (carried from spec)

No durable cross-restart history, no expected-count alerting, no new web poll beyond `useSubscriberHealth`, `/status` localhost-only, `remote` IP not surfaced in `/health`.
