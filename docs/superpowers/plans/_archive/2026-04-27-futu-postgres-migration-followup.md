# Futu → Postgres Migration — Follow-up

**Status:** Deferred. Filed 2026-04-27 after the IB portfolio read-path fix
(`docs/plans/2026-04-27-portfolio-postgres-read-path.md`). User target:
"later this week" (~2026-05-03).

**Context:** PR #52 (postgres-migration) generalized the schema to support
`broker IN ('IB','FUTU')` but only IB writes were ever wired up. Futu
positions still flow through `data/futu_portfolio.json` exclusively.

## Current Futu architecture

```
Futu OpenD (local)
    ↓
xenon-futu-sync subprocess (src/xenon/execution/futu_sync.py)
    ↓ atomic_save
data/futu_portfolio.json
    ↓ readFile
FastAPI /futu/portfolio + /futu/sync (server.py:2241, 2315)
    ↓ xenonFetch
web/lib/useFutuPortfolio.ts → AccountTabBar Futu tab
```

## Why now (cost/benefit when ready)

- **Pro:** unifies the portfolio surface — one `GET /portfolio?broker=...`
  endpoint scoped by `AccountScope` covers both brokers. Removes the last
  data/\*.json on the runtime hot path.
- **Pro:** future "all positions across brokers" queries become trivial
  (single Postgres query, joinable to other tables).
- **Con:** Futu is read-only and refresh-on-demand (10s cooldown, asyncio
  singleflight). No concurrent-writer pressure, so the current JSON-snapshot
  pattern is structurally sufficient.
- **Con:** Futu's data shape lacks Kelly / structure_type / evaluator
  metadata, so it doesn't fit cleanly into the IB-shaped `payload` JSONB.

## Scope checklist

- [ ] Decide payload shape — share `account_snapshots.payload` across IB and
      Futu (with a `source: 'ib'|'futu'` discriminator, already in
      `PortfolioDataSchema`) OR split into a separate `futu_snapshots` table.
- [ ] Update `futu_sync.py` to write Postgres in addition to (or instead of)
      `data/futu_portfolio.json`. Tag rows with `broker='FUTU'`,
      `account_env='live'` (Futu is always live), `broker_account=<futu acct>`.
- [ ] Update `/futu/portfolio` and `/futu/sync` to read from Postgres scoped
      by `Depends(get_account_scope)` with `broker='FUTU'` override (Futu
      sits in its own tab — the broker is fixed by route, not derived from
      app.state).
- [ ] Update `web/lib/useFutuPortfolio.ts` if response shape changes. The
      adapter `web/lib/futuPortfolioAdapter.ts` already normalizes; should
      be insulated from the source change.
- [ ] Tests: end-to-end `/futu/sync` → PG write → `/futu/portfolio` GET
      returns the same shape; cross-broker isolation (Futu sync doesn't
      stamp IB rows and vice versa).
- [ ] Delete `data/futu_portfolio.json` and `FUTU_PORTFOLIO` references in
      `portfolio_adapter.py`.
- [ ] Consider unifying `/portfolio` and `/futu/portfolio` into one
      `/portfolio?broker=IB|FUTU` endpoint — bigger API change, separate
      decision.

## Non-goals

- **No Futu order routing.** CLAUDE.md is explicit: Futu is read-only.
  Don't accidentally enable order writes during the migration.
- **No quote / market-data path through Futu.** IB stays the sole quote
  source.
- **Don't unify portfolio adapters in this PR.** The Web TS adapters
  (`futuPortfolioAdapter.ts`, IB equivalent) handle UI-specific normalization
  and merging them is a separate refactor.

## Closes when

- All checkboxes above ticked
- `data/futu_portfolio.json` and its readers gone
- This file moves to `docs/plans/archive/`
