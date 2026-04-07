# docs/ — Index

Authoritative reference material. Policy rules live in `CLAUDE.md` files in the project tree; this directory holds the detailed specs, catalogs, and runbooks.

## Live state
- `status.md` — decision log and audit trail (NOT a live portfolio view — IB is source of truth)

## Workflows
- `workflows/plans.md` — evaluation milestone workflow
- `workflows/implement.md` — execution runbook
- `workflows/prompt.md` — spec, hard constraints, deliverables

## Architecture
- `architecture/architecture.md` — high-throughput, parallel scanning, atomic state, WS relay, perf page
- `architecture/api-infrastructure.md` — FastAPI server, auth, IB pool, gateway modes, cloud deployment
- `architecture/data-files.md` — `data/` catalog
- `architecture/performance-reconstruction.md` — perf calc methodology

## Trading
- `trading/options-structures.md` + `trading/options-structures.json` — 58-structure catalog, guard decisions, position classification
- `trading/strategies.md` — full strategy specs
- `trading/strategy-garch-convergence.md` — GARCH vol divergence strategy
- `trading/strategy-vcg.md` — Volatility-Credit Gap mathematical spec
- `trading/intraday-interpolation.md` — dark pool intraday interpolation formulas
- `trading/signal-thresholds.md` — P/C, flow side, analyst, discovery, seasonality cutoffs

## Reference
- `reference/brand-identity.md` — brand spec
- `reference/chart-system.md` — chart system reference
- `reference/web-ui-reference.md` — regime/VCG panels, reports, share cards, WS state machine, seasonality
- `reference/menthorq-prompts.md` — QUIN screener prompt presets
- `reference/unusual_whales_api.md` — UW endpoint quick reference
- `reference/unusual_whales_api_spec.yaml` — full OpenAPI spec
- `reference/ib_tws_api.md` — IB TWS API reference

## Runbooks
- `runbooks/ib-connection-troubleshooting.md`
- `runbooks/ib-gateway-docker.md`
- `runbooks/ibc-remote-access.md`
- `runbooks/oauth-subscription-auth.md`
- `runbooks/options-flow-verification.md`
- `runbooks/ops.md` — log rotation

## Subdirectories
- `autoresearch/` — research notes and ideas
- `plans/` — dated implementation plans
- `reference/apex-futu/` — Apex/Futu broker reference
