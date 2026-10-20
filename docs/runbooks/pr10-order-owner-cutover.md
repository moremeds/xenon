# PR-10 cutover — single order owner

PR-10 moves every Xenon order action (place, cancel, modify, wizard protect) onto one IB connection: the API pool `orders` role, clientId `base + 4`. The order-write CLIs `xenon-ib-place-order` and `xenon-ib-order-manage` are removed. An order that this connection did not place gets 409 `ORDER_NOT_OWNED` ("cancel or modify it in TWS / IBKR Mobile"). Code: `src/xenon/api/order_owner.py`. Rules: `src/xenon/api/CLAUDE.md` § Order owner.

**Database migration:** PR-10 has none (no file under `src/xenon/db/migrations/` changes). The migrator does not need to run for this PR. The general rule still applies: Watchtower never runs the migrator, so any PR that adds a migration needs `docker-compose --profile migrate run --rm migrator` on the mini (`docs/runbooks/remote-deploy.md`).

## 1. Deployment contract

This is an operational contract, not fencing. Every environment gets its own clientId block. The api refuses to start when the base does not match its database.

| Gateway                                                 | Environment      | `XENON_IB_CLIENT_ID_BASE`                                    | Host / supervisor                     |
| ------------------------------------------------------- | ---------------- | ------------------------------------------------------------ | ------------------------------------- |
| Live, mini `:4001`                                      | prod             | `0` (set in `deploy/compose.prod.yml`)                       | Docker stack `/opt/xenon`, Watchtower |
| Live, mini `:4001` (read-only) or paper, laptop `:4002` | dev              | `100` (`scripts/infra/dev.sh`; `docker-compose.yml` default) | MacBook dev session                   |
| any                                                     | second dev stack | another base (`200` … `800`, step 100)                       | — or keep it stopped                  |

Zones inside a block (absolute id = base + offset):

| Offset    | Owner                                                                                                               |
| --------- | ------------------------------------------------------------------------------------------------------------------- |
| 3 / 4 / 5 | API pool `sync` / `orders` / `data` — `orders` is the order owner                                                   |
| 10–19     | realtime relay (Python never opens these)                                                                           |
| 20–49     | `client_id="auto"` read/data scripts (`xenon-ib-sync`, `xenon-ib-orders`, chain / depth / greeks). No order writes. |

`IBClient.connect` refuses an id outside the caller's zone before any socket opens. Base 0 is accepted only with `core_dev`; a base other than 0 is refused with `core_dev`.

## 2. Before the deploy

1. **Apply the compose change first.** PR-10 sets `stop_grace_period: 60s` on `api` and `WATCHTOWER_TIMEOUT: 60s` on Watchtower. Watchtower stops containers with its own timeout (default 10s), not with the service's grace period. Copy the new `deploy/compose.prod.yml` with `deploy/README.md` § Operator rollout checklist, section A, **before** the PR-10 image is published. Otherwise Watchtower replaces the old api with a 10s stop and can leave stale sockets (§ 4).
2. **List the open orders that the new build will refuse.** On the mini, run against `core_dev` (read-only):

   ```sql
   SELECT submission_id, ticker, action, quantity, state,
          placing_client_id, ib_order_id, perm_id, submitted_at
   FROM xenon.order_submissions
   WHERE broker = 'IB' AND account_env = 'live'
     AND state IN ('PENDING', 'WORKING', 'PARTIALLY_FILLED', 'UNKNOWN')
     AND placing_client_id IS DISTINCT FROM 4
   ORDER BY submitted_at;
   ```

   - `placing_client_id` 20–49 (old place subprocess) or 26 (old fallback id): after the deploy, cancel and modify answer 409 `ORDER_NOT_OWNED`. Give every row a disposition before the deploy: cancel it now with the old build, or plan to manage it in TWS / IBKR Mobile. "Operator manages it in TWS" is a workflow; the order does not transfer to the owner.
   - `placing_client_id` 0–2, 10–19, or another block: also 409, as before.
   - `placing_client_id` NULL: the request goes to the owner. The owner's fresh open-order snapshot refuses a foreign or ambiguous owner before any send.
   - `UNKNOWN` rows: the activity poller resolves them by orderRef `xo:<submission_id>` when IB shows the order. Check them in TWS.

3. **Stop every other user of the prod block.** No dev stack, worktree api or script may run against the live gateway with base 0. `macmini-prod.sh` (launchd) must stay unloaded. The radon relay uses clientId 900 and is outside the block.

