# futu-quotes — Futu OpenD quote feed for the realtime relay

`xenon-futu-quotes` streams US stock/ETF `SubType.QUOTE` subscriptions from a
local Futu OpenD into the `ib_realtime` relay over one internal WebSocket, so
live ticking prices come from Futu instead of overloading the IB realtime
relay. The relay is the service's only client; browsers and ordinary API-key
connections stay on IB. Opt-in is `?quotes=futu-first` on the relay
connection. Authoritative protocol/architecture contract:
`docs/architecture/futu-quotes.md`.

**Scope guard:** quotes only. No orders, no ORDER_BOOK/TICKER subtypes, no
non-US symbols, no extended-hours symbols — those stay on IB. Indexes and
options are rejected `not_supported`.

## Run

```bash
# package entry point (registered by the integration PR):
xenon-futu-quotes [--host 0.0.0.0] [--port 8767] [--log-level INFO]

# same thing without the console script:
uv run python -m xenon.futu_quotes
```

## Env vars

| Var                | Default        | Meaning                                             |
| ------------------ | -------------- | --------------------------------------------------- |
| `FUTU_QUOTES_HOST` | `0.0.0.0`      | listen address (internal only — not host-published) |
| `FUTU_QUOTES_PORT` | `8767`         | listen port                                         |
| `FUTU_QUOTE_QUOTA` | `800`          | local admission ceiling cap                         |
| `FUTU_OPEND_HOST`  | `127.0.0.1`    | Futu OpenD host                                     |
| `FUTU_OPEND_PORT`  | `11111`        | Futu OpenD port                                     |

## Wire protocol

Inbound: `{"action": "want", "gen": N, "symbols": [...]}` — the complete
desired set; gen is a per-connection monotonic counter starting at 1.

Outbound:

- `assign` — `{ok, pending[{symbol,reason}], rejected[{symbol,reason}]}`, a
  disjoint partition of the wanted set. pending reasons: `subscribing`,
  `session`, `opend_down`, `backoff`. rejected reasons: `quota`,
  `unknown_symbol`, `not_supported`, `provider_error`.
- `batch` — `{updates: {symbol: PriceData}}`, last-value coalesced, at most
  one batch per 100 ms. Every update carries `source: "futu"` and a UTC
  `timestamp` derived from Futu's ET wall clock (DST-safe). A per-symbol
  high-water mark drops replayed stale quotes.
- `health` — `{opend_connected, quota: {ceiling, own_used, all_used,
  remain}}`, every 5 s and on change. Relay treats 15 s without `health`
  as Futu-down.

Liveness uses WebSocket protocol ping/pong: ping every 10 s, pong timeout
20 s. One active connection: a new client replaces the old (gen restarts).

## Health, session, failover

- `opend_connected` tracks the OpenD socket; on loss the service emits
  `health.opend_connected=false` and an `assign` with empty `ok`
  (`pending/opend_down`), then reconnects with 1→30 s backoff and
  **resubscribes the complete desired set** (subscriptions are never assumed
  to survive reconnect).
- US session is open only when `get_global_state().market_us` is `MORNING`
  or `AFTERNOON`, polled every 30 s; unknown/failed counts as closed and
  wanted symbols sit at `pending/session`.
- Unsubscribe discipline: a symbol is unsubscribed only after it leaves the
  desired set, ≥60 s after subscribe plus a further 60 s grace; "too early"
  provider rejections are retried. Re-adds inside the grace window cancel
  the unsubscribe.
- Operation fencing: every provider call is reserved per symbol
  (`subscribing`/`unsub_inflight`), re-validated under the serialized
  adapter lock immediately before the SDK call, and bound to the OpenD
  session epoch at issue — results from a dead session are discarded and
  never certify the replacement session. A failed unsubscribe never frees
  the local record (the provider may still hold and count it); retries are
  bounded-escalated. On reconnect, desired symbols believed subscribed are
  *reconfirmed* idempotently without reserving a second quota unit, while
  genuinely new admissions still respect the ceiling.
- Buffered quotes are dropped the moment a symbol leaves `ok`, and the
  whole coalesce window is cleared on relay-owner replacement and on OpenD
  loss — a stale tick can never reach a new owner or a re-added symbol.
- Snapshot verification batches fall back to per-code calls when the batch
  call fails: OpenD reports whole-call errors only, so one invalid code
  cannot reject its batch-mates.

## Quota

Futu subscription quota is checked two ways:

- **Local ceiling**: `min(FUTU_QUOTE_QUOTA, own_used + remain)`, recomputed
  from `query_subscription(is_all_conn=True)` on connect/reconnect, after
  any provider rejection, and every 5 min. Symbols over the ceiling are
  `rejected/quota` and retried with bounded backoff.
- **Provider rejection is authoritative** — a `quota` error from
  `subscribe()` rejects the symbol and triggers an immediate quota refresh
  even when the local count is below the ceiling.

To inspect quota directly against OpenD:

```bash
uv run python - <<'EOF'
from futu import OpenQuoteContext, RET_OK
ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
ret, data = ctx.query_subscription(is_all_conn=True)
print(data if ret == RET_OK else f"ERR {data}")
ctx.close()
EOF
```

`remain`/`own_used`/`total_used` feed the ceiling; `sub_list` merges all
OpenD connections, so it is **never** used for per-symbol attribution —
this service's own subscribe records are the ownership truth.

## Smoke test

```bash
uv run python -m xenon.futu_quotes --self-test   # or: xenon-futu-quotes --self-test
```

Runs the scripted fake-OpenD scenario over a real loopback WebSocket —
connect → `want`/`assign` (incl. `rejected/unknown_symbol`) → tick/`batch`
→ OpenD loss → `health.opend_connected=false`. Prints `SELF-TEST PASS`,
exit 0; `SELF-TEST FAIL: <detail>`, exit 1. No live OpenD or network needed.

The **live** smoke test against a real OpenD runs on the mini (not the
laptop): start the service, connect the relay with `?quotes=futu-first`,
and confirm ticks flow for a liquid symbol.

## Diagnostics

| Symptom                                   | Check                                                                                          |
| ----------------------------------------- | ---------------------------------------------------------------------------------------------- |
| All symbols `pending/session`             | `market_us` is not MORNING/AFTERNOON, or `get_global_state` is failing (OpenD up but degraded)   |
| All symbols `pending/opend_down`          | OpenD socket dead — look for `OpenD connection lost`; supervisor reconnects automatically        |
| Symbols `rejected/quota`                  | `health.quota` block + the `query_subscription` snippet above; raise `FUTU_QUOTE_QUOTA` only if provider headroom exists |
| Symbol `rejected/unknown_symbol`          | Futu code spelling (`US.<ticker>`); class-share spellings the provider cannot parse land here permanently |
| Relay sees stale/delayed ticks            | `batch` cadence is 100 ms; check `timestamp` (source ET time) vs receive time                    |
| No frames at all                          | WebSocket ping/pong — service pings every 10 s, drops silent conns after 20 s                    |
