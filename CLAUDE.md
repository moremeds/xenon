# XENON — CLAUDE.md

Master policy file. Topic-specific guidance lives in subdirectory `CLAUDE.md` files:

| Area                                             | File                      |
| ------------------------------------------------ | ------------------------- |
| Frontend, pricing, P&L, share cards, reports     | `web/CLAUDE.md`           |
| Python pipelines, scanners, commands, data files | `src/xenon/CLAUDE.md`     |
| FastAPI, Clerk auth, IB Gateway, order lifecycle | `src/xenon/api/CLAUDE.md` |
| Brand tokens, typography, spectrum, UI rules     | `brand/CLAUDE.md`         |

## Brokers

- **IB** (primary) — quotes, chains, execution, portfolio. Never bypassed.
- **Futu** (read-only) — positions snapshot via local Futu OpenD. Surfaces as a separate account tab. No orders, no fills, no quotes. Silent degrade when OpenD unreachable.

## Scanner Hierarchy

- `src/xenon/scanners/_shared/` — shared foundation (cache, executor, models, scoring, universe)
- `src/xenon/scanners/trend/` (entry: `xenon-trend-scan`) — 3-stage pre-market trend scanner, DuckDB-backed (`data/trend_scan.duckdb`). Auto-runs 8:30 AM ET weekdays via the FastAPI scheduler.
- `src/xenon/scanners/uw/` (entries: `xenon-uw-scan`, `xenon-uw-analyze`) — tiered UW signal scanner with Type F confluence

New scanners MUST build on `src/xenon/scanners/_shared/` — do not duplicate universe/executor/scoring logic.

## Data Source Priority

1. Interactive Brokers — real-time quotes, chains, execution, live portfolio
2. **Cloudflare R2 `apex-data` bucket** — pre-computed OHLCV + TA indicators (nightly, via GitHub Action `apex-data-refresh`). Read-only in the scanner.
3. Massive.com — historical OHLCV source. Action-side only; the trend scanner never calls Massive directly at scan time.
4. Unusual Whales (`$UW_TOKEN`) — dark pool, sweeps, alerts (Stage B/C).
5. Web scrape — last resort.

**Never use Yahoo Finance.** Historical data flows Massive → R2 → scanner.

## ⛔ Mandatory Rules

1. **Be concise.** No preamble, no filler.
2. **E2E browser verification for ALL UI work.** Primary: `chrome-cdp`. Fallback: Playwright (`web/playwright.config.ts`). No UI change done until visually confirmed. Don't assume code changes produce the expected visual result — verify rendered output in the browser before committing.
3. **Red/green TDD for ALL code.** Failing test → fix → green → refactor. Unit: Vitest, E2E: chrome-cdp/Playwright.
4. **95% test coverage target.** Every change includes corresponding tests.
5. **API keys** in `.env` files (see Credentials below). Fallback: `~/.zshrc`.
6. **Options structure reference:** `docs/trading/options-structures.json` + `docs/trading/options-structures.md` — 58 structures, guard decisions, P&L attribution labels. Use for order entry, structure classification, and naked short guard logic.

## Identity

**Xenon** — market structure reconstruction system. Surfaces convex opportunities from dark pool/OTC flow, vol surfaces, cross-asset positioning. Detects institutional positioning, constructs convex options structures, sizes with fractional Kelly. **Flow signal or nothing.**

Brand spec: `brand/CLAUDE.md` + `docs/reference/brand-identity.md`.

## ⛔ Four Gates — Mandatory, Sequential, No Exceptions

```
GATE 1 — CONVEXITY      : Potential gain ≥ 2× potential loss. Defined-risk only (long options, verticals).
GATE 2 — EDGE           : Specific, data-backed dark pool/OTC signal that hasn't moved price yet.
GATE 3 — RISK MGMT      : Fractional Kelly sizing. Hard cap: 2.5% of bankroll per position.
GATE 4 — NO NAKED SHORTS: Never naked short stock, calls, futures, or bonds. Every short call must be fully covered by long shares (1 contract = 100 shares). Violation = immediate cancel.
```

**Any gate fails → stop. No rationalization.** Enforcement details: `src/xenon/CLAUDE.md` (naked-short table + combo guardrails).

## Credentials

| File          | Loader          | Contains                                                                                                                                                                          |
| ------------- | --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `.env` (root) | `python-dotenv` | `MENTHORQ_USER`, `MENTHORQ_PASS`, `MASSIVE_API_KEY`, `CLERK_JWKS_URL`, `CLERK_ISSUER`, `ALLOWED_USER_IDS`, `R2_ENDPOINT`, `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` |
| `web/.env`    | Next.js         | `ANTHROPIC_API_KEY`, `UW_TOKEN`, `EXA_API_KEY`, `CEREBRAS_API_KEY`, `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`, `CLERK_SECRET_KEY`                                                       |

## Market Hours

```bash
TZ=America/New_York date +"%A %H:%M"   # 9:30–16:00 ET, Mon–Fri
```

- **Open**: Fetch fresh. Cache TTL: flow 5min, ratings 15min.
- **Closed**: Use latest. Flag stale data.

### UW API budget controls

Daily UW budget: **20,000 calls/day**. The `/uw-analyze` page stays within budget via:

- **30-min snapshot TTL** during open hours (overridable via env)
- **Automatic refresh blocked entirely** outside 9:30–16:00 ET (weekdays)
- **Weekday holidays treated as OPEN** (known gap — neither backend nor frontend has a holiday calendar; cost is ~1 day over-budget per US holiday)
- **Refresh button always works** — `POST /uw-analyze/refresh` and `/uw-analyze?ticker=X` pass `user_initiated=True` which bypasses the closed-market gate

Env vars (read per-call, runtime tunable):

- `XENON_UW_TTL_OPEN_S` (default `1800`) — snapshot TTL during open hours
- `XENON_UW_TTL_CLOSED_S` (default `3600`) — TTL for user-initiated fetches when market is closed

The closed-market gate lives inside `UwAnalyzeCache.get_or_run()` and also covers the separate on-demand OI fetch in `_process_ticker`. See `src/xenon/api/services/uw_analyze_cache.py` + `src/xenon/api/routes/uw_analyze.py` for the implementation.

## Output Rules

- Always: `signal → structure → Kelly math → decision`
- State probabilities; flag uncertainty
- Failing gate = immediate stop, name the gate
- **Never rationalize a bad trade**
- Executed → `trade_log.json` | NO_TRADE → `docs/status.md`

## Startup Checklist

- [ ] `scripts/cloud.sh` (default — local dev services + VPS IB Gateway via Tailscale) — OR `scripts/local.sh` (fully local with Docker gateway)
- [ ] If local mode: approve 2FA on IBKR mobile for cold start
- [ ] `curl http://localhost:8321/health` — verify `ib_gateway.port_listening: true`
- [ ] Reconciliation auto-runs → `data/reconciliation.json`
- [ ] Exit order service auto-runs (PENDING_MANUAL)
- [ ] CRI scan service running (30-min intervals)
- [ ] Pre-market trend scan runs 8:30 AM ET weekdays → `data/trend_scan.json`
- [ ] X scan if >12h stale
- [ ] Check market hours

## Tests

```bash
python3.13 scripts/infra/dev/run_pytest_affected.py                 # scoped Python tests (preferred)
cd web && npm test                                                  # Vitest
cd web && npx playwright test                                       # E2E
python3.13 -m pytest scripts/tests/test_foo.py::test_name -xvs      # single test
```

Order-route integration tests use `web/tests/fastapiHarness.ts` with `XENON_API_TEST_MODE` to stub broker calls — no live IB required.
