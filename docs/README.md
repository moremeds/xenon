# docs/ — Index

Authoritative reference material. Policy rules live in `CLAUDE.md` files in the project tree; this directory holds the detailed specs, catalogs, and runbooks.

## Live state
- `status.md` — decision log and audit trail (NOT a live portfolio view — IB is source of truth)

## Workflow / planning
- `plans.md` — evaluation milestone workflow
- `implement.md` — execution runbook
- `prompt.md` — spec, hard constraints, deliverables

## Architecture & infrastructure
- `architecture.md` — high-throughput, parallel scanning, atomic state, WS relay, perf page
- `api-infrastructure.md` — FastAPI server, auth, IB pool, gateway modes, cloud deployment
- `web-ui-reference.md` — regime/VCG panels, reports, share cards, WS state machine, seasonality
- `ops.md` — log rotation

## Trading references
- `options-structures.md` + `options-structures.json` — 58-structure catalog, guard decisions, position classification
- `strategies.md` — full strategy specs
- `strategy-garch-convergence.md` — GARCH vol divergence strategy
- `strategy-vcg.md` — Volatility-Credit Gap mathematical spec
- `intraday-interpolation.md` — dark pool intraday interpolation formulas
- `signal-thresholds.md` — P/C, flow side, analyst, discovery, seasonality cutoffs
- `performance-reconstruction.md` — perf calc methodology
- `data-files.md` — `data/` catalog
- `menthorq-prompts.md` — QUIN screener prompt presets

## API specs
- `unusual_whales_api.md` — UW endpoint quick reference
- `unusual_whales_api_spec.yaml` — full OpenAPI spec
- `ib_tws_api.md` — IB TWS API reference

## Ops runbooks
- `ib-connection-troubleshooting.md`
- `ib-gateway-docker.md`
- `ibc-remote-access.md`
- `oauth-subscription-auth.md`
- `options-flow-verification.md`

## Brand + UI
- `brand-identity.md` — brand spec
- `chart-system.md` — chart system reference

## Subdirectories
- `autoresearch/` — research notes and ideas
- `reference/apex-futu/` — Apex/Futu broker reference
