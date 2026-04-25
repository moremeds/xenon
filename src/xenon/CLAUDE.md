# src/xenon/ — CLAUDE.md

Python pipelines, scanners, clients, commands — the installable `xenon` package. Root `CLAUDE.md` is authoritative for policy; `src/xenon/api/CLAUDE.md` covers FastAPI/IB Gateway infra.

**Reference (not inline):** `docs/architecture/architecture.md` (high-throughput, perf), `docs/trading/intraday-interpolation.md` (dark pool), `docs/trading/signal-thresholds.md` (P/C, flow, analyst), `docs/architecture/data-files.md` (data/ catalog), `docs/runbooks/ops.md` (logs), `docs/reference/unusual_whales_api.md` (UW endpoints), `docs/trading/options-structures.md` (structure classification).

## Data Source Priority

1. Interactive Brokers (TWS/Gateway) — real-time quotes, chains, execution, live portfolio
2. Unusual Whales (`$UW_TOKEN`) — dark pool, sweeps, alerts
3. Web scrape — last resort

**Never use Yahoo Finance.**

**Clients:** `src/xenon/clients/` — `IBClient`, `UWClient`, `MenthorQClient`, `FutuClient`. Legacy `src/xenon/utils/{ib_connection,uw_api}.py` preserved; new code uses clients.

**Futu (read-only):** `src/xenon/clients/futu_client.py` — positions snapshot from local Futu OpenD. Never write, never subscribe to market data. Server-side singleton with asyncio singleflight lock in `src/xenon/api/server.py` (`/futu/sync` / `/futu/portfolio`). Silent degrade when OpenD unreachable.

## Scanner Libs — Shared Foundation

- `src/xenon/scanners/_shared/` — `cache`, `executor` (parallel_fetch), `models`, `scoring`, `universe`. Every scanner builds on this.
- `src/xenon/scanners/trend/` — **DEPRECATED.** Code retained for repurposing; R2/ta_lib data source removed 2026-04-26.
- `src/xenon/scanners/uw/` — tiered UW signal scanner (entries: `xenon-uw-scan`, `xenon-uw-analyze`). Signals: `dark_pool_accumulation`, `deep_conviction_flow`, `earnings_iv_crush`, `gex_pinning`. Context: `pcr_sentiment`. Confluence ranking: `ranking.py` + `confluence.py`.

New scanners compose `_shared` primitives — do not reimplement universe/executor/scoring logic.

## UW API Observability

`src/xenon/utils/uw_api_stats.py` — thread-safe singleton that records every `UWClient._get()` call (latency, cache hits, retries, rate-limits, errors). Exposed via FastAPI `/uw-stats` and surfaced in the web sidebar (`useUwStats.ts`).

`src/xenon/utils/uw_cache.py` is lock-protected because `UWClient._get` runs under `asyncio.to_thread()` and multiple evaluator threads hit the cache concurrently — **do not drop the lock**.

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
   - chain builder → `/api/orders/place` → FastAPI bridge → `src/xenon/execution/ib_place_order.py`
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

**Implementation**: `web/lib/nakedShortGuard.ts` (shared guard), `src/xenon/execution/naked_short_audit.py` (audit + cancel)
**Tests**: `web/tests/naked-short-guard.test.ts` (21 tests), `scripts/tests/test_naked_short_audit.py`

## Evaluation — 7 Milestones (Stop on Failure)

1. Validate ticker → `xenon-fetch-ticker` (`src/xenon/fetchers/fetch_ticker.py`)
   1B. Seasonality (context) | 1C. Analyst ratings (context) | 1D. News/catalysts (context)
2. Dark pool flow → `xenon-fetch-flow` (`src/xenon/fetchers/fetch_flow.py`, intraday interpolation: `docs/trading/intraday-interpolation.md`)
3. Options flow → `xenon-fetch-options` (`src/xenon/fetchers/fetch_options.py`)
   3B. OI changes → `xenon-fetch-oi` (`src/xenon/fetchers/fetch_oi_changes.py`, REQUIRED)
4. Edge decision — PASS/FAIL (FAIL = stop)
5. Structure — convex position (R:R < 2:1 = stop)
6. Kelly sizing — enforce 2.5% cap
7. Log → `trade_log.json` or `docs/status.md`

