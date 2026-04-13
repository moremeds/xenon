# scripts/ — CLAUDE.md

Python pipelines, scanners, clients, commands. Root `CLAUDE.md` is authoritative for policy; `scripts/api/CLAUDE.md` covers FastAPI/IB Gateway infra.

**Reference (not inline):** `docs/architecture/architecture.md` (high-throughput, perf), `docs/trading/intraday-interpolation.md` (dark pool), `docs/trading/signal-thresholds.md` (P/C, flow, analyst), `docs/architecture/data-files.md` (data/ catalog), `docs/runbooks/ops.md` (logs), `docs/reference/unusual_whales_api.md` (UW endpoints), `docs/trading/options-structures.md` (structure classification).

## Data Source Priority

1. Interactive Brokers (TWS/Gateway) — real-time
2. Unusual Whales (`$UW_TOKEN`) — dark pool, sweeps, alerts
3. Yahoo Finance — fallback only
4. Web scrape — last resort

**Never skip to Yahoo/web without trying IB → UW first.**

**Clients:** `scripts/clients/` — `IBClient`, `UWClient`, `MenthorQClient`, `FutuClient`. Legacy `scripts/utils/{ib_connection,uw_api}.py` preserved; new code uses clients.

**Futu (read-only):** `scripts/clients/futu_client.py` — positions snapshot from local Futu OpenD. Never write, never subscribe to market data. Server-side singleton with asyncio singleflight lock in `scripts/api/server.py` (`/futu/sync` / `/futu/portfolio`). Silent degrade when OpenD unreachable.

## Scanner Libs — Shared Foundation

- `scripts/scanner_lib/` — `cache`, `executor` (parallel_fetch), `models`, `scoring`, `universe`. Every scanner builds on this.
- `scripts/trend_scan_lib/` — 3-stage pre-market trend scanner. Stages: `ta_prefilter` → `options_structure` + `volatility` + `flow_confirmation`. Config: `config.py`. Storage: DuckDB (`data/trend_scan.duckdb`) via `storage.py` — `duckdb` package imported lazily so scanners that don't need persistence still run.
- `scripts/uw_scan_lib/` — tiered UW signal scanner. Signals: `dark_pool_accumulation`, `deep_conviction_flow`, `earnings_iv_crush`, `gex_pinning`. Context: `pcr_sentiment`. Confluence ranking: `ranking.py` + `confluence.py`.
- `scripts/ta_lib/` — TA-Lib indicators with IB historical data and DuckDB caching. `bars.py` (IB OHLC fetch), `indicators.py` (TA-Lib wrappers), `store.py` (DuckDB at `data/ta.duckdb`), `service.py` (`TAService` read-through cache). Supports `1d` and `1h` timeframes. First consumer: `trend_scan.py`. Design spec: `docs/superpowers/specs/2026-04-13-ta-lib-module-design.md`.

New scanners compose `scanner_lib` primitives — do not reimplement universe/executor/scoring logic.

## UW API Observability

`scripts/utils/uw_api_stats.py` — thread-safe singleton that records every `UWClient._get()` call (latency, cache hits, retries, rate-limits, errors). Exposed via FastAPI `/uw-stats` and surfaced in the web sidebar (`useUwStats.ts`).

`scripts/utils/uw_cache.py` is lock-protected because `UWClient._get` runs under `asyncio.to_thread()` and multiple evaluator threads hit the cache concurrently — **do not drop the lock**.

## Combo / BAG Order Guardrails

1. **Never map combo `Order.action` from debit vs credit.**
   - In IB, combo leg actions define the intended structure.
   - A `SELL` BAG envelope reverses the legs.
   - For entry/open chain combos, keep the envelope on `BUY` and preserve per-leg actions.
2. **When the order-builder structure changes, clear stale top-level manual net pricing.**
   - Single-leg → combo transitions must invalidate the previous manual limit price.
   - Recompute the limit field from the normalized combo quote for the current structure.
3. **Required regressions for combo-entry bugs:**
   - unit test for combo action/ratio/net-price semantics
   - browser test for displayed combo net price and submitted payload
4. **Trace the full path before fixing:**
   - chain builder → `/api/orders/place` → FastAPI bridge → `scripts/ib_place_order.py`
   - verify whether the bug is UI state, payload semantics, or IB combo behavior before patching

## Naked Short Protection (Gate 4)

**Hard rule — no exceptions.** The system must never allow naked short exposure.

| Scenario                                          | Rule                                               | Action                      |
| ------------------------------------------------- | -------------------------------------------------- | --------------------------- |
| SELL stock, no long shares                        | Naked short stock                                  | BLOCK                       |
| SELL call, no long shares or long calls           | Naked short call                                   | BLOCK                       |
| SELL call, long calls at same expiry (any strike) | Vertical spread                                    | ALLOW                       |
| SELL N call contracts, shares < N × 100           | Short a tail                                       | BLOCK                       |
| SELL put (cash-secured)                           | Defined risk                                       | ALLOW                       |
| Vertical spread (BUY C + SELL C)                  | Long call covers short                             | ALLOW                       |
| Short risk reversal (SELL C + BUY P)              | Naked short call — long put does not cover         | BLOCK                       |
| 1x2 ratio spread (BUY 1C + SELL 2C)               | 1 uncovered short call                             | BLOCK (unless stock covers) |
| Jade Lizard / Seagull (BUY C + SELL C + SELL P)   | Call spread covers short call; put is cash-secured | ALLOW                       |
| Combo closing (action=SELL)                       | Reduces exposure                                   | ALLOW                       |
| BUY anything                                      | No short exposure                                  | ALLOW                       |

