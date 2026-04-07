# Xenon

<p align="center">
  <img src=".github/hero.png" alt="Xenon - Reconstructing Market Structure" width="900" />
</p>

<p align="center">
  <img alt="Python 3.13" src="https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white" />
  <img alt="Next.js 16" src="https://img.shields.io/badge/Next.js-16-000000?logo=nextdotjs&logoColor=white" />
  <img alt="Test stack" src="https://img.shields.io/badge/Tests-pytest%20%7C%20Vitest%20%7C%20Playwright-0A7F6F" />
</p>

**Reconstructing market structure from institutional signals.**

Xenon detects institutional positioning through dark pool flow, volatility signals, and cross-asset data, then turns it into convex options trades sized by fractional Kelly.

**No narrative trades. No TA trades. Flow signal or nothing.**

---

## Four Gates (sequential, no exceptions)

| Gate | Rule |
|---|---|
| **1. Convexity** | Potential gain ≥ 2× potential loss. Defined-risk only. |
| **2. Edge** | Specific, data-backed dark pool / OTC signal that hasn't moved price yet. |
| **3. Risk** | Fractional Kelly. Hard cap: 2.5% of bankroll per position. |
| **4. No Naked Shorts** | Every short call must be covered by long shares (1:100). Violations auto-cancel. |

Any gate fails → stop. Full enforcement matrix: [`scripts/CLAUDE.md`](scripts/CLAUDE.md).

## Strategies

| Strategy | Signal | Typical Structure | Timeframe |
|---|---|---|---|
| **Dark Pool Flow** | Hidden institutional accumulation/distribution | Long options, vertical spreads | 2–6 weeks |
| **LEAP IV Mispricing** | Realized vol above long-dated IV | Long LEAPs, diagonals | Weeks–9 months |
| **GARCH Convergence** | Cross-asset vol repricing lag | Calendars, verticals | 2–8 weeks |
| **Risk Reversal** | Put/call skew distortion | Risk reversal | 2–8 weeks |
| **Volatility-Credit Gap (VCG-R)** | VIX>28 + VCG>2.5σ | HYG puts, bear put spreads | 1–5 days |
| **Crash Risk Index (CRI)** | CTA deleveraging + COR1M stress | Index puts, tactical hedges | 3–5 days |

Full specs: [`docs/trading/strategies.md`](docs/trading/strategies.md) · VCG math: [`docs/trading/strategy-vcg.md`](docs/trading/strategy-vcg.md).

## Quick Start

**Prerequisites**

