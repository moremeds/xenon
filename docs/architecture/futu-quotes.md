# Futu quotes: relay ↔ futu-quotes contract

Status: frozen contract for R1 (X1 service, X2 relay), extended by the X7
Futu-primary work (paired QUOTE+ORDER_BOOK, option keys, held/view tiers).
Fixtures live in `scripts/infra/ib_realtime/fixtures/futu_quotes/`. All fixture prices are synthetic.

## Why

The IB realtime relay holds at most 85 live IB lines (`IB_REALTIME_MAX_LINES`). Argon subscribes to
about 176 symbols, so the rest only get a snapshot and go stale. Futu OpenD gives quota per
(symbol, SubType); this account has 1000 units and each served symbol holds one paired
QUOTE + ORDER_BOOK subscription (`FUTU_UNITS_PER_SYMBOL`, default 2 units — a conservative
policy: X7-0 calibration measured +1/+1 units for a stock's QUOTE/ORDER_BOOK pair and no
counter movement for US options). The `futu-quotes` service streams US stocks/ETFs and US
options from OpenD, and the relay serves them to clients that opt in.

## Who gets Futu

- A relay connection opts in with `?quotes=futu-first` on the upgrade URL. The relay honours it
  only on connections that authenticated with `X-API-Key` (service clients). Every other
  connection takes the relay default `XENON_QUOTES_DEFAULT` (`ib` unless set). The default and
  `XENON_FUTU_OPTIONS` follow the runtime settings live (relay pull every 10 s): a default flip
  re-routes every connected client that did not opt in itself, an options flip re-pins or
  unpins every subscribed option key.
- Futu serves US stocks and ETFs in four sessions, and US options in `rth` only. The service polls
  `get_global_state().market_us` every 30 s and reads its wall clock (UTC, converted to
  America/New_York) at each poll (`admission.us_session`):
  - `rth`: `market_us` is `MORNING` or `AFTERNOON`, at any clock. Futu's own state covers holidays
    and early closes.
  - `pre`: `market_us` is `PRE_MARKET_BEGIN`. `after`: `market_us` is `AFTER_HOURS_BEGIN`.
    `pre`, `after` and `overnight` are the extended sessions; they share every extended rule below
    and differ only in the QUOTE fields they read and in `close`.
  - `overnight` (Blue Ocean): `market_us` is `OVERNIGHT`, or `AFTER_HOURS_END` while the ET clock is
    inside Sun–Thu 20:00 → next day 03:50. Live OpenD (SDK 10.03.6308) reports `AFTER_HOURS_END`
    in this window, not `OVERNIGHT`.
  - Every other state (`PRE_MARKET_END`, weekend, failed or unknown) counts as closed:
    symbols go `pending` with reason `session` and stay on IB. A clock edge (20:00, 03:50) takes
    effect at the next poll, so up to 30 s late.
