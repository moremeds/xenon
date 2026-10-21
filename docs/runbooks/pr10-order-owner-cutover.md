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

1. **Apply the compose change first.** Watchtower stops containers with its own timeout (default 10s), not with the service's grace period. Stage the new `deploy/compose.prod.yml` with `deploy/README.md` § Operator rollout checklist, section A, **before** the PR-10 image is published, and apply it with the controlled sequence from section A step 6 (stop `api realtime` with `-t 60`, socket check, then `mv` and `docker-compose up -d --pull never api web realtime`); a bare `up -d` would recreate the api before the check. Otherwise Watchtower replaces the old api with a 10s stop and can leave stale sockets (§ 4). Section A checks, in this order:
   - `WATCHTOWER_NOTIFICATION_URL` is defined in `/opt/xenon/.env` (`grep -c`, never print the value);
   - `docker-compose config` renders the PR-10 values: `XENON_IB_CLIENT_ID_BASE` `"0"` on `api` and `realtime`, `IB_REALTIME_STATUS_URL`, `api` `stop_grace_period` 60s, `WATCHTOWER_TIMEOUT` `60s`, and a non-empty webhook;
   - the diff against the live file shows only those changes plus `WATCHTOWER_NOTIFICATION_URL` becoming `${WATCHTOWER_NOTIFICATION_URL}` interpolation and comment lines. Explain any other difference before you apply; if you cannot, stop.
2. **List the open orders that the new build will refuse, and save the list.** On the mini, read-only against `core_dev` (the read-only session setting refuses any write):

   Connection: the commands below use `/opt/xenon/.pgpass-refresh`, which exists only after the refresh-core-test job is installed (`deploy/README.md`). Check first with `test -f /opt/xenon/.pgpass-refresh && echo present`. If it is missing, run the same SQL from an operator machine with the `core_dev` URL instead (strip any `+psycopg` / `+asyncpg` driver suffix): `PGOPTIONS='-c default_transaction_read_only=on' psql "<core_dev URL>" --csv -c "<same SQL>"`. Never print the URL.

   ```bash
   cd /opt/xenon
   PGOPTIONS='-c default_transaction_read_only=on' PGHOST=localhost PGUSER=xenon_dev \
   PGPASSFILE=/opt/xenon/.pgpass-refresh psql core_dev --csv -c "
     SELECT submission_id, ticker, action, quantity, state,
            placing_client_id, ib_order_id, perm_id, submitted_at
     FROM xenon.order_submissions
     WHERE broker = 'IB' AND account_env = 'live'
       AND state IN ('PENDING', 'WORKING', 'PARTIALLY_FILLED', 'UNKNOWN')
       AND placing_client_id IS DISTINCT FROM 4
     ORDER BY submitted_at" > pr10-predeploy-inventory.csv
   ```

   Add a `disposition` for every row in a copy of the file (`pr10-predeploy-dispositions.csv`): `cancel-before-deploy`, `manage-in-TWS`, or `unresolved-UNKNOWN`. § 5 step 2 re-checks every saved `submission_id`.

   - `placing_client_id` 20–49 (old place subprocess) or 26 (old fallback id): after the deploy, cancel and modify answer 409 `ORDER_NOT_OWNED`. Give every row a disposition before the deploy: cancel it now with the old build, or plan to manage it in TWS / IBKR Mobile. "Operator manages it in TWS" is a workflow; the order does not transfer to the owner.
   - `placing_client_id` 0–2, 10–19, or another block: also 409, as before.
   - `placing_client_id` NULL: the request goes to the owner. The owner's fresh open-order snapshot refuses a foreign or ambiguous owner before any send.
   - `UNKNOWN` rows: the activity poller resolves them by orderRef `xo:<submission_id>` when IB shows the order. Check them in TWS.

3. **Stop every other user of the prod block.** No dev stack, worktree api or script may run against the live gateway with base 0. `macmini-prod.sh` (launchd) must stay unloaded. The radon relay uses clientId 900 and is outside the block.

## 3. Deploy

Use the controlled cutover in `docs/runbooks/remote-deploy.md` § Standard release flow: stop Watchtower before the tag push, pull after `release.yml` finishes, `docker-compose stop -t 60 api realtime`, run the stale-socket check (§ 4) **before** the new api connects, then `docker-compose up -d --pull never api web realtime`, and resume Watchtower only at step 10. Left running, Watchtower would replace `api` on its own when the new `:latest` image appears, with no socket check in between.

1. Wait for `healthy`, then run the post-deploy checks (§ 5).

## 4. Stale gateway sockets (colima usernet)

