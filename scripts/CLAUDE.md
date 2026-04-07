# scripts/ — CLAUDE.md

Python pipelines, scanners, clients, commands, data contracts. Root `CLAUDE.md` is authoritative for policy; `scripts/api/CLAUDE.md` covers the FastAPI + IB Gateway infra.

## Data Source Priority

1. Interactive Brokers (TWS/Gateway) — real-time
2. Unusual Whales (`$UW_TOKEN`) — dark pool, sweeps, alerts
3. Yahoo Finance — fallback only
4. Web scrape — last resort

**Never skip to Yahoo/web without trying IB → UW first.**

**Clients:** `scripts/clients/` — `IBClient`, `UWClient`, `MenthorQClient`. Legacy `scripts/utils/{ib_connection,uw_api}.py` preserved; new code uses clients.

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

| Scenario | Rule | Action |
|----------|------|--------|
| SELL stock, no long shares | Naked short stock | BLOCK |
| SELL call, no long shares or long calls | Naked short call | BLOCK |
| SELL call, long calls at same expiry (any strike) | Vertical spread | ALLOW |
| SELL N call contracts, shares < N × 100 | Short a tail | BLOCK |
| SELL put (cash-secured) | Defined risk | ALLOW |
| Vertical spread (BUY C + SELL C) | Long call covers short | ALLOW |
| Short risk reversal (SELL C + BUY P) | Naked short call — long put does not cover | BLOCK |
| 1x2 ratio spread (BUY 1C + SELL 2C) | 1 uncovered short call | BLOCK (unless stock covers) |
| Jade Lizard / Seagull (BUY C + SELL C + SELL P) | Call spread covers short call; put is cash-secured | ALLOW |
| Combo closing (action=SELL) | Reduces exposure | ALLOW |
| BUY anything | No short exposure | ALLOW |

**Enforcement layers:**
1. **UI pre-submission** — `checkNakedShortRisk()` in `OrderTab.tsx` blocks form submission
2. **API gate** — `orders/place/route.ts` returns 403 if guard fails
3. **Post-sync audit** — `naked_short_audit.py` runs after every `ib_sync`, cancels violating open orders

**Combo check design**: IB BAG orders always use `action=BUY` envelope. Guard inspects leg-level `right` and `action` fields. `sellCallRatio - buyCallRatio` = uncovered short calls. Checked before the BUY early-return.

**Implementation**: `web/lib/nakedShortGuard.ts` (shared guard), `scripts/naked_short_audit.py` (audit + cancel)
**Tests**: `web/tests/naked-short-guard.test.ts` (21 tests), `scripts/tests/test_naked_short_audit.py`

## High-Throughput Architecture

500+ symbols, <500ms signal-to-order.

**Parallel scanning:** `scanner.py` (15 workers), `discover.py` (10 workers). `--workers N` CLI. `UWRateLimitError` skips ticker, doesn't crash batch.

**Atomic state:** `scripts/utils/atomic_io.py` — `atomic_save()` (temp + `os.replace()` + SHA-256), `verified_load()`. Writers: `ib_sync.py`. Readers: reconcile, flow, free_trade, performance, leap scanner.

**Batched WS relay:** `ib_realtime_server.js` — per-client last-write-wins, 100ms flush. 5000 msg/s → 10 batched/s. Initial state immediate.

**Stale tick detection:** Relay tracks `lastTickTimestamp`, checks every 30s during market hours. No ticks for 45s → auto-restart Gateway (120s cooldown).

**Vectorized math:** `kelly_size_batch()` (NumPy), `portfolio_greeks_vectorized()`. Cross-validated with TS `approxDelta()` to 10⁻¹².

**Resilient IBClient** (`scripts/clients/ib_client.py`): Subscription tracking, disconnect recovery (5 attempts, 2ⁿs capped 30s), pacing violations (162/366: 10s backoff, 3 retries), invalid contracts (200/354: no retry, added to `_failed_contracts`).

**Incremental sync:** `scripts/utils/incremental_sync.py` — diff by `(ticker, expiry)` + contract count, skip full sync when unchanged.

