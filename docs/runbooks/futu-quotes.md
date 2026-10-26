# futu-quotes — Futu OpenD quote feed for the realtime relay

`xenon-futu-quotes` streams US stock/ETF/option quotes from a local Futu
OpenD into the `ib_realtime` relay over one internal WebSocket — each
symbol one paired `SubType.QUOTE` + `SubType.ORDER_BOOK` subscription — so
live ticking prices come from Futu instead of overloading the IB realtime
relay. The relay is the service's only client; browsers and ordinary API-key
connections stay on IB. Opt-in is `?quotes=futu-first` on the relay
connection. Authoritative protocol/architecture contract:
`docs/architecture/futu-quotes.md`.

**Scope guard:** quotes only. No orders, no TICKER subtype, no non-US
symbols. Sessions served: RTH and the US overnight session (Blue Ocean,
Sun–Thu 20:00 → 03:50 ET); options are RTH only and never go overnight.
Pre-market and after-hours stay on IB. Indexes are rejected
`not_supported`, as is any option key the service cannot map to a Futu
code (`SYM_YYYYMMDD_STRIKE_RIGHT` → `US.<UND><YYMMDD><C|P><strike×1000>`,
relayed only when the relay's `XENON_FUTU_OPTIONS` is on) or any code
whose ORDER_BOOK leg OpenD refuses.

## Run

```bash
# package entry point (registered by the integration PR):
xenon-futu-quotes [--host 0.0.0.0] [--port 8767] [--log-level INFO]

# same thing without the console script:
uv run python -m xenon.futu_quotes
```

## Env vars

| Var                    | Default        | Meaning                                                                                     |
| ---------------------- | -------------- | ------------------------------------------------------------------------------------------- |
| `FUTU_QUOTES_HOST`     | `0.0.0.0`      | listen address (internal only — not host-published)                                         |
| `FUTU_QUOTES_PORT`     | `8767`         | listen port                                                                                 |
| `FUTU_QUOTE_QUOTA`     | `800`          | local admission ceiling cap, in provider quota units                                        |
| `FUTU_UNITS_PER_SYMBOL`| `2`            | provider units one held symbol costs (paired QUOTE + ORDER_BOOK)                             |
| `FUTU_BOOK_STALE_S`    | `0`            | ORDER_BOOK receipt-age cutoff in seconds; `0` = age rule off (Amendment A1), positive re-enables |
| `FUTU_GREEK_FACTORS`   | unset          | JSON per-field multipliers for option greeks/IV; unset or invalid = those fields stay `null` |
| `FUTU_OPEND_HOST`      | `127.0.0.1`    | Futu OpenD host                                                                             |
| `FUTU_OPEND_PORT`      | `11111`        | Futu OpenD port                                                                             |

`FUTU_GREEK_FACTORS` maps each emitted greek key to the factor applied to
Futu's raw QUOTE push value — `impliedVol`←`implied_volatility`×`iv`,
`delta`←`delta`, `gamma`←`gamma`, `theta`←`theta`, `vega`←`vega`. The
calibrated value is `{"iv":0.01,"delta":1,"gamma":1,"vega":1,"theta":1}`
(`iv` 0.01 turns Futu's percent IV into the fraction `PriceData` expects;
X7-0 calibration, PR #249 — not merged yet). Missing keys or non-finite
values leave that field `null`; `undPrice` needs no factor — it is the
held underlying's latest Futu `last`.

## Wire protocol

Inbound: `{"action": "want", "gen": N, "symbols": [...],
"tiers": {"<sym>": "held"|"view"}}` — the complete desired set; gen is a
per-connection monotonic counter starting at 1. `tiers` is optional; a
symbol missing from it (or the whole key absent/malformed) means `held`.
`symbols` may carry option relay keys `SYM_YYYYMMDD_STRIKE_RIGHT`.

Outbound:

- `assign` — `{ok, pending[{symbol,reason}], rejected[{symbol,reason}]}`, a
  disjoint partition of the wanted set. pending reasons: `subscribing`,
  `session`, `opend_down`, `backoff`. rejected reasons: `quota`,
  `unknown_symbol`, `not_supported`, `provider_error`.
- `batch` — `{updates: {symbol: PriceData}}`, last-value coalesced, at most
  one batch per 100 ms. Every update carries `source: "futu"`. RTH updates
  carry a UTC `timestamp` derived from Futu's ET wall clock (DST-safe) —
  or `max(quote ts, book receipt)` when a book push re-emitted the row;
  `bid`/`ask`/`bidSize`/`askSize` come from the ORDER_BOOK top level
  (`null` pre-book and outside RTH; `FUTU_BOOK_STALE_S` above). Option
  rows add factored greeks/IV and `undPrice` (see env table); overnight
  updates carry `session: "overnight"` and the receipt time. A
  missing `session` field means `rth`. A per-symbol high-water mark drops
  replayed stale RTH quotes; the overnight baseline drops unchanged
  overnight rows.
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
- US session (`admission.us_session`, polled every 30 s with the wall
  clock): `rth` when `market_us` is `MORNING` or `AFTERNOON`; `overnight`
  when it is `OVERNIGHT`, or `AFTER_HOURS_END` while ET is inside Sun–Thu
  20:00 → next day 03:50. Anything else (pre-market, after-hours, weekend,
  unknown/failed) counts as closed and wanted symbols sit at
  `pending/session`. A clock edge is picked up by the next poll (≤ 30 s).
- Overnight ticks: `last`/`high`/`low`/`volume` from `overnight_*`,
  `close` = RTH `last_price`, `open` null, `timestamp` = receipt time,
  `session: "overnight"`. A row with `overnight_price` or
  `overnight_volume` missing or ≤ 0 is dropped. A row is published only
  when `overnight_volume` or `overnight_price` changed; the first row
  after a subscribe, OpenD reconnect, relay-owner change or session change
  is a baseline.
- Overnight readiness: a subscribed symbol is `ok` overnight only after
  the service has seen one valid trade row for it (price and volume > 0)
  since the last baseline reset; until then it stays `pending/session`
  and the relay keeps it on IB. A symbol with no overnight trading never
  leaves IB. A ready but quiet symbol (relay routes it to Futu and drops
  its IB line) gets no tick until its next overnight trade.
- Overnight latency per symbol after 20:00 ET: session poll (≤ 30 s) +
  first trade row (readiness) + relay hold-down (60 s) + next changed
  trade row. Until then the client stays on IB. After 03:50 ET rows are
  still accepted for up to 30 s (until the next poll).
- Live evidence (2026-10-25 Sun 20:25–20:27 ET, prod `xenon-futu-quotes-1`,
  SDK 10.03.6308, read-only probe): `market_us` = `AFTER_HOURS_END`, not
  `OVERNIGHT`. QUOTE pushes kept firing every few seconds; `last_price` and
  `data_date/data_time` stayed at Friday's RTH close (`2026-10-23
  16:00:00.x`), while `overnight_price`/`overnight_volume` ticked (SPY
  761.08 → 761.25, volume 17485 → 18132 within about 60 s). There is no
  overnight timestamp field. `subscribe(session=)` does not affect QUOTE.
- Unsubscribe discipline: a symbol is unsubscribed only after it leaves the
  desired set or is marked for quota-pressure eviction (see § Quota), ≥60 s
  after subscribe plus a further 60 s grace; "too early"
  provider rejections are retried. Re-adds inside the grace window — and
  resolved quota pressure — cancel the unsubscribe.
- Operation fencing: every provider operation (snapshot verify, subscribe,
  unsubscribe, session poll, quota poll) runs on **one worker, one at a
  time**, in queue order. Each op carries the OpenD session epoch and a
  per-symbol reservation taken when the evaluation emitted it. Right before
  the SDK call the worker re-checks the epoch, the reservation and the
  symbol's current state; for an unsubscribe that includes the current
  60 s lifetime, grace and retry deadlines, so a re-add followed by a new
  removal waits a full new grace. A declined op sends nothing. After the
  call, a result from an older epoch or a superseded reservation is
  discarded and changes no state; a down error from an old-session op does
  not mark the current session down. An old-session SDK call that is
  already in flight still holds the queue until it returns (bounded by the
  SDK call timeout).
- Ownership: a symbol is counted against the ceiling ("held") from the
  moment its subscribe is sent. A successful unsubscribe, or an
  unsubscribe answered "not subscribed", releases it. A definite rejection
  (`quota`, `unknown_symbol`, `not_supported`) in the current session
  releases only the provisional slot of a fresh admission (an invalid code
  or unsupported type is then terminal: `rejected/unknown_symbol` or
  `rejected/not_supported`). It never releases ownership from an earlier
  attempt (a reconfirm, or a previously uncertain subscribe): that symbol
  stays held, keeps its verification, and is retried with the normal
  bounded backoff for every rejection reason. An
  uncertain outcome (timeout, lost or undecodable reply, down error, any
  other provider error) and a late success from an old session keep it
  held but not ready. Disconnects, failed quota refreshes and failed
  unsubscribes do not release it either; failed unsubscribes retry with
  bounded backoff. A held symbol that left demand is unsubscribed through
  the normal lifetime and grace. After a reconnect a counted, wanted symbol is not `ok` until a
  subscribe succeeds in the new session (reconfirm). The reconfirm uses
  no second quota unit, retries with backoff until it succeeds or the
  symbol is released, and its slot is never given to a new symbol.
- SDK callbacks: each OpenD context carries a generation and each quote
  push carries the context's link counter, read on the SDK thread. Pushes
  and disconnect/reconnect callbacks from a closed or replaced context are
  dropped, and so is a push read before a later disconnect/reconnect of
  the same context. Push and disconnect callbacks run on different SDK
  threads, so this fence cannot order them exactly; a push that passes it
  while OpenD is down, or before the reconfirm, is dropped by the `ok`
  check.
- Buffered quotes are dropped at the transition that removes them from
  `ok`: a `want` that removes the symbol, an unsubscribe result that
  releases it (with its high-water reset), relay-owner replacement or
  close, session close, and OpenD loss or reconnect. The flush also re-checks
  `ok`.
- Snapshot verification: a failed multi-code batch call is retried one
  code at a time (OpenD reports whole-call errors only, so one invalid code
  cannot reject its batch-mates). Only a definite not-found gives
  `rejected/unknown_symbol`; a generic provider or transient error leaves
  the symbol `pending/backoff` and retries (5 s doubling to 120 s).

## Quota

Futu subscription quota is checked two ways, all math in provider units
(one held symbol = `FUTU_UNITS_PER_SYMBOL`, default 2, for the paired
QUOTE + ORDER_BOOK subscription):

- **Local ceiling**: `min(FUTU_QUOTE_QUOTA, provider own_used + remain)`,
  recomputed from `query_subscription(is_all_conn=True)` on connect/reconnect,
  after any provider rejection, and every 5 min. Symbols over the ceiling are
  `rejected/quota` and retried with bounded backoff. The health frame's
  `own_used` counts this service's held symbols; `ceiling`/`remain`/
  `all_used` are provider units.
- **Provider rejection is authoritative** — a `quota` error from
  `subscribe()` rejects the symbol and triggers an immediate quota refresh
  even when the local count is below the ceiling.
- **Tiers under pressure**: `held` candidates admit before `view`, and a
  `view` never takes a slot while a `held` is quota-blocked. When believed
  usage exceeds the ceiling (or a blocked `held` needs a freed slot) a
  still-wanted `view` is marked for release before any `held`; the mark
  reports `rejected/quota` immediately but the unsubscribe still waits for
  the 60 s min-lifetime plus 60 s grace, and retracts if pressure resolves
  first. A `want` promoting the symbol back to `held` cancels the mark.

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
| Symbols `rejected/quota`                  | `health.quota` block + the `query_subscription` snippet above; raise `FUTU_QUOTE_QUOTA` only if provider headroom exists. A `view` symbol sitting at `rejected/quota` while still wanted may be marked for eviction — check whether a `held` candidate is blocked |
| Symbol `rejected/unknown_symbol`          | OpenD said the code does not exist: Futu code spelling (`US.<ticker>`); class-share spellings the provider cannot parse land here permanently |
| Symbol `rejected/not_supported`           | Unmappable option relay key, or OpenD refused the ORDER_BOOK leg — the symbol is terminal for this service and stays on IB |
| Symbol stays `pending/backoff`            | Snapshot verify keeps failing with a generic/transient provider error; check OpenD logs and rate limits |
| Relay sees stale/delayed ticks            | `batch` cadence is 100 ms; check `timestamp` (source ET time) vs receive time                    |
| No frames at all                          | WebSocket ping/pong — service pings every 10 s, drops silent conns after 20 s                    |
