# Futu quotes: relay ↔ futu-quotes contract

Status: frozen contract for R1 (X1 service, X2 relay). Fixtures live in
`scripts/infra/ib_realtime/fixtures/futu_quotes/`. All fixture prices are synthetic.

## Why

The IB realtime relay holds at most 85 live IB lines (`IB_REALTIME_MAX_LINES`). Argon subscribes to
about 176 symbols, so the rest only get a snapshot and go stale. Futu OpenD gives one quota per
(symbol, SubType); this account has 1000. The `futu-quotes` service streams US stocks/ETFs from
OpenD, and the relay serves them to clients that opt in.

## Who gets Futu

- A relay connection opts in with `?quotes=futu-first` on the upgrade URL. The relay honours it
  only on connections that authenticated with `X-API-Key` (service clients). Browser/ticket
  clients and API-key clients without the flag stay on IB.
- Futu serves US stocks and ETFs only while `get_global_state().market_us` is `MORNING` or
  `AFTERNOON`, polled every 30 s. Futu's own state covers holidays and early closes. A failed or
  unknown state counts as closed: symbols go `pending` with reason `session` and stay on IB. Indexes,
  options, non-US symbols, ETH and anything Futu rejects stay on IB.

## Transport

- `futu-quotes` listens on an internal WebSocket on the compose network. It is not published to the
  host. The relay is its only client (`FUTU_QUOTES_URL`).
- One active relay connection. A new connection replaces the old one, and the service closes the
  old socket. Each connection is an epoch with its own `gen` counter starting at 1. When the owner
  connection is lost, its desired set is released through the normal unsubscribe grace.
- Liveness on this internal link: WebSocket protocol ping every 10 s, pong timeout 20 s.
- Health deadline: if no `health` frame arrives for 15 s, the relay treats Futu as down even when
  the socket is open (a stalled process) and fails over to IB.

## Frames: relay → futu-quotes

`want`: the complete desired set. There are no deltas. Within one connection `gen` increases
monotonically, and the service ignores any `gen` lower than the last one it saw on that connection.

```json
{ "action": "want", "gen": 7, "symbols": ["AAPL", "BRK.B", "HYG"] }
```

Symbols are plain tickers, as the relay uses them. The service maps `AAPL` ↔ `US.AAPL`. Any
class-share mapping it cannot verify is rejected with `unknown_symbol`.

## Frames: futu-quotes → relay

`assign`: per-symbol admission for a `gen`. The service sends it after every `want` and on every
change: a subscription succeeds, quota is refreshed or released, OpenD is lost or reconnects, or
the session changes.

```json
{
  "type": "assign",
  "gen": 7,
  "ok": ["AAPL"],
  "pending": [{ "symbol": "HYG", "reason": "session" }],
  "rejected": [{ "symbol": "ZZZZ", "reason": "quota" }]
}
```

- For the latest `want`, `ok`, `pending` and `rejected` are disjoint and together list every wanted
  symbol. `pending` and `rejected` items are `{symbol, reason}`.
- `ok`: subscribed on OpenD while the session is RTH. The relay serves a symbol from Futu only when
  it is in `ok` of the latest `gen` of the active connection.
- Assignment transitions are serialized. A late completion of an older subscribe never changes the
  assignment for a newer `gen`.
- `pending.reason`: `subscribing` | `session` | `opend_down` | `backoff`.
- `rejected.reason`: `quota` | `unknown_symbol` | `not_supported` | `provider_error`.
- After `pending` or `rejected`, the relay serves the symbol from IB. The service re-evaluates
  pending and rejected symbols when quota is refreshed or released, when OpenD reconnects, and when
  the session opens, with bounded backoff. A symbol that later moves to `ok` arrives in a new `assign`.

`batch`: ticks.

```json
{
  "type": "batch",
  "updates": { "AAPL": { "...PriceData...": "...", "source": "futu" } }
}
```