## Performance Page Optimization

`scripts/portfolio_performance.py` — two-phase parallel fetch:
- **Phase A** (sequential): IB stock history + cache checks
- **Phase B** (ThreadPoolExecutor): UW/Yahoo fallbacks + option history. Per-worker `UWClient`.

`PERF_FETCH_WORKERS` env (default 8, clamped 1-20). Disk cache: `data/price_history_cache/`, SHA-256 filenames, TTL 15min/24h. SWR: cached → background rebuild via `POST /performance/background`. Cold start blocks on sync `POST /performance` (180s). Tests: 211 total (160 Python + 51 TS).

## Evaluation — 7 Milestones (Stop on Failure)

1. Validate ticker → `scripts/fetch_ticker.py`
1B. Seasonality (context) | 1C. Analyst ratings (context) | 1D. News/catalysts (context)
2. Dark pool flow → `scripts/fetch_flow.py` (with intraday interpolation)
3. Options flow → `scripts/fetch_options.py`
3B. OI changes → `scripts/fetch_oi_changes.py` (REQUIRED)
4. Edge decision — PASS/FAIL (FAIL = stop)
5. Structure — convex position (R:R < 2:1 = stop)
6. Kelly sizing — enforce 2.5% cap
7. Log → `trade_log.json` or `docs/status.md`

## Intraday Dark Pool Interpolation

When evaluating during market hours, today's partial data is volume-weighted interpolated to estimate full-day values. **Always output BOTH actual and interpolated values.**

### Why Interpolation is Required

Comparing today's partial data (e.g., 45% of day) to yesterday's full-day data is misleading. A "55% buy ratio" at noon could become 75% by close, or could be masking active distribution.

### Calculation Method

**Step 1: Trading Day Progress**
```
Progress = Minutes Since 9:30 AM ET / 390 minutes
```

**Step 2: Project Today's Volume**
```
Projected Volume = Actual Volume / Progress
Projected Buy = Actual Buy Volume / Progress
Projected Sell = Actual Sell Volume / Progress
```

**Step 3: Blend with Prior Pattern**
```
Actual Weight = Progress (e.g., 0.45 at noon)
Prior Weight = 1 - Progress (e.g., 0.55)

Prior Avg Buy Ratio = Mean of prior 5 days' buy ratios
Blended Ratio = (Today's Projected Ratio × Actual Weight) + (Prior Avg × Prior Weight)
```

**Step 4: Recalculate Aggregate**
Use interpolated today + actual prior days for aggregate strength.

### Confidence Levels

| Progress | Confidence | Blending |
|----------|------------|----------|
| 0-25% | VERY_LOW | 75%+ prior weight |
| 25-50% | LOW | 50-75% prior weight |
| 50-75% | MEDIUM | 25-50% prior weight |
| 75-100% | HIGH | <25% prior weight |

### Volume Pace

```
Expected Volume = Avg Prior Volume × Progress
Volume Pace = Actual Volume / Expected Volume
```

Pace >1.1x = above average (signal more reliable). Pace <0.9x = below average (signal less reliable).

### Output Format (MANDATORY)

Always show both when `is_interpolated: true`:

```
TODAY'S FLOW (45% of trading day)
                      ACTUAL          INTERPOLATED
  Buy Ratio:           25.4%           53.3%
  Direction:          DISTRIBUTION   NEUTRAL
  Strength:            49.3             0.0

AGGREGATE (5-Day)
                      ACTUAL          INTERPOLATED
  Buy Ratio:           70.4%           65.3%
  Strength:            40.7            30.6
```

### Edge Assessment with Interpolation

Use **interpolated values** for edge determination, but flag confidence level:
- LOW/VERY_LOW confidence → recommend re-evaluation after 2 PM ET
- Volume pace >1.2x → signal is real despite partial data
- Today's actual direction opposite to prior pattern → likely reversal, not noise

## Commands

