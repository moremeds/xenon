# Changelog

All notable changes to Xenon are documented here. Format loosely based on
[Keep a Changelog](https://keepachangelog.com/) with semver-ish versioning.

## [Unreleased]

- **Nightly Futu history sync reaches OpenD in Docker.** The 16:30 ET loop built its own `FutuClient()` (127.0.0.1, `trd_env` REAL, first account) instead of the scope's client, so in the api container every run failed with `OpenD unreachable at 127.0.0.1:11111` (the cause behind the 2026-10 nightly wedge) and `futu_trades` / `futu_cash_flow` / Futu NAV stopped filling. `run_history_sync` now builds the client from the scope (`futu_scope.new_client`: `FUTU_OPEND_HOST` / `_PORT`, exact account + TrdEnv, shared with the display clients), resolves `since=None` to the incremental watermark (the loop passed None, which would have crashed next), and the loop and the CLI sync every configured Futu scope (paper, then live) with one `service_health("futu_history")` row per scope; one scope failing does not skip the next.
- **Operator console: Futu support.** The Futu card drops the stale "read-only / orders not supported" text and shows each scope (paper / live): orders on/off (`futu.trading`), last sync, observation state, and held rows (`FUTU_FILL_UNPRICED` / `FUTU_FILL_CONFLICT` / `FUTU_NOT_OBSERVED`) as a warn pill with the order ids; order-type capabilities in one line. A Quotes line shows the Futu quote feed from the relay: OpenD up/down, symbols routed futu / ib, quota used / ceiling. Relay `GET /status` adds `futu` (`connected`, `opend_connected`, `quota`, `routes` totals), passed through as `realtime_subscribers.futu_quotes` on `/admin/operator`.
- **Flex divergence job is scheduled.** The /admin Flex Divergence tile never had a run (`ran_at` null): the only schedule was a pre-Docker host cron in the ops runbook. New launchd job `deploy/jobs/com.xenon.flex-divergence.plist` (+ crontab line) runs `xenon-flex-divergence-check --apply` (new entry point for `xenon.jobs.flex_divergence_check`) from the deployed api image daily at 07:30 ET; install on the mini after the release.
- **Operator console fixes.** The Snapshotter tile turns red only while the market is open or extended (neutral overnight); the writer table shows each errored writer's `last_error`; the rollup header and IB card name any pool role that is down while the gateway is authenticated (e.g. `IB authenticated · data down`).

## [0.18.1] — 2026-10-27

- **IB quote / qualify deadline (incident 2026-10-26).** Every request-path data-role quote and qualify call (`/orders/quote`, the place quote gate including Futu SIMULATE, Gate-4 leg meta, `/contract/qualify`, and the qualify step of historical bars and head-timestamp) now finishes or fails within `XENON_IB_QUOTE_TIMEOUT_S` (default 10 s): 503 with top-level `reason_code` `QUOTE_UNAVAILABLE`, and no order is sent. The IB request inside the worker is bounded too, so the worker frees itself. A call abandoned while it still runs fences the `data` role (fail fast, never two calls on one client) until it returns; `/health` `ib_pool.<role>.fenced`. The role lock is no longer leaked when a caller is cancelled during a reconnect, and a reconnect abandoned mid-connect fences the role until it registers (no second connect on the same clientId). Historical bars / head-timestamp put lock wait + reconnect + qualify under the deadline (the download keeps its own timeout); the Futu observation metadata resolver and the wizard risk-alert combo snapshot run with the same worker bound and upstream check.
- **Gateway lost IB servers is visible (incident 2026-10-26).** `/health` `ib_gateway.upstream` = `{state: connected|lost|unknown, since, last_code}` from 1100 / 2110 / 1101 / 1102 on the pool connections (matched by code), and top-level `status: degraded` while it is lost (`orders_halted` still wins; HTTP stays 200). While the data connection reports lost, quote / qualify calls fail at once with 503 `IB_BROKER_DISCONNECTED`. The sidebar IB Gateway row shows `NO IB SERVERS` with the warning dot, and the last code and time.
- **Relay tests are hermetic.** Relay test spawns no longer read the repo `.env` (`IB_REALTIME_NO_DOTENV=1`) or the parent env, target IB port 1 only, use a private temp dir per stack, and retry a lost ephemeral-port race.

## [0.18.0] — 2026-10-26

- **futu-quotes paired QUOTE+ORDER_BOOK subscriptions (server, X7a).** Every served symbol now holds one atomic QUOTE+ORDER_BOOK subscription, counted as `FUTU_UNITS_PER_SYMBOL` (default 2) provider quota units — admission math moves to units while the health `own_used` keeps counting symbols. `bid`/`ask`/`bidSize`/`askSize` fill from the book top level; they are `null` before any book arrives and outside RTH, and — only when `FUTU_BOOK_STALE_S` is positive — once the book receipt ages out (default `0` = the age rule is off: Futu pushes ORDER_BOOK only on change, so a quiet healthy book stays valid; disconnect, unsubscribe and session change always clear it). Book pushes re-emit the row with `timestamp` = max(quote time, book receipt); the coalescer and per-symbol high-water mark still use the QUOTE timestamp only. A symbol is `ok` only while the pair is held; an ORDER_BOOK refusal is `rejected/not_supported`.
- **futu-quotes option relay keys (server, X7a).** `want.symbols` may carry `SYM_YYYYMMDD_STRIKE_RIGHT` option keys (relay `optionKey` form; sent only when relay `XENON_FUTU_OPTIONS` is on), mapped to `US.<UND><YYMMDD><C|P><strike×1000>` and reported back under the relay key; unmappable keys reject `not_supported` without an OpenD call. Options are RTH only — never `session:"overnight"`, they sit `pending/session` overnight. `impliedVol`/`delta`/`gamma`/`theta`/`vega` fill from QUOTE push ex-data × `FUTU_GREEK_FACTORS` (JSON, e.g. `{"iv":0.01,"delta":1,"gamma":1,"theta":1,"vega":1}`; unset = `null`), and `undPrice` is the held underlying's latest Futu `last`.
- **futu-quotes held/view admission tiers (server, X7a).** `want` accepts an optional `tiers` map (`held`|`view`; absent or malformed = `held`). Held candidates admit before view and a view never takes a slot while a held is quota-blocked; under quota pressure a still-wanted `view` is marked for release before any `held`, reports `rejected/quota` immediately, releases through the unchanged 60 s min-lifetime plus 60 s grace rules, retracts if pressure resolves first, and cancels on promotion to `held`.
- **Futu-first quote routing in the realtime relay (X7b).** `XENON_QUOTES_DEFAULT=ib|futu-first` (default `ib`) picks every client's quote route — browser tickets included — while `X-API-Key` clients keep the `?quotes=futu-first` opt-in (no `?quotes=ib`). `XENON_FUTU_OPTIONS=1` lets option contract keys enter `want`; indexes, forex and non-US venues stay IB-pinned. Subscribe entries may carry `tier` (`held`/`view`, absent = held); per-symbol tiers merge held-over-view across all subscribers into `want.tiers`. A symbol goes Futu-live only once a row is cached (hold-down caching hydrates via a `price` frame), served Futu rows are stamped `stale` past `STALE_TICK_MS`, and `status` reports per-client `providers.futu.routes` counts.
- **Web futu quote surface (X7b).** `PriceData` gains optional `stale` / `source:"futu"` / `session:"overnight"`; subscribe entries may carry `{symbol, tier}` / `contract.tier`, and `WorkspaceShell` tags portfolio/order legs `held` and ticker-detail chain legs `view` (a tier-only change re-emits without unsubscribing). A stale routed quote renders a small `--warning` dot in position and expanded-leg last-price cells; `---` fallback rules are unchanged.

## [0.17.0] — 2026-10-26

- **Futu order types (server, X5a).** Futu place now takes `orderType` `LMT` (default), `MKT`, `STP`, `STP LMT`, `MIT`, `LIT`, `TRAIL`, `TRAIL LIMIT` with `auxPrice`, `trailType` / `trailValue`, `trailSpread`, `tif` `DAY` / `GTC` and `session` `RTH` / `ETH` / `ALL` / `OVERNIGHT` (contract `docs/architecture/futu-orders.md`). A place is accepted only when its `SEC:TYPE:TIF:SESSION` tuple is in the static Futu set and in the operator allowlist (`XENON_FUTU_TUPLES_LIVE` / `XENON_FUTU_TUPLES_PAPER`); the defaults keep the v0.16 live LMT surface exactly and add stock MKT on paper only; paper GTC is withdrawn (Futu SIMULATE is DAY only; an existing paper GTC order keeps cancel). Modify per type: `XENON_FUTU_MODIFY_TYPES` (default `LMT`); cancel works for every type. Refusals before any IO: `FUTU_ORDER_TYPE_UNSUPPORTED`, `FUTU_TIF_UNSUPPORTED`, `FUTU_SESSION_UNSUPPORTED`, `FUTU_ORDER_NOT_MODIFIABLE`; precision is refused, never rounded. An IB body with any of the new fields is 400 `INVALID_ORDER_BODY`. Operator procedure to enable a live tuple: runbook § 7.
- **Futu placement intent and NULL price.** The row stores `limit_price` NULL for types without a fixed limit, from reservation through observation to `GET /orders` (`limitPrice: null`; the SDK placeholder price is never stored). `legs.futu_intent` (frozen placement fingerprint) and `legs.futu_terms` (current confirmed terms) are written with the reservation; a replay of one `clientAttemptId` with another type, trigger, trail or session is a body mismatch, also after a confirmed amend. Remark binding of an uncertain place compares the intent. Orders made outside Xenon get no fingerprint and are managed from a fresh read; an unreadable type or term refuses modify and keeps cancel. An outside order's session must be established (reported, or N/A with `fill_outside_rth` false = RTH); a trailing order's read unit must equal the placed unit; confirmation also needs the same order type and trail unit; observed terms (unit and values from one read) are ordered by the start of the broker read: an older read never replaces them, and a confirmed amend publishes its confirming read unless a newer read is stored.
- **Futu per-type IB quote gate, place and modify.** LMT keeps the limit band. MKT (stock) needs the regular session by the market calendar (else `FUTU_SESSION_UNSUPPORTED`), spread at most 1 % of mid (`MKT_SPREAD_TOO_WIDE`) and quantity x ask at most `XENON_FUTU_MKT_NOTIONAL_CAP` (default 25 000 USD; `MKT_NOTIONAL_CAP`; a bad value refuses every MKT). STP / MIT must not fire at once (`TRIGGER_WOULD_FIRE`, no override); STP LMT / LIT the same plus the limit-vs-trigger band (`LIMIT_OUT_OF_BAND`), both overridable with `acknowledge_limit_override` and audited in `PREFLIGHT_ACK_LIMIT` with the intent. TRAIL / TRAIL LIMIT: AMOUNT at most 20 % and spread at most 5 % of the IB mid (`FUTU_INVALID_ORDER`). Every type needs a sized, non-crossed IB quote. Modify takes `newAuxPrice` / `newTrailValue` / `newTrailSpread`, applies the same rules to the full outgoing terms after the fresh read (no override; a refusal sends nothing and keeps the reservation), confirms only when every sent field is reported on its own grid (never a derived trailing price) and then updates `legs.futu_terms`.
- **Futu observation and read fields.** The observation pass stores the Xenon type label, trigger, trail type / value / spread and session in `legs.futu_order` (placeholders dropped; never the intent or terms). `GET /orders?broker=FUTU` returns `orderType` as the contract label (was the raw Futu enum for non-LMT / MKT types), `auxPrice`, `trailType`, `trailValue`, `trailSpread`, `session` (observed, else confirmed terms, else placement; null for a term the type lacks). WAITING_SUBMIT stays UNKNOWN with its demand kept. `/health` futu adds `capabilities[env] = {tuples, modify}` after the allowlists.
- **Futu order types in the web.** The Futu ticket offers order type, TIF and session only from `/health` `futu.capabilities[env].tuples` for the row's security type (no client copy of the matrix): `LMT`, `MKT`, `STP`, `STP LMT`, `MIT`, `LIT`, `TRAIL`, `TRAIL LIMIT`, each with only its own fields (trigger, limit, trail type and value, trail spread; `TRAIL LIMIT` has no limit price). For stocks a session selector replaces the "Fill outside RTH" checkbox, and a confirm line names the type, trigger and trail. `/api/orders/place` and `/api/orders/modify` forward `orderType` / `auxPrice` / `trailType` / `trailValue` / `trailSpread` / `session` and `newAuxPrice` / `newTrailValue` / `newTrailSpread` for FUTU only; an IB body that carries any of them gets 400 `INVALID_ORDER_BODY`. Futu modify shows the fields of the order's type and is offered only for types in `capabilities[env].modify`; cancel works for every type. A trigger-only or trail-only modify counts as a change (`confirmed:false` never shows success), and the uncertain-operation lock also checks trigger and trail terms. The Futu orders view adds Trigger, Trail and Session columns, and a missing limit price shows `---`. Trigger, trail value and spread must be one whole decimal (`1,5` or `1oops` is invalid and disables submit); BID / MID / ASK references keep the full quote (a 0.5123 bid sends 0.5123, never rounded); a change of order type clears the limit, trigger and trail fields; a trail unit other than RATIO / AMOUNT shows `---`, and modify of that trail value is off while cancel stays.

## [0.16.2] — 2026-10-26

- **futu-quotes serves the US overnight session.** `futu-first` relay clients now get Futu ticks during Blue Ocean overnight (Sun–Thu 20:00 → 03:50 ET; OpenD reports `AFTER_HOURS_END` or `OVERNIGHT`), not only RTH. Overnight rows use `overnight_price` / `_high_price` / `_low_price` / `_volume`, `close` = the RTH last price, `timestamp` = receipt time, and carry the additive field `session: "overnight"` (RTH rows are unchanged). An overnight row is published only when its overnight volume or price changed and its overnight volume is > 0, so a cached replay after a subscribe is never shown as fresh. Overnight, a symbol is `ok` only after one valid overnight trade row was seen (until then `pending/session` on IB), so a symbol with no overnight trading never leaves IB. A missing `session` field means rth. Pre-market and after-hours stay on IB.

## [0.16.1] — 2026-10-25

- **Futu SIMULATE / REAL switch moved into the Futu account tab.** The switch is now part of the FUTU tab card instead of a separate panel under the tab bar, and it shows on every page that shows the tab bar. Picking an env from the IB view also opens the Futu tab. Active REAL uses the warning token; the separate SIMULATE / REAL badge is removed because the switch shows the same state.

## [0.16.0] — 2026-10-25


- **Futu orders in the web, with a SIMULATE / REAL switch.** The Futu tab switches between SIMULATE and REAL in the page (no restart); every Futu call carries `?broker=FUTU&account_env=paper|live`. When `/health` `futu.trading[env]` is true, Futu rows get single-leg LIMIT place, and Futu working orders get cancel and modify by `brokerOrderId`; combo, replace and wizard are never offered. An env without an account shows a not-configured empty state. Uncertain outcomes (`PLACE_OUTCOME_UNKNOWN`, `FUTU_SEND_UNCERTAIN`) lock the order form. Futu reason codes join the shared `ReasonCode` enum and `orderReasonCodes.ts`.
- **futu-quotes healthcheck without log noise.** The service answers plain HTTP `GET /healthz` with 200 through the websockets `process_request` hook (no upgrade, the relay connection is untouched). `deploy/compose.prod.yml` probes `/healthz` instead of a bare TCP connect, which made websockets log a handshake traceback every 30 s. Compose change: stage the new file per `deploy/README.md` section A on the next deploy.
- **Futu observation pass.** New `api/services/futu_observation.run_pass(scope, ctx)`: takes `pass_seq` and `as_of` first, verifies the configured scope with `get_acc_list`, reads positions, open orders and history orders (`refresh_cache=True`, explicit acc_id / env; history covers `as_of - FILL_LAG_S` across the ET date boundary; deals are never read), mirrors every US order into `order_submissions` (broker FUTU, `broker_order_id`, Xenon rows bound by remark `xo:<id>`, others as `futu-<account_env>-<acc_id>-<order_id>`; option legs only with IB-qualified metadata, else NULL legs), and only then publishes a FUTU `account_snapshots` row with the observation stamp through the stale-publication fence. Fills are order grain and replay-safe (`orders_store.mirror_futu_order`): under the scope lock and a row lock, only a cumulative `dealt_qty` above the stored `filled_qty` writes one `order_fills` row (`exec_id futu:<account_env>:<acc>:ord:<order_id>:<cum_qty>`, price from the notional change, time = Futu `updated_time` converted from New York wall time to UTC). Any failed, unsupported, unkeyed, multi-leg, fractional-quantity or unpersisted read publishes `complete:false`; an unverified account, failed position read or missing `pass_seq` publishes nothing and invalidates the Gate-4 view.
- **Futu observation loop, ledger read path, no Futu JSON file.** The API lifespan runs the observation pass every 60 s for the configured Futu trade scope (`FUTU_TRADE_ACC_ID` + `FUTU_TRADE_ENV`) on its own worker thread and its own read-only trade context; option legs are qualified through the IB pool `data` role (place-gate path, fail closed). It is not gated on `XENON_FUTU_TRADING`, is skipped under `XENON_READ_ONLY=1` or without a configured scope, stops on shutdown (1 s budget step), and an unreachable OpenD invalidates the Gate-4 view instead of renewing a stamp. A published `complete:true` pass runs the shadow naked-short audit (`service_health` keyed by the FUTU scope; it never cancels). `GET /orders?broker=FUTU` and `POST /orders/refresh` for FUTU now read `order_submissions` + `order_fills` of the configured scope: string `submissionId` and `brokerOrderId`, the persisted `modifySequence`, `broker` / `accountEnv` / `brokerAccount`, `orderId` / `permId` 0 (no display hash); no configured scope returns empty lists with `scope_reason`, and a request scope (the tab's account) that is not exactly the configured scope returns empty lists with `scope_reason: "scope_mismatch"` plus both scopes, so paper and live orders never blend. `futu_orders` / `futu_trades` are history only. `POST /futu/sync` stores its result as a FUTU display snapshot in `account_snapshots` (payload without `observation`, bankroll = net liquidation) instead of `data/futu_portfolio.json`; `GET /futu/portfolio`, the partial-failure comparison and the `/health` futu block read it from Postgres (the error sidecar file is gone; `/health` also reports the scope's last observation `as_of` / `complete` and `observation_loop`). Display readers never select observation rows, so their `bankroll` 0 (no balance read) is never shown as a real value.
- **Futu observation review fixes (PR #236 round 1).** A remark or stored broker id binds a Futu order only when symbol, security type, side and option right / expiry / strike (and con_id when both known) match; a contradiction binds and writes nothing and makes the pass incomplete. Only rows positively outside the US (known non-US code prefix, market field not US) are left out; a missing or unknown code, or a code that contradicts `position_market` / `order_market`, makes the pass incomplete. A pass that exceeds its timeout invalidates the scope's Gate-4 view at once and is retired: it cannot publish (checked under the scope lock) and a late connection is closed; the worker thread owns the trade context, and a connection that arrives after stop is closed without a pass. Fill deltas use a precise cumulative notional kept in the legs payload (`futu_fill`) instead of the 4-place display average; a non-positive notional change is left unresolved. Observed TIF (column), order type and trigger price (`legs.futu_order`) are stored on insert and update and returned by `GET /orders` for FUTU. Display snapshots are read per exact scope (broker, env, account): `/futu/portfolio` uses the FUTU request scope (`scope_mismatch` when only other scopes have a snapshot), cooldown and partial-failure use the client's matched scope, `/health` uses one scope for display and observation. `POST /orders/refresh` for FUTU runs or joins one observation pass of the matching configured scope and returns the ledger; it no longer runs the history sync.
- **Futu observation review fixes (PR #236 round 2).** A cumulative fill increase without a usable price never makes the row terminal: it stays non-terminal (new rows UNKNOWN) with reason_code `FUTU_FILL_UNPRICED` and the pass is incomplete. After a pass with every read OK, a non-terminal Futu row that was not seen is looked up by order id; absent from two such passes it becomes UNKNOWN (`FUTU_NOT_OBSERVED`, WARNING log, demand kept); a partial pass or a failed lookup counts nothing. The history window ends at the read instant (ET), never in the future. Qualified option metadata is cached across passes (failures are not cached), and a disconnected IB `data` role leaves options unresolved without a reconnect. With a configured Futu trade scope, `get_broker_scope(FUTU)` returns it without OpenD (no 503, never another account's cached scope). Portfolio, preflight, entry-date and snapshotter-heartbeat readers of `account_snapshots` skip FUTU observation rows. Futu Postgres reads in async routes (`/futu/portfolio`, `/futu/sync`, `/health`, `/admin/operator`, `GET /orders`, `/orders/refresh`) run off the event loop. The raw Futu status is stored (`legs.futu_order.status`) and shown as `futuStatus`; `FILL_CANCELLED` logs a WARNING. External submission ids are `futu-<account_env>-<acc_id>-<order_id>`. An option without qualified metadata gets a NULL multiplier, not 1.
- **Futu observation review fixes (PR #236 round 3).** With a configured Futu trade scope the `/futu/sync` display client selects exactly that account and environment (`FUTU_TRD_ENV` applies only without a scope; an unlisted account fails the connect, no fallback), and every sync answer (cooldown cache, preserved or fresh) requires the client's matched account to equal that scope, else `{ok:false, code:"scope_mismatch"}` with nothing served or stored. Fill ids are `futu:<account_env>:<acc_id>:ord:<order_id>:<cum_qty>`; an exec_id already owned by another submission advances nothing (`FUTU_FILL_CONFLICT`, row non-terminal, pass incomplete). Option identity is the exact Futu code stored in `legs.futu_code` (the 2-dp strike column is display only); without a code on both sides the strike must be exactly equal, else the observation is a mismatch (fail closed). `/health` futu `held_rows` lists, per reason (`FUTU_FILL_UNPRICED`, `FUTU_FILL_CONFLICT`, `FUTU_NOT_OBSERVED`), the count and Futu order ids of non-terminal rows that hold demand; operator action for a held unpriced or conflicting fill: check the order in the Futu app, then record the fill (scoped exec_id, `filled_qty`) or set the row terminal by hand; until then Gate 4 stays fail closed for the scope. `FUTU_NOT_OBSERVED` rows are looked up by id again on every 10th full pass (and the first after a restart), at most 20 per pass, fewest misses first so the set rotates; a terminal result updates them normally. A refused Futu connect (for example the configured account not listed) closes the context it opened and keeps no matched account. FUTU `POST /orders/refresh` waits at most 10 s for the pass, then returns the current ledger with `refresh: "in_progress"` (the pass continues).
- **Futu orders (X3c).** Single-leg Futu stock / option limit orders: `POST /orders/place|cancel|modify?broker=FUTU&account_env=paper|live`. A Futu order adapter (`execution/futu_orders.py`, the only module that sends Futu SDK trade mutations; remark `xo:<submission_id>`, price-increment check without rounding, unlock with `FUTU_TRADE_PWD_MD5` before every REAL mutation and never for SIMULATE) runs on its own Futu order worker (`api/futu_order_worker.py`: one thread and trade context for both envs, `get_acc_list` + `verify_live` of the job's scope before every core, settled exactly once like the IB owner). The SIMULATE / REAL switch: `FUTU_TRADE_ACC_ID_SIMULATE` / `FUTU_TRADE_ACC_ID_REAL`, `XENON_FUTU_TRADING` as a set over `simulate`, `real` (the old `1` is off), `account_env` on the query as the request scope, one observation loop per scope including stored scopes with unresolved rows, and the per-env `FUTU_SCOPE_CHANGE_PENDING` veto. Dispatch runs in the existing cores in the order read-only → capability → Gate 4 (FUTU request scope) → IB quote gate (IB down → 503 `QUOTE_UNAVAILABLE`) → worker; own rows carry the observation identity (broker FUTU, request scope, `broker_order_id`, `legs.futu_code`). An uncertain send is UNKNOWN and never re-sent (non-OK after the send mark = UNKNOWN; recovery only by the exact remark); cancel keeps the demand and modify keeps the larger reservation until fresh Futu evidence (a modify sends Futu's current value for an omitted field, read fresh on the worker, and nothing without that read; Gate 4 reserves the full outgoing quantity, also for a price-only modify, before the send; confirmation compares on the price tick; an unconfirmed modify keeps `MODIFY_PENDING` until an operator resolves it); all row changes after a send go through the observation mirror, the adapter never writes `order_fills`. Replace, combo and wizard stay refused for FUTU, and a FUTU request never falls back to IB. `/health` futu adds `trading: {paper, live}` (booleans from the same checks as the guard, no account numbers). Runbook: `docs/runbooks/futu-orders.md`.
## [0.15.0] — 2026-10-24

Summary: argon's live quotes can come from Futu instead of IB (new `futu-quotes` service + relay per-client routing, protocol-ping liveness); Futu order capability groundwork (default off, no Futu order path).

### Deploy

- **Migration required before the new api starts.** The api now selects `order_submissions.broker_order_id`. Run the migrator once: it applies `2026_10_20_drop_signal_tables` (DESTRUCTIVE, shipped in an earlier release, see its `### Database` note: pg_dump the twelve tables first) and then `2026_10_24_futu_order_sub` (additive).
- **Compose change.** `deploy/compose.prod.yml` adds the `futu-quotes` service (api image, command `xenon-futu-quotes`, explicit env only, not host-published) and `FUTU_QUOTES_URL` on `realtime`. Stage it per `deploy/README.md` section A and start `api web realtime futu-quotes`.

- New `xenon.futu_quotes` package: a `futu-quotes` service that streams Futu OpenD US stock/ETF `SubType.QUOTE` subscriptions to the ib_realtime relay over one internal WebSocket (`assign`/`batch`/`health` frames per `docs/architecture/futu-quotes.md`). Handles admission with a quota ceiling, epoch/`gen` fencing, 60 s minimum-lifetime + 60 s grace unsubscriptions, RTH-only session gating from `get_global_state().market_us`, last-value batching at ≤1 batch/100 ms, ET→UTC source timestamps with DST handling, and a per-symbol high-water mark that drops replayed stale quotes. Entry point `xenon-futu-quotes` is registered by the integration PR; `--self-test` runs a fake-OpenD scenario over a real loopback WebSocket. Runbook: `docs/runbooks/futu-quotes.md`. Review round 1 hardened the lifecycle: the SDK reconnect callback preserves its `(ret, msg)` contract and only reports recovery on success; provider ops are reserved per symbol, re-validated under the serialized adapter lock, and fenced by an OpenD session epoch so stale-session completions cannot certify the new session; failed unsubscribes keep ownership counted with bounded-escalated retries; reconnect reconfirms believed-owned subs without double-charging quota; buffered quotes are invalidated on ok-set loss, owner replacement and OpenD loss; and failed snapshot batches isolate per-code so one bad code cannot reject the rest. Review round 2 replaced the per-path patches with one design: every provider operation (verify, subscribe, unsubscribe, session and quota polls) runs on one worker, one at a time; each op carries the session epoch and a per-symbol reservation id taken at evaluation, and the worker re-checks both plus the current state and deadlines (a renewed removal gets its own grace) right before the SDK call and again after it, discarding stale results without touching state (an old-session down error no longer marks the new session down). A held subscription stays counted until an unsubscribe releases it and is ok only after a subscribe succeeds in the current session, so reconfirm intent survives failed attempts and failed quota refreshes and never frees a slot for a new symbol. SDK pushes and lifecycle callbacks are dropped when they come from a closed or replaced context. A want removal, session close, owner change and OpenD loss/reconnect drop buffered quotes at the transition. Only a definite not-found makes `unknown_symbol`; generic snapshot errors retry with backoff. Review round 3: a subscribe counts as held from the moment it is sent; a completed unsubscribe releases it, and a definite rejection releases only a fresh admission's provisional slot (a rejected reconfirm keeps the earlier ownership and retries with backoff for any reason, including an invalid-code or not-supported answer), so a lost reply or a late old-session success can no longer free the slot for another symbol; an unsubscribe that releases a symbol also drops its buffered quote.
- **WS relay: protocol ping/pong counts as client liveness.** `ib_realtime` keep-alive moved into `liveness.js`; a client that answers protocol pings (or sends its own) stays connected without the app-level `{action:"pong"}`, so argon-style service clients no longer hit the 65 s dead-peer close every ~90 s. The relay now also sends a `ws.ping()` per sweep; browser `{type:"ping"}`/`{action:"pong"}` handling is unchanged. Tests: `scripts/infra/ib_realtime/__tests__/liveness.test.mjs`.
- **WS relay: per-client Futu-first quote routing + `mdType` exposure.** When `FUTU_QUOTES_URL` is set, `X-API-Key`-authed connections may opt into `?quotes=futu-first` and receive Futu-sourced ticks for admitted US-stock symbols; browsers, ticket clients, and un-opted connections stay on IB unchanged. New `futu_quotes_client.js` (per-connection `gen` wants, 10 s protocol ping / 20 s pong timeout, 15 s health deadline, reconnect) and `futu_routing.js` (ok-set hold-down ≥60 s, per-source hydration/buffers, failover to the IB line budget) keep one symbol live on both sources with each client seeing only its routed one; indexes/options/forex/non-US keys are pinned to IB and never enter `want`. `status` frames gain `providers.futu:{connected,opend_connected,quota}`; `ib_connected` is unchanged. IB `marketDataType` callbacks are recorded per symbol, reset on resubscribe/reconnect, and exposed as `mdType` in price frames. Unset `FUTU_QUOTES_URL` = exactly today's behavior. Tests: `futu_routing`, `futu_quotes_client`, `md_type`, `futu_relay_e2e` (real relay subprocess vs fake futu service).
- **Futu order capability table, default off (no Futu order path yet).** New `execution/broker_capabilities.py` lists per-broker operations: IB keeps every operation; FUTU `place_single` / `cancel` / `modify` are allowed only with `XENON_FUTU_TRADING=1` and a valid selected scope, and FUTU `replace` / `combo` / `wizard` are never allowed. New `execution/futu_scope.py`: `configured_scope()` reads `FUTU_TRADE_ACC_ID` (exact nonzero digits) + `FUTU_TRADE_ENV` (SIMULATE → paper, REAL → live), independent of the trading flag; `verify_live()` matches that scope against `get_acc_list()` rows (exact acc_id, env, US authority, ACTIVE; no first-account fallback). `guards.ensure_order_allowed` checks the capability for non-IB brokers (403 top-level `reason_code: READ_ONLY_BROKER`, `XENON_READ_ONLY` first) and still refuses every Futu write, because the order cores have no Futu dispatch.
- **Migration `2026_10_24_futu_order_sub` (additive, no writer).** `order_submissions` CHECK `ck_order_sub_broker_ib_only` is replaced by `ck_order_sub_broker` (`broker IN ('IB','FUTU')`); new nullable `broker_order_id TEXT` with partial unique index `uq_order_sub_broker_order_id` on (broker, account_env, broker_account, broker_order_id) WHERE broker_order_id IS NOT NULL. No row changes; trades and wizard tables stay IB-only; order_fills already allowed FUTU. Lock waits bounded by `lock_timeout = 5s`. Downgrade restores the IB-only CHECK and refuses (nothing changed) while any FUTU row or broker_order_id value exists.
- **Caller guard covers the future Futu adapter.** `scripts/checks/order_path_caller_allowlist.py` now protects `xenon.execution.futu_orders` (only `services/order_entry.py` and the module itself may import it; direct, relative and multiline imports are caught) and fails any non-adapter Python file that calls a Futu SDK mutation (`place_order`, `modify_order`, `cancel_all_order`, `unlock_trade`) on a Futu trade context: names or attributes assigned from `Open*TradeContext(...)`, aliases, `with … as`, subclasses, context attributes created in another file (e.g. `client._trd_ctx`), and literal `getattr`. IB's `client.place_order` and the existing IB allowlist are unchanged.
## [0.14.0] — 2026-10-23

Summary: the wizard protect take-profit goes through the Gate-4 reservation; the Futu closed-trades matcher applies reviewed splits and option re-codes; the IB NAV backfill works again and can load historical deposits/withdrawals into `ib_cash_flow`; docs match the real Postgres roles. No Alembic migration and no compose change.

- **IB NAV + cash-flow backfill path fixed.** `scripts/migrations/_2026_06_02_backfill_nav_from_ib_flex.py` works again: `--from-csv` parses every section of an IB Flex CSV export, writes NAV rows through `upsert_nav_sync(source='close')` (its old hand-rolled `ON CONFLICT` targeted the pre-`2026_06_03_nav_src_pk` primary key and always errored), and upserts `Deposits/Withdrawals` CashTransactions into `xenon.ib_cash_flow` via the shared `ib_nav_flex` parser/persister — the only historical cash-flow ingest path, since the daily Flex job is a rolling ~2-week window. Adds `--dry-run` (counts only), drops the no-flag Flex-API path (a duplicate of `xenon-nav-flex-refresh`; no flags now prints usage, exit 2), and refuses writes under `XENON_READ_ONLY=1` (exit 3). `fetch_ib_nav_series` now logs a WARNING once per run when the statement has no CashTransactions section or the section lacks required columns, instead of silently leaving `ib_cash_flow` empty. Runbook backfill steps: `docs/runbooks/nav-flex-refresh.md` § Historical backfill.
- **Futu closed-trades matcher handles corporate actions.** `match_closed_lots` gained a pure `apply_corporate_actions` pre-pass fed by a static reviewed `CORPORATE_ACTIONS` list (apex-lake, checked 2026-10-22): pre-ex-date deals of a split stock get qty×ratio and price÷ratio (KLAC 1:10, MULL 1:25, MUU 1:20, RKLX 1:3), and pre-ex option deals of a renamed contract family are re-keyed to the post-adjustment code (LCID…→LCID1… after the 10:1 reverse split; ZIM put strikes −$0.84 after the special dividend). Both callers (`sync_futu_orders`, `backfill_futu_nav`) pass the same list, so the closed-trades surface and the NAV walk stay in sync. On the prod deal export this cuts "close with no open lot" warnings 48 → 35 and corrects the stored realized-P&L sum by +12,615.65 (−37,052.27 → −24,436.62); the remaining warnings are pre-upgrade transfer-in positions plus one unexplained share. The warning text now names the likely causes (pre-upgrade transfer-in, unhandled corporate action) instead of "(pre-inception?)". Rebuilding prod `futu_closed_trades` happens on the next sync, not in this change.
- **Wizard protect take-profit goes through Gate 4.** `POST /wizard/sessions/{id}/protect` now places its GTC SELL BAG take-profit through `order_entry.place`, like every other order: Gate 4 checks coverage against the snapshot and every working reservation and writes an `order_submissions` row before the order owner sends it. A refusal returns the place response with its top-level `reason_code` (for example `COMBO_SPLIT_REQUIRED`, `ETF_CALL_UNCOVERED`, `ORDERS_HALTED`) and leaves the session FILLED. The TP is always a guaranteed combo (`nonGuaranteed: 0`), so IB cannot leg it and leave the short call naked; its body carries only symbol, quantity and the legs' contract fields from the entry. The session is claimed (FILLED → `PROTECTION_PENDING`) before the place, so two protects cannot send two TPs; only a response that proves nothing is at IB releases the claim (an uncertain place or an exception keeps it). The TP attempt id, target, alert threshold and polarity are stored before the send, and protect is re-entrant for `PROTECTION_PENDING`: a retry binds a TP that is live at IB and arms the Risk Alert without a new order, places one new TP only when the stored attempt was never at IB (FAILED / REJECTED with no IB ids) or was never reserved, and otherwise answers 409 `PLACE_OUTCOME_UNKNOWN`. A dead TP that may have reached IB (CANCELLED, or with an IB order id / permId) is never re-placed: its executions cannot be proven, so protect answers 409 `PROTECT_TP_PARTIAL` ("check its fills and close any rest from the order ticket"), sends nothing, and the session stays `PROTECTION_PENDING` for the operator. An explicit IB reject after the send now keeps the IB order id / permId the place core learned on the REJECTED `order_submissions` row (all orders), and a protect place refusal releases the session only when that row proves IB never had the TP. With `XENON_GATE4_NETTING` unset (the default) a vertical close with no extra cover is refused `COMBO_SPLIT_REQUIRED`. The TP retry loop, `place_combo_tp` and the unused `ComboWizardIbAdapter` module are removed.
- Docs match the real Postgres roles: prod uses `xenon_app`; the new limited `xenon_dev` role is read-only on `core_dev` and will own `core_test` (runbook `docs/runbooks/dev-prod-db-cutover.md`, ownership step pending).
- `refresh-core-test.sh` runs as `xenon_dev` by default and dumps only the `xenon` and `events` schemas (`pg_dump -n xenon -n events`); the LaunchAgent is not installed yet.
## [0.13.0] — 2026-10-21

Summary: the Ratings tab works again on FastAPI; one ET-aware market calendar that treats holidays as closed; `server.py` split into route and service modules; docs and runbooks match the code.

- **Ratings tab works again.** `GET /ticker/ratings?ticker=XYZ` on FastAPI returns the raw Unusual Whales analyst rows (`screener/analysts`) with `as_of` and `source`. It caches each ticker for 15 minutes and rejects malformed UW rows (wrong types, non-finite or oversized numbers) and evicts the rejected payload from the shared 60 s UW cache, so the next request refetches instead of reusing it. Status codes: 400 `INVALID_TICKER`, 502 `UW_UPSTREAM_ERROR` (UW error or malformed payload), 504 `UW_TIMEOUT` (no answer within 12 s), 503 `RATINGS_BUSY` (both of the 2 dedicated fetch workers are in use; a stuck UW read keeps its worker until it returns). Every error has a top-level `reason_code`. `/api/ticker/ratings` calls it through `xenonFetch` instead of spawning the removed `xenon-fetch-analyst` CLI and maps failures to stable generic bodies with a top-level `reason_code`: `INVALID_TICKER` 400, `UW_UPSTREAM_ERROR` 502, `RATINGS_UNAVAILABLE` 502 (FastAPI unreachable or unexpected body), `RATINGS_TIMEOUT` 504, `RATINGS_BUSY` 503. The tab lists recent analyst actions with the price target of each; it no longer shows an aggregate recommendation or consensus counts.

- **One market calendar (PR-21).** `xenon.utils.market_calendar` is the only Python "is the US market open" check (the web `useMarketHours` hook and `scripts/infra/ib_realtime/ib_realtime_server.js` have their own): ET-aware, 09:30 to 16:00 (16:00 exclusive), weekdays minus `market_holidays.json`. Option order entry, the portfolio-snapshot stale thresholds (order preflight and gate4) and the performance cache TTL now treat market holidays as closed; before, they used a weekday-only rule. The snapshot stale checks keep the whole 16:00 minute open (`close_inclusive_minute`). A naive `datetime` is rejected; a year missing from, or malformed content in, the holiday file raises `CalendarYearMissing`, and the runtime callers above log an error and fall back to the weekday rule. The duplicate `utils/market_hours.py` and `execution/market_hours.py` are deleted; `xenon-market-hours` now lives in `market_calendar`. Early closes (13:00 ET) are still not modelled.
- **Order-path caller guard parses Python imports.** `scripts/checks/order_path_caller_allowlist.py` now reads `.py` files with `ast`, so multiline imports (`from xenon.execution import (\n    ib_place_order,\n)`) and relative imports of `ib_place_order` / `ib_order_manage` outside the allowlist fail the build; before, only single-line imports matched. Other file types keep the line match. A file that does not parse falls back to the line match. Tests: `scripts/tests/test_order_path_caller_allowlist.py`.
- **`server.py` split (PR-20).** `src/xenon/api/server.py` keeps the app, lifespan, middleware and shared state; HTTP handlers moved to `src/xenon/api/routes/{market,order_entry,futu,blotter,system,portfolio}.py` and the place / cancel / modify / replace cores to `src/xenon/api/services/order_entry.py`. Routes, status codes and lifespan tasks are unchanged.
- Docs match the code after the uplift (root and subdirectory `CLAUDE.md`, README, release and deploy runbooks with the controlled cutover).

## [0.12.0] — 2026-10-21

Summary: the Futu tab syncs again after page load; the naked-short audit gains an enforce mode that stays off unless `XENON_AUDIT_CANCEL=1`.

- **Futu positions sync on page load again.** The sync loop never started until the browser tab changed visibility: the market-hours state flips from CLOSED after mount, and that flip cancelled the first scheduling. The loop now starts once the cached read is done and the market is active, in either order. When the cached snapshot is older than 60 s (or missing), the first sync runs after 0.5 s instead of 30 s. Disabling or unmounting clears pending timers and prevents an in-flight sync from rearming the loop; that request may still finish.
- **Flex token banner reads FastAPI.** `/api/flex-token` uses `GET /health` `flex_token` through `xenonFetch` instead of reading `data/flex_token_config.json`; the banner no longer shows the masked token.
- IB farm-status codes 2107 and 2119 are logged at INFO, not ERROR (1100 / 2110 / 326 unchanged).
- Futu closed-trade rebuild: a close with no matching open lot warns once per deal per process (repeats at DEBUG) instead of on every sync. Its realized P&L is still omitted; the cause is not yet verified.
- Release workflow: the dead `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` build arg is removed.
- **Naked-short audit enforce mode (PR-11), default off.** With `XENON_AUDIT_CANCEL=1` the API cancels audit violations after each sync: one order at a time, newest first, each confirmed by a fresh sync pass before the cancel, then a re-sync before the next, at most 5 per sync. It cancels only orders this environment's order owner placed, by permId (no orderId fallback), through the `POST /orders/cancel` core; a confirmed cancel marks the order `CANCELLED` with reason `NAKED_SHORT_AUDIT`. It never acts under `XENON_READ_ONLY`, with a halted owner, or on a skipped / stale / incomplete audit; an unconfirmed cancel stops it with a `service_health` error. Orders placed in TWS / IBKR Mobile are reported, not cancelled. `xenon-ib-sync --sync` now prints the audit summary as its last stdout line. The API also recovers a script's final JSON object when earlier stdout noise contains an opening brace.

## [0.11.0] — 2026-10-20

- **Single order owner (PR-10).** Every Xenon order is placed, cancelled and modified by one connection: the API pool `orders` role (clientId base+4) on its own worker. An order this environment's owner did not place answers 409 `ORDER_NOT_OWNED` (manage it in TWS / IBKR Mobile). The order-write CLIs are removed.
- **Fail closed on the broker.** A send needs a fresh broker round trip first. For placement, a pre-send broker or owner failure answers 503 (`IB_CONNECTION` / `IB_BROKER_DISCONNECTED` / `ORDERS_HALTED`, nothing sent) and an uncertain outcome 502 `PLACE_OUTCOME_UNKNOWN`. Cancel and modify can answer 503 `IB_CONNECTION` with `amend_outcome: unconfirmed`; that outcome stays uncertain, not "nothing sent". A clientId-in-use (326) or an unexplained loss of the `orders` connection halts order entry (`ORDERS_HALTED`) with zero reconnects.
- **Graceful IB disconnect on shutdown.** On SIGTERM the api stops the order owner and queues a disconnect for every pool role, waiting up to 35 s; compose (`stop_grace_period`) and Watchtower (`WATCHTOWER_TIMEOUT`) allow 60 s to stop. A broker call that blocks past that budget can still leave a stale clientId on the gateway, so the runbook's stale-socket check stays required. Deploy with `docs/runbooks/pr10-order-owner-cutover.md`: the prod compose gains `XENON_IB_CLIENT_ID_BASE`, `IB_REALTIME_STATUS_URL`, `stop_grace_period` and `WATCHTOWER_TIMEOUT`, and `.env` needs `WATCHTOWER_NOTIFICATION_URL`.

### Added

- PR-10 (single order owner), part 1 — IB clientId blocks and broker readiness. `XENON_IB_CLIENT_ID_BASE` (0-800, step 100) gives each environment its own block: prod 0 (pool 3/4/5, relay 10-12, `auto` 20-49), `dev.sh` 100 (pool 103/104/105, relay 110-112, `auto` 120-149). `IBClient.connect` (and every reconnect) refuses an id outside the caller's zone before any socket opens: only the API pool opens pool ids, `auto` stays in base+20..49, Python never opens relay ids, and no caller opens another block. The relay derives its default id from the base and refuses ids outside base+10..19. FastAPI refuses to start when the base does not match the database (base 0 only with `core_dev`). `/health` reports `ib_pool.<role>.broker_readiness` (`unknown` / `ready` / `lost`) next to `connected`: unknown after every (re)connect until an open-orders round trip completes, lost on IB 1100/2110, ready on 1102. This is an operational contract, not fencing.

### Changed

- PR-10 (single order owner), part 2 — connected-client cores, no behaviour change. `ib_order_manage.cancel_order_on_client` / `modify_order_on_client` and `ib_place_order.place_order_on_client` act on an already-connected client and return the structured reply. They never print, exit, arm the watchdog, connect or disconnect; their errorEvent listener is removed in `finally`; the amend stage (`amend_outcome`) is per invocation, not module state. By default a core refuses an order owned by another clientId (`ownership`). The CLIs are thin adapters over the cores with the same JSON / exit-code contract (the cancel/modify CLI still reconnects as the owner). The place CLI's combo summary line and the 110 tick-rejection record now go to the `xenon.ib_place_order` logger instead of stdout/stderr prints.
- PR-10 (single order owner), part 3 — cancel, modify and wizard protect run on the API pool `orders` connection (base+4) through one order owner (`api/order_owner.py`), on its pinned worker. No subprocess, no second IB connection. Positive routing: an order whose recorded placing clientId is not this environment's `orders` id (TWS / mobile / clientId 0, the legacy subprocess range, relay or pool ids, another environment) gets 409 `ORDER_NOT_OWNED` ("cancel or modify it in TWS / IBKR Mobile") with no broker work; an order without a recorded clientId goes to the owner, whose fresh scoped open-order snapshot refuses a foreign or ambiguous owner before any send. `OWNERSHIP` is replaced by `ORDER_NOT_OWNED`. Each action has an invocation record (queued → started → sent → done / abandoned): a request deadline while queued abandons the job (zero sends, amend released); after start the request answers `amend_outcome: unconfirmed` and the worker settles the Gate 4 amend itself with the captured amend id — the request path never settles a job the worker owns. Any failure after the possible-send mark is unconfirmed, never not-sent. Before the core runs, the worker does one fresh open-orders round trip (READY only when that round trip completed and no readiness event moved it); right before every send a non-blocking check refuses on the halt latch or any readiness event since that round trip: 503 `IB_BROKER_DISCONNECTED` / `ORDERS_HALTED`, nothing sent. A supervisor owns the `orders` socket: a loss that is positively a gateway outage (1100/2110 before the loss, gateway port down, the whole pool down, or a gateway restart by the API) gets reconnects with backoff on the orders worker; any other loss latches `ORDERS_HALTED` (503) with zero reconnect attempts, a CRITICAL log and `/health` `status: orders_halted` (still HTTP 200) until the API restarts. A clientId-in-use error at startup or during recovery also latches the halt, with that cause. Request paths never reconnect `orders` (`acquire` / `get_with_reconnect_sync` refuse it; the gateway-restart path reconnects only sync/data). Shutdown stops the supervisor and abandons queued jobs. Until part 4 moves placement onto the owner, orders placed by the place subprocess (clientIds base+20..49) route to `ORDER_NOT_OWNED`.
- PR-10 (single order owner), part 4 — placement runs on the order owner too (`place_order_on_client` on the pool `orders` connection, base+4), so every Xenon order is placed, cancelled and modified by one clientId and the stored `placing_client_id` is always that id. The place job has the same lifecycle as cancel / modify: a refusal before any send (owner missing, broker not ready, orders halted, or an error before the send mark) marks the row FAILED and answers 503 `IB_BROKER_DISCONNECTED` / `ORDERS_HALTED` / `IB_CONNECTION` (nothing sent, the toast is terminal); a request deadline after the job started, or a send without an IB acknowledgement, answers 502 `PLACE_OUTCOME_UNKNOWN` (row UNKNOWN) and the worker still writes the final state when the core returns. An explicit IB rejection from the place core (`amend_outcome: rejected`) stays a terminal REJECTED; it is never read as unconfirmed. The place core validates its own fields (`INVALID_ORDER_BODY`) and never falls back to `XENON_BROKER_ACCOUNT`. The order-path caller guard now scans `src`, `scripts`, `web`, `deploy`, `docker`, `.github` and `pyproject.toml` and fails on any import of the order cores outside `server.py` or any reference to the removed CLIs.
- PR-10 (single order owner), part 5 — shutdown and deployment. The api disconnects every IB pool role on SIGTERM (parallel, 35 s budget, never raises) after stopping the order owner; uvicorn's request drain is bounded to 10 s; `api` gets `stop_grace_period: 60s` in both compose files and Watchtower `WATCHTOWER_TIMEOUT: 60s` (Watchtower stops containers with its own 10 s default, not the grace period). This targets the stale gateway clientIds seen after the 2026-10-20 redeploy; it does not guarantee them away (a broker call blocked past the budget is SIGKILLed with its socket open), and a stale `orders` id halts orders at startup (`ORDERS_HALTED`, fail closed). **Operator:** apply the new `deploy/compose.prod.yml` before the PR-10 image lands and follow `docs/runbooks/pr10-order-owner-cutover.md` (open orders placed by the removed CLIs, clientIds 20-49 / 26, now answer 409 `ORDER_NOT_OWNED` and must be managed in TWS / IBKR Mobile). No database migration in PR-10.
- Place responses for a send that provably never happened: 503 `IB_CONNECTION` / `IB_BROKER_DISCONNECTED` / `ORDERS_HALTED` (row FAILED) replace the old 502 `SUBPROCESS_ERROR`. The web shows them as terminal; `PLACE_OUTCOME_UNKNOWN` (502) is unchanged.

### Fixed

- PR-10 review fixes (Astra T1–T3). The broker round trip no longer runs inside the pre-send hook: ib_async's `openOrder` handler rewrote the cached `trade.order` (price / quantity) after modify had changed it, and the blocking round trip could push a modify past its `send_by`. `IBClient.confirm_broker_ready` no longer returns a stale READY after a timed-out or failed round trip. A cancel that was sent but never acknowledged is now uncertain (`amend_outcome: unconfirmed`; a replacement marks its row UNKNOWN and places nothing) instead of an IB reject; only an explicit IB reject code keeps a sent action terminal. The clientId base ↔ database startup check refuses a `DATABASE_URL` whose query can re-route the connection (asyncpg `?database=`, libpq `?dbname=`, `host`, ...), an unparseable URL or one without a database.
- PR-10 review fixes (Astra T4). A place job that started before the request deadline and finished after it now completes the row: a proven pre-send refusal moves the request's UNKNOWN row to FAILED (releasing the Gate 4 reservation), and an uncertain result stores the orderId / permId / owner clientId it learned on the UNKNOWN row. Neither write touches a row the poller already moved from broker evidence (WORKING / FILLED / ...), and a FAILED write that finds the row moved on is not reported as FAILED.

### Removed

- `api/pool_order_manage.py` (dormant pool cancel/modify, never called) and `ib_pool.acquire_owner` / `ClientIdBusy` (no caller since the naked-short audit stopped cancelling) with their tests — replaced by the order owner.
- The order-write CLIs `xenon-ib-place-order` and `xenon-ib-order-manage` (pyproject scripts, `main` / argparse / JSON-exit adapters, the sender watchdog and the reconnect-as-original-clientId path) and their `CLIENT_IDS` entries 20 / 24. FastAPI no longer spawns any order-write subprocess; `_run_ib_script_with_recovery` serves data scripts only, so the order cooldown lane and `_send_window_closed` are gone. The place verdict's legacy fallback (accepting a payload with a permId but an error status) is removed with the CLI it served.
- `utils/ib_connection.connect_ib` and its legacy `CLIENT_IDS` (raw `ib_async` connect with clientId 0 that bypassed `IBClient`; no caller outside tests).

## [0.10.0] — 2026-10-20

- **Large dead-code removal (pure-portfolio cleanup).** Unused API routes, execution paths, data-layer queries, utilities, the dormant launchd deploy and spent scripts are gone. A new CI job (`ruff --select F`) keeps unused imports and undefined names out.
- **The monitor daemon is removed.** The Wizard Risk Alert now runs as a FastAPI lifespan task and feeds the workspace banner. The IB Flex token expiry appears in the Operator console and on `/health`.
- **Every registered CLI answers `--help` without opening a socket.** `xenon-nav-flex-refresh`, `xenon-futu-statement-sync` and `flex_divergence_check --apply` refuse to write under `XENON_READ_ONLY=1`.
- **IB 326 (clientId in use) is detected.** Auto clientId allocation moves to the next free id instead of failing, and an owner conflict on cancel/modify returns 409.
- **Prod compose fixes.** The relay binds `0.0.0.0` and validates WebSocket tickets against `http://api:8321`.
- **Database: a DESTRUCTIVE migration drops twelve dead signal-layer tables.** Nothing is dropped until the operator runs the migrator; dump the tables first (see Database below).

### Added

- Wizard Risk Alert runs inside FastAPI as a lifespan task (`api/services/wizard_risk_alert.py`). Every 30 s it checks PROTECTED wizard sessions with an enabled alert against the signed combo net mid (ib_pool `data` role, snapshot quotes). It uses the old daemon rule (`net mid <= threshold`). A quote older than 2 min is a "no quote" state and never a trigger. The first crossing records `RISK_ALERT_CROSSED` in `xenon.wizard_events` (no write under `XENON_READ_ONLY=1`). The task never places, cancels or modifies an order. `GET /wizard/alerts` → `/api/wizard/alerts` feeds a new risk-alert banner in the workspace. Set `XENON_WIZARD_RISK_ALERT=0` to disable it.
- `flex_token` field on `GET /health` and `/admin/operator` (IB Flex token expiry, same `data/flex_token_config.json` source and 30/14/7/1-day thresholds as the old daemon handler) and a Flex Token tile in the Operator console.

### Changed

- CI runs `ruff check --select F src scripts` (new `ruff-pyflakes` job, ruff from the locked `dev` extra). Unused imports, unused locals and placeholder-less f-strings are removed across `src/` and `scripts/` (PR-16, no behaviour change).

### Removed

- Dead data-layer code (PR-14a): `db/queries/{orders,trades,wizard,regime_overrides,scans,uw,cache}.py`, the unused `portfolio` and `events` query functions and their tests (the test-only `futu_history` readers stay). Tables stay; a separate migration drops the signal tables.
- `reports/portfolio_performance.py`, `reports/performance_explainer_report.py`, the `reports/blotter.py` shim and `utils/price_cache.py`, with the `xenon-portfolio-perf`, `xenon-perf-explainer` and `xenon-presets` entry points. `fetch_ib_nav_series` and its Flex CSV/cash-flow helpers moved to `xenon.jobs.ib_nav_flex` (used by `xenon-nav-flex-refresh`). `xenon-portfolio-attrib` stays because `GET /attribution` has a web caller.
- Dead utils: `env_loader`, `launchd_calendar`, `launchd_schedule`, `vectorized_greeks`, `presets`, and the write-only UW call-stats collector `uw_api_stats` (removed from `UWClient._get`). `utils/ib_connection.py` stays because `IBClient` imports `assert_broker_endpoint_allowed`.
- `trade_blotter/test_integration.py`, the live-IB connection and P&L probes (`live_ib`-marked; this removes that live integration coverage). Unit tests moved to `scripts/tests/test_trade_blotter_core.py`; `run_pytest_affected.py` no longer special-cases `src/xenon/trade_blotter`.
- The monitor daemon (`src/xenon/monitor_daemon/`, `xenon-monitor-daemon`, `xenon-preset-rebalance`, `config/com.xenon.monitor-daemon.plist`). Its `fill_monitor` and `preset_rebalance` handlers are dropped (the IB activity poller already mirrors fills). In the daemon, `wizard_stop_monitor` was registered without a quote source, so it never fired an alert.
- `POST /ib/restart`, `POST /performance` and `POST /performance/background` (no caller in `web/` or `scripts/`; `GET /performance` and `GET /attribution` stay) and the FastAPI CORS middleware (the browser never calls FastAPI; Next.js proxies server-side).
- `xenon-ib-execute` (`execution/ib_execute.py`), a parallel place-and-monitor path with no preflight, naked-short, quote, idempotency or read-only guard, and no in-repo caller. Place orders through `POST /orders/place`.
- `execution/contract_normalize.py` (tests were its only importer) and `quote_guard.check()` (the server calls `check_market_hours` and `check_payload`).
- Twelve `IBClient` methods with no prod caller: `reconnect`, `clear_subscriptions`, `get_portfolio`, `get_account_summary`, `get_pnl_single`, `place_bracket_order`, `modify_order`, `get_open_trades`, `get_trades`, `get_order_status`, `get_option_price`, `wait_for_fill`.
- Dead `server.py` helpers: the internals-skew expiry cluster (it called three undefined functions), `_SCAN_TYPE_MAP`, `_coerce_float`, `_series_span_days`, `_empty_discover_payload`, `_empty_cri_payload`, the route-level `app.state.broker` branches on `/orders/place` and `/orders/replace` (`ensure_order_allowed` stays the single broker guard), unused imports, and `perf_cache.warm`.
- The dormant launchd deploy mechanism: `scripts/deploy/macmini-{bootstrap,prod,data-promote}.sh`, `config/templates/*.plist.template`, `docs/runbooks/mac-mini.md`, and the superseded `scripts/infra/nav-flex-refresh.sh` + `scripts/infra/launchd/` plists (`deploy/` is the deploy truth).
- Legacy gateway/daemon installers that could not run since the 2026-04-18 path move: `scripts/infra/docker/{cloud,local}.sh` (Hetzner VPS switchers), `scripts/infra/setup_ibc.sh`, `config/com.xenon.ibc-gateway.plist`, `scripts/services/{setup,run}_monitor_daemon.sh`, `config/com.xenon.monitor-daemon.plist`, `scripts/infra/cleanup-dead-code.sh`.
- The `UserPromptSubmit` dead-code audit hook (`.claude/hooks/audit-dead-code.sh`); it mostly reported `__pycache__` noise.
- Spent one-shot scripts: `scripts/spike/`, tracked `scripts/research/*` (Futu statement fetch/inspect/validate, SPX short-put backtest), and `scripts/migrations/` DuckDB/trade_log/Postgres-cutover one-shots with their tests. Kept: `_2026_06_02_backfill_nav_from_ib_flex.py`, `_2026_06_13_repair_zero_qty_fills.py`, the apex schema SQL and `option_chain/`.
- Stale `HANDOVER.md` (performance rebuild, shipped in #109) and the dead `XENON_ORDERS_DB_PATH` entry in `.env.example`.

### Changed

- Every `[project.scripts]` CLI answers `--help` with exit 0 and usage text without opening a socket (`xenon-nav-flex-refresh`, `xenon-market-hours`, `xenon-ib-reconcile` and `xenon-ib-place-order` used to run their job; `xenon-api` started a server). A subprocess test (`scripts/tests/test_cli_help_inert.py`) pins this.
- `xenon-portfolio-report` and `xenon-blotter` use the configured gateway port and `client_id="auto"` instead of 4001 / clientId 55 and 88. `xenon-portfolio-report` no longer reads `data/trade_log.json`; its thesis-check section and Kelly card are gone.
- `xenon-nav-flex-refresh`, `xenon-futu-statement-sync` and `flex_divergence_check --apply` refuse to write under `XENON_READ_ONLY=1` (exit 3).
- One shared `connect_ib` (`execution/ib_connect.py`) replaces the three copies in `ib_sync`, `ib_orders` and `ib_reconcile`. `ib_orders` now prints the `ib_sync` success and troubleshooting text.
- `scripts/infra/docker_ib_gateway.sh` and `scripts/infra/ibc_remote_control.sh` resolve the repo root with the right depth after the 2026-04-18 move; runbook paths follow. `install-pre-commit.sh` already runs all six guards.
- `scripts/release/cut.sh` runs its stdlib Python through `uv run --no-sync` instead of bare `python3.13`.
- `.env.example`: accurate `IB_GATEWAY_MODE` note, `IB_FLEX_*` placeholders; `nav-flex-refresh` and `dev-prod-db-cutover` runbooks point at `deploy/` instead of a mini-side checkout; stale plan citations in the order-path hook and guards now cite `docs/reference/order-path-incident-history.md`; two dead `no_ib_insync` allowlist entries and the `spx_short_put_backtest.py` Yahoo baseline dropped.

### Fixed

- IB error 326 (clientId already in use) is now detected from `errorEvent`. ib_async 2.1.0 raises an empty `TimeoutError()` after 326, so the old text match never fired: auto client-id allocation failed on one busy id instead of trying the next, and `xenon-ib-order-manage` owner reconnects were classified `connection` (503) instead of `ownership` (409). `IBClient.connect` now raises the typed `ClientIdInUse` (explicit id: fail fast; auto: rotate).
- `deploy/compose.prod.yml`: the realtime service now sets `WS_HOST=0.0.0.0`. Since 0.9.0 the relay binds `127.0.0.1` by default, so inside the container the published port refused argon, apex and the browser. The web healthcheck now probes `/` instead of the removed `/sign-in` page. Prod was hot-fixed with the same two lines on 2026-10-19.
- `deploy/compose.prod.yml`: realtime sets `TICKET_VALIDATE_URL=http://api:8321/ws-ticket/validate` and api sets `IB_REALTIME_STATUS_URL=http://realtime:8765/status`. Both defaults pointed at the container's own loopback, so browser WS tickets failed with `validate_unreachable` (since before 0.9.0) unless an env file overrode them. `.env.example` now says prod `XENON_ALLOWED_HOSTS` belongs in `web.env`.

### Database

- **DESTRUCTIVE migration `2026_10_20_drop_signal_tables`: this release DROPS twelve dead signal-layer tables** left over from the pure-portfolio pivot (no reader or writer remains in `src/`, `scripts/`, `web/`, `deploy/` or `docker/`): `scan_results`, `cri_series`, `vcg_series`, `gex_snapshots`, `uw_analyze_snapshots`, `uw_analyze_flow_alerts`, `uw_analyze_gex_strikes`, `uw_analyze_short_volume_trend`, `uw_flow_events`, `uw_flow_event_ticks`, `ticker_cache`, `uw_api_stats`. It also drops the `xenon.regime_state` view (it only projected `vcg_series` + `cri_series`) and the four `fanout_uw_*` trigger functions with their triggers. `regime_overrides` (order-path audit) is NOT touched. The migrator is a manual step (`docker-compose --profile migrate run --rm migrator`), so nothing is dropped until the operator runs it. `downgrade()` recreates the structure only, never the rows.
  **Operator, BEFORE running the migrator**, dump exactly these tables (schema + data, custom format) and keep the file:

  ```bash
  # libpq does not accept the app's driver schemes (postgresql+asyncpg:// / +psycopg://); strip them.
  PGURL="$(printf '%s' "$DATABASE_URL" | sed -E 's#^postgresql\+(asyncpg|psycopg)://#postgresql://#')"
  BACKUP="/path/to/backup/xenon-dropped-signal-tables-$(date +%Y%m%d).dump"
  # Use a pg_dump whose major version matches the server (prod is PostgreSQL 17).
  pg_dump -Fc "$PGURL" \
    -t xenon.scan_results -t xenon.cri_series -t xenon.vcg_series -t xenon.gex_snapshots \
    -t xenon.uw_analyze_snapshots -t xenon.uw_analyze_flow_alerts -t xenon.uw_analyze_gex_strikes \
    -t xenon.uw_analyze_short_volume_trend -t xenon.uw_flow_events -t xenon.uw_flow_event_ticks \
    -t xenon.ticker_cache -t xenon.uw_api_stats \
    -f "$BACKUP" && pg_restore --list "$BACKUP" | grep -c 'TABLE DATA'   # expect 12
  ```

  Run the migrator only after the dump succeeds and lists 12 `TABLE DATA` entries. `DATABASE_URL` is the prod (`core_dev`) connection string from the operator's own environment; never echo it or paste it into tickets or logs. The `-t` patterns also dump the sequences owned by those tables.

## [0.9.0] — 2026-10-19

### Removed

- The standalone marketing site (`site/`, 68 files) and its site-only tooling — the core product does not need it.
- Clerk's runtime integration: `@clerk/*` packages, sign-in/up pages, providers, the FastAPI JWT path, `CLERK_*` runtime env vars, `ALLOWED_USER_IDS`, `XENON_DISABLE_AUTH`, `PLAYWRIGHT_DISABLE_AUTH`, and `dev.sh --no-auth`. One stale `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` build argument remains in `release.yml`.
- ~18k lines of unreachable web code and most of `lib/tools/` (kept only `runner.ts` for the Ratings tab), the dead regime client in `OrderTab`, mirror tests, the dead `VERCEL === "1"` HSTS branch, and the unused `@vercel/analytics` dependency.
- The dead nightly Playwright workflow (disabled for inactivity; every recent run failed) and 31 obsolete e2e specs that targeted routes removed in the pure-portfolio pivot. The standalone root-page performance script (`prices-performance.test.js`) is also gone — the root page is now covered by `shell-smoke.spec.ts`.

### Changed

- **Auth is credential-only; the browser boundary is enforced.** FastAPI accepts `X-Internal-Token` (Next → API) or a path-scoped `X-API-Key` when `XENON_AUTH_ALLOW_DEV_OPEN` is unset — `/health` is the sole exempt path, the loopback pass is deleted, and the dev-open flag remains an explicit operator bypass (a `tailscale serve` or Docker proxy makes every peer look local). Next middleware now guards every `/api/**` request: `Host` must be on `XENON_ALLOWED_HOSTS`, GET/HEAD require `Sec-Fetch-Site: same-origin` or `none` when present, and mutations need a matching `Origin` (or the same-origin marker) plus `Content-Type: application/json`. Rejections return 403 before any FastAPI call. Realtime relay upgrades authenticate with a single-use ticket or an `Origin`-absent service key; a supplied non-empty `Origin` must be on the allowlist. **Release prerequisites:** prod env must set `XENON_ALLOWED_HOSTS` and `XENON_WS_ALLOWED_ORIGINS`, and the WS service consumers (`argon-ws-consumer`, apex) must send `X-API-Key` on upgrade.
- **Order idempotency is account-scoped.** The attempt key is now `(broker, account_env, broker_account, client_attempt_id)` — caller identity no longer participates. A retry with the same key and a different body returns **409 `ATTEMPT_BODY_MISMATCH`** instead of a duplicate order. `order_submissions` gains a durable `legs jsonb` column; the migration is expand-only and must be applied (manual migrator) before or with the new image.
- **One natural-quote implementation.** `computeNetOptionQuote` now serves `ModifyOrderModal`, the OrderTab combo, `resolveSpreadPriceData` and `OptionsChainTab`; the hand-rolled loops are deleted. Leg ratios are applied everywhere (a 1x2 quote no longer sums raw leg prices) and the credit/debit sign convention holds — the modify strip no longer drops the sign. `ModifyOrderModal` accepts a negative limit for BAG orders (signed credit); single-leg orders still require a price > 0.
- **API resilience.** IB cooldown is split per traffic class — a data failure no longer blocks order entry, and an order failure no longer blocks data. After a gateway restart order requests are never auto-resent (the first attempt may already have reached IB); data requests keep their retry. `/futu/sync` gives connect plus fetch one 15s response deadline — pending work stays single-flight on a dedicated executor and cleanup runs off-loop, so a hung OpenD returns 503 instead of wedging the route. The Postgres LISTEN/NOTIFY subscriber reconnects with exponential backoff, and journal auto-import replays unconsumed outbox rows after a reconnect.
- **Cancel/modify error contract.** Non-JSON or empty upstream error bodies now reach the toast as their text or `HTTP <status>` instead of collapsing to `HTTP 502`; the structured cancel/modify errors produced by `order_error()` carry a top-level `reason_code`.
- **Futu portfolio route proxies FastAPI only** — the disk fallback is gone: 404 renders a "not yet synced" empty state, other failures return 502.
- **CI cleanup.** Dead `--ignore` flags removed; CI runs on Node 22 and `uv run` (no bare `python3.13`); the realtime relay gets a `node --test` job; the `:latest` GHCR tag is promoted only after the full build matrix succeeds, so a failed image build promotes nothing.
- `WorkspaceSections.tsx` (2754 lines) is split into `web/components/workspace/` modules. Pure move; no behaviour change.

- **Naked-short coverage is one module, checked and reserved atomically.** Gate-4 projects coverage from the latest account snapshot plus every working order in a pure `coverage` module that the UI guard mirrors (shared parity fixture). The check and its reservation run under a per-account Postgres advisory lock, so two concurrent orders can no longer both use the same cover; a modify reserves the larger of the old and new terms; a stale or incomplete snapshot blocks the order instead of passing it (`XENON_PORTFOLIO_SNAPSHOT_STALE_S`, 300 s in RTH). A naked `SELL` BAG envelope is refused before IB (`INDEX_CALL_UNCOVERED` / `INSUFFICIENT_SHARES`); a guaranteed all-option BAG that only passes under netting returns `COMBO_SPLIT_REQUIRED` unless `XENON_GATE4_NETTING=1`; the BAG guarantee mode is persisted and a modify cannot change it (`MODIFY_GUARANTEE_MODE`).
- **The post-sync naked-short audit runs again, in shadow mode.** A lazy top-level import made it raise `ImportError` on every sync, so it never ran. It now runs after each `ib_sync`, logs violations and records `service_health`, and cancels nothing (enforce mode is a later release). `/portfolio/background-sync` is single-flight.
- **Orders go to the configured IB account.** Placement uses `XENON_BROKER_ACCOUNT` and never `managedAccounts()[0]` (a two-account gateway listed the other account first; IB error 435 otherwise). Unknown or unmanaged accounts return `ACCOUNT_UNRESOLVED` / `ACCOUNT_NOT_MANAGED`. The portfolio reads that account's snapshots.
- **Uncertain places become UNKNOWN and are recovered, never resent (closes OP-1).** A place timeout, a crash after `placeOrder`, or a Gateway drop leaves the row `UNKNOWN` with its coverage reservation held and returns `502 PLACE_OUTCOME_UNKNOWN`; every order carries `orderRef = xo:<submission id>` and the activity poller recovers `UNKNOWN`/`PENDING` rows by that ref. The place budget is 30 s. Cancel/modify/replace refuse an `orderId` that matches orders of more than one client (`409 ORDER_TARGET_AMBIGUOUS`); a clientId-qualified target acts on exactly that order.
- **Order state machine.** One `ORDER_STATES` list (8 states) backs a `ck_order_sub_state` CHECK constraint (migration `2026_10_18_order_state_check`; lists and refuses unknown states before adding it, `lock_timeout` 5 s); every state writer names its expected prior states, so a late reply can no longer overwrite a terminal state. A modify persists the broker-confirmed price/quantity. `/health` gains `stale_pending_count`.
- Web colors and spacing use CSS/brand tokens only (no raw hex, decorative gradients or soft shadows; a guard test enforces it).
- `deploy/compose.prod.yml` mirrors the production compose (tailnet-only bindings, no secrets) with launchd job templates; `deploy/README.md` is the operator checklist, including the migrator/schema gate for releases with a migration.

### Fixed

- **An identical replay of a recovered stock order is recognised as the same attempt.** After a place whose outcome was unknown, the activity poller recovered the row by orderRef and the open-order mirror replaced the stored leg with IB's view of the stock contract (right `"?"`, strike `0.0`). The idempotency body match compared those IB values with the body's empty right/strike, so an identical replay of that attempt got 409 (`ATTEMPT_BODY_MISMATCH` on the live run, `ATTEMPT_ALREADY_PLACED` on current master) instead of the duplicate answer. A stock leg now compares on action, symbol, sec type and ratio only. Nothing was ever placed twice. A different price, quantity, action or symbol still returns 409.
- **The Python/TypeScript combo-quote parity test runs in CI again.** `computeNetOptionQuote` changed its leg contract (per-leg `symbol`/`id` prefix), so the test crashed in node wherever `web/node_modules/tsx` existed, and CI never saw it because `python-tests` has no `node_modules` and the test skipped. The test (now `scripts/tests/test_combo_quote_ts_parity.py`, no Postgres) passes real `OrderLeg` objects and compares the signed quote (`net_bid`/`net_ask`/`signed_mid` vs TS `bid`/`ask`/`mid`); the `web-tests` job runs it with `XENON_REQUIRE_TS_PARITY=1`, which turns a missing tsx into a failure.
- **A cancelled order no longer comes back as WORKING.** `IBClient.get_open_orders()` returned ib_async's `openTrades()` cache instead of the `reqAllOpenOrders()` response. On the long-lived FastAPI pool that cache keeps an order another client cancelled as `Submitted` forever, so the activity poller resurrected the cancelled order (CANCELLED → WORKING) on its next tick and the disappeared-order sweep could never see it vanish. It now returns IB's fresh response.
- **An order IB accepted is no longer marked REJECTED.** `xenon-ib-place-order` returned `error` on any IB message during the ack wait (such as a 399 "not placed until market open" warning or a connection-wide notice), so the route marked a live order REJECTED and a retry could duplicate it. The CLI now decides from the ordered ib_async trade log (a later warning can no longer hide a rejection or a terminal status, and any fill counts as accepted) and returns an explicit `outcome` (accepted / rejected / unconfirmed) that the route trusts; warnings come back as `ibNotices`. The route stores the real placing clientId and returns a top-level `reason_code` on every place rejection; an empty CLI result marks the order UNKNOWN with `PLACE_OUTCOME_UNKNOWN` (check working orders, do not resubmit). The timeout-after-accept duplicate risk (OP-1) is closed by the place result contract below.
- **pytest is guarded against writing to the `DATABASE_URL` database (prod `core_dev`).** An import-time singleton cached an engine on the exported/`.env` `DATABASE_URL`, and the autouse fixtures kept it whenever the test DB looked offline (any TRUNCATE error, or xdist workers silently sharing one DB). The root conftest now points `DATABASE_URL` at the test DB before collection and refuses to start when the test DB is that database, is `core_dev`, or has no `test` in its name, when either URL has a query key outside a small allowlist (`sslmode`, `ssl*` certs, `connect_timeout`, `application_name`), or when only one URL has an empty host; both autouse fixtures clear the cached engines on every path; only connection errors count as offline (reachability probe and TRUNCATE); worker DB names suffix the parsed database, not the raw URL; an xdist worker DB that cannot be created fails loudly (on-demand creation also used the wrong template name).
- **Read-only mode is actually read-only.** Under `XENON_READ_ONLY=1` (`dev.sh live`), the wizard POSTs, in-process combo-session calls (`submit_combo`, `attach_protection`), and `POST /orders/refresh` could still write or reach IB — only the three order routes checked the flag. One guard now covers every order-mutating path (including `POST /journal`, whose refusal now precedes scope resolution), malformed-JSON and trailing-slash write forms get the same 403 contract, refresh returns the Postgres view without running a sync subprocess, and all `orders_store` writers refuse.
- **Cancel/modify can no longer act on the wrong order.** The stored-owner lookup preferred `orderId` and took the first row, so two clients sharing an `orderId` could inherit each other's orders; identity is now asserted as (scope account, owner clientId, orderId, permId), `permId` never falls back to `orderId`, and the first observed `permId` is latched across reconnects and polls. `IBConnectionError` now produces the JSON error contract instead of a traceback, and BAG credit modifies accept a signed negative limit.
- **Postgres tests actually run.** The async PG fixture called `text()` without importing it, and a broad `except` reported the NameError as "PG unreachable", so tests using that `async_engine` fixture silently skipped on a reachable database. A reachable-but-unmigrated database now errors loudly instead of skipping. Unmarked tests — and their child processes — can no longer dial broker ports 4001/4002/7496/7497 (`live_ib` / `paper_ib` markers with a socket guard).

## [0.8.4] — 2026-10-16

### Fixed

- xenon-api no longer stops answering every weekday from about 16:20 to 20:25 ET. The nightly Futu history sync called `FutuClient.connect()` on the event loop, and the futu SDK retries a refused OpenD forever, so every route (including `/options/greeks`) hung until the Colima watchdog restarted the VM. `connect()` now TCP-probes OpenD (3 s) and fails fast, and the loop runs its connect in a thread with a 30 s bound. While OpenD stays unreachable from the container, the nightly sync records an error instead of hanging.

## [0.8.3] — 2026-10-11

### Added

- `POST /historical/bars` and `/contract/qualify` accept `CONTFUT` (IB continuous futures, ratio back-adjusted on IB's own roll calendar) and resolve `FUT` legs that expired within about the last year (`includeExpired`).

### Changed

- `FUT`/`CONTFUT` with `exchange` `SMART` or empty → 422 (a live SMART `CONTFUT` lookup got no IB answer in 20 s, and the route has no qualify timeout, so it would hold the `data` worker). `CONTFUT` with `end_date_time` → 422 instead of an empty 200.

## [0.8.2] — 2026-10-11

### Added

- `POST /historical/bars` and `/contract/qualify` accept
  `CASH` (FX on IDEALPRO, e.g. EUR/USD) and `CMDTY` (e.g. XAUUSD) contracts, so
  query-key clients can pull IB `MIDPOINT` FX bars.

### Changed

- `POST /historical/bars` and `/contract/qualify` return **404** naming the contract when IB
  cannot qualify it (unknown or ambiguous). Before, bars returned `200 {"bars": []}` —
  indistinguishable from "no data" — and qualify crashed with a 500. Callers that relied on
  an empty list for an unsupported route (e.g. `IND RUT@CBOE`) now get a 404.
- `POST /historical/bars` returns **504** when the IB request hits ib_async's 60 s timeout.
  ib_async cancels the request and returns `[]`, which used to surface as `200 {"bars": []}`
  (seen live: `USDCAD` `17 Y` daily MIDPOINT). Split long windows with `end_date_time`.

## [0.8.1] — 2026-07-04

### Fixed

- IB realtime relay now serves the **correct last price when a streaming line is
  frozen**. IB has two independent data planes: streaming (`reqMktData`) and
  historical (`reqHistoricalData`). A streaming line can freeze per-contract —
  when it was starved across a session boundary (error-101, see 0.8.0) it never
  advances and keeps emitting the last session's `CLOSE`/`LAST` it managed to
  receive (observed on prod 2026-07-04: TSLA stuck at Jul-1 `423.60/425.30` while
  the real Jul-2 close is `393.45` — the same gateway's historical plane knew the
  correct value). The relay only ever read the streaming plane, so it forwarded
  the stale value with no way to notice. Now, when a stock/index line goes stale
  (or has no `last`), the relay lazily fetches the authoritative most-recent daily
  bar close from the historical plane and overrides the frozen streaming value
  (`applyAuthoritativeClose`). Only the most-recent **completed** session is used
  (`selectCompletedBarClose` rejects an in-progress current-day bar, so an
  intraday partial is never served as a close). Demand-driven (only stale
  symbols), throttled at `IB_REALTIME_HIST_SPACING_MS` (default 12s — after the
  16:00 close the whole universe goes stale at once, and IB caps historical at 60
  requests / 10 min), cached per ET day, and swept every `IB_REALTIME_HIST_SWEEP_MS`
  (default 30s) so a silently-frozen line self-corrects even without a re-tick.
  Failed/hung fetches back off (`IB_REALTIME_HIST_COOLDOWN_MS`, default 5m) and a
  per-request timeout plus a reconnect reset prevent a stuck request from
  permanently blocking a symbol. During RTH nothing is stale, so no historical
  requests are issued and the live streaming `last` is never overridden.

## [0.8.0] — 2026-07-04

### Fixed

- IB realtime relay no longer serves a stale `last` with a fresh timestamp. Two
  causes addressed: (1) `updatePriceFromTickPrice`/`updatePriceFromTickSize` now
  bump `data.timestamp` **only** when a freshness field actually changes — a
  closed-market `-1` quote (normalized to null) no longer advances the timestamp,
  so a held cache reads with an honest (old) time; (2) error 101 "Max number of
  tickers has been reached" (IB's ~100-line per-account market-data budget,
  observed firing 88× in 15 min on prod) is now handled — the dead `tickerId` is
  cleared instead of the symbol silently pretending to be subscribed.
- Each `price`/`batch` frame carries an additive per-symbol `stale` boolean
  (`true` when the symbol has no active IB line or no real tick within
  `IB_REALTIME_STALE_TICK_MS`, default 60s) so consumers can distinguish a live
  tick from a held cache.

### Added

- IB realtime relay now enforces an **LRU market-data line budget**
  (`IB_REALTIME_MAX_LINES`, default 85) instead of blindly subscribing past IB's
  ~100-line per-account cap. The root cause of the stale-`last` oversubscription:
  one open option chain requests ~62 lines (C+P × ~31 visible strikes), which on
  top of the portfolio blows the cap. The relay now holds only the hottest lines
  live — evicting the idlest (oldest subscribe/last-tick) to admit a new symbol,
  and re-subscribing the hottest evicted-but-wanted symbol when a line frees.
  Illiquid far-OTM strikes age out first; liquid portfolio/ATM lines stay hot.
  Evicted symbols read `stale: true` rather than serving a silent cache.

## [0.7.2] — 2026-06-23

### Fixed

- IB foreign-stock (JPY/KRW) rows: native per-share price columns (Underlying, Avg Entry, Last Price) widened to 11% so 7-digit values (₩2,919,000) no longer overflow and collide; the Underlying cell now formats native currency (₩/¥) via `fmtNative` instead of a bogus `$` prefix
- `/portfolio/sync` logs a loud error when it returns a stale snapshot — catches the silent class of failure where `xenon-ib-sync` exits 0 but the Postgres write was swallowed (e.g. schema drift from an un-applied migration). Non-fatal: the read still returns, but the staleness is now visible in the API log

## [0.7.1] — 2026-06-22

### Fixed

- Realized P&L for non-USD fills (KRW, JPY) now FX-converted to USD before display; previously ₩595,618 rendered as -$595,618 (native magnitude treated as USD). `CommissionReport.currency` threaded from IB fill pipeline through fill metadata, API response, and `computeRealizedPnlFromFills`; fills with unknown non-USD currency are skipped rather than added at native magnitude

## [0.7.0] — 2026-06-22

### Added

- Japan (TSEJ/JPY) and Korea (KSE/KRW) cash equity support end-to-end: IB quotes, order entry, portfolio display, and Futu read-only sync
- Live IDEALPRO FX ticks (USD/JPY, USD/KRW) subscribed by the relay; snapshot fallback via IB account ExchangeRate
- All position aggregates (MV, P&L, deployed capital, open risk, DAY MOVE, UNREALIZED) converted to USD using live FX rates
- `FxBadge` component — one relevant FX capsule per card (USD/JPY for JP, USD/KRW for KR, none for USD); filled dot = live IDEALPRO tick, hollow = snapshot rate
- Inline FX badge in each ticker card header (after ticker name); suppressed from per-table row to eliminate leak of unrelated currencies
- `currency` and `exchange` columns on `xenon.positions` (Alembic migration `2026_06_22_positions_currency`; existing rows backfill to `USD`)
- `nativeToDisplayUsd()` helper in `web/lib/fx.ts`; `useFx` hook returns `usd_per_unit` map
- Foreign venue/currency forwarded through order preflight, place, and quote flows

### Fixed

- Futu Japan equities now correctly classified as `Stock` (not `Unknown`); no spurious "OTHER" collapsible group in the card
- IB `BASE` pseudo-currency filtered from FX harvest so it never appears as a `USD/BASE` badge
- Futu `gross_position_value` and per-position values USD-converted via FX rates

## [0.6.6] — 2026-06-21

### Changed

- `docs/` restructured: `docs/plans/` eliminated — all completed plans consolidated into `docs/superpowers/plans/_archive/`; new `docs/research/` for long-form notes; `docs/reviews/` merged from two stale dirs; design PNGs moved to `docs/reference/`; `docs/reference/apex-futu/` removed
- `CLAUDE.md` and `AGENTS.md` updated with directory layout table and file placement rules
- `.gitignore` updated with glob patterns for screenshots (`/*.png`, `docs/plans/*.png`) replacing per-file entries

## [0.6.5] — 2026-06-18

### Added

- `GET /options/greeks` — broker-computed option greeks (IB `modelGreeks`:
  `impliedVol`/`delta`/`gamma`/`vega`/`theta`/`undPrice`) for a single contract,
  subprocess-backed (mirrors `/market-depth`). Requires the full option triplet
  (`symbol`+`expiry`+`strike`+`right`); returns the qualified `conId` and bid/ask.
  Uses IB frozen market-data fallback so greeks are returned 24/7 (last-session
  values after hours); `greeks: null` + `note` when IB computes none. bid/ask of
  IB's `-1` "no quote" sentinel surface as `null`. Exposed via `XENON_QUERY_API_KEY`
  (GET only). New CLI `xenon-ib-option-greeks`. Consumer docs:
  `docs/reference/readonly-query-api.md`.

## [0.6.4] — 2026-06-18

### Added

- `GET /market-depth` — point-in-time L2 order-book snapshot (subprocess-backed,
  mirrors `/options/chain`). Accepts `symbol` (stock/index) or a full option triplet
  (`expiry`+`strike`+`right`); returns the qualified `conId`, `bids`/`asks`
  (`price`/`size`/`marketMaker`), and a permission-only `entitled` flag with a `note`
  distinguishing no-entitlement from an empty book. Exposed via `XENON_QUERY_API_KEY`.
  New CLI `xenon-ib-market-depth`. Consumer docs: `docs/reference/readonly-query-api.md`.

## [0.6.3] — 2026-06-18

### Security

- Expand query-key allowlist: `/options/chain`, `/options/expirations`, `/historical/bars`, `/historical/head-timestamp`, `/contract/qualify`, `/orders/quote`, `/attribution`, `/watchlist`, `POST /ws-ticket` now accessible via `XENON_QUERY_API_KEY` for external read-only access.

## [0.6.2] — 2026-06-18

### Security

- Fail-closed API authentication: localhost bypass, X-Internal-Token for web→api trust, XENON_QUERY_API_KEY for read-only external access (GET /portfolio, /orders, /blotter, /journal, /futu/portfolio, /trades/entry-dates, /performance). Closes open-internet prod hole.

## [0.6.1] — 2026-06-17

### Added

- **Recognize 1:2:1 butterfly in portfolio structure grouping (#156).** Multi-leg option positions that form a symmetric butterfly (two equal-size wings + a 2× body, body strike strictly between the wings) are now grouped as a `butterfly` virtual combo with a `Put/Call Butterfly` label instead of three unrelated `SINGLE` rows. Broken-wing flies (equal wing contracts, asymmetric strikes) qualify; unequal-wing ratio spreads are rejected. Runs as "Pass 0" before the existing vertical/straddle detection so a genuine fly isn't fragmented.

### Fixed

- **Dashboard "Working & Filled" card dropped IB orders while on the FUTU tab (and vice versa) (#156).** The card now merges open/executed orders from both brokers via `mergeDashboardOrders`, tagging each row with its broker (`IB · …` / `FUTU · …`). Each working-order row also formats raw status strings into readable labels (`PENDINGSUBMIT` → `Pending Submit`).
- **Portfolio snapshot breakdown numbers were ragged (#156).** Net-liq and P&L value columns are now right-aligned with a fixed minimum width so the breakdown rows line up vertically.
- **IB/FUTU open-order flicker on account-tab switch (#155).** A stale in-flight orders response from the previous broker could briefly overwrite the newly selected broker's orders. `useOrders` now discards responses whose requested broker no longer matches the current tab.

## [0.6.0] — 2026-06-17

### Added

- **Read-only Futu order querying — unified with IB, structure-grouped, DB-first (#153).** Brings the Futu broker tab to IB parity for the Orders, Historical Trades (blotter), and Trade Journal surfaces. Open + historical orders, per-order fees, and a FIFO-matched closed-trade book sync from the local Futu OpenD into Postgres (`futu_orders`, `futu_order_fees`, `futu_closed_trades`) and render through the existing FastAPI read routes + Next.js — DB-first, no JSON fallbacks, fully read-only (no orders/fills/quotes). Multi-leg option closes are grouped into structures by closing order id: the blotter and journal show SYMBOL = underlying and DESCRIPTION = the classified structure name (vertical, straddle, risk reversal, …) instead of duplicate full-OCC strings, sorted newest-first, with a compact non-wrapping date column. The Trade Journal auto-imports one self-healing `FUTU_AUTO_IMPORT` entry per closing structure (purge-and-upsert keyed on the close-order group, scoped per account).

### Fixed

- **Stale Futu data (~half a month behind) and silent sync aborts (#153).** The sync now pulls fresh deals/orders from OpenD on an incremental watermark (`resolve_incremental_since`) instead of leaning on the fixed daily-history window, and tolerates Futu's `'N/A'` / NaN / list-valued frame cells (`_coerce_num` / `_na_to_none`) — including `fetch_order_fees` — that previously raised `ValueError`/array-ambiguity and silently aborted the entire sync before the closed-trade rebuild ran. Closed-trade rebuild and journal sync are guarded by a per-scope Postgres advisory lock and honor `XENON_READ_ONLY=1`.

## [0.5.1] — 2026-06-17

### Added

- **Stock/option book URL split in AssetCockpit (#151).** The cockpit book is now URL-driven: bare `/TICKER` shows the underlying stock book; `?leg=<optionKey>` shows that option's book (head, montage, tape, and depth all follow the option subject). A `?posId=`-selected single-leg option position also opens the option book automatically without changing the URL. The underlying link in the option book head navigates back to the stock view. Multi-leg positions continue to show the stock book with a spread-net header.

### Fixed

- **Option book fell back to stock book when quote was momentarily absent (#151).** `positionOptionKey` previously derived the option key via `resolveTickerQuote`'s `priceKey`, which is only set when a live or calculated mark exists. Cold start and illiquid contracts silently showed the stock book. Fixed by computing the key directly from the position leg via `legPriceKey` (quote-independent).
- **Option book borrowed the underlying's L1 bid/ask when the option quote was absent (#151).** `BookTab`'s price fallback chain (`tickerPriceData ?? prices[ticker]`) leaked the stock's live bid/ask/last into the option head display. Fixed: the stock ticker fallback only applies for stock book subjects.
- **Non-canonical `?leg=` values failed to resolve prices/depths/tape (#151).** `resolveBookSubject` now canonicalises the key via `optionKey(parseOptionKey(...))` so dashed-expiry or lowercase leg parameters (e.g. `QQQ_2026-07-17_692_P`) match the canonical form used to key the price/depth/tape stores.
- **Lowercase→uppercase ticker redirect dropped `?leg` and `?posId` (#151).** `/qqq?leg=QQQ_20260717_692_P` redirected to `/QQQ` (bare), losing the option selection. Fixed: redirect now preserves `tab`, `posId`, and `leg` via `URLSearchParams`.
- **Option tape shown by default; "OPTION" label in book head (#151).** Option books now start with the tape collapsed (no tick-by-tick `AllLast` data from IB for options) and omit the `OPTION` kind badge from the head — the contract spec (`QQQ $692P 07/17/26`) already identifies the instrument.

## [0.5.0] — 2026-06-17

### Added

- **radon→xenon AssetCockpit port — Phase 3: L2 depth + time-and-sales tape (#150).** Brings the cockpit book panel to live L2. **3a** migrates the IB realtime relay from `ib@0.2.9` to `@stoqey/ib` (plain-object contract builders, rewired events, L1-parity gate) with a frozen WS-URL/fallback-port contract pinned by `ws-url-contract.test.ts` so the handshake can't drift. **3b** adds a unit-tested L2 ladder accumulator + bounded tape ring buffer and emits **additive** WS messages (`depth-batch`, `tape-batch`, `depth-unavailable`, `depth`) with per-symbol budget/LRU — existing L1 consumers untouched. **3c** ports radon's book/montage/tape frontend: `OrderBook` + `DepthMontage` + `TimeAndSales`, `usePrices` depth/tape subscription + reducer, and click-to-fill from a book level or tape print through `OrderPrefill` in `TickerDetailContext` into the order ticket. `BookTab` renders the L2 montage when an entitled depth book is present and falls back to the existing L1 panel otherwise. The option book head shows the contract spec (`QQQ $692P 07/17/26`) with the underlying linked to its stock page; the panel sizes to content (no empty void), the bid NBBO tag/size sit left, and the tape shows an empty-state for instruments IB gives no tick-by-tick `AllLast` (options, code 10189). Scope is stock + option; the futures ladder is deferred (xenon surfaces no FUT instrument).

### Fixed

- **L2 depth froze ~5s after subscribing (#150).** IB **code 2152** on an entitled book is an _informational_ venue-permission summary, not an entitlement loss, but the relay's broad error handler misclassified it as `no-entitlement` and tore down the working depth ticket. Narrowed the classification (`isDepthPermissionError` in `ib_connection_status.js`) so only real permission errors (10089/10092, or messages matching `depth.*not (allowed|eligible)|not supported for this combination`) tear down; 2152 and other 21xx info codes are logged and ignored. Live-verified: 99 depth-batches over 16s with zero teardowns and `ib_connected` steady.

## [0.4.2] — 2026-06-16

### Added

- **radon→xenon AssetCockpit port — Phases 1–2 (#149).** Ports the full AssetCockpit shell from radon: cockpit grid (`AssetCockpit`, `CockpitDeck`, `GlyphRail`), quick-win deck sections (CompanyTab with ETF-aware description, OptionsChainTab with Black-Scholes implied-vol column, BookTab/TapeTab stubs), and the `TickerDetailContent` adapter that wires the existing options chain + order flow into the cockpit frame. Includes height-threading fix (`globals.css` `:has(.cockpit-host)` block) so the cockpit fills its container on tall viewports instead of floating at intrinsic height with dead space below.

## [0.4.1] — 2026-06-16

### Fixed

- **Order place 500 on every live submission (#147).** The pure-portfolio pivot (#104) deleted `xenon.api.services.regime_gate` but left its call sites in `_orders_place_from_body` and `_orders_modify_from_body`. Every live order placement raised `NameError: name 'get_regime_state_for_scope' is not defined`, returning HTTP 500. Removed the dead wiring blocks and five orphaned helpers (`_run_regime_gate`, `_run_modify_regime_gate`, `_is_regime_gate_risk_reducing_exit`, `_resolve_regime_bankroll_usd`, `_build_override_audit`, `_resolve_scope_obj`). `cover_ratio_for_preflight` reverts to its NORMAL/no-gate default (1.0). CI stayed green because the gate self-disabled under `_is_test_mode()`; a new regression test forces the gate path via `XENON_REGIME_GATE_IN_TESTS=1` to pin this forever.

### Changed

- **README realigned to pure-portfolio pivot (#148).** Removed Four Gates, Strategies, and VCG/CRI signal-layer sections; added Brokers table, Naked-Short Guard overview, Data Sources table, and a grouped CLI reference with paper/live setup notes.
- **Bootstrap default PostgreSQL version bumped to 17 (#148).** `scripts/deploy/macmini-bootstrap.sh` and `docs/runbooks/mac-mini.md` now default `XENON_PG_VERSION=17`; the previous `=16` default was stale (timescaledb ships against PG17). Snapshotter design doc updated with verified IB Quote Booster pricing and pacing limits.

## [0.4.0] — 2026-06-15

### Added

- **Operator console at `/admin` (#144).** A read-only operations/health dashboard aggregating live system state behind the existing global auth gate: IB Gateway reachability + auth verdict + connection-pool roles, trading-mode/account verification, snapshotter freshness, unknown-state order submissions, Flex divergence, realtime WS subscribers, Futu connectivity, live Unusual Whales rate-limit quota, and a per-writer heartbeat table backed by a new `xenon.service_health` table (`record_service_health`, scoped per `(service, broker, account_env, broker_account)`). Surfaced via `GET /admin/operator`; the page issues no portfolio reads so visiting it never triggers a background sync. Writer heartbeats no-op under `XENON_READ_ONLY=1`.

### Fixed

- **Full-suite test isolation: nine schema tables leaked across tests (#146).** `XENON_TABLES` — the per-test/per-session reset list in `src/xenon/_test_db.py` — had drifted from the schema and was missing `service_health` plus eight others (`flex_divergence_runs`, `ib_cash_flow`, `regime_overrides`, `benchmark_closes`, and the four `futu_*` statement tables). A committed write to any of them (e.g. the `ib_activity_poller` heartbeat from a `committed_db` lifespan test) survived the whole session and surfaced as a flaky `pk_service_health` UniqueViolation in an unrelated test — green on affected-only PR runs, red on the full master suite (the operator console's first full-suite run on master). All nine tables are now reset, and a new metadata-derived guard (`test_xenon_tables_covers_every_schema_table`) turns future drift into a deterministic local failure instead of an intermittent cross-test leak.

## [0.3.5] — 2026-06-15

### Fixed

- **"Today's Executed Orders" panel listed stale fills and malformed stock rows (#140).** The executed-orders payload had no date predicate, so the panel rendered every historical fill for the account under a "Today's" header. Single-name stock fills also showed a redundant `Bought QQQ`-style label. The payload now filters executed fills to the current Eastern-time day (matching the realized-P&L day boundary in `web/lib/realized-pnl.ts`), with a midnight-Eastern-as-UTC boundary helper, and stock fills render a clean `Stock` descriptor. The earlier zero-quantity rows were a separate fractional-share truncation (fixed in #134); the affected prod fills were repaired with their true IB quantities.

## [0.3.4] — 2026-06-14

### Fixed

- **Sidebar version row rendered "—" in prod (#139).** `next.config.mjs` inlines `NEXT_PUBLIC_APP_VERSION` from the root `VERSION` file at build time, but `docker/web.Dockerfile` never copied `VERSION` into the build context, so `readAppVersion()` hit its `catch` and the prod web image shipped an empty version. The builder stage now `COPY`s the root `VERSION`.
- **Realtime subscriber health showed "stream offline" in prod (#139).** In the Docker topology the api and realtime run as separate containers, so the api could not read the realtime `/status`: runtime-file port discovery isn't shared across containers (fell back to `127.0.0.1:8765`, refused), and the loopback-only `/status` guard 403s a cross-container request. `/status` now also accepts a matching `X-Status-Token` header (`IB_REALTIME_STATUS_TOKEN`) in addition to loopback, and the api resolves the relay via `IB_REALTIME_STATUS_URL` (prod → `http://realtime:8765/status`) sending that token; single-host dev stays loopback-only with no token. Prod deploy adds `IB_REALTIME_STATUS_URL` to the api service and `IB_REALTIME_STATUS_TOKEN` to the shared `.env`.

## [0.3.3] — 2026-06-14

### Added

- **Realtime subscriber connection health in the health sidebar (#138).** The sidebar gains a **Subscribers** section listing each identified WS price-stream client (clients that connect to the IB realtime relay with `?id=<name>`): a liveness dot (green `<35s` since last pong, amber `35–65s`, red `offline <age>` within a 15-min TTL, `IB_REALTIME_SUBSCRIBER_TTL_MS`) and a last-seen age, plus a muted `+N app clients` count of anonymous browser-tab connections. Backed by a new in-memory `subscriber_registry.js` and a loopback-only `GET /status` on the realtime server, surfaced through a silent-degrading `realtime_subscribers` block on FastAPI `/health` (resolves the relay port from the runtime file; `reachable:false` when the relay is down), and polled by a dedicated `useSubscriberHealth` hook. `/status` is localhost-only; `remote` IPs are never forwarded to the public `/health`.

### Changed

- **Dev stack moved to ports 3200 / 8421 / 8866 (#138).** Next.js, FastAPI, and the IB realtime relay now bind 3200 / 8421 / 8866 in development (was 3000 / 8321 / 8765), so the xenon dev stack coexists with another local stack holding the legacy ports. Production launchd keeps 3000 / 8321 / 8765; the `8321`/`8765` code defaults are prod-shared and unchanged, with dev overriding via env. `XENON_API_PORT` is now honored end-to-end by `npm run dev` (uvicorn `--port` + the Next proxy URL), and `web/playwright.config.ts` defaults to 3200.

## [0.3.2] — 2026-06-14

### Added

- **App version in the health sidebar (#135).** The sidebar footer gains a `Version` row showing the release version (e.g. `v0.3.1`), injected at build time into `NEXT_PUBLIC_APP_VERSION` from the root `VERSION` file via `next.config.mjs` (the release source of truth, not `web/package.json`).

### Changed

- **`web/package.json` version is now tracked by the release tooling (#135).** It had silently drifted to `0.6.1` while the release version was `0.3.1`. Since backend and frontend ship from a single release procedure today, `version_sync_check.py` (the CI `version-sync` job) now validates `web/package.json` against `VERSION` and `cut.sh` bumps it in lockstep. `site/package.json` (the separate Vercel marketing site) stays independent.

## [0.3.1] — 2026-06-14

### Added

- **TWS cancel mirroring (#134).** The IB activity poller now sweeps `WORKING`/`PARTIALLY_FILLED` orders that vanish from the open-order snapshot and transitions them to `FILLED` (when `order_fills` for the same `(perm_id, scope)` cover the order quantity) or `CANCELLED` (`reason_code=TWS_CANCEL_MIRROR`, after a one-tick grace), closing the long-standing gap where an order cancelled in TWS stayed `WORKING` forever (`sweep_disappeared_orders` in `ib_activity_mirror.py`). Safety guards: an empty snapshot skips the whole sweep (never mass-cancel on a stale post-reconnect read), presence is matched on perm_id **or** ib_order_id (survives the permId=0 race), a BAG with leg fills but no envelope row stays `WORKING`, and `mark_terminal` gained an optimistic `expected_states` guard so a concurrent fill/cancel is never clobbered. Deliberate limitations: a TWS cancel of your only open order mirrors on the next non-empty sweep or boot rehydrate, not instantly.

### Fixed

- **Fractional-share fills no longer recorded as `qty=0` (#134).** `order_fills.qty` and `trades.quantity` widened `Integer` → `Numeric(20,8)` (migration `2026_06_13_fill_qty_numeric`); all four `record_fill` feeders — `ib_reconcile`, `single_leg_rehydrate`, `combo_wizard` rehydrate, and `ib_execute` — preserve `Decimal` end to end. Recurring fractional QQQ/SPY buys now show their true quantity and cost in the blotter and in derived `xenon.trades`. A one-shot script (`scripts/migrations/_2026_06_13_repair_zero_qty_fills.py`) patches the historical `qty=0` rows from IB statements.
- **Stock fills with realized P&L classify as closing (#134).** `isClosingFill` now treats `STK` (not just `OPT`) positions with realized P&L > $0.01 as closing fills in the executed-orders panel.
- **SPX/NDX/RUT CHAIN tab no longer 502s (#134).** `ib_option_chain` qualifies index symbols as `Index` on their home exchange (CBOE/NASDAQ) with `underlyingSecType="IND"` instead of hardcoding `Stock`/`SMART`; the `/options/chain` route also passes the resolved gateway port (4002 paper / 4001 live) instead of defaulting to 4001.
- **Flex blotter failures surface an actionable banner instead of an opaque 502 (#134).** `POST /blotter` returns a structured payload (`configured`, `flex_error`, `message`) and the Historical Trades panel renders the error plus the XML-format hint for IB ErrorCode 1001.
- **`dev.sh` refuses to start when the API port is already bound (#134).** Detects a zombie `uvicorn` on the API port (e.g. one surviving a deleted worktree) and exits with the kill command instead of silently coexisting and serving stale code.

### Changed

- **Blotter fill times pinned to exchange time (ET) (#134).** `formatEtTime` renders fill timestamps in `America/New_York` rather than browser-local, so a 15:17 ET fill no longer shows as 03:17 for a UTC+8 operator.

## [0.3.0] — 2026-06-04

### Added

- **Performance page — period selector + honest returns + IB cash-flow ingest (#130).** End-to-end rebuild of the `/performance` surface. **Period selector** lets the user pick `1M / 3M / YTD / 1Y / ALL` (default `YTD`); the choice flows from the page → Next route `/api/performance?broker=&period=` → FastAPI `/performance` → the compute layer, which slices `nav_history` accordingly and caches per-(scope, period). **Honest returns** replaces the single ambiguous "total return %" with three flavors computed inside `src/xenon/api/services/performance.py`: `simple_total_return` (raw `(end_nav − start_nav) / start_nav`), `twr_total_return` (Time-Weighted Return, multiplicative chain of daily returns with deposit days excluded), and `irr_total_return` (Money-Weighted IRR, Newton-solved against the cash-flow series). The headline tooltip now surfaces all three plus net deposits, and the methodology basis label is uniformly "Time-Weighted Return (TWR)" across IB and FUTU. **IB cash-flow ingest** parses Section 2 of the existing IB Flex CSV (`Xenon_NAV.csv` deposit/withdrawal/transfer rows) into a new `ib_cash_flow` table, so TWR can correctly null-out the deposit days. Naming compatibility shim mirrors the new field set (`twr_total_return`, `simple_total_return`, `net_external_flows`) onto legacy keys (`total_return`, `simple_return`, `net_inflow`, `pnl`) when flows are present, so the existing `PerformancePanel.tsx` keeps rendering through the transition.

### Changed

- **`nav_history` primary key widened to include `source` (#130).** Migration `2026_06_03_nav_src_pk` drops the 4-column PK `(broker, account_env, broker_account, date)` and recreates it as `(broker, account_env, broker_account, date, source)`. Intraday (`ib_sync`) and post-close (`xenon-nav-flex-refresh`) NAV rows for the same scope+date now coexist as separate audit rows — `nav_history` IS the audit table. The cross-env collision guard is preserved by a new partial unique index `nav_history_one_env_per_day_per_source` on `(broker, broker_account, date, source)`. Downgrade is destructive on `close` twins (the old 4-col PK rejects them as duplicates); the downgrade path DELETEs them first, preserving the intraday row, and operators can re-run `xenon-nav-flex-refresh` to recover close NAVs.

### Fixed

- **`test_nav_history_pk_is_scoped` matches the new 5-column PK.** The post-merge CI run on master HEAD `17de90aa` failed solely because the assertion in `src/xenon/db/tests/test_schema_scope.py:42` still expected the old 4-column PK, while the schema definition and the `2026_06_03_nav_src_pk` migration had already moved to the 5-column form. Assertion updated; `src/xenon/CLAUDE.md` PK note also refreshed.

## [0.2.0] — 2026-06-03

### Added

- **Option-chain archive snapshotter — scaffolding (#125).** New long-running service skeleton that will capture full IB option chains for the four CBOE-listed index underliers (SPX, NDX, RUT, VIX) every 10 min into a TimescaleDB-backed Postgres archive. This PR lands design + IMPL plan + the first executable foundation only; the full 10-PR rollout (limiter, pool, persister, workers, launchd) is staged for follow-up. Includes: design spec (`docs/plans/2026-06-02-option-chain-snapshotter-design.md`, hardened through a six-pass review-cycle) + 3,352-line IMPL plan; pre-work — `CLIENT_IDS["option_chain_snapshotter_a"]=95` / `_b=96`, `LOCK_KEY_OPTION_CHAIN_SNAPSHOTTER=7343001` for the single-instance advisory-lock guard, `exchange-calendars>=4.5,<5.0` dependency (resolved 4.13.2); a separate alembic environment at `scripts/migrations/option_chain/` with an initial schema migration creating five tables (`snapshot_config` seeded with the 4-ticker cadence, `option_universe`, `snapshot_run`, `option_chain`, `underlying_ohlcv`), two TimescaleDB hypertables with `add_compression_policy`, the `v_staleness` operator-dashboard view, and conditional `READ` grants for `xenon_prod` / `xenon_dev` / `argon_app`; and a minimal end-to-end spike (`scripts/spike/option_chain_minimal.py`) that proved the live IB → Postgres flow against all four tickers on 2026-06-02 (six rows persisted to `archive.option_chain` with bid/ask/IV/delta intact for SPX/RUT/VIX; NDX correctly reported `partial` with NULL greeks owing to a known market-data subscription gap).

### Fixed

- **`v_staleness` no longer reports `health='fresh'` for never-run tickers (codex tribunal — Pass 2 of `/review-cycle`).** The original `CASE WHEN now() - last_run.finished_at > make_interval(secs => c.cadence_seconds * 4) THEN 'stale' ELSE 'fresh' END` mis-classified the NULL case: `now() - NULL` is NULL, `NULL > interval` is NULL, the `CASE` fell through to `ELSE 'fresh'`. A freshly seeded enabled ticker with zero runs would silently report healthy, defeating the operator dashboard. Explicit `WHEN last_run.finished_at IS NULL THEN 'stale'` branch added; verified against tmp DB with never-run, recently-run, and stale fixtures.
- **`scripts/migrations/option_chain/env.py` handles plain `postgresql://` URLs.** SQLAlchemy defaults `postgresql://` to psycopg2, which xenon does not install. `get_url()` now normalises plain URLs to `postgresql+psycopg://` so a copy-pasted `OPTION_CHAIN_DATABASE_URL` works on the first alembic run; explicit driver prefixes (`+asyncpg`, `+psycopg`) are left alone. Verified end-to-end by running `alembic upgrade head` against a fresh DB with a driverless URL.

## [0.1.3] — 2026-06-03

### Added

- **Daily IB Flex NAV auto-refresh (#124).** New `xenon-nav-flex-refresh` CLI invoked by a macOS LaunchAgent at 17:30 ET on the macmini. Polls IB Flex Web Service for `EquitySummaryByReportDateInBase` rows and upserts them into `xenon.nav_history` with `source='close'`. The shell wrapper sources `.env` so the plist stays secret-free (matches the `refresh-core-test.sh` pattern). Architecture is a rolling ~2-week reconciliation window — a single missed run is absorbed by the next day; historical backfill stays on the one-shot CSV-download path. Install procedure documented in `docs/runbooks/nav-flex-refresh.md`.
- **`upsert_nav_sync` `source` parameter.** Optional `source: str | None = None` arg distinguishes post-close (`'close'`) from intraday (`'intraday'`) writes. Omitting preserves the existing PG value on conflict, so a daily `'close'` write is not clobbered by a same-day intraday `ib_sync` snapshot. Backward-compatible — existing call sites get the server default unchanged.

### Fixed

- **`fetch_ib_nav_series` API path now actually works at runtime.** The legacy `Universal/servlet/FlexStatementService.*` endpoint is XML-only, so it returned permanent `ErrorCode 1001` against the saved query `1529248` (CSV format) — diagnosed as not a transient throttle but a structural format incompatibility. Migrated to the current `ndcdyn/AccountManagement/FlexWebService/*` endpoint with response-body sniffing: lines starting with `"ClientAccountID"` → CSV branch (current saved-query config), `<FlexStatements>` → XML branch (forward-compat). Also now writes `source='close'` instead of the server-default `'intraday'`.
- **`dev.sh live` no longer hard-fails on the core_dev guard.** Paper mode substitutes `DATABASE_URL_PAPER → DATABASE_URL`; live mode had no equivalent and so kept `.env`'s `DATABASE_URL=core_dev`, tripping the guard on every invocation. Live mode now substitutes `DATABASE_URL_TEST → DATABASE_URL` _only when DATABASE_URL points at core_dev_ — empty / custom URLs are respected so the `test_dev_sh_exports.py` harness doesn't pick up an inherited `DATABASE_URL_TEST` (`XENON_READ_ONLY=1` continues to block writes).
- **`test_dev_sh_db_guard` actually exercises the guard.** Tests had been silently red — `dev.sh` hardcoded its env-file path, so the operator's live `.env` (with `DATABASE_URL_PAPER` substituting `core_test` for any `core_dev` value) bypassed the guard in test runs. Added `XENON_ENV_FILE` override; rewrote tests to write a per-test tmpdir stub. Expanded coverage: refuse in both modes, pass-path tests for `core_test` in both modes.
- **`scripts/infra/refresh-core-test.sh` produces a clean restore.** Two latent bugs had been preventing the nightly `core_dev → core_test` LaunchAgent from working: (1) `pg_restore --clean` cannot drop tables with FK dependents, so prior runs generated 7 errors per pass and left `core_test` with a phantom partially-migrated schema; (2) operator MacBooks default to homebrew's `pg15` client tools, which abort against the macmini's `pg17` server with a version mismatch. Added: pre-drop `xenon` + `events` schemas with `CASCADE` before the restore; auto-pick the highest available `postgresql@N` from `/opt/homebrew/opt/`.

## [0.1.0] — 2026-06-02

### Added

- **FUTU NAV curve in the Performance tab (#120).** The FUTU account tab now renders a real backward-walked NAV curve in the existing Performance page — no UI changes, no new components. Persistence layer pulls historical trades + cashflows from Futu OpenD into two new tables (`xenon.futu_trades` + `xenon.futu_cash_flow`), then a FIFO-matched backward walk (with 100× contract multiplier for OCC-format options) populates `xenon.nav_history` rows the existing `/performance?broker=FUTU` route already reads. Verified end-to-end against the operator's live account: 6,189 trades + 424 cashflows over 2024-07-15 → 2026-06-02 produce 688 daily NAV rows anchored at today's `accinfo_query.net_liquidation`.
- **Automatic nightly Futu sync.** New FastAPI lifespan loop runs at 16:30 ET every weekday, calling `xenon-futu-history-sync` end-to-end inside the running xenon-api process. Incremental watermark (`max(filled_at, occurred_at) - 7d`) keeps each tick at ~8 seconds instead of re-walking inception. Disabled in test mode and via `XENON_FUTU_HISTORY_LOOP=0`. Single-day failures log + retry tomorrow; no schedule poisoning.
- **`xenon-futu-history-sync` CLI.** Operator-runnable command that chains the OpenD pulls, persistence UPSERTs, and backward walk. Defaults to incremental; `--since YYYY-MM-DD` forces a deeper rewalk. Resolves FUTU scope from the matched OpenD account per spec §10 (never trusts env vars for FUTU).

### Fixed

- **Futu cashflow type is an open enum.** Prior assumption (`MoneyIn` / `MoneyOut` / …) missed every actual value Futu returns (`Cash Dividend`, `Fund Subscription`, `IPO Subscription`, `Currency Exchange`, `Others`, …). Dropped the `cashflow_type` CHECK and persist verbatim; M5 NAV walk decides which raw types move NAV externally.
- **Bulk insert chunked under Postgres' 32767 bind-param cap.** Live inception backfill of 6,189 deals tripped asyncpg's `InterfaceError: the number of query arguments cannot exceed 32767` on a single `pg_insert.values(...)` call. Now batched at 2000 rows per statement within one transaction.
- **`history_deal_list_query` throttle (10 req/30s).** Multi-window pulls (e.g. 6+ years across 26 paginated windows) tripped Futu's per-endpoint rate limit. Sleeps 3.5s between window calls; single-window pulls pay nothing.

## [0.0.10] — 2026-06-02

### Fixed

- **Realtime relay silently stopped reconnecting after `ECONNREFUSED` (#113).** `classifyIBConnectionError()` built its detector regex by interpolating the configured IB host (e.g. `host.docker.internal:4001`), but Node's `net` module emits the **resolved IP** in the error text (`connect ECONNREFUSED 192.168.5.2:4001`). The regex never matched in Docker deployments, so `scheduleReconnect()` was not called from the `ib.on("error")` handler and the relay sat silent until the process restarted. Symptom: portfolio page rendered positions from Postgres but every live-tick column (`last`, `bid`, `ask`, `close`, `volume`) stayed `—` indefinitely. Fix matches the connect-error code family directly and widens coverage to `ETIMEDOUT`/`EHOSTUNREACH`/`ENETUNREACH`/`ENOTFOUND`/`EAI_AGAIN`/`EADDRNOTAVAIL` — the same silent-death failure mode applied to any of them.

## [0.0.8] — 2026-06-01

### Fixed

- **IB status badge over Tailscale (#110).** `/api/ib/ws-config` now derives the realtime WS URL from the request's `Host` / `X-Forwarded-Host` headers (with `0.0.0.0` / `::` / empty rejected) and respects `X-Forwarded-Proto=https` → `wss://`, instead of echoing back the server's bind address. Previously every request from a remote Tailscale device received `ws://0.0.0.0:8765`, the browser couldn't open it, and the sidebar IB Gateway badge was stuck on "disconnected" even when IB Gateway was fully connected.
- **Connected badge survives realtime relay drops.** `IBStatusContext` + `usePrices` now fall back to polling `/api/health` (15 s) when the WS is offline; if any `ib_pool` role reports `connected: true`, the badge stays green and a new warning banner shows `Live data stream offline — IB Gateway is still connected; prices may be delayed.` rather than telling the user IB itself is down. Distinguishes "relay down" from "broker down" in the UI.

## [0.0.7] — 2026-05-31

### Changed

- **Pure-portfolio pivot.** Xenon is now a broker terminal for options
  portfolio management — IB (primary, live + paper) + Futu (read-only
  positions tab). All signal-generation surfaces removed: scanners (VCG,
  GEX, CRI, GARCH, leap-IV, leap-UW, trend, UW discover, UW analyze),
  flow-analysis page, regime gate + scan loop, CTA / MenthorQ pipelines,
  share-card generators. ChatPanel reframed for portfolio Q&A only.
  See `docs/plans/2026-05-22-pure-portfolio-pivot.md` for the full
  rationale + four-PR breakdown. UW token retained as a historic-data
  source for portfolio_performance + portfolio_report CLIs only.

### Removed

- Python signal layer: `src/xenon/scanners/`, `src/xenon/analysis/`,
  `src/xenon/fetchers/`, `src/xenon/shares/`,
  `src/xenon/services/cta_sync_service.py`, the signal-only
  `api/routes/{regime,uw_analyze,uw_stats}.py` + matching
  `api/services/{regime_*,uw_analyze_*}.py`, the strategy-eval
  `reports/{evaluate,kelly,risk_reversal,free_trade_analyzer,
verify_options_oi,scenario_*}.py`, `clients/{menthorq,massive,
inspect_dashboard,map_*}.py`. ~22k Python LOC.
- server.py inline routes: `/scan`, `/discover`, `/flow-analysis`,
  `/cta/share`, `/regime`, `/regime/scan`, `/regime/share`, `/vcg`,
  `/vcg/scan`, `/vcg/share`, `/gex`, `/gex/scan`, `/gex/share`,
  `/internals/share`, `/internals/skew-history` (18 handlers + 14
  helpers + vcg_cri lifespan + `/health.vcg_cri_loop` field).
- Web pages: `/cta`, `/discover`, `/flow-analysis`, `/internals`,
  `/kit`, `/regime`, `/scanner`, `/uw-analyze`. ~10k web LOC.
- Web components: `CtaPage`, `GexPanel`, `RegimePanel`,
  `InternalsPanel`, `CriHistoryChart`, `SharePnlButton`,
  `ShareReportModal`, `VcgPanel`, `RegimeRelationshipView`, etc.
- Sidebar UW telemetry footer + `useUwStats` / `useUwStatsHistory` hooks.
- pyproject.toml: ~31 CLI entry points (`xenon-fetch-*`, `xenon-*-scan`,
  `xenon-evaluate`, `xenon-kelly`, `xenon-scenario-*`, `xenon-leap-*`,
  `xenon-trend-scan`, `xenon-garch`, `xenon-uw-*`, `xenon-discover*`,
  `xenon-generate-*-share`, `xenon-cta-sync-service`,
  `xenon-repair-cri-rvol`). Dropped `playwright` + `yfinance` deps.
- Launchd plists: `com.xenon.{cri-scan,cta-sync,data-refresh}.plist`
  and companion shell scripts under `scripts/services/`.
- Env vars: `MENTHORQ_USER` / `MENTHORQ_PASS` / `MASSIVE_API_KEY`
  (clients gone), `CEREBRAS_API_KEY` (always dead).

### Deferred (separate follow-up)

- Postgres tables `uw_analyze_snapshots` (+ child tables), `vcg_series`,
  `gex_snapshots`, `scan_results`, `regime_state_view`,
  `regime_overrides`, `uw_flow_events` are intentionally left in place.
  A separate Alembic downgrade will drop them once we've confirmed
  zero readers in the running services.

## [0.0.6] — 2026-05-26

### Documentation

- **Clarify Docker-to-IB Gateway routing for Mac mini deploys.** `docs/runbooks/remote-deploy.md` now distinguishes co-located IB Gateway (`host.docker.internal`) from IB running on another LAN/Tailscale host or behind a localhost-only SSH tunnel. The bootstrap step no longer rewrites `IB_GATEWAY_HOST` unconditionally, and `docker-compose.yml` comments now describe the same topology boundary.

## [0.0.5] — 2026-05-05

### Fixed

- **Forward `UW_TOKEN` into the FastAPI container (#99).** `docker-compose.yml` api service now also loads `./web/.env` (after `./.env`, marked `required: false`). Previously the container booted without `UW_TOKEN` because the credential lives in `web/.env` per CLAUDE.md and the api Dockerfile never ships the `web/` tree, so the in-process `load_dotenv(web/.env)` at `server.py:81-82` silently no-opped — and every UW-backed endpoint failed with the existing `"UW_TOKEN not set"` warning. Side benefit: same channel now plumbs `ANTHROPIC_API_KEY` through to `menthorq_client`. Regression test in `scripts/tests/test_docker_compose_env_plumbing.py` pins env_file order + optional flag.
- **`scripts/infra/dev.sh` paper-mode now works off-LAN (#98).** Per-mode IB Gateway host resolution: `paper` always pins `127.0.0.1:4002`, `live` honors `IB_GATEWAY_HOST` from `.env`. `.env.example` documents the new var.

### Changed

- **GHCR image tags standardized on `X.Y.Z` (no `v` prefix) (#97).** Aligns with Docker convention; the matching tag in `release.yml::ghcr-push` was the previous odd-one-out.

### Documentation

- Stage C auto-deploy planning handover (#88), backlog status updates (#93), GHCR per-package ACL prerequisite (#94), `XENON_BROKER_ACCOUNT` runbook + Clerk dev-limit correction (#92), and archival of completed plans + top-level docs (#95, #96).

## [0.0.4] — 2026-05-04

### Added

- **Default-private route gating with explicit public allowlist (#90).** Inverts `web/middleware.ts` from "gate everything except sign-in/sign-up/api" to default-private with an explicit `PUBLIC_ROUTES` array of scanner / market-data surfaces (`/scanner`, `/discover`, `/regime`, `/flow-analysis`, `/uw-analyze`, `/cta`, `/kit`). Trading workspace (`/`), portfolio, orders, journal, internals, dashboard, performance, and `/[ticker]` stay private. New routes inherit auth automatically — opt out by adding to `PUBLIC_ROUTES`. Fail-closed default protects against scattered per-flow gating regressions called out in `project_universal_auth_gating` memory. New unit test `web/tests/middleware-route-gating.test.ts` pins 24 path classifications including the "unlisted route is private" invariant.
- **Mac mini Docker deploy runbook (#87).** New `docs/runbooks/remote-deploy.md` documents topology, first-time bootstrap, standard release flow, Colima auto-start via `brew services`, rollback, logs/diagnostics, and a reference compose template. `docs/runbooks/mac-mini.md` now points at it as the authoritative source.
- **Stage C auto-deploy planning handover (#88).** `docs/handovers/2026-05-04-stage-c-auto-deploy.md` — three-item plan covering release verify, build-args, and Watchtower on the mini.

### Fixed

- **`release.yml::verify` now provisions Postgres (#89).** Adds `postgres:16-alpine` service container, schema bootstrap (`xenon`, `events`), and `alembic upgrade head` before `uv run pytest`. Mirrors `ci.yml::python-tests` so the tag-trigger verify matches what master CI proves green. Without it, v0.0.3 verify hit 282 connection-refused errors and blocked `ghcr-push`; v0.0.4 is the first tag where verify is actually expected to pass clean.
- **Clerk publishable key now baked into web image (#89).** `release.yml::ghcr-push` passes `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` as a `docker/build-push-action` build-arg via matrix conditional (web image only). `NEXT_PUBLIC_*` vars must be present at `next build` time so they're inlined into the client bundle; without it the production web image shipped with an undefined Clerk key and round-tripped to localhost on auth flows. Requires GHA repo secret `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` to be set (added 2026-05-04).

## [0.0.3] — 2026-05-04

### Added

- **Phase 1 Stage A — containerization (#85).** Four production deploy images (`api`, `web`, `realtime`, `migrator`) plus a single `docker-compose.yml` at repo root that runs the long-running stack (api + web + realtime); migrator is gated behind `profiles: ["migrate"]` so `compose up` never auto-runs alembic. Web image uses Next.js standalone output with `NEXT_PUBLIC_*` build-args inlined at build time; realtime image carries `scripts/lib/` for the relay's `lru-cache` import. Tag-triggered `ghcr-push` matrix job in `release.yml` builds and publishes all four images to `ghcr.io/<owner>/xenon-{api,web,realtime,migrator}` (linux/arm64) with `:vX.Y.Z` + `:latest` tags. `host.docker.internal` reaches host-native Postgres + IB Gateway + Futu OpenD; `XENON_API_URL=http://api:8321` wires server-side Next fetches to FastAPI inside the compose network.
- **VCG-CRI regime gate Phase 3 — order entry wiring (#78).** `RegimeGate` now intercepts every order placement and modify/cancel path, scoped via `AccountScope`. Composite 4-tuple FK on `regime_overrides` (`submission_id, broker, account_env, broker_account`) prevents scope drift between override rows and parent submissions. New audit table records every blocked/permitted decision with the resolved (vcg_tier, cri_tier) tuple at decision time.
- **VCG-CRI rewiring Phase 0/1/2/4 (#68).** CRI persistence (`cri_series`), `pg_try_advisory_lock` helper, web `/api/regime` rewrite, audit doc. The eight tribunal corrections from the v2 design are folded inline.
- **Per-mode IB Gateway host routing in `dev.sh` (#83).** `paper` resolves to `127.0.0.1:4002` (always local), `live` resolves to whatever `IB_GATEWAY_HOST` from `.env` points at (typically the remote production server) on port `4001`. `IB_GATEWAY_HOST` and `IB_GATEWAY_PORT` are exported so child processes (uvicorn, Next, the Node IB realtime relay) inherit the resolved values.

### Changed

- **`ib_insync` → `ib_async` (#81).** `ib_insync` was last released July 2023 (0.9.86) and is unmaintained; newer Gateway versions (10.30+) tightened the API handshake. Migrated to the actively-maintained community fork `ib_async==2.1.0`. Package-name-only swap across 34 source/test files; no code-shape changes. 132 ib-related unit tests pass against the new dep.
- **PG migration clean cutoff — drop `data/*.json` runtime reads (#84).** Strips silent JSON fallbacks from three UW services and tightens the order-path JSON-fallback allowlist to zero. Conftest gains `scope_fixture`, `pg_test_engine`, and an offline-tolerant PG truncate that caches unreachability so the test suite no longer dies on a 5-minute psycopg timeout when the LAN is unreachable. UW analyze cache hardened: closed-market gate covers user-initiated paths, write failures emit observability events.
- **`output: 'standalone'` in `web/next.config.mjs`.** Required for the slim web runtime image; preserves the existing `outputFileTracingRoot` pointing at the repo root.
- **Multi-service Postgres design (#82).** Same cluster, role-isolated schemas (`xenon`, `apex`, `events`); three DB names (`core`, `core_dev`, `core_test`) for prod/dev/test separation. `xenon_app/xenon_dev`, `apex_app/apex_app`, admin `moremeds/moremeds`. Documented in `docs/architecture/production-database-strategy.md`.

### Fixed

- **Combo SELL blanket bypass closed (#79).** `_is_regime_gate_risk_reducing_exit` no longer trusts a SELL BAG envelope. Combo SELL bypass now requires every per-leg inverse to exist in the portfolio with sufficient contracts via `preflight.combo_close_covered_by_portfolio`. `preflight.combo_uncovered_short_call_ratio` + `evaluate_combo` drop their `action == "SELL" → accept` short-circuit. Per-leg ratio analysis is the trustworthy check for naked-short risk; the envelope action is untrusted.
- **Stale portfolio snapshot allowed bypass (#79).** SELL-to-close bypass now calls `_portfolio_snapshot_stale_response`. A snapshot beyond the freshness threshold (300s open / 1800s closed) refuses the bypass and lets RegimeGate block as new exposure.
- **BAG quantity-increase modify outside NORMAL tier (#80).** `_run_modify_regime_gate` now blocks modify operations that increase exposure when the regime tier is anything but NORMAL.

### Removed

- **Dead `incremental_sync` + post-hoc reliability plan (#73).** Code removed; tests rewired to the live sync path.
- **Vestigial `db_path` parameter in `orders_store` (#75).** All 11 functions stripped of the DuckDB-era param. Tracked in memory `feedback_orders_store_no_db_path.md` so it never returns.

### Documentation

- **Tick-stub by-design doc (#76).** `_lookup_min_tick_via_pool` returns 0.01 for every contract on purpose; IB enforces the real rule and code 110 maps to LIMIT_OFF_TICK. Captured in code comments + memory.
- **Order-stack end-to-end walkthrough (#83).** New `docs/architecture/order-stack-end-to-end.md` traces a single order from web click through FastAPI to IB Gateway and back through the activity poller.
- **Backlog hygiene (#74, plus chronological cross-reference of inbox items vs commits).**

## [0.0.2] — 2026-04-30

### Added

- **IB→Postgres activity mirror (#70).** Boot replay of IB executions into `xenon.order_fills` + aggregated `xenon.trades`; periodic poller (default 60s, env `XENON_IB_ACTIVITY_POLL_S`) runs open-order import + fill replay every tick with independent failure isolation. `register_from_snapshot` now UPDATEs `snapshot-*` rows on TWS price/qty drift and emits `IB_MIRROR_UPDATE` events. `record_external_fills` resolves `(perm_id, scope)` → `submission_id` so blotter rows tie back to their originating order.
- **`tif` column on `order_submissions` (#71).** Alembic migration `8a3f2c7d1e90` replaces the route's hard-coded `"DAY"` with the IB-reported TIF, threaded through writer + drift detection + reader. GTC orders now render correctly.
- **Snapshot row resurrection (#71).** `register_from_snapshot` restores rows in any terminal state (CANCELLED/FILLED/REJECTED/FAILED/UNKNOWN) when IB still reports them open. Emits `IB_MIRROR_RESURRECT` event for audit. Self-heals from cancel-route races and operator errors.
- **Leg-contract enrichment in rehydrate (#71).** `single_leg_rehydrate._enrich_records_via_ib` qualifies leg conIds via `IB.qualify_contracts` when `Fill.contract` lacks strike/right/expiry. Fixes "Bull Put Spread (Short Unknown Unknown / Long Unknown Unknown)" rendering on closing combos.
- **`docs/reference/order-path-incident-history.md`.** Chronological log of every non-trivial order-path bug since PR #27, with root cause, fix, and the regression test that protects against recurrence. Cross-linked from root + api CLAUDE.md.

### Fixed

- **Cancel doesn't clear Open Orders panel (#71).** `/orders/cancel` now calls `mark_terminal(state="CANCELLED", reason_code="USER_CANCEL")` on the success path. The activity poller intentionally cannot disambiguate fill vs cancel on disappearance, so the cancel route is the only authoritative trigger.
- **BAG combo missing from panel (#71).** `sync_open_orders_to_postgres` no longer drops orders with `orderId=0` (which IB returns for combos viewed from non-originating clientIds). `perm_id` alone is sufficient identity.
- **Trade aggregator labels combos as "Stock" (#71).** New `_has_bag_signal` heuristic — checks both `source.security_type` and per-fill `metadata.sec_type` — derives "Spread"/"Combo" from leg shape so snapshot-\* and legacy_id BAG groups never fall through to "Stock".

## [0.0.1] — 2026-04-24

- Versioning reset. Begin semver from `0.0.1` as part of introducing the CI/release/deploy pipeline.

## [Pre-0.0.1 history]

### Changed

- **Futu ticker navigation (feat/futu-ticker-chain-fixes).** `TickerLink` no longer short-circuits to a non-interactive `<span>` in read-only contexts — Futu rows now route into the ticker workspace like IB rows. Execution surfaces (order modal, order API) stay guarded deeper in the flow; the label on Futu rows still signals read-only via `aria-label`.
- **IB option subscriptions (feat/futu-ticker-chain-fixes).** `ib_realtime_server.js` now qualifies option contracts with `exchange="SMART"` and `currency="USD"` when subscribing. Fixes empty Delta/IV/Gamma columns in the options chain caused by node-ib rejecting under-specified option contracts.

### Added

- `web/tests/options-chain-0dte-selection.test.tsx` — documents the current contract: default expiry skips same-day options; manually selecting a 0DTE expiry loads the chain.

### Changed

- **Apex R2 ETL (feat/apex-r2-etl).** Historical OHLCV + TA-indicator computation moved out of the trend scanner into a nightly GitHub Action (`apex-data-refresh`). Scanner now reads pre-computed Parquet from Cloudflare R2 `apex-data` bucket via a local mirror at `data/apex_mirror/`. No Massive or UW calls at scan time for Stage A.
- New dependencies: `boto3`, `pyarrow`, `moto[s3]` (test). See `pyproject.toml`.
- New tribunal amendments A1–A22 shipped inline; see `docs/superpowers/plans/2026-04-16-apex-r2-etl.md` on `trend-scan-cleanup` anchor branch for the full audit trail.

### Added

- `scripts/ta_lib/r2_store.py` — Cloudflare R2 S3-compatible wrapper (ETag, typed errors, retry).
- `scripts/ta_lib/parquet_store.py` — pyarrow I/O with UTC enforcement, DST-safe HKT→UTC normalization, UTC-midnight daily bars.
- `scripts/ta_lib/apex_sync.py` — scanner-side mirror sync with atomic swap + R2-outage fallback.
- `scripts/ta_lib/dry_run_store.py` — local-filesystem stand-in for `R2Store` under `--dry-run`.
- `scripts/apex_refresh.py` — GitHub Action entrypoint. Parallel ThreadPoolExecutor driver, conditional-PUT manifest update with retry, session-completeness guard (A18).
- `.github/workflows/apex-data-refresh.yml` — nightly + Saturday-full cron + workflow_dispatch.
- `docs/runbooks/apex-r2-cutover.md` — operator runbook.

### Removed

- `scripts/ta_premarket_prep.py`, `scripts/ta_reseed_massive.py`, `scripts/ta_seed_yahoo.py`, `scripts/ta_cli.py`.
- `scripts/api_status/` directory (entire package).
- `data/ta.duckdb` runtime cache (superseded by parquet mirror).
- FastAPI scheduler hook for pre-market data prep (`_premarket_data_prep_loop` in `scripts/api/server.py`).
- Orphaned tests: `test_trend_scan_bearish.py`, `test_trend_scan_e2e.py`, `test_trend_universe.py`, `test_ta_lib/test_premarket_prep.py`, `test_ta_lib/test_seed_yahoo.py`, `test_ta_reseed_massive.py`, `test_ta_premarket_status.py`, `test_e2e_massive_pipeline.py`, `test_ta_lib/test_store_e2e.py`, `test_ta_lib/test_store.py`, `test_ta_lib/test_ta_cli_offline.py`, `test_trend_scan_status.py`.

### Follow-ups (post-soak)

- Update `docs/superpowers/specs/2026-04-16-apex-r2-etl-design.md` (on `trend-scan-cleanup` anchor) §6 to state the RSI threshold is `rsi > 40 / rsi < 60` (matching the implementation in `scripts/trend_scan_lib/stages/ta_prefilter.py:157,169`), not `rsi > 50` as originally written. This is amendment **A20**; documented here because the spec file itself is not on this branch.
- Retire `trend-scan-cleanup` branch after one clean week of the new pipeline.

### Previously [0.1.1] - 2026-04-16

#### Fixed — UW portfolio SSE cache preservation + daily stats reset at 8PM ET

Two coupled fixes to the Unusual Whales telemetry path.

##### SSE streaming no longer wipes cached tiles

`useUwPortfolio` now uses a two-Map architecture during streaming: SSE rows
accumulate independently, and the displayed state is a merge of cached tickers
(loaded from the on-disk snapshot) with incoming SSE tickers (SSE wins on
conflict). Previously, the first SSE row reset the visible list to a
single-element array, causing the rest of the portfolio's tiles to vanish for
several seconds until later events repopulated. Monotonicity now holds: the
visible ticker count never decreases during a stream. On a valid `done` event,
the snapshot cache is finalized to the SSE-only set (authoritative); incomplete
streams preserve the merged view so remounts don't drop tiles.

##### Daily stats aligned to UW's 8PM ET quota boundary

New `get_stats_with_daily()` and `get_daily_stats()` on the process-wide
`UWApiStats` singleton expose counters for the current UW daily quota window,
rolled up from hourly buckets. The sidebar now shows "UW Today" with daily
request count, cache-hit %, and 2xx/4xx/5xx breakdown — previously session
totals were displayed, which never reset and bore no relation to UW's 20k/day
budget ceiling. Boundary computation uses `ZoneInfo("America/New_York")` for
DST-correct wall-clock math. The `/uw-stats` endpoint returns session + daily
under a single lock to prevent torn snapshots under concurrent writes.

##### Tests

- `test_uw_api_stats_history.py`: 40 tests covering hour-boundary correctness,
  DST transitions in both directions, cache-hit exclusion from request count,
  zero-state behavior, and concurrent-write snapshot consistency.
- `useUwPortfolio.test.ts`: 8 tests covering cached-tile preservation during
  streaming, SSE-wins-on-conflict merge, `done`-gated finalization, and
  incomplete-stream cache behavior.

### Previously [0.1.0] - 2026-04-15

#### Added — Trend Scanner: bearish pipeline, catalyst stage, pre-market prep

Fourteen-commit feature branch addressing all nine findings from the
Codex+Gemini+Claude tribunal review of `feat/ta-integration`. Plan:
`docs/superpowers/plans/2026-04-14-trend-scanner-tribunal-fixes.md`.

##### Signal accuracy

- Breakout detection now requires `close >= high_20d` — consolidation
  narrowness alone no longer flags as breakout (Finding #3).
- Volume profile isolates up-day vs down-day volume via new
  `up_day_volume_ratio`, weighted 2× so distribution patterns penalize
  the trend score (Finding #9).
- Stage B rejects unsupported overhead walls: call wall within 2% above
  spot with no supportive put wall below now hard-fails like severe
  pinning (Finding #8).
- Snapshot exposes `high_20d`, `low_20d`, `low_52w`, `up_day_volume_ratio`
  — additive schema change, enables both breakout gate and bearish mirror.

##### Scope expansion

- **Bearish pipeline** runs alongside bullish (Finding #1). Stage A split
  into direction-neutral data fetch + direction-specific gate. Mirrored
  `passes_bearish_gate`, `detect_breakdown`,
  `has_unsupported_underhead_wall`. Structure / OI / flow scoring branch
  on direction. Live scan emits bullish AND bearish candidates per ticker.
- **Stage C catalyst check** via UW headlines + earnings/FDA/guidance
  flags (Finding #4). Degrades gracefully when headlines unavailable.
  Weight 0.10 in final ranking; weights rebalanced to explicit 5-key
  dict: `{trend: 0.30, structure: 0.25, volatility: 0.20, flow: 0.15,
catalyst: 0.10}`.
- **Analysis-only scoping**: `suggested_trade` field removed from
  `TrendCandidate`; replaced with advisory `structure_hint` (defined-risk
  long-side only). Every candidate auto-flagged
  `four_gates_not_applied`. Cross-layer change — Python model + DuckDB
  schema + TypeScript types + web components kept in lockstep
  (Finding #2).

##### Defensive hardening

- SPY pre-cache crash guard: scan no longer aborts when SPY is cold
  and IB is unavailable (Finding #7).
- Staleness check unified: `ta_premarket_prep.classify_tickers`
  delegates to `TAService._is_stale` — audit and scanner now share one
  truth. New `TAService.read_only(conn)` factory encapsulates the audit
  construction (Finding #6).
- Pre-market prep warms the full triple-source scanner universe
  (static + UW flow + IB scanner), not just the S&P 500 static slice
  (Finding #5). Persists to `data/ta_premarket_universe.json` with
  UTC timestamps + honest `source_counts` telemetry. Scanner reuses
  it if <2h old.
- `--audit-only` stays strictly offline — no UW/IB connection attempts.
- UW client cleanup symmetric with IB disconnect; refresh phase wrapped
  in try/finally.

#### Fixed

- `fetch_catalysts` tolerates `earnings_days=None` (UW may not resolve
  earnings date for every ticker) — was raising `TypeError` on the
  `0 <= None` comparison. Regression test added.
- Scanner top-level error handler now logs with `exc_info=True` so
  future regressions surface at stderr instead of being silently
  flattened to a one-line message.

#### Known follow-ups (documented, not blocking)

See `docs/superpowers/plans/2026-04-15-trend-scanner-post-verification-followups.md`:

- `UWClient.get_headlines` not implemented — catalyst score stuck at
  neutral 0.5 until added.
- `TAService.bulk_refresh` 10-min IB pacing sleep drops the socket —
  universe larger than one pacing window cannot be warmed.
- SPY/VIX should be prioritized first in refresh ordering so
  `market_context.regime` isn't `unknown` on partial refreshes.

#### Test coverage

2220 passed / 90 skipped / 0 failed across `scripts/tests/` at the tip
of the branch.

### Previously [0.0.1] - Initial

Project scaffold prior to this changelog.
