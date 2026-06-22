# Changelog

All notable changes to Xenon are documented here. Format loosely based on
[Keep a Changelog](https://keepachangelog.com/) with semver-ish versioning.

## [Unreleased]

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