**Enforcement layers:**

1. **UI pre-submission** — `checkNakedShortRisk()` in `OrderTab.tsx` blocks form submission
2. **API gate** — `orders/place/route.ts` returns 403 if guard fails
3. **Post-sync audit** — `naked_short_audit.py` runs after every `ib_sync`, cancels violating open orders

**Combo check design**: IB BAG orders always use `action=BUY` envelope. Guard inspects leg-level `right` and `action` fields. `sellCallRatio - buyCallRatio` = uncovered short calls. Checked before the BUY early-return.

**Implementation**: `web/lib/nakedShortGuard.ts` (shared guard), `scripts/naked_short_audit.py` (audit + cancel)
**Tests**: `web/tests/naked-short-guard.test.ts` (21 tests), `scripts/tests/test_naked_short_audit.py`

## Evaluation — 7 Milestones (Stop on Failure)

1. Validate ticker → `scripts/fetch_ticker.py`
   1B. Seasonality (context) | 1C. Analyst ratings (context) | 1D. News/catalysts (context)
2. Dark pool flow → `scripts/fetch_flow.py` (intraday interpolation: `docs/trading/intraday-interpolation.md`)
3. Options flow → `scripts/fetch_options.py`
   3B. OI changes → `scripts/fetch_oi_changes.py` (REQUIRED)
4. Edge decision — PASS/FAIL (FAIL = stop)
5. Structure — convex position (R:R < 2:1 = stop)
6. Kelly sizing — enforce 2.5% cap
7. Log → `trade_log.json` or `docs/status.md`

## Append-only data files

- `data/portfolio.json` — open positions, bankroll, exposure
- `data/trade_log.json` — **append-only** trade journal

Full data catalog: `docs/architecture/data-files.md`.

## Commands

| Command                          | Action                                                                                                                                                                     |
| -------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `scan`                           | Watchlist dark pool scan (legacy HTML scanner, `scripts/scanner.py`)                                                                                                       |
| `discover`                       | Market-wide flow for new candidates                                                                                                                                        |
| `uw-scan`                        | Tiered UW signal scanner with Type F confluence detection (distinct from `scan`)                                                                                           |
| `uw-analyze [TICKER]`            | Per-ticker deep-dive: VRP state, regime, 4-bucket composite score (chained in-process by `uw-scan --analyze-top N`)                                                        |
| `trend-scan`                     | 3-stage pre-market trend scanner (TA prefilter → structure/vol/flow). DuckDB-backed. Auto-runs 8:30 AM ET weekdays.                                                        |
| `futu-sync`                      | Pull read-only positions from local Futu OpenD (no orders, no market data)                                                                                                 |
| `evaluate [TICKER]`              | Full 7-milestone eval                                                                                                                                                      |
| `portfolio`                      | Positions, exposure, capacity                                                                                                                                              |
| `journal`                        | Recent trade log                                                                                                                                                           |
| `sync`                           | Pull live portfolio from IB                                                                                                                                                |
| `blotter`                        | Today's fills + P&L                                                                                                                                                        |
| `blotter-history`                | Historical trades (Flex Query)                                                                                                                                             |
| `leap-scan [TICKERS]`            | LEAP IV mispricing                                                                                                                                                         |
| `garch-convergence [TICKERS]`    | Cross-asset GARCH vol divergence                                                                                                                                           |
| `seasonal [TICKERS]`             | Monthly seasonality                                                                                                                                                        |
| `x-scan [@ACCOUNT]`              | X post sentiment                                                                                                                                                           |
| `analyst-ratings [TICKERS]`      | Ratings + targets                                                                                                                                                          |
| `vcg-scan`                       | Vol-credit gap divergence                                                                                                                                                  |
| `cri-scan`                       | Crash Risk Index (CTA deleveraging)                                                                                                                                        |
| `menthorq-cta`                   | MenthorQ CTA positioning                                                                                                                                                   |
| `menthorq-dashboard [CMD]`       | Dashboard image (vol/forex/eod/intraday/futures/cryptos_technical/cryptos_options). `--ticker` for eod/intraday/futures/crypto (16 tickers)                                |
| `menthorq-screener [CAT] [SLUG]` | Screener (6 categories, 45 sub-screeners)                                                                                                                                  |
| `menthorq-forex`                 | Forex gamma levels + blindspot (14 pairs)                                                                                                                                  |
| `menthorq-summary [CAT]`         | Summary tables (futures: 93 rows, cryptos: 16)                                                                                                                             |
| `menthorq-quin [PROMPT]`         | QUIN AI screener. Presets: `docs/reference/menthorq-prompts.md`                                                                                                            |
| `ta-cli [TICKERS]`               | TA-Lib manual test CLI. `--history` full DataFrame, `--refresh` bulk IB fetch, `--cache-only` no IB, `--query SQL` raw DuckDB, `--stats` DB overview, `-tf 1h` hourly bars |