- Producer fence: the service emits ticks only for symbols in the current `ok` set of the active
  connection, and drops buffered ticks for a symbol that leaves it. The relay accepts a Futu tick
  only for a symbol it currently routes to Futu.
- Each value is a full `PriceData` (`web/lib/pricesProtocol.ts`) plus `source: "futu"`. Fields
  Futu's QUOTE push does not carry are `null`: bid, ask, sizes, greeks, 52-week and avgVolume.
- Filled fields: `last`, `volume`, `open`, `high`, `low`, `close` (previous close) and `timestamp`.
- `timestamp` is ISO-8601 UTC with a `Z` suffix. It comes from the quote's own `data_date` +
  `data_time`, read as America/New_York with DST handling. It is never the receive, flush or
  reconnect time.
- Every numeric field is a finite JSON number or `null`. An invalid raw field becomes `null`. The
  whole quote is dropped only when `last` or the time is invalid.
- The cached quote OpenD sends right after a subscribe is forwarded with its true source
  `timestamp`, so consumers see its real age. A per-symbol high-water mark drops any quote older
  than the last one forwarded. The mark resets when the symbol is unsubscribed.
- Cadence: coalesce to the last value per symbol; at most one `batch` per 100 ms.

`health`: every 5 s and on change.

```json
{
  "type": "health",
  "opend_connected": true,
  "quota": { "ceiling": 800, "own_used": 171, "all_used": 171, "remain": 829 }
}
```

- `ceiling` = min(`FUTU_QUOTE_QUOTA`, default 800; own_used + remain), where `remain` comes from
  `query_subscription(is_all_conn=True)`. It is refreshed every 5 min and after a rejection. It is
  a local admission limit only: OpenD's own rejection is authoritative, because other consumers of
  the same account (livewire) can race us. Controlled usage = `own_used` + pending admissions, and it
  must stay ≤ `ceiling`. `all_used` (all connections) is reported for information only.
- When OpenD is lost, the service sends `opend_connected: false` together with an `assign` whose
  `ok` is empty.

## Subscription rules (Futu)

- SubType QUOTE only: 1 quota per symbol. No ORDER_BOOK or TICKER in v1.
- Unsubscribe only when the symbol has left the desired set, at least 60 s after it was subscribed
  (Futu rule), and after a further 60 s grace. Re-check the desired set when the timer fires.
  When Futu rejects an unsubscribe as too early, retry it later.
- After a reconnect, resubscribe the whole desired set. Do not assume subscriptions survived.

## Relay routing rules

- State is kept per symbol and per source. One symbol may be live on both Futu and IB. Each client
  receives frames only from the source chosen for it. Hydration, cache and buffered ticks are kept
  per source; obsolete buffered ticks are cleared on a switch.
- IB demand = symbols wanted by IB-preference clients + indexes + symbols not in `ok`.
- Failover: when a symbol leaves `ok`, when `health.opend_connected` is false, or when the socket is
  lost, the affected symbols go to the IB line budget. This is degraded service: with 85 lines a
  large universe is partly stale. A symbol returns to Futu only after a new `ok` and a hold-down of
  at least 60 s.
- The `status` frame adds `providers.futu: {connected, opend_connected, quota}`. `ib_connected`
  keeps its meaning.

## Fixtures

| File                                                                                           | Use                                                                                                               |
| ---------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `want.json`                                                                                    | relay → service desired set                                                                                       |
| `assign_ok.json`, `assign_partial_quota.json`, `assign_session.json`, `assign_opend_lost.json` | admission cases                                                                                                   |
| `health_ok.json`, `health_opend_lost.json`                                                     | health cases                                                                                                      |
| `batch_normal.json`, `batch_dst_fallback.json`                                                 | expected relay-facing ticks                                                                                       |
| `_futu_raw_*.json`                                                                             | raw Futu quote rows with the expected output (EDT; 2026-11-02, the first RTH after the 2026-11-01 fall-back; stale replay; NaN as an invalid string input) for X1 unit tests |
