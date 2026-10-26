# Futu orders — client contract

The wire contract for Futu place / cancel / modify, for the web and any other
client. Operator steps: `docs/runbooks/futu-orders.md`. Policy and internals:
`src/xenon/api/CLAUDE.md` § Futu scopes. Code: `src/xenon/api/services/order_entry.py`
(`_futu_place`, `_futu_cancel`, `_futu_modify`, `futu_trading_status`).

## Scope

Every Futu call carries `?broker=FUTU&account_env=paper|live`. `paper` is the
Futu SIMULATE account, `live` the REAL account. The client never sends an
account number; the server resolves it from its config.

- A mutation without `account_env` → 400 `FUTU_SCOPE_REQUIRED`; another value →
  400 `FUTU_SCOPE_INVALID`.
- A Futu request never falls back to IB.
- Supported: single-leg US stock or option, with the order types, TIF and
  session of § Order types, as far as the env's allowed tuples permit.
  Refused for Futu (403 `READ_ONLY_BROKER`): combo / BAG, replace, combo wizard.

## Order types

Place body fields (FUTU only; an IB body carrying any of them → 400
`INVALID_ORDER_BODY`, never stripped):

| `orderType`     | `limitPrice` | `auxPrice` (trigger) | `trailType` + `trailValue` | `trailSpread` |
| --------------- | ------------ | -------------------- | -------------------------- | ------------- |
| `LMT` (default) | required     | —                    | —                          | —             |
| `MKT`           | —            | —                    | —                          | —             |
| `STP`           | —            | required             | —                          | —             |
| `STP LMT`       | required     | required             | —                          | —             |
| `MIT`           | —            | required             | —                          | —             |
| `LIT`           | required     | required             | —                          | —             |
| `TRAIL`         | —            | —                    | required                   | —             |
| `TRAIL LIMIT`   | —            | —                    | required                   | required      |

"—" means the field must be absent (400 `INVALID_ORDER_BODY`). `TRAIL LIMIT`
has no fixed limit: Futu derives it from the moving trigger and `trailSpread`.
`tif`: `DAY` | `GTC`. `session`: `RTH` (default) | `ETH` | `ALL` | `OVERNIGHT`.

Precision (excess precision is refused, never rounded): `limitPrice` /
`auxPrice` on the Futu price grid (`FUTU_PRICE_PRECISION`) and the IB tick rule;
`trailValue` RATIO = percent, 2 dp, 0 < v ≤ 20; AMOUNT = stock 4 dp / option
2 dp, 0 < v ≤ 20 % of the IB reference price; `trailSpread` 3 dp,
0 ≤ v ≤ 5 % of the reference price.

**Allowed tuples.** A place is accepted only when `(security, orderType, tif,
session)` is in the static Futu set AND in the env's operator allowlist:

- Static set, paper: stock or option `LMT DAY RTH`; stock `MKT DAY RTH`
  (Futu SIMULATE: limit and market only, DAY only).
- Static set, live: stock `LMT` (DAY or GTC, any session); stock `MKT DAY RTH`;
  stock `STP`, `STP LMT`, `MIT`, `LIT`, `TRAIL`, `TRAIL LIMIT` (DAY or GTC, RTH);
  option `LMT`, `STP LMT`, `LIT` (DAY or GTC, RTH). Option MKT / STP / MIT /
  TRAIL / TRAIL LIMIT are never accepted.
- Allowlists (exact `SEC:TYPE:TIF:SESSION` tuples, e.g. `STK:STP LMT:DAY:RTH`):
  `XENON_FUTU_TUPLES_LIVE` (default `STK:LMT:DAY:RTH,STK:LMT:GTC:RTH,
OPT:LMT:DAY:RTH,OPT:LMT:GTC:RTH`, the v0.16 surface),
  `XENON_FUTU_TUPLES_PAPER` (default `STK:LMT:DAY:RTH,OPT:LMT:DAY:RTH,
STK:MKT:DAY:RTH`; paper GTC was withdrawn because Futu SIMULATE is DAY only).
  The operator adds a live tuple only after its probe (runbook § Enable a tuple).
  A working order whose tuple is outside the allowlist keeps cancel; its modify
  is refused with the reason of the failing part.
- Modify per order type: `XENON_FUTU_MODIFY_TYPES` (default `LMT`). Cancel
  works for every type.

Refusals before any IO: `FUTU_ORDER_TYPE_UNSUPPORTED`, `FUTU_TIF_UNSUPPORTED`,
`FUTU_SESSION_UNSUPPORTED` (detail names the failing part).

`GET /health` → `futu.capabilities[env] = {"tuples": [...], "modify": [...]}`
after the allowlists. Clients offer only these.

## Is trading on? `GET /health`