| Command | Action |
|---------|--------|
| `scan` | Watchlist dark pool scan |
| `discover` | Market-wide flow for new candidates |
| `evaluate [TICKER]` | Full 7-milestone eval |
| `portfolio` | Positions, exposure, capacity |
| `journal` | Recent trade log |
| `sync` | Pull live portfolio from IB |
| `blotter` | Today's fills + P&L |
| `blotter-history` | Historical trades (Flex Query) |
| `leap-scan [TICKERS]` | LEAP IV mispricing |
| `garch-convergence [TICKERS]` | Cross-asset GARCH vol divergence |
| `seasonal [TICKERS]` | Monthly seasonality |
| `x-scan [@ACCOUNT]` | X post sentiment |
| `analyst-ratings [TICKERS]` | Ratings + targets |
| `vcg-scan` | Vol-credit gap divergence |
| `cri-scan` | Crash Risk Index (CTA deleveraging) |
| `menthorq-cta` | MenthorQ CTA positioning |
| `menthorq-dashboard [CMD]` | Dashboard image (vol/forex/eod/intraday/futures/cryptos_technical/cryptos_options). `--ticker` for eod/intraday/futures/crypto (16 tickers) |
| `menthorq-screener [CAT] [SLUG]` | Screener (6 categories, 45 sub-screeners) |
| `menthorq-forex` | Forex gamma levels + blindspot (14 pairs) |
| `menthorq-summary [CAT]` | Summary tables (futures: 93 rows, cryptos: 16) |
| `menthorq-quin [PROMPT]` | QUIN AI screener. Presets: `docs/menthorq-prompts.md` |

## Key Scripts

| Script | Purpose |
|--------|---------|
| `scripts/cloud.sh` | Hybrid dev: local services + VPS IB Gateway via Tailscale (default workflow) |
| `scripts/local.sh` | Fully local: stop VPS gateway, start local Docker gateway, launch dev |
| `scripts/api/server.py` | FastAPI — 21 endpoints, IB pool, auto-restart |
| `scripts/api/ib_pool.py` | Role-based IB pool (sync=3, orders=4, data=5) |
| `scripts/api/ib_gateway.py` | IB Gateway health + auto-restart |
| `scripts/api/subprocess.py` | Async subprocess helper |
| `scripts/clients/ib_client.py` | IBClient — orders, quotes, options, fills, flex, resilient reconnect |
| `scripts/clients/uw_client.py` | UWClient — dark pool, flow, chain, ratings, seasonality, 50+ endpoints |
| `scripts/clients/menthorq_client.py` | MenthorQClient — browser automation, dashboards, screeners, CTA |
| `scripts/scanner.py` | Watchlist batch scan (ThreadPoolExecutor) |
| `scripts/discover.py` | Market-wide flow scanner |
| `scripts/kelly.py` | Kelly calc — scalar + vectorized batch |
| `scripts/ib_sync.py` | Sync IB portfolio (atomic writes). Detects: covered calls, verticals, synthetics, risk reversals, straddles, all-long combos |
| `scripts/ib_reconcile.py` | Reconcile fills vs trade_log |
| `scripts/ib_place_order.py` | JSON-in/out order placement (client ID 26) |
| `scripts/ib_order_manage.py` | Cancel/modify open orders |
| `scripts/exit_order_service.py` | Pending exit orders |
| `scripts/portfolio_performance.py` | Parallel price history + performance calc |
| `scripts/cri_scan.py` | Crash Risk Index |
| `scripts/vcg_scan.py` | Vol-Credit Gap scanner (20-session history) |
| `scripts/generate_vcg_share.py` | VCG X share report (4 cards + preview) |
| `scripts/fetch_menthorq_cta.py` | MenthorQ CTA (S3 + Vision) |
| `scripts/fetch_menthorq_dashboard.py` | MenthorQ dashboards (S3/screenshot + Vision) |
| `scripts/ib_realtime_server.js` | WS relay — batched, 100ms flush, ticket-based auth on upgrade |
| `scripts/utils/atomic_io.py` | Atomic JSON save/load + SHA-256 |
| `scripts/utils/vectorized_greeks.py` | NumPy portfolio delta/gamma |
| `scripts/utils/incremental_sync.py` | Diff-based portfolio sync |
| `scripts/utils/price_cache.py` | Price cache — SHA-256 filenames, atomic, TTL, thread-safe prune |
| `scripts/run_cri_scan.sh` | Holiday-aware CRI wrapper for launchd |
| `scripts/monitor_daemon/run.py` | Monitor daemon — fills, exit orders, rebalance, Flex token check |
| `scripts/benchmarks/autoresearch.sh` | Scanner benchmark (timing + metrics) |

