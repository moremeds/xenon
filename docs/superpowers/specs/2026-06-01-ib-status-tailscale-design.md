# Design — Fix IB status showing "down" over Tailscale (realtime WS host resolution)

- **Date:** 2026-06-01
- **Status:** Approved (design); pending implementation plan
- **Area:** `web/` realtime WebSocket URL resolution + IB connection-status UX
- **Related:** `docs/reference/order-path-incident-history.md` (pattern: env-difference bugs), `web/CLAUDE.md` (UI verification), `src/xenon/api/CLAUDE.md` (WS ticket / relay)

## Summary

The IB connection badge shows **live on `localhost` but down when the app is opened remotely through Tailscale**. Root cause: `GET /api/ib/ws-config` returns `ws://0.0.0.0:8765` to every client, because the Next standalone server runs with `HOSTNAME=0.0.0.0` and the route derives the host from `request.nextUrl` (the server bind address) instead of the browser's `Host` header. Remote browsers cannot reach `0.0.0.0:8765`, so the realtime WebSocket never opens; the badge — fed by `usePrices()` whose `ibConnected` only flips `true` on a WS `status` message — sits at its default "down." On the Mac mini, `0.0.0.0:8765` happens to resolve locally, so it works there.

IB Gateway itself is genuinely connected the whole time (`GET /health` reports `ib_pool` sync/orders/data all connected). This is purely a client-reachability + status-sourcing bug, not an IB outage.

## Evidence (confirmed during investigation)

- `docker inspect xenon-web-1` → `HOSTNAME=0.0.0.0`.
- Live `curl http://localhost:3000/api/ib/ws-config` → `{"url":"ws://0.0.0.0:8765"}`, **unchanged even when a remote `Host` header is spoofed** (`-H 'Host: macmini.tailnet.ts.net:3000'`).
- `web/app/api/ib/ws-config/route.ts` passes `request.nextUrl.toString()` into `resolveBrowserIbRealtimeWsUrl`.
- `web/lib/server/ibRealtimeRuntime.ts::resolveBrowserIbRealtimeWsUrl` builds `${proto}//${url.hostname}:${port}` from that `requestUrl`.
- `web/lib/usePrices.ts`: `ibConnected` defaults `false` (line ~94) and is set **only** by the WS `status` message (line ~419). `ws.onclose` (line ~443) sets `connected=false` (wsConnected) but does **not** force `ibConnected=false` — so when the WS never connects, `ibConnected` stays at its initial `false`.
- `web/lib/IBStatusContext.tsx`: a parallel WS consumer that **does** force `setIbConnected(false)` on `ws.onclose` (line ~176).
- Realtime relay `scripts/infra/ib_realtime/ib_realtime_server.js` is up, connected to IB (clientId 10), publishing `status` messages. Its auth gate (lines ~360-368) bypasses ticket validation when `CLERK_JWKS_URL` is unset — and it is unset in `/opt/xenon/.env` — so the relay accepts **all** WS upgrades. Auth is **not** a factor.
- `:8765` is published on all interfaces (`*:8765` via the Colima ssh forward); plain `ws://`, no Tailscale Serve configured. So reachability and TLS are **not** the blockers — only the host string is wrong.

### Re-verified live 2026-06-01 (from a remote dev Mac over Tailscale)

```
$ curl -sS http://macmini.tail20094b.ts.net:3000/api/ib/ws-config
{"url":"ws://0.0.0.0:8765"}

$ curl -sS -H 'Host: macmini.tail20094b.ts.net:3000' \
        http://macmini.tail20094b.ts.net:3000/api/ib/ws-config
{"url":"ws://0.0.0.0:8765"}        # route ignores the Host header
```

Companion checks confirm everything else is healthy:

- `GET http://macmini.tail20094b.ts.net:8321/health` → `ib_gateway.port_listening: true`; `ib_pool.{sync,orders,data}.connected: true`; `trading_mode: live`. IB is **not** down.
- `GET http://macmini.tail20094b.ts.net:8765/` → `HTTP 426 "WebSocket upgrade required"` (matches the relay's plain-HTTP handler). A forged WS-upgrade reaches the `ws` library's handshake validation — confirming the upgrade pipeline is wide open (consistent with the `CLERK_JWKS_URL`-unset bypass). Auth, reachability, and TLS are all ruled out — only the host string is wrong.

## Decisions

- **WS URL resolution:** header-derived host (chosen over a same-origin reverse proxy and over a client-only fallback). Smallest change, no new infra, correct for the current setup (plain HTTP over Tailscale with `:8765` published).
- **IB status sourcing:** decouple `ibConnected` from the WS — a dropped realtime stream must not read as "IB Gateway down."

## Design

### Part 1 — Header-derived realtime WS host

**`web/lib/server/ibRealtimeRuntime.ts` — `resolveBrowserIbRealtimeWsUrl`**

Change the input from `requestUrl: string` to an explicit host + forwarded-proto pair. Resolution precedence is preserved:

1. `NEXT_PUBLIC_IB_REALTIME_WS_URL` env (explicit override / escape hatch) — return as-is.
2. Otherwise build from the request:
   - **scheme:** `x-forwarded-proto === "https"` → `wss:`, else `ws:`.
   - **host:** `x-forwarded-host` ?? `host` header, with the **port stripped** (handle IPv6 `[::1]:3000` brackets).
   - **port:** runtime-file port if present, else `DEFAULT_IB_REALTIME_PORT` (8765).
   - Result: `${scheme}//${hostNoPort}:${port}`.
3. If the host header is missing/blank, fall back to the prior behavior (so server-side / non-browser callers don't break).

**`web/app/api/ib/ws-config/route.ts`**

Read `request.headers` (`x-forwarded-host`, `host`, `x-forwarded-proto`) and pass them to the resolver. Stop using `request.nextUrl`.

Net effect:

- `localhost` → `ws://localhost:8765` ✓
- Tailscale → `ws://mini.tailnet.ts.net:8765` ✓ (reaches the published `:8765`)

The client resolver (`web/lib/ibRealtimeWsClient.ts`) is unchanged: it still calls `/api/ib/ws-config` and only uses its `window.location.hostname` fallback if that call fails.

### Part 2 — Decouple IB status from the realtime WS

Split the two signals cleanly:

- **`wsConnected`** — the live-data-stream (realtime WS) state. Unchanged source.
- **`ibConnected`** — the actual IB Gateway state. While the WS is delivering `status` messages, the WS value remains authoritative (fast path). When the WS is **not** connected, source `ibConnected` from a periodic `GET /api/health` poll (`ib_pool` / `ib_gateway` connected flags) instead of leaving it at `false` (`usePrices`) or forcing it `false` (`IBStatusContext`).

Apply to both WS consumers so behavior is consistent:

- `web/lib/usePrices.ts` — feeds the visible badge via `WorkspaceShell` (2s debounce already present).
- `web/lib/IBStatusContext.tsx` — remove the `setIbConnected(false)` force on `ws.onclose`; rely on the health fallback.

Resulting UI semantics:

- WS up + IB up → **"live"**
- WS down + IB up (per `/health`) → **"live data stream offline"** (not "IB Gateway down")
- IB down (per WS `status` or `/health`) → **"IB Gateway down"**

### Part 3 — Deployment-config fix (related, one line)

`IB_REALTIME_WS_URL=ws://localhost:8765` in the web container env is wrong **inside Docker** (the web container's own loopback, not the relay) and breaks the server-side `previous-close` snapshot path (`resolveServerIbRealtimeWsUrl`). Set it to `ws://realtime:8765` (the compose service name) in the deployment env (`/opt/xenon/web.env` or the compose `environment` block). Low-risk; same root-cause class. Not required for the badge fix, but applied in the same change.

## Testing (red/green TDD)

Per `web/CLAUDE.md`: Vitest for unit, chrome-cdp/Playwright for browser verification; no UI change is "done" until visually confirmed.

- **Vitest — host derivation** (`resolveBrowserIbRealtimeWsUrl`):
  - `Host: mini.tailnet.ts.net:3000` → `ws://mini.tailnet.ts.net:8765`
  - `x-forwarded-host` preferred over `host`
  - `x-forwarded-proto: https` → `wss://…:8765`
  - IPv6 `[::1]:3000` → `ws://[::1]:8765`
  - missing host header → documented fallback
  - `NEXT_PUBLIC_IB_REALTIME_WS_URL` set → precedence preserved (override wins)
- **Vitest — status sourcing** (`usePrices`, `IBStatusContext`):
  - WS delivering `status` → `ibConnected` tracks the message (fast path)
  - WS down + `/health` reports IB up → `ibConnected` true, `wsConnected` false
  - WS down + `/health` reports IB down → both false
- **Browser (Playwright/chrome-cdp):**
  - badge shows "live" when the WS connects via the derived host
  - badge shows "live data stream offline" (not "IB Gateway down") when the WS is force-closed but `/health` reports IB up
- **Manual smoke test (post-deploy, explicitly called out — not skipped):** open the app from a remote Tailscale device and confirm the IB badge reads live. The tailnet cannot be reproduced in CI.

## Non-goals

- Reverse-proxy / Tailscale Serve (HTTPS) support — header-derived was chosen; revisit only if HTTPS Serve/Funnel is adopted (then `:8765` would need TLS termination / same-origin proxying).
- Clerk / WS-ticket auth changes — Clerk is unset; the relay already accepts all upgrades.
- The Futu `/health` `connected` flag — correct by design (reflects the lazy `_futu_client` singleton, not OpenD reachability); not a bug.

## Risks / notes

- `request.headers` host can be spoofed by a client, but the value is only used to tell _that same client_ which host to open its own WS to — no privilege boundary is crossed.
- The `/health` poll adds a low-frequency request only while the WS is down; gate it so it stops when the WS reconnects.
- Two WS consumers (`usePrices`, `IBStatusContext`) must stay consistent — the shared resolver fix (Part 1) covers both; Part 2 must be applied to both.