`futu.trading = {"paper": bool, "live": bool}`. True only when the server is not
read-only, the Futu order worker runs, the env is enabled (`XENON_FUTU_TRADING`), the env has an account,
no account-change veto applies, and for live the unlock secret is well-formed.
Show order actions for an env only when its value is true. A wrong password
still reads true; the first REAL order then fails with `FUTU_UNLOCK_FAILED`.

## Place — `POST /orders/place?broker=FUTU&account_env=…`

Body: the IB single-leg place body (`clientAttemptId` required) plus the
fields of § Order types. Order of checks: guard → body and allowed tuple →
Gate 4 (naked-short coverage, Futu scope, by quantity for every type) → IB
quote gate (IB down → 503 `QUOTE_UNAVAILABLE`, nothing sent) → Futu worker.

IB quote gate per type (a sized, non-crossed IB quote is required for every
type, also in ETH / OVERNIGHT):

- `LMT`: the limit band (`LIMIT_OUT_OF_BAND`, overridable with
  `acknowledge_limit_override`).
- `MKT` (stock): US regular session by the market calendar (else
  `FUTU_SESSION_UNSUPPORTED`); spread ≤ 1 % of
  mid (`MKT_SPREAD_TOO_WIDE`); quantity × ask ≤ `XENON_FUTU_MKT_NOTIONAL_CAP`
  (default 25 000 USD, `MKT_NOTIONAL_CAP`). No override.
- `STP` / `MIT`: the trigger must not fire at once: BUY STP above the ask, SELL
  STP below the bid, BUY MIT below the bid, SELL MIT above the ask. Else
  `TRIGGER_WOULD_FIRE`, no override.
- `STP LMT` / `LIT`: the same trigger rule, overridable; the limit is judged
  against the trigger: BUY in [aux, min(aux × 1.05, aux + 2 ticks)], SELL in
  [max(aux × 0.95, aux − 2 ticks), aux], else `LIMIT_OUT_OF_BAND` (overridable).
- `TRAIL` / `TRAIL LIMIT`: the trail bounds of § Order types, plus an AMOUNT
  trail ≤ 20 % and a trail spread ≤ 5 % of the IB mid. Else `FUTU_INVALID_ORDER`,
  no override.

The IB reference price is the IB mid. A zero bid is `QUOTE_UNAVAILABLE` for
every type except `LMT`.

The row stores `limit_price` NULL for types without a fixed limit. The placed
terms are kept as a frozen fingerprint (`legs.futu_intent`); a replay of the
same `clientAttemptId` with a different type, trigger, trail or session is a
body mismatch, never a duplicate.

200:

```json
{
  "status": "ok",
  "broker": "FUTU",
  "accountEnv": "paper",
  "brokerOrderId": "<futu id>",
  "orderId": 0,
  "permId": 0,
  "initialStatus": "<futu status>",
  "state": "<row state>",
  "submission_id": "<id>",
  "message": "Order accepted by Futu"
}
```

Uncertain outcome: the same body as IB `PLACE_OUTCOME_UNKNOWN` (`reason_code`
`PLACE_OUTCOME_UNKNOWN`, `submission_id`). The order may be live at Futu. The
client locks the form and never resubmits; the observation pass binds it. When
Futu accepted the order but the confirming read failed, the body also carries
`brokerOrderId`: the row is UNKNOWN but already cancellable / modifiable.

## Cancel — `POST /orders/cancel?broker=FUTU&account_env=…`

Body: `{"brokerOrderId": "<futu id>"}` (digits). The row must be a working Futu
order in the request scope, else 404 `ORDER_NOT_FOUND` (400
`ORDER_IDENTIFIER_REQUIRED` when missing).

200: `{"status": "ok", "brokerOrderId", "state", "confirmed": bool, "message"}`.
`confirmed: true` only when Futu reports the order CANCELLED. `confirmed: false`
with a terminal `state` (FILLED, REJECTED) means the order ended otherwise and was
not cancelled; with a working state it means sent, not yet reported cancelled (the
row stays working until the observation pass sees it terminal).

## Modify — `POST /orders/modify?broker=FUTU&account_env=…`

Body: `{"brokerOrderId", "modifySequence", "newPrice"?, "newQuantity"?,
"newAuxPrice"?, "newTrailValue"?, "newTrailSpread"?}` (at least one new field;
only the fields the order type has; type, trail type and session never change).
The order type must be in `capabilities[env].modify`, else 400
`FUTU_ORDER_NOT_MODIFIABLE` (also when the type or a required term of an order
made outside Xenon cannot be read). The full outgoing terms pass the place
quote-gate rules of their type before the send, with no override
(`acknowledge_limit_override` is ignored on modify); IB down → 503
`QUOTE_UNAVAILABLE` before the sequence advances. `modifySequence` comes from the open-order row and must grow
(409 `MODIFY_STALE`). An omitted field keeps Futu's current value: the server
reads the order fresh before the send and sends nothing when it cannot. Gate 4
checks and reserves the full outgoing quantity (also for a price-only change)
before the send; a refusal sends nothing (400 coverage code, or 409
`MODIFY_PENDING`).

