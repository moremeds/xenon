# Realtime subscriber connection health in the health bar — design

- **Date:** 2026-06-14
- **Status:** Design (pending implementation plan)
- **Owner:** chenxi

## Problem

The user runs two external WebSocket subscribers that connect to the IB realtime
price-stream server (`scripts/infra/ib_realtime/ib_realtime_server.js`, default
port 8765) to consume quotes. There is currently no visibility into whether those
subscribers are connected and healthy. The health sidebar (`web/components/Sidebar.tsx`)
should show a per-subscriber connection-status indicator.

## Goal

Add a **Subscribers** section to the health sidebar that lists each identified
subscriber with a liveness dot and last-seen age, keeps a recently-disconnected
subscriber visible as an "offline" row for a retention window, and shows a muted
count of anonymous app-tab connections.

## Decisions (from brainstorming)

| #   | Decision                 | Choice                                                                                               |
| --- | ------------------------ | ---------------------------------------------------------------------------------------------------- |
| 1   | What are the subscribers | WS clients on :8765 (anonymous price-stream consumers)                                               |
| 2   | Detail level             | Per-subscriber rows                                                                                  |
| 3   | Identity                 | Each subscriber passes `?id=<name>` in its WS URL                                                    |
| 4   | Data path                | Aggregate into FastAPI `/health` (Approach A)                                                        |
| 5   | Dropped subscriber       | Keep as an "offline" row, aged out after a TTL                                                       |
| 6   | Row fields               | `id · dot · last-seen` (no symbol count)                                                             |
| 7   | Anonymous app tabs       | Show muted `+N app clients`                                                                          |
| 8   | Offline retention TTL    | **15 min** since last-seen, env-overridable (`IB_REALTIME_SUBSCRIBER_TTL_MS`) — _flagged for review_ |

## Architecture / data path (Approach A)

```
subscriber ──ws?id=hedge-bot──▶ realtime server (:8765)
                                  │  subscriberRegistry (in-memory, keyed by id)
                                  │  GET /status  (loopback-only JSON)
                                  ▼
FastAPI /health ──asyncio.to_thread(GET 127.0.0.1:<port>/status, 0.5s)──▶ "realtime_subscribers" block (silent-degrade)
                                  ▼
web /api/health (existing proxy) ──▶ IBStatusContext (existing poll) ──▶ WorkspaceShell ──▶ Sidebar rows
```

Rationale: `/health` already aggregates `futu`/`snapshotter`/`order_submissions`
blocks, each silent-degrading when its source is down, and the web already polls
`/api/health` via `IBStatusContext`. Adding one block means **no new poll loop and
one source of truth**. The only new infra is a small `/status` JSON on the realtime
server's existing HTTP server.

## Component 1 — Realtime server (`ib_realtime_server.js`)

### Identity capture

In the existing `wss.on("connection", (ws, req) => …)` handler (where
`clientLastPong` / `clientSymbols` are already initialized):