## Critical Data Files

| File | Purpose |
|------|---------|
| `data/portfolio.json` | Open positions, bankroll, exposure |
| `data/trade_log.json` | **Append-only** trade journal |
| `docs/options-structures.json` | Options structure catalog — 58 structures, guard decisions, bias, risk profile |
| `data/watchlist.json` | Surveillance tickers |
| `data/ticker_cache.json` | Ticker → company cache |
| `data/reconciliation.json` | IB reconciliation |
| `data/seasonality_cache/` | Per-ticker seasonality |
| `data/menthorq_cache/` | CTA + dashboard cache (daily) |
| `data/cri_scheduled/` | Intraday CRI time-series |
| `data/vcg.json` | VCG scan cache (signal, 20-session history) |
| `data/price_history_cache/` | Stock + option price histories (auto-pruned at 500) |

## Position Structure Classification (`ib_sync.py`)

`detect_structure_type()`:

| Structure | Risk Profile |
|-----------|-------------|
| Stock | `equity` |
| Long Call/Put | `defined` |
| Short Call/Put | `undefined` |
| Bull/Bear Spread | `defined` |
| Synthetic Long/Short | `undefined` |
| Risk Reversal | `undefined` |
| Straddle/Strangle (both long) | `defined` |
| Covered Call | `defined` |
| **All-long combo** (no shorts, no stock) | **`defined`** |
| Unrecognized | `complex` → routed to Undefined Risk table |

Tests: `test_covered_call_detection.py` (7), `test_all_long_combo.py` (8), `complex-risk-profile.test.ts` (5).

## UW API Quick Reference

```
Base: https://api.unusualwhales.com | Auth: Bearer $UW_TOKEN
```

| Endpoint | Use |
|----------|-----|
| `/api/darkpool/{ticker}` | Dark pool (primary edge) |
| `/api/option-trades/flow-alerts` | Sweeps, blocks |
| `/api/stock/{ticker}/info` | Validation |
| `/api/stock/{ticker}/option-contracts` | Chain |
| `/api/stock/{ticker}/greek-exposure` | GEX |
| `/api/screener/analysts` | Ratings |
| `/api/seasonality/{ticker}/monthly` | Seasonality |
| `/api/shorts/{ticker}/interest-float/v2` | Short interest |

Full spec: `docs/unusual_whales_api.md`

## Signal Interpretation

**P/C Ratio:** >2.0 BEARISH | 1.2–2.0 LEAN_BEAR | 0.8–1.2 NEUTRAL | 0.5–0.8 LEAN_BULL | <0.5 BULLISH
**Flow Side:** Ask-dominant = buying | Bid-dominant = selling
**Analyst Buy%:** ≥70% BULL | 50–69% LEAN_BULL | 30–49% LEAN_BEAR | <30% BEAR
**Discovery Score:** 60–100 Strong | 40–59 Monitor | 20–39 Weak | <20 None
**Seasonality:** >60% FAVORABLE | 50–60% NEUTRAL | <50% UNFAVORABLE

> Seasonality/ratings = context, not gates. Strong flow overrides weak seasonality.

## Log Rotation

Two layers prevent log bloat in `logs/`:

| Layer | Mechanism | Config |
|-------|-----------|--------|
| Python | `RotatingFileHandler` in `scripts/monitor_daemon/run.py` | 10MB max, 2 compressed backups |
| System | `newsyslog` via `/etc/newsyslog.d/xenon.conf` | 10MB max, 2 bzip2 backups, covers all `logs/*.log` |