200: `{"status": "ok", "brokerOrderId", "confirmed": bool, "confirmedQuantity",
"confirmedPrice", "applied_sequence", "message"}`. `confirmed: true` only when a fresh
read shows every sent field: quantity, limit tick (types with a fixed limit),
trigger tick, trail value and spread. A derived broker price (the moving stop
of a trailing order) is never compared. `confirmed: false` = sent, new
terms not reported yet. After an unconfirmed modify every later modify gets 409
`MODIFY_PENDING` (with `applied_sequence`) until an operator resolves it (runbook
§ 5); nothing clears it automatically. Cancel still works.

## Working orders — `GET /orders?broker=FUTU&account_env=…`

`open_orders[]` rows carry `brokerOrderId` (string), `orderId: 0`, `permId: 0`,
`modifySequence`, `orderType` (labels of § Order types), `limitPrice` (null
without a fixed limit), `auxPrice`, `trailType`, `trailValue`, `trailSpread`,
`session`, `tif`, `futuStatus` (raw), `broker`, `accountEnv`, `brokerAccount`.
Cancel any row with a `brokerOrderId`; modify only rows whose `orderType` is in
`capabilities[env].modify`.

## Reason codes (top-level `reason_code`)

| Code                                                                                        | Meaning                                                                     | Client action             |
| ------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- | ------------------------- |
| `PLACE_OUTCOME_UNKNOWN`                                                                     | place may have reached Futu                                                 | lock form, no resubmit    |
| `FUTU_SEND_UNCERTAIN`                                                                       | cancel / modify may have reached Futu                                       | no resend; refresh orders |
| `FUTU_SCOPE_REQUIRED` / `FUTU_SCOPE_INVALID`                                                | `account_env` missing / bad                                                 | client bug                |
| `FUTU_SCOPE_CHANGE_PENDING`                                                                 | account changed with unresolved rows                                        | operator                  |
| `READ_ONLY_BROKER`                                                                          | env not enabled / not configured, or unsupported op                         | hide actions              |
| `READ_ONLY_MODE`                                                                            | server is read-only                                                         | hide actions              |
| `FUTU_ACCOUNT_ABSENT` / `_AMBIGUOUS` / `_ENV_MISMATCH` / `_NOT_ACTIVE` / `_NO_US_AUTHORITY` | OpenD account check failed, nothing sent                                    | operator                  |
| `FUTU_OPEND_UNREACHABLE` / `FUTU_NOT_READY`                                                 | OpenD down, nothing sent                                                    | retry later               |
| `FUTU_WORKER_BUSY` / `FUTU_WORKER_STOPPED`                                                  | worker full / not running, nothing sent                                     | retry later               |
| `FUTU_SEND_WINDOW_CLOSED`                                                                   | modify deadline passed, nothing sent                                        | retry                     |
| `FUTU_ORDER_UNVERIFIED`                                                                     | modify: the fresh order read failed or did not show the order, nothing sent | retry later               |
| `FUTU_ORDER_NOT_WORKING`                                                                    | modify: Futu reports the order terminal, nothing sent                       | refresh orders            |
| `FUTU_UNLOCK_SECRET_MISSING` / `_INVALID` / `FUTU_UNLOCK_FAILED`                            | REAL unlock failed, nothing sent                                            | operator                  |
| `FUTU_PRICE_PRECISION`                                                                      | price off the tick grid (never moved)                                       | fix price                 |
| `FUTU_ORDER_TYPE_UNSUPPORTED` / `FUTU_TIF_UNSUPPORTED` / `FUTU_SESSION_UNSUPPORTED`         | tuple not allowed for this env                                              | hide the choice           |
| `FUTU_ORDER_NOT_MODIFIABLE`                                                                 | modify off for this type, or terms unreadable                               | cancel instead            |
| `TRIGGER_WOULD_FIRE`                                                                        | trigger on the wrong side of the market                                     | fix trigger               |
| `MKT_SPREAD_TOO_WIDE` / `MKT_NOTIONAL_CAP`                                                  | market order refused                                                        | use a limit               |
| `FUTU_INVALID_ORDER` / `FUTU_REMARK_TOO_LONG`                                               | body Futu cannot take                                                       | fix input                 |
| `FUTU_NOT_SENT`                                                                             | refused before send                                                         | retry                     |

The client keys on `reason_code`, not on the HTTP status. Every other code is shared with IB (Gate 4, quote gate, `INVALID_ORDER_BODY`,
`MODIFY_STALE`, `MODIFY_PENDING`, `ORDER_NOT_FOUND`). HTTP status per adapter refusal: `src/xenon/execution/futu_orders.py` (`FutuOrderRefused`).