## 3. Deploy

Watchtower replaces `api` when the new `:latest` image appears. The steps are the same for a manual `docker-compose pull api && docker-compose up -d api`.

1. When the old api has stopped, check the gateway for stale sockets (§ 4) **before** the new api connects. With Watchtower the gap is short. To control it, stop `api` by hand first (`docker-compose stop api`, which waits 60s), check, then `docker-compose up -d api`.
2. Wait for `healthy`, then run the post-deploy checks.

## 4. Stale gateway sockets (colima usernet)

Observed on 2026-10-20 (v0.10.0 Watchtower redeploy): the old api's pool sockets stayed `ESTABLISHED` on the mini through colima usernet (`limactl`) after the old container was gone. The gateway kept clientIds 4 and 5 in use. The new pool got IB error 326 (clientId in use) at startup.

With PR-10, a startup 326 on `orders` latches `ORDERS_HALTED` with that cause. This fails closed: no order action is sent.

- **Prevention:** the api disconnects every pool role on SIGTERM (bounded, 35s), uvicorn's request drain is bounded to 10s, and the compose and Watchtower stop budgets are 60s.
- **Check on the mini, before every api (re)start:**

  ```bash
  lsof -nP -iTCP:4001 -sTCP:ESTABLISHED
  ```

  With the api stopped, only the relay (and any other known client) may stay connected. A `limactl` connection that remains after the api container is gone is a stale socket.

- **Clear stale sockets** before you start the api. Restart IB Gateway (it may need IBKR mobile 2FA), or restart colima (this restarts every container). Both options are inferred from the 2026-10-20 incident; neither has been tested as a recovery step.

## 5. After the deploy

1. `curl -fsS http://127.0.0.1:8321/health`:
   - `status` is `ok`, not `orders_halted`;
   - `orders_owner.client_id` is `4`, `halted` is `false`, `halt_reason` is `null`;
   - `ib_pool.orders.connected` is `true` and `ib_pool.orders.broker_readiness` is `ready` (`unknown` until the first open-orders round trip completes; `lost` after IB 1100/2110).
2. Recheck the open orders against a READY broker snapshot: the `/orders` page (or `GET /orders`) must list every order from § 2 step 2, each with its disposition.
3. Optional on paper only: place, modify and cancel one order. The stored `placing_client_id` must be `base + 4`.

## 6. `ORDERS_HALTED` — meaning and recovery

`ORDERS_HALTED` (HTTP 503, `/health` `status: orders_halted`, CRITICAL log) means the `orders` connection was lost for a reason the owner could not prove was a gateway outage, or the owner hit clientId-in-use (326). The owner then makes **zero** reconnect attempts: another process may own clientId `base + 4`, and a second owner must never send orders. Read-only routes still work.

Recovery:

1. Read `orders_owner.halt_reason` in `/health`.
2. Confirm that no other process owns the id: check the stale sockets (§ 4) and stop any dev stack or script that uses base 0.
3. Restart the api (`docker-compose restart api`, which waits 60s). The latch clears only on restart.

A loss that is positively a gateway outage (1100/2110 before the loss, gateway port down, the whole pool down, or a gateway restart by the API) is not a halt. The owner reconnects on its worker with backoff and answers 503 `IB_BROKER_DISCONNECTED` until the broker is READY again.

## 7. Reason codes the user sees

| Code                     | HTTP | Meaning                                                                               |
| ------------------------ | ---- | ------------------------------------------------------------------------------------- |
| `ORDER_NOT_OWNED`        | 409  | Not placed by this environment's `orders` connection. Manage it in TWS / IBKR Mobile. |
| `IB_BROKER_DISCONNECTED` | 503  | IB is not connected to its servers (or the owner is not running). Nothing was sent.   |
| `ORDERS_HALTED`          | 503  | Order actions are halted (§ 6). Nothing was sent.                                     |
| `IB_CONNECTION`          | 503  | The place failed before any send (row FAILED). Nothing was sent.                      |
| `PLACE_OUTCOME_UNKNOWN`  | 502  | The place may have reached IB. Check working orders; do not resubmit.                 |

Known limit: the pre-send check reads the halt latch and the broker readiness that the orders worker has already processed. It is not an atomic network guarantee. A disconnect that IB has not yet reported can still race a send. That send is then reported as unconfirmed (UNKNOWN), never as not sent.
