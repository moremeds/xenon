# Xenon

<p align="center">
  <img alt="Python 3.13" src="https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white" />
  <img alt="Next.js 16" src="https://img.shields.io/badge/Next.js-16-000000?logo=nextdotjs&logoColor=white" />
  <img alt="Test stack" src="https://img.shields.io/badge/Tests-pytest%20%7C%20Vitest%20%7C%20Playwright-0A7F6F" />
</p>

**Broker terminal for options portfolio management.**

Xenon places orders, tracks fills, surfaces P&L and Greeks attribution, and enforces the naked-short guard against Interactive Brokers. **Bring your own thesis** — no scanners, no signal generation, no strategy recommendations.

---

## Brokers

- **Interactive Brokers** (primary) — quotes, chains, execution, portfolio. Never bypassed.
- **Futu OpenD** (read-only) — positions snapshot from a local Futu OpenD instance. Surfaces as a separate account tab in the terminal. No order execution and no quote feed; it reads positions and historical deals (`xenon-futu-history-sync`). Silent degrade when OpenD is unreachable.

Every execution and portfolio row carries `broker`, `account_env`, and `broker_account` columns so paper and live data never blend in a shared Postgres. Scope resolves via `AccountScope` (`src/xenon/execution/account_scope.py`); FastAPI depends on `get_account_scope`; sync subprocesses read `XENON_TRADING_MODE` + `XENON_BROKER_ACCOUNT`.

## ⛔ Naked-Short Guard

The only hard-enforced trading rule. Every short call must be covered by long shares (1 contract = 100 shares) or by long calls at the same expiry. Cash-secured puts are allowed. Combos that net to uncovered short calls are blocked at three layers:

1. **UI pre-submission** — `web/lib/nakedShortGuard.ts`
2. **API gate** — `POST /api/orders/place` returns 403 on violation
3. **Post-sync audit** — runs after every `ib_sync` (`naked_short_audit.py`). Shadow mode by default: it logs and records `service_health`, nothing is cancelled. With `XENON_AUDIT_CANCEL=1` on the API, it cancels confirmed violators that the order owner placed. The `xenon-naked-short-audit` CLI is an offline check and never cancels.

Full enforcement matrix: [`src/xenon/CLAUDE.md`](src/xenon/CLAUDE.md) § Naked Short Protection.

## Quick Start

**Prerequisites**

