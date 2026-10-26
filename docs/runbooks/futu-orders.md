# Futu orders — operator runbook

Futu order dispatch (X3c, order types X5): single-leg US stock and option
orders on a Futu account, through
`POST /orders/place|cancel|modify?broker=FUTU&account_env=paper|live`. Order
types, fields and reason codes: contract `docs/architecture/futu-orders.md`.
Replace, combo and wizard orders are refused for Futu. A Futu request never
falls back to IB. Code: `src/xenon/execution/futu_orders.py` (adapter),
`src/xenon/api/futu_order_worker.py` (worker), `src/xenon/api/services/order_entry.py`
(dispatch). Policy: `src/xenon/api/CLAUDE.md` § Futu scopes; incident history row 42.

## 1. Turn Futu trading on

The api reads these from `/opt/xenon/.env` on the macmini (`env_file` of the
`api` service in `deploy/compose.prod.yml`). Dev sessions read the repo `.env`.

| Variable                     | Value                                             | Needed for          |
| ---------------------------- | ------------------------------------------------- | ------------------- |
| `FUTU_TRADE_ACC_ID_SIMULATE` | the SIMULATE (paper) Futu account id, digits only | `account_env=paper` |
| `FUTU_TRADE_ACC_ID_REAL`     | the REAL (live) Futu account id, digits only      | `account_env=live`  |
| `XENON_FUTU_TRADING`         | `simulate`, `real` or `simulate,real`             | any Futu order      |
| `FUTU_TRADE_PWD_MD5`         | MD5 of the Futu trade password, 32 hex characters | every REAL mutation |

1. Set the account id of each env you want to trade. An env without an account
   reads empty lists and refuses orders.
2. Set `XENON_FUTU_TRADING` to the envs to trade. Unset or empty means off. Any
   other value (also the old `1`) means off for both envs and logs one ERROR.
3. For REAL: set `FUTU_TRADE_PWD_MD5`. The adapter unlocks trading with it before
   every REAL place, modify and cancel; SIMULATE never unlocks. The value is never
   logged or returned. Do not put it in the `futu-quotes` service env.
4. Restart the api with the controlled command (`docs/runbooks/remote-deploy.md`
   § Standard release flow): `docker-compose up -d --pull never api web realtime futu-quotes`.
   The worker starts only when at least one env is both enabled and configured,
   and never under `XENON_READ_ONLY=1`.
5. Check `GET /health` → `futu.trading`:

   ```json
   "trading": {"paper": true, "live": false}
   ```

   A value is true only when: not `XENON_READ_ONLY=1`; the Futu order worker
   runs; the env is in
   `XENON_FUTU_TRADING`; the env has a configured account; no
   `FUTU_SCOPE_CHANGE_PENDING` veto applies; and for live, `FUTU_TRADE_PWD_MD5` is
   32 hex characters (format check only, no unlock call). The web order ticket
   hides Futu order actions for an env that is false. A wrong password still shows
   true; the first REAL order then fails with 403 `FUTU_UNLOCK_FAILED`, nothing sent.

   The same block lists `capabilities`: per env the allowed tuples and the
   modify types after the allowlists (§ 7). Clients offer only these.

To turn trading off: remove the env from `XENON_FUTU_TRADING` and restart the api.
Observation (positions, orders, fills) continues without it.

## 2. What an order does

1. Guard: read-only → capability (env enabled and configured) → scope-change veto.
2. Body and allowed tuple (§ 7): fields of the order type, precision, tuple in
   the env's allowlist. A refusal reserves nothing.
3. Gate 4 coverage (naked-short guard) in the Futu request scope; the row is
   reserved PENDING with broker FUTU, the request scope, `legs.futu_code`, the
   frozen placement fingerprint `legs.futu_intent` and the current terms
   `legs.futu_terms`. `limit_price` is NULL for types without a fixed limit.
4. IB quote gate per order type (contract § Place): the limit band for LMT;
   MKT regular session, spread and notional cap (`XENON_FUTU_MKT_NOTIONAL_CAP`,
   default 25 000 USD); trigger side for STP / MIT / STP LMT / LIT; trail
   bounds. IB down → 503 `QUOTE_UNAVAILABLE`. A refusal marks the row REJECTED;
   nothing is sent to Futu.
5. The worker checks the account (`get_acc_list` + `verify_live`), then sends
   with remark `xo:<submission_id>`.