- Python `3.13` (3.14 has ib_insync/eventkit incompatibility)
- Node.js `18+`
- [Interactive Brokers](https://ibkr.com/referral/joseph5632) Gateway (cloud via Tailscale, Docker, or local TWS)
- [Unusual Whales](https://unusualwhales.com/referral#39985a64-656c-4642-a051-db89f6324d64) API access
- (Optional) Futu OpenD — read-only positions from a Futu account

```bash
git clone https://github.com/moremeds/xenon.git
cd xenon
pip install -r requirements.txt
cd web && npm install && cd ..

# Dev (default: local services + VPS IB Gateway via Tailscale)
scripts/cloud.sh

# Fully local alternative
scripts/local.sh
```

Terminal at `http://localhost:3000`. Health: `curl http://localhost:8321/health`.

## Environment

**`web/.env`** — frontend + API keys:

```bash
ANTHROPIC_API_KEY=...
UW_TOKEN=...
EXA_API_KEY=...
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_...
CLERK_SECRET_KEY=sk_...
```

**`.env`** (root) — Python scripts, IB Gateway, auth:

```bash
MENTHORQ_USER=...
MENTHORQ_PASS=...
IB_GATEWAY_HOST=127.0.0.1           # or ib-gateway (cloud/Tailscale)
IB_GATEWAY_PORT=4001
IB_GATEWAY_MODE=docker              # docker | cloud | launchd
CLERK_JWKS_URL=...
CLERK_ISSUER=...
ALLOWED_USER_IDS=user_...
```

MenthorQ workflows additionally need `pip install playwright httpx && playwright install chromium`.

## Xenon Terminal

Real-time trading terminal built with **Next.js 16**. Streams IB prices, computes live greeks, visualizes portfolio exposures, and drives scans, evaluation, and order management.

**Core capabilities**

- Real-time price streaming with live greeks, shared `BID/MID/ASK/SPREAD` layout across ticker, chain, and modify views
- Multi-leg position monitoring with per-leg P&L
- Multi-broker account tab bar: switch between IB (live trading) and Futu (read-only positions snapshot)
- YTD portfolio performance with ET-session refresh guarding against stale snapshots
- `/regime` strip: VIX/VVIX/SPY/RVOL/COR1M with day-change indicators and 20-session history charts
- RVOL/COR1M relationship view (Systemic Panic / Fragile Calm / Stock Picker's / Goldilocks)
- Combo spread order workflows with natural-market pricing
- VCG/CRI regime panels with adaptive polling
- AI chat for command execution and analysis

Authentication via [Clerk](https://clerk.com) — Next.js middleware protects routes, FastAPI validates JWTs, WebSocket uses 30s single-use tickets. Local dev bypasses auth when `CLERK_JWKS_URL` is unset.

Component-level reference: [`docs/reference/web-ui-reference.md`](docs/reference/web-ui-reference.md).

## CLI Commands

**Scanning:** `scan` · `discover` · `leap-scan` · `garch-convergence` · `seasonal` · `analyst-ratings`
**Evaluation:** `evaluate [TICKER]` · `stress-test` · `risk-reversal` · `vcg` · `cri-scan`
**Portfolio:** `portfolio` · `free-trade` · `journal` · `sync` · `blotter` · `blotter-history`
**Research:** `strategies` · `menthorq-cta` · `x-scan` · `commands`

Full table with descriptions: [`scripts/CLAUDE.md`](scripts/CLAUDE.md).

## Architecture

```text
IB / UW / MenthorQ / Exa  →  Signal Detection  →  Strategy Evaluation
                                                    │
                                                    ▼
                                        Convex Structure Builder
                                                    │
                                                    ▼
                                        Kelly Position Sizing
                                                    │
                                                    ▼
                                        Execution + Monitoring (IB)
```

- `scripts/` — Python scanners, evaluators, broker clients, FastAPI bridge
- `web/` — Next.js terminal (portfolio, orders, regime, AI)
- `site/` — standalone marketing site (separate deployment)
- `brand/` — design system and tokens
- `data/` — runtime artifacts (gitignored)
- `docs/` — strategy specs, API refs, runbooks ([index](docs/README.md))

High-throughput design, perf optimization, WS relay: [`docs/architecture/architecture.md`](docs/architecture/architecture.md).
FastAPI, auth, IB Gateway modes: [`docs/architecture/api-infrastructure.md`](docs/architecture/api-infrastructure.md).

## Data Source Priority (strict)

1. **[Interactive Brokers](https://ibkr.com/referral/joseph5632)** — real-time quotes, chains, portfolio, execution
2. **[Unusual Whales](https://unusualwhales.com/referral#39985a64-656c-4642-a051-db89f6324d64)** — dark pool, sweeps, flow, analysts
3. **Exa** — research
4. **Cboe** — COR1M historical fallback
5. **Yahoo Finance** — strict last resort

Auxiliary: **Futu OpenD** (read-only positions snapshot for Futu-held accounts — never written to), **MenthorQ** (CTA positioning for CRI), **xAI** (X sentiment).

### Futu (read-only)

Futu support is intentionally observe-only: `scripts/clients/futu_client.py` fetches positions and account info from a local Futu OpenD instance, exposed via `/futu/sync` on the FastAPI bridge (10s cooldown, singleton-lifecycle, singleflight lock). The terminal surfaces it as a separate account tab alongside IB. No orders, no fills, no market-data subscriptions flow through Futu. Requires Futu OpenD running locally; the client stays quiet and degrades gracefully when unreachable.

## Testing

- **Python:** `pytest` — scanners, evaluation, utilities, adapters
- **Frontend:** `Vitest` — web logic
- **E2E:** `Playwright` — browser workflows

```bash
python3.13 scripts/run_pytest_affected.py    # scoped — only affected tests
cd web && npm test                            # Vitest
cd web && npx playwright test                 # E2E
```

Prefer `run_pytest_affected.py` over a full repo run. Unit tests mock IB/UW, so most work needs no live connection. Order-route integration tests use an isolated FastAPI harness (`web/tests/fastapiHarness.ts`) that stubs broker calls — see `XENON_API_TEST_MODE`.

## Services

| Service | Purpose |
|---|---|
| IB Gateway (cloud/Docker/launchd) | Broker session for quotes, execution, reports |
| CRI scan service | Intraday crash-risk refresh with atomic cache snapshots |
| CTA sync service | MenthorQ CTA cache at 4:15/5:00 PM ET with `RunAtLoad` catch-up |
| Monitor daemon | Fills, exit orders, off-hours rebalance, Flex token checks (10MB log rotation) |

CTA freshness is an explicit contract: `data/menthorq_cache/health/cta-sync-latest.json` is the machine-readable health record; `/api/menthorq/cta` triggers background sync when stale and exposes `cache_meta` + `sync_health`.

Full ops runbooks: [`docs/runbooks/ib-connection-troubleshooting.md`](docs/runbooks/ib-connection-troubleshooting.md), [`docs/runbooks/ib-gateway-docker.md`](docs/runbooks/ib-gateway-docker.md), [`docs/runbooks/ops.md`](docs/runbooks/ops.md).

## Glossary

| Term | Definition |
|---|---|
| **Convexity** | Asymmetric payoff — expected upside materially exceeds downside |
| **CRI** | Crash Risk Index — composite crash-risk and deleveraging model |
| **CTA** | Commodity Trading Advisor — systematic trend-following funds |
| **Dark Pool** | Private off-exchange venue for institutional trading |
| **Edge** | Specific reason the market is mispricing an outcome |
| **Kelly Criterion** | Position-sizing framework scaled to edge and odds |
| **VCG-R** | Volatility-Credit Gap — VIX>28 + VCG>2.5σ divergence triggers risk-off |
