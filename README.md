# Xenon

<p align="center">
  <img alt="Python 3.13" src="https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white" />
  <img alt="Next.js 16" src="https://img.shields.io/badge/Next.js-16-000000?logo=nextdotjs&logoColor=white" />
  <img alt="Test stack" src="https://img.shields.io/badge/Tests-pytest%20%7C%20Vitest%20%7C%20Playwright-0A7F6F" />
</p>

**Broker terminal for options portfolio management.**

Xenon places orders, tracks fills, surfaces P&L and Greeks attribution, and enforces position-close rules against Interactive Brokers. **Bring your own thesis** — no scanners, no signal generation, no strategy recommendations.

---

## Brokers

- **Interactive Brokers** (primary) — quotes, chains, execution, portfolio. Never bypassed.
- **Futu OpenD** (read-only) — positions snapshot from a local Futu OpenD instance. Surfaces as a separate account tab in the terminal. No orders, no fills, no quotes. Silent degrade when OpenD is unreachable.

Every execution and portfolio row carries `broker`, `account_env`, and `broker_account` columns so paper and live data never blend in a shared Postgres. Scope resolves via `AccountScope` (`src/xenon/execution/account_scope.py`); FastAPI depends on `get_account_scope`; sync subprocesses read `XENON_TRADING_MODE` + `XENON_BROKER_ACCOUNT`.

## ⛔ Naked-Short Guard

The only hard-enforced trading rule. Every short call must be covered by long shares (1 contract = 100 shares) or by long calls at the same expiry. Cash-secured puts are allowed. Combos that net to uncovered short calls are blocked at three layers:

1. **UI pre-submission** — `web/lib/nakedShortGuard.ts`
2. **API gate** — `POST /api/orders/place` returns 403 on violation
3. **Post-sync audit** — `xenon-naked-short-audit` cancels violators after every `ib_sync`

Full enforcement matrix: [`src/xenon/CLAUDE.md`](src/xenon/CLAUDE.md) § Naked Short Protection.

## Quick Start

**Prerequisites**

