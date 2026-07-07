# P3.3 — Push order lifecycle events over the existing realtime WS (kill 5s/30s polling)

- **Date:** 2026-07-05
- **Branch (when executed):** `feat/order-events-over-ws`
- **Finding / roadmap:** `docs/fable/10-roadmap.md` § P3.3 ("Order events pushed over the
  existing WS — kills 5s/30s polling"). Phase 3, **gated on Phase-1 measurements.**
- **Severity:** MEDIUM. No new order-write surface; the WS carries a _change hint_, never
  authoritative state. The authoritative read stays DB-first through `/api/orders`.
- **Goal (one line):** When an order changes state, push a lightweight `order-event` over
  the realtime WS so the frontend revalidates immediately instead of waiting up to 30s
  (open orders) / 5s (cancel poll). Acceptance: fill→UI ≤ poller tick + 1s.

## HARD GATE — do not execute without both

1. **P1.3 measured `fill_to_ui_seconds` and it is a real problem.** Roadmap Phase 3 is
   "gated on P1 measurements." If P1.3 (`docs/fable/measurements-<date>.md`) shows median
   fill→UI already ≤ ~2s, **REJECT this plan at execution time** — record the rejection in
   the plans index and stop. The 30s open-order sync interval is the suspected cost; confirm
   it dominates before building transport.
2. **P2.2 merged** — `orders_store.transition()` exists as the single state-write chokepoint
   (`docs/superpowers/plans/2026-07-05-fable-p2-2-transition-chokepoint.md`). That function
   is the ONE emit point; without it, order-state writes are scattered across
   `mark_submitted`/`mark_terminal`/`mark_uncertain`/`resolve_stuck_submission`/
   `single_leg_rehydrate._update_state_only` and there is no clean hook.

## Re-verify preamble (MANDATORY — written far ahead of its prereqs)

S2/S3/S4/P2.2/P2.4 reshape the order path before this runs. Anchor everything below by
**function name + snippet** and re-confirm at HEAD before writing any code:

1. `grep -n "def transition" src/xenon/execution/orders_store.py` → exists, writes exactly
   one `order_events` row in the caller's transaction (P2.2 invariant). If absent → STOP
   (prereq 2 unmet).
2. `grep -n "def emit_outbox_in_txn\|class EventSubscriber" src/xenon/db/events.py` →
   both present. `emit_outbox_in_txn(conn, channel=, source=, payload=)` is the **sync**,
   same-transaction outbox insert; the outbox trigger `pg_notify(NEW.channel, NEW.id::text)`
   fires on commit (migration `9b645325b50d_add_outbox_notify_trigger.py`).
3. Exemplar consumer still `src/xenon/api/services/journal_auto_import.py`
   (`JournalAutoImportSubscriber` + `handle_notification_id(outbox_id)` fetch-by-id pattern,
   `consumed_by` dedup). Mirror it — do not invent a new subscriber shape.
4. Relay: `scripts/infra/ib_realtime/ib_realtime_server.js` — `DEFAULT_WS_PORT = 8765`
   (prod; dev overrides to **8866** via `--port`), an `http.createServer` at the
   "Create HTTP server for WebSocket upgrade with ticket validation" comment,
   `sendMessage(client, payload)` + `const clients = new Set()`. There is NO
   broadcast-to-all helper — the house pattern is
   `for (const client of clients) sendMessage(client, payload)`.
   `clients` only ever contains sockets that passed the
   upgrade-time ticket validation (or the localhost / no-Clerk dev bypass in
   `httpServer.on("upgrade", …)`), so iterating `clients` IS broadcast-to-authed.
5. Frontend: `web/lib/ibRealtimeWsClient.ts::resolveBrowserIbRealtimeWsUrl` is **URL
   resolution only** (no socket, no dispatch). The main quote socket + `onmessage` type
   switch live in `web/lib/usePrices.ts::usePrices` (`case "price"/"snapshot"/"batch"/…`);
   `web/lib/IBStatusContext.tsx::IBStatusProvider` opens its own second socket. Poll points
   still: `web/lib/useOrders.ts` `SYNC_INTERVAL_MS = 30_000` + `triggerSync()`;
   `web/lib/OrderActionsContext.tsx` `POLL_INTERVAL_MS = 5_000` (pending maps keyed by
   `permId`).
6. WS auth unchanged: `src/xenon/api/ws_ticket.py` — 30s single-use tickets
   (`TICKET_TTL_SECONDS = 30`, `validate_ticket` pops on use); relay validates via
   `POST /ws-ticket/validate`.
7. Prod topology: `docker-compose.yml` runs `api` and `realtime` as **separate services**
   (service keys at ~lines 36 and 137; see `docs/runbooks/remote-deploy.md`). API-container
   `127.0.0.1` is NOT the realtime container — any loopback assumption is wrong in prod.

If any of 1–7 has drifted, re-anchor before coding — never plan against stale line numbers.

## Design (one path — stated, not optioned)

Postgres is the only process that sees ALL order-state writes (in-process FastAPI + place
subprocess clientIds 20–49 + cancel/modify subprocess + activity poller + rehydrate). So the
emit + fan-out is Postgres-anchored, reusing the existing outbox/`EventSubscriber` infra:

```
transition()  ──emit_outbox_in_txn(channel="order.event")──▶  outbox row
                        │ (trigger NOTIFY on commit, any process)
                        ▼
FastAPI lifespan: OrderEventRelayBridge (EventSubscriber, asyncpg LISTEN)
                        │ fetch-by-id → build {submission_id, perm_id, state, broker, scope}
                        ▼ HTTP POST ${XENON_RELAY_INGEST_URL}/ingest/order-event  (X-Internal-Token)
Relay: for (const client of clients) sendMessage(client, {type:"order-event", ...})
                        ▼
Frontend useOrderEvents socket dispatches → useOrders.syncNow() / cancel-poll tick fires NOW
                        ▼ authoritative read still via /api/orders (DB-first)
```

**Why this shape (each a load-bearing decision):**

- **Emit inside `transition()`** — single chokepoint (P2.2), so one insert covers every
  writer and every process. The outbox insert rides the SAME transaction as the
  `order_events` write, so a hint is never emitted for an uncommitted/rolled-back transition.
- **`EventSubscriber` in FastAPI, not a pg client in the relay** — the relay (Node) does not
  import `pg`; the house pattern for reactive consumers is Python-side (`journal_auto_import`).
  FastAPI forwards to the relay over HTTP — loopback in single-host dev, Docker service DNS
  (`http://realtime:8765`) in prod, selected by `XENON_RELAY_INGEST_URL`.
- **WS payload is a _hint_, not state** — the frontend revalidates through `/api/orders`
  (FastAPI → Postgres). This preserves the DB-first read invariant (root CLAUDE.md § Runtime
  Data Read Paths) — no order state is ever trusted off the socket.
- **Broadcast to all authed clients** — xenon is single-operator (`ALLOWED_USER_IDS`). Per-
  account routing in the relay is out of scope (see Non-goals). Payload still carries
  `broker`+scope so the frontend revalidates only the affected broker tab, and a future
  multi-user routing change has the fields it needs.
- **Polling stays as a backstop** — a dropped socket/missed push must still self-heal.
  Widen `SYNC_INTERVAL_MS` to 60s (from 30s) once push is proven; keep the 5s cancel poll's
  count-based deadline but let a push short-circuit it.

## Steps (TDD, strictly ordered; adapt anchors at HEAD)

1. **Feature flags + env.** `XENON_ORDER_EVENTS_WS=1` (backend, default OFF) gates the
   bridge start + the outbox emit in `transition()`. `NEXT_PUBLIC_ORDER_EVENTS_WS=1` gates
   the frontend subscription. Both OFF ⇒ byte-identical to today's polling behavior.
   New env on the api side: `XENON_RELAY_INGEST_URL` (dev default `http://127.0.0.1:8866`;
   prod compose value `http://realtime:8765` — set on the `api` service). Shared secret
   `XENON_INTERNAL_API_TOKEN` must be set identically on BOTH the `api` and `realtime`
   services in `docker-compose.yml` and in dev `.env`.

2. **Emit (Python).** In `orders_store.transition()`, after the successful guarded UPDATE +
   `order_events` insert, add (inside the same `conn` transaction, flag-gated):

   ```python
   if os.getenv("XENON_ORDER_EVENTS_WS") == "1":
       from xenon.db.events import emit_outbox_in_txn
       emit_outbox_in_txn(
           conn, channel="order.event", source="orders_store.transition",
           payload={"submission_id": sid, "to": to},  # perm_id/scope resolved by the bridge from the row
       )
   ```

   Add `CHANNEL_ORDER_EVENT = "order.event"` to `db/events.py` next to the existing channel
   constants and use it (do not hardcode the string in two places). Test first:
   `test_orders_store_transition.py::test_transition_emits_order_event_outbox_when_flag_on`
   — flag ON ⇒ one `outbox` row on channel `order.event`; flag OFF ⇒ zero. Fixture order uses
   a REAL frozen ticker (reuse P2.2's fixture symbol/price — no `FOO`/round numbers).

3. **Bridge (Python).** New `src/xenon/api/services/order_event_relay_bridge.py`, mirroring
   `journal_auto_import.py`: `OrderEventRelayBridge` wraps an `EventSubscriber` on
   `CHANNEL_ORDER_EVENT`; `handle_notification_id(outbox_id)` fetches the outbox row
   (`consumed_by` dedup with its own `CONSUMER_ID`), joins `order_submissions` by
   `submission_id` to read `state`, `perm_id` (`schema.py` — `Column("perm_id", Text)` on
   `order_submissions`), `broker`, `account_env`, `broker_account`, then calls
   `forward_to_relay(payload)` with ALL of those fields — `perm_id` is REQUIRED in the wire
   payload (the frontend cancel/modify pending maps are keyed by `permId`; see Step 6).
   `forward_to_relay` does a short-timeout `POST` to
   `{XENON_RELAY_INGEST_URL}/ingest/order-event` (never a hardcoded 127.0.0.1 — in prod the
   relay is the `realtime` container, not api-container loopback) with header
   `X-Internal-Token: XENON_INTERNAL_API_TOKEN`; a forward failure is logged, not raised
   (the row is still marked consumed — the backstop poll recovers a missed hint). Start it in
   the FastAPI `lifespan` alongside the existing subscribers, flag-gated, and **skip under
   `XENON_READ_ONLY=1`** (no reactive services in read-only mode).

4. **Relay ingest + broadcast (Node).** In `ib_realtime_server.js`, on the existing
   `http.createServer`, add a `POST /ingest/order-event` handler. Auth is the shared token,
   NOT a loopback check (in prod the caller arrives via Docker service DNS and is not
   loopback): if `process.env.XENON_INTERNAL_API_TOKEN` is unset/empty → 503 and never
   broadcast (fail closed); else require
   `req.headers["x-internal-token"] === process.env.XENON_INTERNAL_API_TOKEN` → 403
   otherwise. On success, parse the JSON body and broadcast with the house pattern —
   there is no broadcast-to-all helper:

   ```js
   const payload = { type: "order-event", ...body, relay_ts: Date.now() };
   for (const client of clients) sendMessage(client, payload);
   ```

   `clients` (the module-level `new Set()`) only contains upgrade-authenticated sockets,
   so no per-client auth field is needed. Do NOT touch the quote/depth/tape paths.

5. **Frontend transport (TS).** New `web/lib/useOrderEvents.ts`: a **separate lightweight
   socket** — its own `new WebSocket(await resolveBrowserIbRealtimeWsUrl())` with a tiny
   `onmessage` that dispatches only `type === "order-event"` frames to registered handlers,
   gated on `NEXT_PUBLIC_ORDER_EVENTS_WS`. Deliberate choice, do not revisit: the main
   socket's `onmessage` switch inside `usePrices.ts::usePrices` is quote-subscription-scoped
   and P4.4 plans to refactor that socket core — threading order events through it couples
   this feature to the quote hook's lifecycle and to P4.4's churn. One extra WS connection
   (third, after usePrices + IBStatusContext) is acceptable for a single-operator app.

6. **Frontend revalidate-on-push (TS).**
   - `useOrders.ts`: subscribe via `useOrderEvents`; on an event whose `broker` matches the
     active tab, call `triggerSync()` immediately (debounced ~250ms to coalesce bursts).
     Widen `SYNC_INTERVAL_MS` 30_000 → 60_000 (backstop only). Flag OFF ⇒ leave 30_000.
   - `OrderActionsContext.tsx`: pending-cancel/modify maps are keyed by `permId`. Match the
     event's `perm_id` against those maps and fire the poll `tick()` now instead of waiting
     the 5s interval; when the event's `perm_id` is 0/null/absent (IB permId not yet acked —
     see the `ib_async permId=0 race`), fall back to matching by `submission_id` where the
     UI row carries one, else ignore (the 5s poll still covers it).

## Tests (offline — NO live IB)

| Layer                             | File / command                                                                                                                                                                                                                                                                                                                                                                          | Assert                                                                                                                                                                                                                                                  |
| --------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Python unit (emit)                | `uv run pytest scripts/tests/test_orders_store_transition.py -k order_event_outbox -xvs`                                                                                                                                                                                                                                                                                                | flag ON → exactly 1 `outbox` row channel=`order.event`; OFF → 0                                                                                                                                                                                         |
| Python unit (bridge)              | `uv run pytest src/xenon/api/tests/test_order_event_relay_bridge.py -xvs`                                                                                                                                                                                                                                                                                                               | `handle_notification_id` fetches row, joins state+scope, calls a mocked `forward_to_relay` with `{submission_id,perm_id,state,broker,...}` (perm_id present); `consumed_by` dedup blocks reprocess                                                      |
| Python unit (read-only)           | same file, `-k read_only`                                                                                                                                                                                                                                                                                                                                                               | bridge not started when `XENON_READ_ONLY=1`                                                                                                                                                                                                             |
| Relay unit                        | `node --test scripts/infra/ib_realtime/__tests__/order_event_ingest.test.mjs`                                                                                                                                                                                                                                                                                                           | valid token → fake WS client receives `{type:"order-event"}`; missing/wrong token → 403, no broadcast; token env unset → 503, no broadcast                                                                                                              |
| Vitest (transport)                | `cd web && npm test -- order-events`                                                                                                                                                                                                                                                                                                                                                    | `useOrderEvents` dispatches an `order-event` frame to a registered handler; `useOrders` calls `triggerSync` once on receipt (fake socket); non-matching broker → no sync; perm_id=0 event falls back to submission_id match                             |
| Typecheck/lint                    | `cd web && npx tsc --noEmit && npm run lint`                                                                                                                                                                                                                                                                                                                                            | clean                                                                                                                                                                                                                                                   |
| CI order-path guards              | `uv run python scripts/checks/no_json_fallback_on_order_path.py && ... no_json_write_on_order_path.py && ... order_path_caller_allowlist.py`                                                                                                                                                                                                                                            | all exit 0 (this adds NO json read/write on the order path)                                                                                                                                                                                             |
| Scoped suite                      | `uv run python scripts/infra/dev/run_pytest_affected.py`                                                                                                                                                                                                                                                                                                                                | green                                                                                                                                                                                                                                                   |
| E2E (Playwright)                  | `cd web && npx playwright test order-events`                                                                                                                                                                                                                                                                                                                                            | with `NEXT_PUBLIC_ORDER_EVENTS_WS=1`, inject a fake `order-event` WS frame (or drive a paper fill); orders panel repaints BEFORE the 60s backstop; screenshot → `output/playwright/order-events-ws-2026-07-05.png`; assert the new state text on-screen |
| Live probe (PAPER only)           | `scripts/infra/dev.sh paper` (IB 4002), place a far-from-market limit, then cancel; `curl -s -H "X-Internal-Token: $XENON_INTERNAL_API_TOKEN" -X POST http://localhost:8866/ingest/order-event -d '{"submission_id":"<sid>","perm_id":"<pid>","state":"CANCELLED","broker":"IB"}'`                                                                                                      | 200; open WS client receives the frame; never run against live IB (4001)                                                                                                                                                                                |
| Negative (auth)                   | curl the ingest endpoint with NO token; then with token env unset on the relay                                                                                                                                                                                                                                                                                                          | 403 / 503 respectively; no broadcast either way                                                                                                                                                                                                         |
| Prod compose topology (MANDATORY) | bring up the compose stack (`api` + `realtime` services from `docker-compose.yml`) with `XENON_ORDER_EVENTS_WS=1`, `XENON_RELAY_INGEST_URL=http://realtime:8765`, shared token on both; `docker compose exec api curl -s -X POST -H "X-Internal-Token: $XENON_INTERNAL_API_TOKEN" http://realtime:8765/ingest/order-event -d '{"submission_id":"t","state":"CANCELLED","broker":"IB"}'` | 200 + connected WS client receives the frame. Single-host dev verification alone does NOT count — this repo has shipped 2 features broken exactly this way (see `docs/reference/` incident notes / `feedback_verify_prod_docker_topology`)              |
| Backstop                          | drop the WS mid-test, transition an order                                                                                                                                                                                                                                                                                                                                               | UI still recovers within the 60s backstop sync                                                                                                                                                                                                          |

## Tripwires / abort

- **STOP** if P1.3 shows fill→UI already ≤ acceptance — there is no problem to solve; record
  the rejection and do nothing.
- **STOP** if `orders_store.transition()` is absent at HEAD (P2.2 unmet) — there is no clean
  single emit point; do not scatter emits across the individual state writers.
- **DO NOT SHIP without the prod-compose-topology check above.** `api` and `realtime` are
  separate containers in prod — api-container `127.0.0.1` is not the relay, and the relay
  cannot use "is loopback" as auth. Any loopback shortcut that passes single-host dev is
  exactly the failure mode that has shipped broken twice before.
- **STOP** if the deployment turns multi-user (per-account WS routing needed) — the broadcast-
  to-all simplification no longer holds; re-scope before coding.
- **STOP** if any test passes _before_ your change (anchor is wrong).
- Any live check is **PAPER only** (`dev.sh paper`, IB 4002). Never live money for an
  observability feature.
- Cleanup: never leave a resting paper order after a probe.

## Rollback

Backend `XENON_ORDER_EVENTS_WS` unset ⇒ no outbox emit, bridge never starts (env change,
no deploy). Frontend `NEXT_PUBLIC_ORDER_EVENTS_WS` unset ⇒ no subscription, polling intervals
revert to 30s/5s. Branch discard reverts all code. No schema migration (reuses the existing
`outbox` table + trigger), so no downgrade path is required. Keep both flags OFF until a
paper soak (≥1 week) shows zero missed-hint regressions vs the backstop poll.

## Incident-history row (append when merged — order-path adjacent)

```
| 2026-07-05 | P3.3 order events over WS | Open-order UI lagged up to 30s behind IB fills (poll-only) | transition() emits an `order.event` outbox row (same txn); FastAPI EventSubscriber bridge forwards to the relay via XENON_RELAY_INGEST_URL (dev loopback / prod http://realtime:8765) with a shared internal token; relay broadcasts `order-event` (incl. perm_id) to authed clients; frontend revalidates via /api/orders (hint only, DB-first read preserved) | test_order_event_relay_bridge.py + order_event_ingest.test.mjs + order-events Playwright + prod-compose probe |
```

## Repo invariants honored

uv-only; branch+PR (never push master); no AI-attribution commit trailers; paper-first for any
order-path live check; DB-first read path preserved (WS is a hint); no new JSON read/write on
the order path (CI guards stay green); `XENON_READ_ONLY=1` keeps the bridge off; real frozen
tickers in fixtures; dev ports Next 3200 / FastAPI 8421 / relay 8866, prod 3000/8321/8765;
prod-topology verification mandatory (api ≠ realtime container).
