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
- Supported: single-leg US stock or option, LIMIT, `DAY` (the IB single-leg body).
  Refused for Futu (403 `READ_ONLY_BROKER`): combo / BAG, replace, combo wizard.

## Is trading on? `GET /health`

`futu.trading = {"paper": bool, "live": bool}`. True only when the server is not
read-only, the Futu order worker runs, the env is enabled (`XENON_FUTU_TRADING`), the env has an account,
no account-change veto applies, and for live the unlock secret is well-formed.
Show order actions for an env only when its value is true. A wrong password
still reads true; the first REAL order then fails with `FUTU_UNLOCK_FAILED`.

## Place — `POST /orders/place?broker=FUTU&account_env=…`

Body: the IB single-leg place body (`clientAttemptId` required). Order of
checks: guard → Gate 4 (naked-short coverage, Futu scope) → IB quote gate (IB
down → 503 `QUOTE_UNAVAILABLE`, nothing sent) → Futu worker.

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

Body: `{"brokerOrderId", "modifySequence", "newPrice"?, "newQuantity"?}` (one of
the two at least). `modifySequence` comes from the open-order row and must grow
(409 `MODIFY_STALE`). An omitted field keeps Futu's current value: the server
reads the order fresh before the send and sends nothing when it cannot.

200: `{"status": "ok", "brokerOrderId", "confirmed": bool, "confirmedQuantity",
"confirmedPrice", "applied_sequence", "message"}`. `confirmed: true` only when a fresh
read shows the sent quantity and the same price tick. `confirmed: false` = sent, new
terms not reported yet. After an unconfirmed quantity change every later modify
gets 409 `MODIFY_PENDING` (with `applied_sequence`) until an observation pass
after the send window shows the new quantity, or an operator resolves it; cancel
still works.

## Working orders — `GET /orders?broker=FUTU&account_env=…`

`open_orders[]` rows carry `brokerOrderId` (string), `orderId: 0`, `permId: 0`,
`modifySequence`, `orderType`, `futuStatus`, `broker`, `accountEnv`,
`brokerAccount`. Cancel / modify only rows with `orderType: "LMT"` and a
`brokerOrderId`.

## Reason codes (top-level `reason_code`)

| Code | Meaning | Client action |
| --- | --- | --- |
| `PLACE_OUTCOME_UNKNOWN` | place may have reached Futu | lock form, no resubmit |
| `FUTU_SEND_UNCERTAIN` | cancel / modify may have reached Futu | no resend; refresh orders |
| `FUTU_SCOPE_REQUIRED` / `FUTU_SCOPE_INVALID` | `account_env` missing / bad | client bug |
| `FUTU_SCOPE_CHANGE_PENDING` | account changed with unresolved rows | operator |
| `READ_ONLY_BROKER` | env not enabled / not configured, or unsupported op | hide actions |
| `READ_ONLY_MODE` | server is read-only | hide actions |
| `FUTU_ACCOUNT_ABSENT` / `_AMBIGUOUS` / `_ENV_MISMATCH` / `_NOT_ACTIVE` / `_NO_US_AUTHORITY` | OpenD account check failed, nothing sent | operator |
| `FUTU_OPEND_UNREACHABLE` / `FUTU_NOT_READY` | OpenD down, nothing sent | retry later |
| `FUTU_WORKER_BUSY` / `FUTU_WORKER_STOPPED` | worker full / not running, nothing sent | retry later |
| `FUTU_SEND_WINDOW_CLOSED` | modify deadline passed, nothing sent | retry |
| `FUTU_ORDER_UNVERIFIED` | modify: the fresh order read failed or did not show the order, nothing sent | retry later |
| `FUTU_ORDER_NOT_WORKING` | modify: Futu reports the order terminal, nothing sent | refresh orders |
| `FUTU_MODIFY_QTY_UNRESERVED` | price-only modify: Futu quantity above the reserved one, nothing sent | refresh orders, retry |
| `FUTU_UNLOCK_SECRET_MISSING` / `_INVALID` / `FUTU_UNLOCK_FAILED` | REAL unlock failed, nothing sent | operator |
| `FUTU_PRICE_PRECISION` | price off the tick grid (never moved) | fix price |
| `FUTU_ORDER_TYPE_UNSUPPORTED` / `FUTU_INVALID_ORDER` / `FUTU_REMARK_TOO_LONG` | body Futu cannot take | fix input |
| `FUTU_NOT_SENT` | refused before send | retry |

The client keys on `reason_code`, not on the HTTP status. Every other code is shared with IB (Gate 4, quote gate, `INVALID_ORDER_BODY`,
`MODIFY_STALE`, `MODIFY_PENDING`, `ORDER_NOT_FOUND`). HTTP status per adapter refusal: `src/xenon/execution/futu_orders.py` (`FutuOrderRefused`).