- Python `3.13` via [`uv`](https://docs.astral.sh/uv/) (3.14 has an `ib_async` / `eventkit` incompatibility)
- Node.js `18+`
- [Interactive Brokers](https://ibkr.com/referral/joseph5632) Gateway running for the target mode (paper on `127.0.0.1:4002`, live typically on a remote `:4001`)
- Postgres reachable on the configured `DATABASE_URL`
- (Optional) Futu OpenD — read-only positions for a Futu account tab

```bash
git clone https://github.com/moremeds/xenon.git
cd xenon
uv sync --extra test
cd web && npm install && cd ..

# Paper (local IB Gateway on :4002)
scripts/infra/dev.sh paper

# Live (remote IB Gateway, host from .env)
scripts/infra/dev.sh live
```

Terminal at `http://localhost:3000`. Health: `curl http://localhost:8321/health`.

`dev.sh` does **not** edit `.env` and does **not** start IB Gateway — that step stays manual. Append `--no-auth` to bypass Clerk for the local session.

## Environment

**`.env`** (root) — Python services, database, auth, R2:

```bash
XENON_TRADING_MODE=paper                  # paper (port 4002) or live (port 4001)
XENON_PAPER_ACCOUNT=DU0000000             # DU* prefix required for paper
# XENON_LIVE_ACCOUNT=U1234567             # U* prefix required for live

DATABASE_URL=postgresql+asyncpg://xenon_app:xenon_dev@<lan-host>:5432/core_dev
DATABASE_URL_TEST=postgresql+asyncpg://xenon_app:xenon_dev@<lan-host>:5432/core_test

# Auth (Clerk JWT validation in FastAPI)
CLERK_JWKS_URL=...
CLERK_ISSUER=...
ALLOWED_USER_IDS=user_...

# Object storage (snapshots, exports)
R2_ENDPOINT=...
R2_BUCKET=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...

# Optional — unlocks the Historical Trades panel (IB Flex Query audit overlay)
IB_FLEX_TOKEN=...
IB_FLEX_QUERY_ID=...
```

**`web/.env`** — Next.js terminal:

```bash
ANTHROPIC_API_KEY=...                     # portfolio Q&A chat
UW_TOKEN=...                              # portfolio-perf / portfolio-report CLIs only
EXA_API_KEY=...                           # optional ticker-page enrichment
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_...
CLERK_SECRET_KEY=sk_...
```

When `IB_FLEX_TOKEN` / `IB_FLEX_QUERY_ID` are unset, the Historical Trades panel renders an empty state with a setup hint and `xenon-blotter-history` exits gracefully with `configured=false`.

## Xenon Terminal

Real-time trading terminal built with **Next.js 16**. Streams IB prices, computes live greeks, visualizes portfolio exposures, and drives order management.

**Core capabilities**

- Real-time price streaming with live greeks (shared BID / MID / ASK / SPREAD layout across ticker, chain, and modify views)
- Multi-leg position monitoring with per-leg P&L and structure classification (verticals, straddles, synthetics, covered calls, risk reversals, butterflies / condors)
- Multi-broker account tab bar: switch between IB (live trading) and Futu (read-only positions snapshot)
- YTD portfolio performance with ET-session refresh guarding against stale snapshots
- Combo spread order workflows with natural-market pricing
- Historical Trades panel (IB Flex Query audit overlay — optional)
- AI chat for portfolio Q&A and command execution

Authentication via [Clerk](https://clerk.com) — Next.js middleware protects routes, FastAPI validates JWTs, WebSocket uses 30s single-use tickets. Local dev bypasses auth when `CLERK_JWKS_URL` is unset.

Component-level reference: [`docs/reference/web-ui-reference.md`](docs/reference/web-ui-reference.md).

## CLI Commands

All entry points are installed by `uv sync` and invoked as `uv run <name> [args]`.

| Group                | Commands                                                                                                            |
| -------------------- | ------------------------------------------------------------------------------------------------------------------- |
| **Server**           | `xenon-api` (FastAPI bridge)                                                                                        |
| **Orders**           | `xenon-ib-place-order` · `xenon-ib-order-manage` · `xenon-ib-orders` · `xenon-ib-execute` · `xenon-ib-option-chain` |
| **Sync / reconcile** | `xenon-ib-sync` · `xenon-ib-reconcile` · `xenon-futu-sync`                                                          |
| **Risk / audit**     | `xenon-naked-short-audit`                                                                                           |
| **Reports**          | `xenon-portfolio-report` · `xenon-portfolio-perf` · `xenon-portfolio-attrib` · `xenon-perf-explainer`               |
| **Trade log**        | `xenon-blotter` · `xenon-blotter-history`                                                                           |
| **Daemons**          | `xenon-monitor-daemon` · `xenon-preset-rebalance`                                                                   |
| **Utilities**        | `xenon-market-hours` · `xenon-presets`                                                                              |

Full descriptions: [`src/xenon/CLAUDE.md`](src/xenon/CLAUDE.md).

## Architecture

```text
IB Gateway  ─┐
             ├─►  FastAPI bridge  ─►  Postgres  ─►  Next.js terminal  ─►  User orders
Futu OpenD  ─┘   (xenon.api.server)   (snapshots,        (web/)              │
   (read-only)                          orders,                              │
                                        fills,                               ▼
                                        journal)                       IB Gateway
                                              │
                                              ▼
                                       Monitor daemon
                                     (fills mirror, exit
                                      orders, naked-short
                                      audit, log rotation)
```

- `src/xenon/` — Python services (FastAPI, IB / Futu clients, execution, reports, monitor daemon)
- `web/` — Next.js terminal (portfolio, orders, AI chat)
- `site/` — standalone marketing site (separate deployment)
- `brand/` — design system and tokens
- `data/` — runtime artifacts (gitignored)
- `docs/` — architecture, runbooks, references ([index](docs/README.md))

Postgres is the runtime source of truth for portfolio, orders, fills, and journal surfaces. The terminal does **not** silently fall back to JSON files on runtime read paths — that boundary is enforced by CI (`scripts/checks/no_json_fallback_on_order_path.py`) plus a caller allowlist for `xenon.execution.ib_place_order` (`scripts/checks/order_path_caller_allowlist.py`).

Deeper reading:

- High-throughput design and WS relay: [`docs/architecture/architecture.md`](docs/architecture/architecture.md)
- FastAPI, auth, IB Gateway modes: [`docs/architecture/api-infrastructure.md`](docs/architecture/api-infrastructure.md)
- Postgres as source of truth: [`docs/architecture/production-database-strategy.md`](docs/architecture/production-database-strategy.md)
- Full order lifecycle (HTML + Markdown): [`docs/architecture/order-stack-end-to-end.md`](docs/architecture/order-stack-end-to-end.md)
- Order-path incident history: [`docs/reference/order-path-incident-history.md`](docs/reference/order-path-incident-history.md)

## Data Sources

| Source                                                                                        | Role                                                                                               |
| --------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| **[Interactive Brokers](https://ibkr.com/referral/joseph5632)**                               | Real-time quotes, chains, portfolio, execution, fills (primary — never bypassed)                   |
| **Futu OpenD**                                                                                | Read-only positions snapshot for the Futu account tab                                              |
| **[Unusual Whales](https://unusualwhales.com/referral#39985a64-656c-4642-a051-db89f6324d64)** | Historic OHLC + option contract history for `xenon-portfolio-perf` / `xenon-portfolio-report` only |
| **IB Flex Query**                                                                             | Optional historical fills audit overlay (`xenon-blotter-history`)                                  |

**Never Yahoo Finance.**

## Testing

- **Python** — `pytest` (execution, reports, adapters, monitor daemon, FastAPI routes)
- **Frontend** — Vitest (web logic, order-path harness)
- **E2E** — Playwright (browser workflows)

```bash
uv sync --extra test                                              # one-time / after dep changes
uv run python scripts/infra/dev/run_pytest_affected.py            # scoped — only affected tests (preferred)
uv run pytest                                                     # full suite
cd web && npm test                                                # Vitest
cd web && npx playwright test                                     # E2E
```

Order-route integration tests use `web/tests/fastapiHarness.ts` with `XENON_API_TEST_MODE` to stub broker calls — no live IB required.

## Services

| Service                                 | Purpose                                                                              |
| --------------------------------------- | ------------------------------------------------------------------------------------ |
| IB Gateway (cloud / Docker / launchd)   | Broker session for quotes, execution, reports                                        |
| Monitor daemon (`xenon-monitor-daemon`) | Fills mirror, exit orders, naked-short audit, Flex token checks (10 MB log rotation) |

Ops runbooks: [`ib-connection-troubleshooting`](docs/runbooks/ib-connection-troubleshooting.md) · [`ib-gateway-docker`](docs/runbooks/ib-gateway-docker.md) · [`mac-mini`](docs/runbooks/mac-mini.md) · [`ops`](docs/runbooks/ops.md) · [`release`](docs/runbooks/release.md).

## Release

`VERSION` (root) is the source of truth and is kept in parity with `package.json` by CI (`scripts/release/version_sync_check.py`). To cut a release:

```bash
./scripts/release/cut.sh                # interactive: patch / minor / major / custom
git push origin master --follow-tags    # release.yml fires on the tag
```

Changelog: [`CHANGELOG.md`](CHANGELOG.md).
