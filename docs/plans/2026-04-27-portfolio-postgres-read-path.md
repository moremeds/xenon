# Portfolio Postgres Read Path — Implementation Tracker

**Branch:** `fix/portfolio-postgres-read-path`
**Status:** In progress (2026-04-27)
**Goal:** Restore the IB portfolio read path after the postgres-migration left
the UI reading a stale `data/portfolio.json`.

## Architecture (Phase 1)

```
ib_sync subprocess
    ↓ writes
xenon.account_snapshots.payload (jsonb)
    + xenon.positions / nav_history (existing flat columns)
    ↓ scoped read by AccountScope
GET /portfolio  (new FastAPI endpoint, Depends(get_account_scope))
    ↓ xenonFetch
web/app/api/portfolio/route.ts  (no longer reads JSON file)
    ↓
UI portfolio tab → renders live positions + correct NAV
```

## Tasks

### 1. Schema — add jsonb payload

- [ ] Add `payload jsonb NOT NULL DEFAULT '{}'::jsonb` column to
      `xenon.account_snapshots` in `src/xenon/db/schema.py`.
- [ ] Generate Alembic migration:
      `uv run alembic revision -m "add_account_snapshots_payload"`.
- [ ] Verify migration upgrade + downgrade locally.

### 2. Writer — stamp structured payload at sync

- [ ] Update `_save_portfolio_to_postgres()` in `src/xenon/execution/ib_sync.py`
      to write the full portfolio dict to `account_snapshots.payload`.
- [ ] Existing flat columns (`bankroll`, `peak_value`, `net_liquidation`)
      remain populated for backwards compat — they're cheap.

### 3. Query — read latest payload by scope

- [ ] Add `get_latest_portfolio_payload(conn, *, broker, account_env, broker_account)`
      to `src/xenon/db/queries/portfolio.py`. Returns `dict | None`.
- [ ] Unit test: Phase 1 isolation — paper rows must not leak into live
      response when scope is live, and vice versa.

### 4. Endpoint — `GET /portfolio`

- [ ] Add `GET /portfolio` to `src/xenon/api/server.py` with
      `Depends(get_account_scope)`.
- [ ] Returns `payload` (matches `web/lib/portfolioDataSchema.ts`) or 404
      when no snapshot exists.
- [ ] Route test (TestClient + lifespan): seed snapshot, verify response
      shape and scope filter.

### 5. Refactor `/portfolio/sync`

- [ ] After `xenon-ib-sync` subprocess succeeds, return the result of
      `GET /portfolio` (i.e. read fresh payload from PG, not the JSON file).
- [ ] Remove `verified_load(DATA_DIR / "portfolio.json")` call.

### 6. Web — drop file read

- [ ] `web/app/api/portfolio/route.ts`: remove `readFile(PORTFOLIO_PATH)`,
      call FastAPI `GET /portfolio` via `xenonFetch`.
- [ ] Staleness check uses `last_sync` from response, not file mtime.
- [ ] Background sync trigger still calls FastAPI `/portfolio/background-sync`.
- [ ] Vitest contract test: mock FastAPI response, assert UI route returns
      the same shape.

### 7. Verification

- [ ] `uv run pytest scripts/tests/test_portfolio_query.py
    src/xenon/api/tests/test_portfolio_endpoint.py` green.
- [ ] `cd web && npm test -- portfolio` green.
- [ ] `cd web && npm run typecheck` green.
- [ ] Manual: `dev.sh live` → hard-refresh browser → 11 positions visible,
      live NAV correct.
- [ ] `psql ... -c "SELECT payload->>'last_sync' FROM xenon.account_snapshots
    WHERE account_env='live' ORDER BY snapshot_at DESC LIMIT 1"` returns
      a fresh ISO timestamp.

### 8. Stop-leave-Phase-2 boundary

- [ ] Do **not** delete `data/portfolio.json` or rip out the 8 Python
      readers (analyst_ratings, scanner, ib_reconcile, naked_short_audit,
      preflight, incremental_sync, portfolio_adapter, ib_sync's own
      previous-snapshot read). Phase 2 covers them.
- [ ] Do **not** touch Futu paths. Followup tracked at
      `docs/plans/2026-04-27-futu-postgres-migration-followup.md`.

## Closes when

- All Tasks 1–7 ticked
- PR merged to master
- File moves to `docs/plans/archive/`