Observed on 2026-10-20 (v0.10.0 Watchtower redeploy): the old api's pool sockets stayed `ESTABLISHED` on the mini through colima usernet (`limactl`) after the old container was gone. The gateway kept clientIds 4 and 5 in use. The new pool got IB error 326 (clientId in use) at startup.

With PR-10, a startup 326 on `orders` latches `ORDERS_HALTED` with that cause. This fails closed: no order action is sent.

- **Prevention:** on SIGTERM the api stops the order owner and queues a disconnect for every pool role first (waits up to 35s; a queued disconnect is never dropped), uvicorn's request drain is bounded to 10s, other cleanup to 10s, and the compose and Watchtower stop budgets are 60s. Limit: an order core blocked on a broker call can outlive the budget; SIGKILL then ends the process and its socket may stay open — hence the check below.
- **Check on the mini, before every api (re)start:**

  ```bash
  lsof -nP -iTCP:4001 -sTCP:ESTABLISHED
  ```

  `lsof` shows processes and local ports, not IB clientIds, and the realtime relay also connects through colima (`limactl`). Attribute every connection to the known client inventory before you act:
  - run the check once while the old api is still up and save it (expected: the api pool's 3 connections plus the relay and any other known client; on 2026-10-20 the gateway had 4 API clients: pool 3/4/5 and relay 10);
  - run it again after the api container is gone. Expected: the count drops by the api's 3 connections.
  - A `limactl` connection that should have closed but did not is a **suspected** stale socket until gateway evidence (its API client list or log, if available) or the count difference attributes it to the old api.

- **Clear stale sockets** only when they are attributed to the old api. Restart IB Gateway (it may need IBKR mobile 2FA), or restart colima (this restarts every container). Do not restart these shared services on the process name alone. Both options are inferred from the 2026-10-20 incident; neither has been tested as a recovery step.

## 5. After the deploy

1. `curl -fsS http://127.0.0.1:8321/health`:
   - `status` is `ok`, not `orders_halted`;
   - `orders_owner.client_id` is `4`, `halted` is `false`, `halt_reason` is `null`;
   - `ib_pool.orders.connected` is `true` and `ib_pool.orders.broker_readiness` is `ready` (`unknown` until the first open-orders round trip completes; `lost` after IB 1100/2110).
2. Reconcile the saved inventory, row by row. The `/orders` page is not an exhaustive check (one account scope, no UNKNOWN rows, at most 200 open rows). Re-query exactly the saved ids, read-only:

   ```bash
   ids=$(tail -n +2 pr10-predeploy-inventory.csv | cut -d, -f1 | sed "s/.*/'&'/" | paste -sd, -)
   PGOPTIONS='-c default_transaction_read_only=on' PGHOST=localhost PGUSER=xenon_dev \
   PGPASSFILE=/opt/xenon/.pgpass-refresh psql core_dev --csv -c "
     SELECT submission_id, state, reason_code, placing_client_id, ib_order_id, perm_id, filled_qty
     FROM xenon.order_submissions WHERE submission_id IN ($ids) ORDER BY submitted_at" > pr10-postdeploy-inventory.csv
   ```

   (Skip the query when the inventory is empty.) For every row, compare its state with its disposition and with a broker snapshot taken while `ib_pool.orders.broker_readiness` is `ready` (or TWS): `cancel-before-deploy` rows are terminal and absent at IB; `manage-in-TWS` rows match the order TWS shows. An `UNKNOWN` row stays unresolved until real evidence (an IB order or fill with orderRef `xo:<submission_id>`, or TWS) settles it; absence from a page is not evidence.
3. Optional on paper only: place, modify and cancel one order. The stored `placing_client_id` must be `base + 4`.

## 6. `ORDERS_HALTED` — meaning and recovery

`ORDERS_HALTED` (HTTP 503, `/health` `status: orders_halted`, CRITICAL log) means the `orders` connection was lost for a reason the owner could not prove was a gateway outage, or the owner hit clientId-in-use (326). The owner then makes **zero** reconnect attempts: another process may own clientId `base + 4`, and a second owner must never send orders. Read-only routes still work.

Recovery:

1. Read `orders_owner.halt_reason` in `/health`.
2. Confirm that no other process owns the id: check the stale sockets (§ 4) and stop any dev stack or script that uses base 0.
3. Restart the api (`docker-compose restart api`, which waits 60s). The latch clears only on restart.

A loss that is positively a gateway outage (1100/2110 before the loss, gateway port down, the whole pool down, or a gateway restart by the API) is not a halt. The owner reconnects on its worker with backoff and answers 503 `IB_BROKER_DISCONNECTED` until the broker is READY again.

## 7. Rollback to the previous build

Restoring an image is not an ownership transfer. The previous build manages an order by reconnecting a subprocess as the order's recorded clientId; for an order placed by PR-10 that id is 4, which the previous build's own pool `orders` connection also uses. Do not count on the previous build to cancel or modify orders that PR-10 placed.

1. **Pause Watchtower:** `docker stop xenon-watchtower-1`; `docker inspect -f '{{.State.Running}}' xenon-watchtower-1` prints `false`.
2. **Inventory the orders PR-10 placed or may have placed**, read-only, and save it (same connection settings as § 2 step 2; `<CUTOVER>` = when the PR-10 api first started, UTC):

   ```bash
   PGOPTIONS='-c default_transaction_read_only=on' PGHOST=localhost PGUSER=xenon_dev \
   PGPASSFILE=/opt/xenon/.pgpass-refresh psql core_dev --csv -c "
     SELECT submission_id, ticker, action, quantity, state, placing_client_id,
            ib_order_id, perm_id, submitted_at,
            'xo:' || submission_id AS order_ref
     FROM xenon.order_submissions
     WHERE broker = 'IB' AND account_env = 'live'
       AND state IN ('PENDING', 'WORKING', 'PARTIALLY_FILLED', 'UNKNOWN')
       AND (placing_client_id = 4
            OR (placing_client_id IS NULL AND submitted_at >= '<CUTOVER>'))
     ORDER BY submission_id" > pr10-rollback-inventory.csv
   ```

   - `placing_client_id = 4`: placed by the PR-10 owner.
   - `placing_client_id IS NULL` after the cutover: **potentially** PR-10-owned, not proven. A place that hit its request deadline stays PENDING / UNKNOWN with no placer id until the worker or the poller writes it, but IB may already hold the order. Reconcile each such row by its `order_ref` (`xo:<submission_id>`) against TWS or a READY broker snapshot: an IB order with that orderRef is PR-10-owned; no order is not proof that nothing was sent.

   Give every row a disposition: cancel it now through the current (PR-10) api, only if you intend to, or manage it in TWS / IBKR Mobile after the rollback. Keep `UNKNOWN` rows and their reservations as they are; never edit or clear rows to make a downgrade easier.
3. **Pin and verify the previous image** (`PREV` = previous release version without the `v`): `docker pull ghcr.io/moremeds/xenon-api:PREV`, then check its version / revision labels and digest as in `deploy/README.md` § D step 2. Point the local alias at it: `docker tag ghcr.io/moremeds/xenon-api:PREV ghcr.io/moremeds/xenon-api:latest` (same for `web` / `realtime` if they changed). Restore the previous compose file from its backup (`cp -p compose.yml.bak-<stamp> compose.yml`).
4. **Stop, re-inventory, check, start:**
   1. `docker-compose stop -t 60 api`. From here no PR-10 code can send.
   2. Before the old image starts, run the step-2 query again into `pr10-rollback-inventory-final.csv` and compare: `diff pr10-rollback-inventory.csv pr10-rollback-inventory-final.csv`. A new row, or a row whose state or ids changed, is an order that was in flight during step 2: give it a disposition too (reconcile NULL-placer rows by orderRef as in step 2). Do not start the old image until every row in the final list has one.
   3. Run the stale-socket check (§ 4).
   4. `docker-compose up -d --pull never api`; then `/health` (`ib_pool.*.connected`). Reconcile the final inventory as in § 5 step 2.
5. **Resume Watchtower** (`docker start xenon-watchtower-1`) only when the registry `:latest` is no longer a PR-10 image (a release that reverts PR-10 is published). Until then Watchtower would pull PR-10 again; keep it stopped.

## 8. Reason codes the user sees

| Code                     | HTTP | Meaning                                                                               |
| ------------------------ | ---- | ------------------------------------------------------------------------------------- |
| `ORDER_NOT_OWNED`        | 409  | Not placed by this environment's `orders` connection. Manage it in TWS / IBKR Mobile. |
| `IB_BROKER_DISCONNECTED` | 503  | IB is not connected to its servers (or the owner is not running). Nothing was sent.   |
| `ORDERS_HALTED`          | 503  | Order actions are halted (§ 6). Nothing was sent.                                     |
| `IB_CONNECTION`          | 503  | The place failed before any send (row FAILED). Nothing was sent.                      |
| `PLACE_OUTCOME_UNKNOWN`  | 502  | The place may have reached IB. Check working orders; do not resubmit.                 |

Known limit: the pre-send check reads the halt latch and the broker readiness that the orders worker has already processed. It is not an atomic network guarantee. A disconnect that IB has not yet reported can still race a send. That send is then reported as unconfirmed (UNKNOWN), never as not sent.