6. Only fresh Futu evidence changes the row (`orders_store.mirror_futu_order`):
   an accepted place is bound to the Futu order id; a cancel is CANCELLED only when
   Futu reports it; a modify changes quantity / price / `futu_terms` only when Futu
   reports every sent field. The observation pass stores what Futu reports (type
   label, trigger, trail, session, raw status) in `legs.futu_order`, never in
   `futu_intent` or `futu_terms`.
7. An untriggered order Futu reports SUBMITTED is WORKING. WAITING_SUBMIT stays
   UNKNOWN: it is listed in open orders with `futuStatus`, Gate 4 keeps its
   demand, and cancel works.

## 3. Resolve an UNKNOWN row

A place whose outcome Futu did not confirm is UNKNOWN (`PLACE_OUTCOME_UNKNOWN`).
The worker already searched for the exact remark once. It never re-sends.
Gate 4 keeps the reservation until the row is resolved.

1. Find the row: `/health` futu `held_rows`, or `GET /orders?broker=FUTU&account_env=…`.
2. In the Futu app (same account and env), look for an order with remark
   `xo:<submission_id>`.
3. Order found: the observation pass binds it by remark within one cycle (60 s)
   while the order is inside its history window (about one day). After that, bind
   it by hand: set `broker_order_id` to the Futu order id on that row; the next
   pass sets state and fills.
4. No order found after the trading day: set the row FAILED by hand
   (`state='FAILED'`, `reason_code='FUTU_NOT_SENT_OPERATOR'`). Do this only after
   the Futu app proves that no order carries the remark.

Do not place the order again before step 2 is done.

## 4. Resolve a FUTU_FILL_UNPRICED / FUTU_FILL_CONFLICT row

The observation pass saw a cumulative fill increase that it could not price
(`FUTU_FILL_UNPRICED`) or whose exec id belongs to another submission
(`FUTU_FILL_CONFLICT`). The row stays non-terminal and the scope's passes are
incomplete, so Gate 4 fails closed for that scope.

1. Check the order's fills in the Futu app.
2. Record the missing fill by hand in `order_fills` with the scoped exec id
   `futu:<account_env>:<acc_id>:ord:<order_id>:<cum_qty>`, and set `filled_qty` on the row,
   or set the row terminal by hand when the app proves its final state.
3. The next complete pass clears the reason code.

## 5. MODIFY_PENDING after an unconfirmed Futu modify

Every Futu modify reserves its full outgoing quantity in Gate 4
(`pending_amend_qty`, also for a price-only modify). A modify that Futu did not
confirm keeps that amend: Gate 4 reserves the larger of the old and the
outgoing quantity, and every later modify gets 409 `MODIFY_PENDING`. Cancel
still works.

Nothing clears the amend by itself. The observation pass does not confirm or
expire it: the SDK can still dispatch a modify after its send window closed
(account check and connection wait run after the fence), so a later read does
not prove the change can no longer reach Futu.

Never clear the amend while the api process that sent the modify still runs:
a blocked SDK call can still send the old outgoing quantity after the clear,
on top of new demand that Gate 4 accepted against the freed reservation.

Preferred: cancel the order from Xenon. The reservation stays until the
observation pass sees the order terminal (CANCELLED or FILLED); a late modify
cannot revive a terminal order. Nothing else is needed.

To keep the order working instead:

1. Stop the api container and confirm it has exited (`docker inspect -f
   '{{.State.Running}}' <api container>` prints `false`). This ends the Futu
   order worker and any SDK call it holds; a timeout, a passed `send_by`, a later
   app read or `FutuOrderWorker.stop()` alone is not enough.
2. Wait one minute, then read the order's quantity, price and filled quantity in
   the Futu app (same account and env); for a trigger or trailing order also
   the trigger price, the trail value and the trail spread.
3. With the api still stopped, update the one row in one transaction, scoped
   exactly (`submission_id`, `broker='FUTU'`, `account_env`, `broker_account`,
   and `legs->>'pending_amend_id'` = the generation you read):
   - set `quantity` and, for a type with a fixed limit, `limit_price` to the app
     values (a type without a fixed limit keeps `limit_price` NULL);
   - set `legs.futu_terms` = `{"aux_price", "trail_value", "trail_spread"}` to the
     app values as plain decimal strings (`null` for a term the type does not
     have);
   - remove only the transient keys `pending_amend_id`, `pending_amend_qty`,
     `pending_amend_opened_at`, `pending_amend_send_by` and
     `pending_amend_deadline` from `legs`.

   Never change `legs.futu_intent`: it is the placement fingerprint that a
   replay of the same `clientAttemptId` is compared with. If the row no longer
   matches, stop and re-read.