- Parse `req.url` for the `?id=` query param.
- Clients **with** an `id` are tracked subscribers; clients **without** are anonymous
  (the web app's own tabs open a WS via `usePrices`). Anonymous connections need no
  per-connection metadata — they are counted as `clients.size` minus the number of
  currently-connected id-bearing connections.

Note: localhost connections currently skip URL parsing in `on("upgrade")` (they
bypass ticket validation). `?id=` is therefore read in the `connection` handler from
`req.url`, which is available for all connections regardless of the upgrade path.

### `subscriberRegistry` (in-memory, keyed by id)

```
subscriberRegistry: Map<id, {
  id, connectedAt, lastSeenAt, wsCount, connectedNow, disconnectedAt
}>
```

- **On connect (id present):** upsert; `wsCount += 1`; `connectedNow = true`; set
  `connectedAt` if first; `lastSeenAt = now`.
- **On pong** (existing pong handler): if the ws has an id, `lastSeenAt = now`.
- **On disconnect** (existing close handler): `wsCount -= 1`; if `wsCount === 0`,
  `connectedNow = false`, `disconnectedAt = now`. Entry is retained.
- **Aged out:** entries with `now - lastSeenAt > TTL` are excluded on read (and may
  be pruned). TTL = `IB_REALTIME_SUBSCRIBER_TTL_MS` (default 900_000 ms = 15 min).
- Anonymous (no-id) connections are **not** registry entries; they are counted live
  from `clients` minus id-bearing connections.

### `GET /status` (new, on the existing `http.createServer` handler)

The current request handler responds `426` to everything. Add:

- If `method === "GET"` and path is `/status`:
  - If `req.socket.remoteAddress` is loopback (`127.0.0.1`/`::1`/`::ffff:127.0.0.1`)
    → `200` JSON.
  - Else → `403`.
- Else → `426` (unchanged).

WS upgrades arrive via the separate `on("upgrade")` event, so adding a `/status` GET
response does not affect them.

**Payload (all ages are server-relative ms to avoid clock skew):**

```json
{
  "ib_connected": true,
  "now_ms": 1718370000000,
  "ttl_ms": 900000,
  "subscribers": [
    {
      "id": "hedge-bot",
      "connected": true,
      "connected_at_ms": 1718369000000,
      "last_pong_ms_ago": 3200
    },
    {
      "id": "scalper",
      "connected": false,
      "last_seen_ms_ago": 42000,
      "offline_for_ms": 42000
    }
  ],
  "anonymous_count": 1
}
```

## Component 2 — FastAPI `/health` (`src/xenon/api/server.py`)

New sync helper `_realtime_subscribers_health()` modeled on `_snapshotter_health`
(try/except → silent-degrade):

- Resolve the realtime port from the runtime file used by the web layer:
  `IB_REALTIME_RUNTIME_FILE` env, else `<tmpdir>/xenon-ib-realtime.json`; fall back to
  port `8765` if the file is absent/invalid. (Mirror of `ibRealtimeRuntime.ts`.)
- `GET http://127.0.0.1:<port>/status` with a **0.5s** timeout (stdlib `urllib`).
- Success → selected fields from the payload:
  `{ "reachable": true, "ib_connected", "subscribers", "anonymous_count", "ttl_ms" }`
  (`now_ms` is dropped — row ages are already server-relative).
- Failure/timeout/down → `{ "reachable": false, "subscribers": [], "anonymous_count": 0 }`.

In the `async def health()` body, call it via `await asyncio.to_thread(...)` so the
localhost network hop never blocks the event loop, and add:

```python
"realtime_subscribers": await asyncio.to_thread(_realtime_subscribers_health),
```

`/health` is auth-exempt; the payload exposes only subscriber `id`s the user chose,
no secrets. `remote` IPs are intentionally **not** forwarded to the public block.

## Component 3 — Web

### `useSubscriberHealth` hook (`web/lib/useSubscriberHealth.ts`, new)

`IBStatusContext` is WS-driven and only fetches `/api/health` as a fallback (via
`useIbHealthFallback(!wsConnected)`), so it is not a continuous poll. Instead add a
dedicated hook that polls `/api/health` on an interval (mirroring `useTradingMode`,
which independently fetches `/api/health`) and returns the parsed block:

```ts
type SubscriberHealth = {
  id: string;
  connected: boolean;
  lastPongMsAgo?: number;
  offlineForMs?: number;
  lastSeenMsAgo?: number;
};
type RealtimeSubscribers = {
  reachable: boolean;
  subscribers: SubscriberHealth[];
  anonymousCount: number;
};

function useSubscriberHealth(intervalMs = 10_000): RealtimeSubscribers;
// → { reachable:false, subscribers:[], anonymousCount:0 } until first successful poll
```

Liveness classification lives in a pure helper `web/lib/subscriberHealth.ts`
(`classifySubscriber(sub): "live" | "stale" | "offline"`) so it is unit-testable and
shared by the Sidebar.

### `WorkspaceShell` (`web/components/WorkspaceShell.tsx`)

Call `useSubscriberHealth()`; pass `<Sidebar subscribers={…} subscribersReachable={…}
anonymousCount={…} />` alongside the existing `ibConnected`.

### `Sidebar.tsx`

New **Subscribers** section under the existing status rows:

- `subscribersReachable === false` → single muted row: `Subscribers — stream offline`.
- Reachable, no identified subscribers → muted `Subscribers — none`.
- Otherwise one `status-row` per subscriber: `id` (mono) · dot · right-aligned
  last-seen age (`3s`, or `offline 2m` when disconnected).
- Trailing muted line when `anonymous_count > 0`: `+N app clients`.

### Liveness → dot color (shared thresholds; server pings 30s, drops at 65s)

| State   | Condition                                         | Dot                                            |
| ------- | ------------------------------------------------- | ---------------------------------------------- |
| live    | `connected && last_pong_ms_ago < 35_000`          | green (`status-dot-live`)                      |
| stale   | `connected && 35_000 ≤ last_pong_ms_ago < 65_000` | amber (`status-dot-stale`, **new token**)      |
| offline | `!connected` (within TTL)                         | red (`status-dot-dead`), label `offline <age>` |
| gone    | `last_seen_ms_ago > TTL`                          | not returned by `/status` → no row             |

Add `status-dot-stale` (amber) to the existing dot token set; brand-compliant amber
token, no raw hex.

## Testing

- **Realtime registry** (`web/tests/subscriber-registry.test.ts`, Vitest importing the
  pure `subscriber_registry.js` module): connect → `connected:true` with small
  `last_pong_ms_ago`; pong advances last-seen; disconnect → `connected:false` +
  `offline_for_ms`; two same-id connections stay connected after one drops; an entry
  past `ttl_ms` is pruned; subscribers sorted by id.
- **Realtime `/status` wiring** — the monolithic server has no unit harness (the
  existing `test_ib_realtime.py` is a live-server integration script, not unit tests),
  so the `/status` route + `?id=` capture are verified by a manual integration check:
  start the server, connect a `?id=` client → `curl /status` shows it; an anonymous
  client increments `anonymous_count`; a non-loopback request → `403`.
- **FastAPI** (`src/xenon/api/tests/test_realtime_subscribers_health.py`):
  `_realtime_subscribers_health()` with a stubbed `/status` (reachable passthrough) and
  with the server down (→ `reachable:false`); port resolution falls back to 8765.
- **Web (Vitest):** pure helpers (`classifySubscriber`, `formatAge`,
  `parseRealtimeSubscribers`) in `subscriber-health.test.ts`; Sidebar renders rows,
  dot color per threshold, and `none` / `stream offline` / `+N app clients` states in
  `sidebar-subscribers.test.tsx`.

## Non-goals (YAGNI)

- No durable (cross-restart) subscriber history — registry is in-memory; a realtime
  server restart resets it (live subscribers reconnect; offline history clears).
- No expected-count alerting (the user chose per-row over expected-count).
- No new web poll loop — reuse the `IBStatusContext` `/api/health` poll.
- `/status` is localhost-only; no remote exposure, no auth surface.
- `remote` IP is not surfaced in the public `/health` payload.

## Open question (flagged for review)

- **Offline retention TTL default** = 15 min (`IB_REALTIME_SUBSCRIBER_TTL_MS`). Long
  enough to notice a drop, short enough that retired subscribers age out. Adjust if a
  different window is preferred.
