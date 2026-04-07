# API Infrastructure Reference

FastAPI bridge between Next.js and IB/UW/MenthorQ. Policy rules live in `scripts/api/CLAUDE.md`; this file is reference.

## FastAPI Server Architecture

Next.js routes call FastAPI (`localhost:8321`) via `xenonFetch()` (`web/lib/xenonApi.ts`). No `spawn()`.

### Three-Service Dev Stack (`npm run dev`)

| Service | Port |
|---------|------|
| Next.js | 3000 |
| IB WS relay | 8765 |
| FastAPI | 8321 |

### Files

| File | Purpose |
|------|---------|
| `server.py` | 26 endpoints, CORS, Clerk JWT auth middleware, IB pool, health, auto-restart. `POST /performance/background` = fire-and-forget, 202, dedup |
| `auth.py` | Clerk JWT verification — JWKS validation, single-tenant allowlist (`ALLOWED_USER_IDS`), graceful bypass when unconfigured |
| `ws_ticket.py` | Short-lived single-use WS tickets (30s TTL) — avoids passing JWTs in WebSocket URLs |
| `ib_pool.py` | Role-based IB pool (sync/orders/data), auto-reconnect |
| `ib_gateway.py` | Health check + auto-restart via IBC launchd. Detects CLOSE_WAIT (upstream dead) |
| `subprocess.py` | Async `run_script()`, `run_module()` — uses `sys.executable` (not `python3`) to match server interpreter |
| `routes/historical.py` | Machine-to-machine historical data endpoints (X-API-Key auth) |

### Graceful Degradation

| Scenario | Behavior |
|----------|----------|
| FastAPI + IB up | Normal |
| FastAPI up, IB down | Auto-restart Gateway, retry once, else 503 + cached |
| FastAPI up, IB CLOSE_WAIT | Detected at startup + script errors; auto-restart + kill lingering processes |
| FastAPI down | Cached from disk, `is_stale: true` |

No spawn fallback. Always try FastAPI first.

## Authentication (Clerk)

All FastAPI routes are protected by Clerk JWT middleware. Next.js routes are protected by Clerk middleware (`web/middleware.ts`). WebSocket connections use a ticket-based flow to avoid leaking JWTs in URLs.

| Component | File | Purpose |
|-----------|------|---------|
| FastAPI auth middleware | `scripts/api/auth.py` | Validates Clerk JWTs via JWKS, enforces `ALLOWED_USER_IDS` allowlist |
| FastAPI auth dependency | `scripts/api/auth.py` | `verify_clerk_jwt` — used by `/ws-ticket` endpoint via `Depends()` |
| WS ticket service | `scripts/api/ws_ticket.py` | Issues 30s single-use tickets for WS auth |
| WS ticket proxy | `web/app/api/ib/ws-ticket/route.ts` | Next.js route proxies to FastAPI (same-origin for browser) |
| Next.js middleware | `web/middleware.ts` | Clerk `auth.protect()` on all routes except public share pages |
| WS ticket client | `web/lib/wsTicket.ts` | Browser calls `/api/ib/ws-ticket` (same-origin) before WS connect |
| `xenonApi.ts` | `web/lib/xenonApi.ts` | Attaches Clerk Bearer token to all `xenonFetch()` calls |

**Public share routes:** `/api/regime/share`, `/api/vcg/share`, `/api/internals/share`, `/api/menthorq/cta/share` are public (no auth).

**Graceful fallback:** When `CLERK_JWKS_URL` is not set, auth middleware passes all requests through (local dev without Clerk).

**Tests:** `scripts/api/tests/test_auth.py` (Python), `web/tests/auth-integration.test.ts`, `web/tests/ws-ticket-local.test.ts` (TS).

## IB Gateway Auto-Recovery

Startup: check port 4001 + CLOSE_WAIT detection (`lsof`), restart if needed, poll 45s. Runtime: IB subprocess errors trigger Gateway health check FIRST — only restart if port not listening or CLOSE_WAIT detected. Subprocess failures from client ID collisions, VOL errors, or transient timeouts do NOT trigger restart. Restart script snapshots pre-existing PIDs and only force-kills those that survived SIGTERM (prevents killing newly-spawned processes). Manual: `POST /ib/restart`. Health: `GET /health` returns `upstream_dead: true` when CLOSE_WAIT detected.

### Health Check

```bash
curl http://localhost:8321/health
# Returns: ib_gateway, ib_pool (sync/orders/data), uw
```

## IB Gateway Modes

Three modes controlled by `IB_GATEWAY_MODE` env var (default: `docker`):

### Cloud Mode (Tailscale) — Default for Development

Gateway runs on a Hetzner VM accessible via Tailscale MagicDNS at `ib-gateway:4001`. Set `IB_GATEWAY_HOST=ib-gateway` and `IB_GATEWAY_MODE=cloud` in root `.env`. Both Python (`ib_client.py` loads dotenv at import) and Node (`ib_realtime_server.js` loads dotenv at startup) read this automatically. All scripts import `DEFAULT_HOST` from `ib_client` — no hardcoded `127.0.0.1` in IB connection code.

**VPS port mapping:** `0.0.0.0:4001 → container:4003` (socat). The gnzsnz image runs Java Gateway on localhost:4001 inside the container, with socat on 4003 forwarding to it. External connections (Tailscale) go through socat.

**VPS IB Gateway GUI requirement:** "Allow connections from localhost only" must be **unchecked** in Configure → API → Settings (via VNC at `localhost:5900`). This setting persists in the Docker volume. Without it, Tailscale connections are rejected with `ECONNRESET`.