- Indexes, non-US symbols and anything Futu rejects stay on IB in every session. Options are never
  served in an extended session: an option row there is not a quote source and the symbol sits
  `pending` with reason `session`.

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
{
  "action": "want",
  "gen": 7,
  "symbols": ["AAPL", "BRK.B", "HYG", "AAPL_20261030_150_C"],
  "tiers": { "AAPL": "held", "AAPL_20261030_150_C": "view" }
}
```

Symbols are plain tickers, as the relay uses them, or US option relay keys
`SYM_YYYYMMDD_STRIKE_RIGHT` (the relay's `optionKey` form). The service maps `AAPL` ↔ `US.AAPL`
and `AAPL_20261030_150_C` ↔ `US.AAPL261030C150000` (underlying `[A-Z][A-Z0-9]*`, ≤3-decimal
strike). An option key it cannot map is rejected `not_supported` before any OpenD call —
that includes a class-share underlying (`BRK.B_...`), which has no verified Futu option
code. A class-share *ticker* it cannot verify is rejected with `unknown_symbol`. The relay
sends option keys only when its option routing is on (`XENON_FUTU_OPTIONS`, relay side).

`tiers` is optional. It marks each wanted symbol `held` or `view` for admission priority;
a symbol missing from the map — or the whole `tiers` key absent or malformed — means `held`.

`config`: runtime-settings overrides (DB, `xenon.runtime_settings`) that the relay pulls from the
api and forwards; the service never reads the DB and holds no secret. Every forwarded key is
present; `null` means "use the service's own env value".

```json
{
  "action": "config",
  "values": {
    "FUTU_QUOTE_QUOTA": 600,
    "FUTU_UNITS_PER_SYMBOL": null,
    "FUTU_BOOK_STALE_S": null,
    "FUTU_GREEK_FACTORS": { "iv": 0.01, "delta": 1, "gamma": 1, "theta": 1, "vega": 1 }
  }
}
```

The relay sends it on every socket open, before the first `want`, once it has one successful
settings pull (none before that: the service keeps its current values), and again whenever the
override set changes. The service validates each value with the registry rules; a `null`,
missing or invalid value falls back to its env value (a warning is logged). Quota and units
apply at the next admission evaluation, which the frame triggers; `FUTU_BOOK_STALE_S` and
`FUTU_GREEK_FACTORS` apply to the next emitted row. A frame from a replaced connection, or with
`values` that is not an object, is ignored. The values survive a relay reconnect until the next
`config` frame. There is no reply frame.

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
- `ok`: the paired QUOTE + ORDER_BOOK subscription is held on OpenD while the session is `rth` or
  an extended session (options: `rth` only). The relay serves a symbol from Futu only when
  it is in `ok` of the latest `gen` of the active connection.
- Extended readiness: in an extended session a subscribed symbol is `ok` only after the service has
  seen at least one row for it with a valid session price and a session volume above 0
  (`pre_*` / `after_*` / `overnight_*`) since the last baseline reset (see the replay guard
  below). Until then it stays `pending` with reason `session`, so the relay keeps it on IB. A
  symbol with no trading in the session never leaves IB. The row that makes it ready is the
  baseline and is not published.
- Assignment transitions are serialized. A late completion of an older subscribe never changes the
  assignment for a newer `gen`.
- `pending.reason`: `subscribing` | `session` | `opend_down` | `backoff`.
- `rejected.reason`: `quota` | `unknown_symbol` | `not_supported` | `provider_error`. A `quota`
  rejection also covers a still-wanted `view` symbol marked for release under quota pressure:
  the mark is set after that evaluation's partition is built, so it shows `rejected` from the
  *next* evaluation (the relay should already treat it as unrouted) while the actual
  unsubscribe waits for the subscription rules below. An ORDER_BOOK refusal for a code lands
  in `not_supported`, as does an option key the service cannot map to a Futu code.
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
- Each value is a full `PriceData` (`web/lib/pricesProtocol.ts`) plus `source: "futu"`.
  `bid`, `ask`, `bidSize`, `askSize` come from the top level of the paired ORDER_BOOK push
  (best `(price, volume)`). They are `null` before any book has arrived and whenever the symbol's
  session is not `rth`. Amendment A1: the receipt-age rule is **off by default**
  (`FUTU_BOOK_STALE_S`, default `0`) because Futu pushes ORDER_BOOK only on change, so a quiet
  but healthy book is still valid; a positive value re-enables nulling once the book receipt is
  older than that many seconds. Disconnect, unsubscribe and session change clear the book either
  way. 52-week, `avgVolume` and — for stocks — all greeks stay `null`: the QUOTE push does not
  carry them.
- RTH rows fill `last`, `volume`, `open`, `high`, `low`, `close` (previous close) and `timestamp`.
  They carry no `session` field (absent means `rth`).
- RTH `timestamp` is ISO-8601 UTC with a `Z` suffix. It comes from the quote's own `data_date` +
  `data_time`, read as America/New_York with DST handling. It is never the receive, flush or
  reconnect time — except that a book-driven re-emit stamps `max(quote ts, book receipt time)`.
  The per-symbol high-water mark and the coalescer order still use the QUOTE timestamp only, so
  a book receipt never advances the mark or drops a newer quote.
- Extended rows add `session: "pre" | "after" | "overnight"`. `last` / `high` / `low` / `volume`
  are the session's `<p>_price` / `<p>_high_price` / `<p>_low_price` / `<p>_volume` with `<p>` =
  `pre`, `after` or `overnight`; `open` is `null`. `close` is the previous session day's close: the
  RTH `last_price` pre-market and overnight, `prev_close_price` after-hours (there `last_price` is
  already today's close). Outside RTH the QUOTE push keeps `last_price` and
  `data_date`/`data_time` at the last RTH quote, and Futu has no extended-session time field, so
  `timestamp` is the service's receipt time (UTC). Extended rows carry no book (`bid` / `ask`
  `null`). A row with a missing, non-finite or ≤ 0 session price or volume is dropped (for example
  a holiday or a rolled row): it is not published and does not count for readiness.
- Option rows (RTH only, keyed by the relay option key from `want.symbols`) fill the option
  fields: `impliedVol`, `delta`, `gamma`, `theta`, `vega` = the QUOTE push's
  `implied_volatility` / `delta` / `gamma` / `theta` / `vega` each × its factor from
  `FUTU_GREEK_FACTORS` (JSON object, e.g. `{"iv":0.01,"delta":1,"gamma":1,"theta":1,"vega":1}` —
  `iv` 0.01 converts Futu's percent IV into the fraction PriceData expects). Unset or invalid
  `FUTU_GREEK_FACTORS`, or a missing/non-finite push value or factor, leaves that field `null`.
  `undPrice` is the latest Futu `last` of the held underlying, refreshed on every emit including
  book-driven rebuilds, else `null`. Options never carry a `session` field.
- A missing `session` field means `rth`. Consumers must not require the field.
- Extended replay guard: the service publishes an extended row only when the session volume or
  price differs from the last value seen for that symbol. The first row after a subscribe, an
  OpenD reconnect, a relay-owner change or a session change (for example `after` → `overnight` at
  20:00) is a baseline and is not published. Each of those resets also clears extended readiness.
  A ready but quiet symbol sends nothing until its next trade in the session.
- Overnight latency: after 20:00 ET, a symbol's first Futu tick reaches a futu-first client after
  the session poll (≤ 30 s), its first trade row (readiness, not published), the relay hold-down
  (≥ 60 s after `ok`) and then its next changed trade row. Until it is routed to Futu the client
  stays on IB. After 03:50 ET, rows are still accepted for up to 30 s (until the next poll).
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

- `ceiling` = min(`FUTU_QUOTE_QUOTA`, default 800; own_used + remain), in provider quota units,
  where `remain` comes from `query_subscription(is_all_conn=True)`. It is refreshed every 5 min
  and after a rejection. It is a local admission limit only: OpenD's own rejection is
  authoritative, because other consumers of the same account (livewire) can race us. Controlled
  usage = held symbols × `FUTU_UNITS_PER_SYMBOL` (default 2) + pending admissions × the same, and
  it must stay ≤ `ceiling`. The health `own_used` counts this service's held symbols (the
  provider's own per-connection unit count feeds `ceiling`); `all_used` (all connections) is
  reported for information only.
- When OpenD is lost, the service sends `opend_connected: false` together with an `assign` whose
  `ok` is empty.

## Subscription rules (Futu)

- One paired `SubType.QUOTE` + `SubType.ORDER_BOOK` subscription per symbol, issued as a single
  OpenD subscribe so ownership is atomic: `FUTU_UNITS_PER_SYMBOL` (default 2) provider quota
  units per symbol — a conservative policy, not a per-kind measurement (see § Why). A symbol is
  held only while both subtypes are held in the current OpenD session; if
  OpenD refuses ORDER_BOOK for the code the symbol is rejected `not_supported`. No TICKER.
- Tiers: each wanted symbol is `held` or `view` (`want.tiers`; absent = `held`). `held`
  candidates are evaluated first — when quota does not fit every wanted symbol, `held` takes
  the slots and a `view` is never admitted while a `held` candidate is quota-blocked. Under
  quota pressure (believed usage above the ceiling, or a blocked `held` candidate a `view`
  slot could free) a still-desired `view` subscription is marked for release before any `held`
  one, and only once `view` candidates are exhausted does pressure evict `held`. The mark goes
  through the same unsubscribe rules as a removal below — min-lifetime plus grace — so it never
  unsubscribes early, and it retracts if the pressure resolves first. A `want` promoting a
  marked symbol to `held` cancels its eviction; a completed release ends it.
- Unsubscribe only when the symbol has left the desired set or is marked for eviction, at least
  60 s after it was subscribed (Futu rule), and after a further 60 s grace. Re-check the desired
  set when the timer fires. When Futu rejects an unsubscribe as too early, retry it later.
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
| `batch_normal.json`, `batch_dst_fallback.json`                                                 | expected relay-facing RTH ticks (overnight rows are covered by `scripts/tests/test_futu_quotes_overnight.py`)     |
| `_futu_raw_*.json`                                                                             | raw Futu quote rows with the expected output (EDT; 2026-11-02, the first RTH after the 2026-11-01 fall-back; stale replay; NaN as an invalid string input) for X1 unit tests |
