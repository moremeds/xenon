# Decision: Web-test PG seed via dedicated `uv run` script

**Date:** 2026-05-03
**Status:** Decided — implement when on LAN
**Decider:** chenxi
**Context:** PG migration clean-cutoff plan
(`docs/plans/2026-05-03-pg-migration-clean-cutoff.md`) deletes
`data/portfolio.json` and `data/trade_log.json`. The web Vitest suite
currently seeds those files via `web/tests/setup/seed-fixtures.ts:7-25`,
so removing them breaks `npm test` and the CI `web-tests` job.

## TL;DR

**Add `scripts/dev/seed_test_pg.py`** invoked by Vitest globalSetup. It
writes a known fixture row into `account_snapshots` for the test
account scope, mirroring what the JSON seed does today but in PG.
`fastapiHarness.ts` keeps doing only what it does today (manage uvicorn).

## Problem

Today's seed:

```ts
// web/tests/setup/seed-fixtures.ts:16-23
"data/portfolio.json": {
  bankroll: 100000,
  position_count: 0, defined_risk_count: 0, undefined_risk_count: 0,
  last_sync: "2099-01-01T00:00:00Z",
  positions: [],
},
"data/trade_log.json": { trades: [] },
```

After cutoff:

- `data/portfolio.json` is gone — `formatPortfolio` in `web/app/api/pi/route.ts` reads from FastAPI `GET /portfolio`, which reads from PG `account_snapshots`.
- `data/trade_log.json` is gone — readers migrated to PG queries.

The web tests need an equivalent fixture seed in PG, scoped to the test
trading mode + broker account.

## Options considered

### Option A — Test-only FastAPI endpoint (`POST /test/seed-portfolio`)

Add a route gated on `XENON_API_TEST_MODE=1` that takes a JSON payload
and inserts into `account_snapshots`. Vitest globalSetup hits it via
`fetch`.

**Pro:** Goes through the same FastAPI lifespan that the tests already
use; no new process to manage.

**Con:** Test surface bleeds into production code. Requires guarding the
route with the test-mode flag (precedent exists: `POST /dev/rehydrate/synthetic`
in `src/xenon/api/routes/dev_probes.py`). One more place to remember to
keep gated.

### Option B — Dedicated `uv run` script (chosen)

`scripts/dev/seed_test_pg.py`:

- Reads the test scope (`AccountScope("IB", "paper", "DU0000000")`) and inserts a known row into `xenon.account_snapshots` with the empty-portfolio payload that today's `seed-fixtures.ts` provides
- Idempotent: TRUNCATE + INSERT (or `ON CONFLICT DO UPDATE`)
- Connection: `DATABASE_URL_TEST` from env

Vitest `globalSetup` invokes it once per test session:

```ts
// web/tests/setup/seed-fixtures.ts (after migration)
import { execSync } from "node:child_process";
export default function setup(): void {
  execSync("uv run python scripts/dev/seed_test_pg.py", { stdio: "inherit" });
}
```

**Pro:** Test infrastructure stays in test infrastructure. No production
code changes. Mirrors the existing `uv run` pattern used elsewhere
(`scripts/migrations/*`, `scripts/checks/*`). Single source of truth for
the fixture payload — Python script, not duplicated TS.

**Con:** New process per test session (~1s startup overhead). Negligible
compared to the FastAPI startup the harness already does.

### Option C — In-process bridge (PyO3 / nanobind)

Embed Python in Vitest via FFI to seed PG.

**Rejected** — over-engineered. Adds toolchain complexity for a 1-second
script invocation.

## Decision

**Option B** — `scripts/dev/seed_test_pg.py` invoked by Vitest globalSetup.

## Implementation outline (when LAN access is available)

1. Write `scripts/dev/seed_test_pg.py`:
   - `from xenon.execution.account_scope import AccountScope`
   - `from xenon.db.engine import get_sync_engine`
   - `from xenon.db.schema import account_snapshots`
   - Build payload identical to current JSON shape: `{bankroll: 100000, position_count: 0, ..., positions: []}`
   - TRUNCATE + INSERT one row scoped to `AccountScope("IB", "paper", "DU0000000")` with `bankroll=100000`, `account="DU0000000"`, `payload=<above>`, `snapshot_at=now()`
   - Exit non-zero on connection failure (loud failure better than silent test corruption)
2. Update `web/tests/setup/seed-fixtures.ts`:
   - Replace JSON file writes with `execSync("uv run python scripts/dev/seed_test_pg.py", { stdio: "inherit" })`
   - Drop `mkdirSync`/`writeFileSync` imports — no longer needed
3. Update `.github/workflows/ci.yml::web-tests`:
   - Ensure `DATABASE_URL_TEST` is set in the job env (it already is; verify)
   - Add `uv sync --frozen --extra test` step before `npm test` if not already present
4. Verify: `cd web && npm test` runs green offline against `core_test`

## What this unblocks

The third feasibility tribunal flagged "fastapiHarness has no PG seeding"
as a unanimous BLOCKER. This decision provides the seeding mechanism
without expanding the harness or polluting production code. Plan v3 can
adopt this with no additional design work.