**Cloud mode behavior:** Health check = TCP port probe only. No local restart, no CLOSE_WAIT detection, no docker/launchd lifecycle management. `POST /ib/restart` returns 503 with "manage it on the remote host". Stale tick detection in the WS relay disconnects and reconnects (no restart attempt).

**Management commands** (local Mac via Tailscale SSH, or directly on VPS):

| Command | Action |
|---------|--------|
| `ibstart` | Start container, wait for port 4001 |
| `ibstop` | Stop and remove container |
| `ibrestart` | Restart, wait for port 4001 |
| `ibstatus` | Container state, port, connections |
| `iblogs` | Last 50 lines (`iblogs 100`) |
| `ibhealth` | Docker healthcheck status |

VPS script: `/usr/local/bin/ibgw`. Local: `ibgw()` in `~/.zshrc` wraps SSH.

### Docker Mode (Primary)

Image: `ghcr.io/gnzsnz/ib-gateway` (pinned to digest). Config: `docker/ib-gateway/`.

| Command | Action |
|---------|--------|
| `scripts/docker_ib_gateway.sh start` | Start (validates secrets, checks launchd not running) |
| `scripts/docker_ib_gateway.sh stop` | Stop |
| `scripts/docker_ib_gateway.sh restart` | Restart |
| `scripts/docker_ib_gateway.sh status` | Status |
| `npm run ib:start` (from web/) | Convenience alias |

Docker handles reliability via `restart: unless-stopped` + healthcheck. `READ_ONLY_API=no` (Xenon places orders). Password via Docker secrets (`docker/ib-gateway/secrets/ib_password.txt`, chmod 600).

### LaunchD Mode (Legacy Fallback)

Global service: `local.ibc-gateway`. Install: `~/ibc-install/`, config: `~/ibc/`. Credentials in macOS Keychain.

| Command | Action |
|---------|--------|
| `~/ibc/bin/start-secure-ibc-service.sh` | Start |
| `~/ibc/bin/stop-secure-ibc-service.sh` | Stop |
| `~/ibc/bin/restart-secure-ibc-service.sh` | Restart |
| `~/ibc/bin/status-secure-ibc-service.sh` | Status |

**Lifecycle:** Mon-Fri 00:00 start → 2FA approve on IBKR Mobile → 11:58 PM daily restart (no 2FA) → Sunday 07:05 cold restart (2FA).

### Gateway Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `IB_GATEWAY_MODE` | `docker` | `docker`, `cloud` (remote, no restart), or `launchd` |
| `IB_GATEWAY_HOST` | `127.0.0.1` | Gateway host (`ib-gateway` for cloud mode) |
| `IB_GATEWAY_PORT` | `4001` | Gateway port |

### Ports

| Port | Service |
|------|---------|
| 3000 | Next.js |
| 8321 | FastAPI |
| 8765 | IB WS relay |
| 4001 | IB Gateway Live |
| 4002 | IB Gateway Paper |
| 7496/7497 | TWS Live/Paper |
| 7462 | IBC Command Server |

### Client ID Ranges

| Range | Usage |
|-------|-------|
| 0-9 | FastAPI IBPool (sync=3, orders=4, data=5) |
| 10-19 | WS relay (rotates on conflict) |
| 20-49 | Subprocess scripts (`client_id="auto"`) |
| 50-69 | Scanners (CRI/VCG rotating) |
| 70-89 | Daemons (fill=70, exit=71) |
| 90-99 | CLI/standalone |

Tests: `test_client_id_allocation.py` (17). IB error `10358` = Reuters inactive → auto-fallback.

## Historical Data API

Machine-to-machine endpoints for headless clients (e.g., market-data-warehouse):

- `POST /contract/qualify` — resolve contract details (conId, exchange, etc.)
- `POST /historical/head-timestamp` — earliest available data date (ISO 8601)
- `POST /historical/bars` — fetch OHLCV bars (ISO `YYYY-MM-DD` dates)

Auth: `X-API-Key` header checked against `MDW_API_KEY` env var. Scoped to these 3 paths only — trading routes (orders, portfolio) remain Clerk JWT-only. Uses `hmac.compare_digest` for constant-time comparison.

Endpoints live in `scripts/api/routes/historical.py` and use the "data" pool role from `IBPool`.

## Cloud Deployment Notes

When deployed on the Hetzner VPS via xenon-cloud:

- **FastAPI auth**: Clerk JWT validated on all external requests. Localhost requests (Next.js → FastAPI on 127.0.0.1:8321) bypass auth — port is never public.
- **Clerk middleware**: API routes (`/api/*`) are excluded from Next.js Clerk middleware `protect()`. Server-side fetches from pages don't carry session cookies, so middleware must not block them. Auth is handled by FastAPI for external API access.
- **`NEXT_PUBLIC_*` env vars**: Baked at build time. After changing `.env`, rebuild Next.js: `cd web && npm run build`
- **Root `node_modules`**: The root `package.json` has shared deps (`@sinclair/typebox`) used by `lib/tools/`. Must be installed before `web/` build.
- **`requirements.txt`**: Includes `cryptography` (needed by PyJWT for RS256), `fastapi`, `uvicorn`, `python-dotenv`, `numpy`, `pytz`, `playwright`.
- **`@sinclair/typebox`**: Pinned to exact `0.34.48` in both root and web `package.json` to prevent version mismatch build failures.
- **Production Clerk**: Uses different user IDs than dev. `ALLOWED_USER_IDS` must be updated.
- **CI/CD**: Push to `main` triggers GitHub Actions → SSH → `deploy.sh` on VPS. IB Gateway is not restarted. Auto-rollback on health check failure.