- Python `3.13` via [`uv`](https://docs.astral.sh/uv/) (3.14 has an `ib_async` / `eventkit` incompatibility)
- Node.js `22` (what CI and the Docker images use; `web/package.json` has no `engines` pin)
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

Dev stack ports: terminal `http://localhost:3200`, FastAPI `8421` (`curl http://localhost:8421/health`), realtime WS `8866`. Production runs as a Docker stack on the Mac mini and publishes `3000` / `8321` / `8765` on 127.0.0.1 (see Release and deploy below).

`dev.sh` does **not** edit `.env` and does **not** start IB Gateway — that step stays manual. It refuses to start without `XENON_INTERNAL_API_TOKEN` (env or `.env`) and takes only the mode (`paper` or `live`) as an argument.

## Environment

**`.env`** (root) — Python services, database, auth:

```bash
XENON_TRADING_MODE=paper                  # paper (port 4002) or live (port 4001)
XENON_PAPER_ACCOUNT=DU0000000             # DU* prefix required for paper
# XENON_LIVE_ACCOUNT=U1234567             # U* prefix required for live

# dev.sh refuses to start when DATABASE_URL points at core_dev (prod)
DATABASE_URL=postgresql+asyncpg://xenon_app:<app-pw>@<db-host>:5432/core_test
# pytest only: a separate, existing database whose name contains "test" (not core_dev,
# not the same database as DATABASE_URL), e.g. xenon_test. pytest refuses to start otherwise
# (src/xenon/_test_db.py); per-worker clones are made from it.
DATABASE_URL_TEST=postgresql+asyncpg://xenon_app:<app-pw>@<db-host>:5432/xenon_test

# Next -> FastAPI shared secret (required; FastAPI fails closed without a credential)
XENON_INTERNAL_API_TOKEN=...
# Optional read-only machine keys (X-API-Key)
# XENON_QUERY_API_KEY=...
# MDW_API_KEY=...

# Optional — unlocks the Historical Trades panel (IB Flex Query audit overlay)
IB_FLEX_TOKEN=...
IB_FLEX_QUERY_ID=...
```

`.env.example` lists every variable with comments.

**`web/.env`** — Next.js terminal (`api/server.py` loads it too in dev):

```bash
ANTHROPIC_API_KEY=...                     # portfolio Q&A chat
UW_TOKEN=...                              # Unusual Whales: ratings, ticker info/news/seasonality, portfolio report
EXA_API_KEY=...                           # optional ticker-page enrichment
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

Access control: production is tailnet-only and there is no user login. `web/middleware.ts` guards `/api/**` against cross-site browser requests (Host allowlist, fetch-metadata and Origin checks). FastAPI is fail-closed and credential-only: Next sends `X-Internal-Token` (`XENON_INTERNAL_API_TOKEN`); machine clients use path-scoped `X-API-Key` keys; `/health` is the only exempt path. The realtime WebSocket uses 30s single-use tickets. Details: [`src/xenon/api/CLAUDE.md`](src/xenon/api/CLAUDE.md) § Auth.

Component-level reference: [`docs/reference/web-ui-reference.md`](docs/reference/web-ui-reference.md).

## CLI Commands

All entry points are installed by `uv sync` and invoked as `uv run <name> [args]`.

| Group                | Commands                                                                                                                                      |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| **Server**           | `xenon-api` (FastAPI dev launcher)                                                                                                            |
| **IB reads**         | `xenon-ib-orders` · `xenon-ib-option-chain` · `xenon-ib-market-depth` · `xenon-ib-option-greeks`                                              |
| **Sync / reconcile** | `xenon-ib-sync` · `xenon-ib-reconcile` · `xenon-futu-sync` · `xenon-futu-history-sync` · `xenon-futu-statement-sync` · `xenon-nav-flex-refresh` · `xenon-nav-reconcile` |
| **Risk / audit**     | `xenon-naked-short-audit` (offline, never cancels)                                                                                            |
| **Reports**          | `xenon-portfolio-report` · `xenon-portfolio-attrib`                                                                                           |
| **Trade log**        | `xenon-blotter` · `xenon-blotter-history`                                                                                                     |
| **Utilities**        | `xenon-market-hours`                                                                                                                          |

Orders are not placed from a CLI: place, cancel and modify run inside the API on one order owner connection.

Full descriptions: [`src/xenon/CLAUDE.md`](src/xenon/CLAUDE.md).

## Architecture

```text
IB Gateway  ─┐
             ├─►  FastAPI bridge  ─►  Postgres  ─►  Next.js terminal  ─►  User orders
Futu OpenD  ─┘   (xenon.api.server)   (snapshots,        (web/)              │
   (read-only)                          orders,                              │
                                        fills,                               ▼
                                        journal)                       IB Gateway
```

- `src/xenon/` — Python services (FastAPI, IB / Futu clients, execution, reports). Background work (IB activity mirror, Wizard Risk Alert, boot reconcile) runs inside the FastAPI lifespan.
- `web/` — Next.js terminal (portfolio, orders, AI chat)
- `brand/` — design system and tokens
- `data/` — runtime artifacts (gitignored)
- `docs/` — architecture, runbooks, references ([index](docs/README.md))

Postgres is the runtime source of truth for portfolio, orders, fills, and journal surfaces. The terminal does **not** silently fall back to JSON files on runtime read paths — that boundary is enforced by CI (`scripts/checks/no_json_fallback_on_order_path.py`) plus a caller allowlist for `xenon.execution.ib_place_order` / `ib_order_manage` (`scripts/checks/order_path_caller_allowlist.py`).

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
| **[Unusual Whales](https://unusualwhales.com/referral#39985a64-656c-4642-a051-db89f6324d64)** | Dark-pool flow for `xenon-portfolio-report`; analyst ratings (`GET /ticker/ratings`); ticker info, news, seasonality and previous-close routes |
| **IB Flex Query**                                                                             | Optional historical fills audit overlay (`xenon-blotter-history`)                                  |

**Never Yahoo Finance.**

## Testing

- **Python** — `pytest` (execution, reports, adapters, FastAPI routes)
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
| FastAPI lifespan tasks                  | IB activity mirror, Wizard Risk Alert; Flex token expiry on `GET /health`            |

Ops runbooks: [`ib-connection-troubleshooting`](docs/runbooks/ib-connection-troubleshooting.md) · [`ib-gateway-docker`](docs/runbooks/ib-gateway-docker.md) · [`remote-deploy`](docs/runbooks/remote-deploy.md) · [`ops`](docs/runbooks/ops.md) · [`release`](docs/runbooks/release.md).

## Release and deploy

`VERSION` (root) is the source of truth and is kept in parity with `package.json` by CI (`scripts/release/version_sync_check.py`). Releases go through a PR; never push `master`. The tag is pushed alone and `release.yml` builds the images:

```bash
./scripts/release/cut.sh                # interactive: patch / minor / major / custom
# then: move the commit to release/vX.Y.Z, open and merge the PR,
#       pause Watchtower on the mini and confirm it is stopped,
#       tag the merge commit, git push origin refs/tags/vX.Y.Z   (steps: docs/runbooks/release.md)
```

Production is a Docker stack on the Mac mini (`deploy/compose.prod.yml`, images from GHCR). A merge alone never deploys; the tag push does. Cutover steps: [`docs/runbooks/remote-deploy.md`](docs/runbooks/remote-deploy.md) § Standard release flow. Deploy truth and scheduled jobs: [`deploy/README.md`](deploy/README.md).

Changelog: [`CHANGELOG.md`](CHANGELOG.md).
