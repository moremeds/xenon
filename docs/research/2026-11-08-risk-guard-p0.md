# Risk guard P0 — read-only findings (2026-11-08)

Scope: read-only. No restarts, no orders, SELECT-only DB access. The evidence
was gathered on the macmini prod stack (image v0.20.3, revision `057c25eb`; the
relay code is identical to master for `scripts/infra/ib_realtime`).

## 1. 11-05 frozen SPX/VIX — the relay published the prior close as live

Confirmed (relay code + argon DB + relay logs):

- IB delivered no live SPX ticks to the relay in 11-05 RTH.
  - `xenon-realtime-1` logged 166 `[stale-data] No ticks received during market hours` cycles. Each cycle triggered a disconnect + reconnect, about one every 2.5 min.
  - The log reports ~22,000–24,000 s without ticks across 176 subscriptions, so the last tick of any symbol was ~08:22Z.
  - It also logged 10,177 `Can't find EId` warnings.
- The relay then published the prior close as `last`. Two code paths do this:
  - `ib_tick_handler.js:58-74` `updateDerivedLast`: for cash indices it falls back to `close`.
  - `ib_tick_handler.js:89-105` `applyAuthoritativeClose`, called at `ib_realtime_server.js:1733-1737`: it overwrites `last` with the daily close when the line is stale.
  - The only marker is `stale:true` (`withStale`, `:1580`), and HTTP stays 200.
- argon `uw_scan.gex_snapshots` SPX: one spot value, 7688.62, with tape_time `2026-11-05T03:45:19.802Z` across 293 rows. The same pattern shows on 11-03 and 11-04, each with tape 03:45:19Z, which is the reconnect after the nightly gateway restart.
- Stale-data cycles per day: 10-28 51, 11-02 130, 11-03 174, 11-04 170, 11-05 166, 11-06 105.
  - SPX had no live RTH ticks on 11-03, 11-04 or 11-05.
  - On 11-06, ticks flowed 14:39–17:02Z, so the data is entitled but delivered only intermittently.
- api logs 11-05 13:30–21:00Z: 427× `IB data role not connected`, 427× `xenon-ib-option-greeks timed out`.

Why IB went silent is NOT confirmed. Hypotheses:

- (a) The gateway reaches IB through a local HTTP proxy at `127.0.0.1:7897` (`~/Jts/jts.ini`, `lsof`), and the farm connections are unstable through it.
- (b) A competing login.

What would confirm one of them: relay logging of IB info codes (10197, 10167, 2103–2158, 1100–1102) during the next RTH. These codes are currently suppressed unless verbose.

Relay fixes (not written):

1. A cash index must not publish a close as price in RTH. Publish `last:null` or tag the source as `close` with `stale:true`.
2. Reset `lastTickTimestamp` on reconnect, and back off / escalate after N fruitless reconnects.
3. Log the IB info codes above at normal level.
4. Consumers (argon, the risk guard) reject a `stale` spot or an old `tape_time`.

## 2. IB account snapshots sparse since ~10-30

`xenon.account_snapshots`, IB live, core_dev:

- 11-05 RTH: 0 rows. 11-04: 1 row. 11-06: 2 rows.
- Healthy days (10-27, 10-29) had a median gap of 33–36 s and a p95 of 34–38 s.
- The collapse starts ~10-30, together with the stale-data cycles.
- Futu live: steady, ~1,400 rows/day.

So the IB cushion is an intraday trigger only while IB sync is healthy. The risk guard must alarm on snapshot age.

## 3. Prod `/portfolio` 500 since the 11-08 boot

`GET /portfolio` → 500. Traceback:

`guards.py:339 get_account_scope → account_scope.py:151`
`ValueError: app.state.trading_mode and app.state.account must be set`

- The api container booted ~02:19Z on 11-08 while the gateway sat at 2FA. The log shows `ConnectionRefusedError ... 4001` at 02:19:33.
- The scope is resolved only at boot. While it stays unresolved:
  - every IB-scoped route 500s;
  - the activity poller and the wizard risk alert never start (`server.py:617-649`).
- `/health` still reports the container healthy, and the `sync` pool shows `connected:false`.
- Fix now: an api restart after the gateway is up. This is the user's call.
- Design fix: re-resolve the scope after a gateway reconnect, or return 503 with a top-level `reason_code`.

## 4. Implications for the risk guard

- The BLIND causes must include: account scope unresolved, IB snapshot age over threshold, data role not connected, relay `stale:true` on a needed instrument.
- Price triggers cannot rely on the relay's `last` for indices. They need the raw tick time plus a second source.