## Order Execution Modules

`src/xenon/execution/` — all IB order/fill logic:

- `ib_place_order.py`, `ib_order_manage.py` (cancel/modify via subprocess; see `api/CLAUDE.md`)
- `orders_store.py` — DuckDB orders journal (UTC-pinned writer; see api/CLAUDE.md migration notes)
- `single_leg_rehydrate.py` — three-source reconcile (orders DB, IB open orders, CRI monitor). Invoked in FastAPI lifespan on boot; exposed for testing via `POST /dev/rehydrate/synthetic`.
- `naked_short_audit.py` — Gate-4 post-sync enforcement
- `quote_guard.py`, `contract_normalize.py`, `preflight.py` — order-path safety layers

**Note:** `quote_tokens.py` exists but the integration flow (#34) was reverted (#35) — the F3 regression is still open; do not wire it back into `/orders/quote` without re-reviewing.

## Dev Environment

Python deps via **`uv`** (not pip). `pyproject.toml` defines `[project.optional-dependencies].test` (pytest + pytest-asyncio) and `.extras`. Locally:

```bash
uv sync --extra test             # install deps incl. test
uv run xenon-uw-scan --top 25    # run any CLI entry point
uv run pytest -xvs <path>        # single test
```

CI uses `uv sync --frozen --extra test` then `uv run pytest` — affected-on-PR, full-on-master.

## Append-only data files

- `data/portfolio.json` — open positions, bankroll, exposure
- `data/trade_log.json` — **append-only** trade journal

Full data catalog: `docs/architecture/data-files.md`.

## Commands

| Command                          | Action                                                                                                                                      |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `scan`                           | Watchlist dark pool scan (legacy HTML scanner, `src/xenon/scanners/scanner.py`)                                                             |
| `discover`                       | Market-wide flow for new candidates                                                                                                         |
| `uw-scan`                        | Tiered UW signal scanner with Type F confluence detection (distinct from `scan`)                                                            |
| `uw-analyze [TICKER]`            | Per-ticker deep-dive: VRP state, regime, 4-bucket composite score (chained in-process by `uw-scan --analyze-top N`)                         |
| `trend-scan`                     | **DEPRECATED.** Code retained for repurposing; R2/ta_lib data source removed 2026-04-26.                                                    |
| `futu-sync`                      | Pull read-only positions from local Futu OpenD (no orders, no market data)                                                                  |
| `evaluate [TICKER]`              | Full 7-milestone eval                                                                                                                       |
| `portfolio`                      | Positions, exposure, capacity                                                                                                               |
| `journal`                        | Recent trade log                                                                                                                            |
| `sync`                           | Pull live portfolio from IB                                                                                                                 |
| `blotter`                        | Today's fills + P&L                                                                                                                         |
| `blotter-history`                | Historical trades (Flex Query)                                                                                                              |
| `leap-scan [TICKERS]`            | LEAP IV mispricing                                                                                                                          |
| `garch-convergence [TICKERS]`    | Cross-asset GARCH vol divergence                                                                                                            |
| `seasonal [TICKERS]`             | Monthly seasonality                                                                                                                         |
| `x-scan [@ACCOUNT]`              | X post sentiment                                                                                                                            |
| `analyst-ratings [TICKERS]`      | Ratings + targets                                                                                                                           |
| `vcg-scan`                       | Vol-credit gap divergence                                                                                                                   |
| `cri-scan`                       | Crash Risk Index (CTA deleveraging)                                                                                                         |
| `menthorq-cta`                   | MenthorQ CTA positioning                                                                                                                    |
| `menthorq-dashboard [CMD]`       | Dashboard image (vol/forex/eod/intraday/futures/cryptos_technical/cryptos_options). `--ticker` for eod/intraday/futures/crypto (16 tickers) |
| `menthorq-screener [CAT] [SLUG]` | Screener (6 categories, 45 sub-screeners)                                                                                                   |
| `menthorq-forex`                 | Forex gamma levels + blindspot (14 pairs)                                                                                                   |
| `menthorq-summary [CAT]`         | Summary tables (futures: 93 rows, cryptos: 16)                                                                                              |
| `menthorq-quin [PROMPT]`         | QUIN AI screener. Presets: `docs/reference/menthorq-prompts.md`                                                                             |