4. Start the api (§ 1 step 4). The next observation pass records fills.

If you cannot stop the api, keep the reservation and cancel instead.

## 6. Modify sends the broker's current terms

Futu `modify_order` sends the full terms together: quantity, price and, by
type, trigger and trail. Before the send, the worker reads the order fresh in
the request scope; a field the request omits (`newPrice`, `newQuantity`,
`newAuxPrice`, `newTrailValue`, `newTrailSpread`) takes Futu's current value (a
change made in the Futu app is kept). The full outgoing terms then pass the
place quote rules of their type (IB quote fetched before the job; no
override on modify), and Gate 4 checks and reserves the outgoing quantity under
the account-scope lock. Nothing is sent when the IB quote is missing (503
`QUOTE_UNAVAILABLE`), the read fails or does not show the order (503
`FUTU_ORDER_UNVERIFIED`), Futu reports the order terminal (409
`FUTU_ORDER_NOT_WORKING`), the type or a required term of an order made outside
Xenon cannot be read (400 `FUTU_ORDER_NOT_MODIFIABLE`), a quote rule refuses the
terms (400 with its code), or Gate 4 refuses the outgoing quantity (400 with
the coverage reason code, or 409 `MODIFY_PENDING`). A refusal before the send
leaves the row and its reservation as they were.

## 7. Enable a tuple

A tuple is `SEC:TYPE:TIF:SESSION`, for example `STK:STP LMT:DAY:RTH`. The api
accepts a place only when its tuple is in the static Futu set (contract § Order
types) and in the env's allowlist:

| Variable                  | Default                                                           |
| ------------------------- | ----------------------------------------------------------------- |
| `XENON_FUTU_TUPLES_LIVE`  | `STK:LMT:DAY:RTH,STK:LMT:GTC:RTH,OPT:LMT:DAY:RTH,OPT:LMT:GTC:RTH` |
| `XENON_FUTU_TUPLES_PAPER` | `STK:LMT:DAY:RTH,OPT:LMT:DAY:RTH,STK:MKT:DAY:RTH`                 |
| `XENON_FUTU_MODIFY_TYPES` | `LMT` (modify per order type; cancel works for every type)        |

An entry that does not parse turns the env's whole list off (one ERROR log); an
entry outside the static set is dropped (one ERROR log). One tuple enables only
itself: GTC, another session and OPT are separate tuples, and a stock probe never
counts for an option tuple. A working order whose tuple leaves the allowlist
keeps cancel and its demand; its modify is refused.

Add a live tuple only through this probe, one tuple at a time:

1. Get the user's explicit go-ahead for this one tuple. REAL needs
   `FUTU_TRADE_PWD_MD5` (§ 1).
2. State the probe order before it is sent, by security type:
   - STK: 1 share.
   - OPT: a whole number of contracts that the user approved by name, with the
     contract (symbol, expiry, right, strike), the multiplier, the possible fill
     and the assignment exposure. Without that approval, OPT tuples stay off.
   - Both: how a filled position is closed. A trigger far from the market can
     still fill on a gap.
3. Add the tuple to `XENON_FUTU_TUPLES_LIVE` (and, if its modify is to be
   probed, the type to `XENON_FUTU_MODIFY_TYPES`). Restart the api with the
   controlled command (§ 1 step 4). Check `GET /health` → `futu.capabilities.live`
   lists the tuple.
4. Place one order through a probe script. The script asserts that the
   security type, unit (shares or the approved contracts) and tuple match the
   approval before it sends.
5. Check the fresh read in the Futu app and in `GET /orders?broker=FUTU&account_env=live`:
   the type (`orderType` label and raw Futu type in `legs.futu_order`), the
   trigger placement, `limitPrice` null for a type without a fixed limit, the
   trail units (RATIO = percent, AMOUNT = price), TIF and session.
6. If modify is probed: change one field and check that the reply is
   `confirmed: true` and the app shows the new value.
7. Cancel the order. Close any filled position as stated in step 2.
8. Keep the tuple, or remove it and restart. Record the evidence (date, tuple,
   order id, result, cancel / close) in the probe log below.

Probe log:

| Date | Tuple | Futu order id | Result | Closed by |
| ---- | ----- | ------------- | ------ | --------- |
