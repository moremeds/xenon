# PG Migration Completion + Remote Deploy (umbrella)

**Date:** 2026-05-03
**Status:** Architectural overview — execution lives in sub-plans below
**Owner:** chenxi

**This is the umbrella / index doc.** Bite-sized executable plans:

- ✅ **Plan 1 — PG migration clean cutoff** → `docs/plans/2026-05-03-pg-migration-clean-cutoff.md` (kills every PG-canonical JSON artifact; reader+writer migration + file deletion + guard tightening per artifact). Replaces what this doc previously called Phase 0+2+3.
- ⏳ **Plan 2 — Container build artifact** → TBD (`docs/plans/<date>-container-build.md`). Dockerfiles + compose + GHCR workflow.
- ⏳ **Plan 3 — Production cutover** → TBD (`docs/plans/<date>-production-cutover.md`). Mac mini provisioning + paper burn-in + DB/IB-Gateway flip.

The architectural decisions, scope boundaries, and clean-slate framing in this doc still apply. Refer to it for the **why**; refer to the sub-plans for the **how**.
**Related:**

- Memory: `project_postgres_migration_read_side_gap.md` (will be updated by Phase 0.3)
- Backlog: `docs/todo-backlog.md` Inbox entries 2026-05-03 (deployment decision, xenon_db teardown)
- Prior plan: `docs/plans/2026-04-28-postgres-migration-completion.md` (W1-W5 — completed the runtime web/API surfaces)
- Architecture: `docs/architecture/order-stack-end-to-end.md`

## Why now

**Clean-slate framing:** there is no live production stack to protect. The Mac mini infrastructure is staged but not yet declared production. This plan defines what "production starts" means: a containerized, stateless artifact reading purely from Postgres. All migration debt drains BEFORE production begins, so the start point itself is clean.

Two converging realities:

1. **Production runtime is being established on remote Mac mini (192.168.50.47) for the first time.** This Mac is now the dev workspace. The dev launcher (`scripts/infra/dev.sh`) handles per-mode IB Gateway routing, but no production stack exists on the Mac mini yet — we're building the start point, not migrating an existing one.
2. **The PG migration completed its scoped Phase 1+2 (runtime web/API), but deferred the CLI/audit tier as low priority.** Today's audit found 8 callers (vs the 4-5 the existing memory entry tracks) still reading `data/portfolio.json`, `trade_log.json`, `blotter.json`, `nav_history.jsonl`. With "production" being declared fresh, these read paths can be drained in the same window as the container build — no need to stagger over weeks to protect anything.

**Goal:** same image runs anywhere (Mac mini today, Linux failover tomorrow), with a `.env` per host as the only environment-specific input. Phase 3 (stateless) lands BEFORE production declaration, not after. The start point of production is a clean slate.

## Architectural decisions (locked in)

- **Deploy artifact:** Docker images (FastAPI + Next.js + ib_realtime relay + alembic migrator), pushed to GHCR on tag.
- **Orchestration:** `docker-compose.yml` at repo root, same file every host.
- **State:** Bind-mount `/opt/xenon/data` to `/app/data` initially. Drains as Phase 2 completes. Eventually replaced with targeted mounts for `locks/`, `service_health/`, `futu_portfolio.json`.
- **Postgres:** External, already on remote at `192.168.50.47:5432`. Per-environment via DB name (`core` prod, `core_dev` dev, `core_test` CI). Same cluster.
- **IB Gateway:** Stays host-native (Java GUI, not containerizable). Containers dial out to `host.docker.internal:4001` on Mac, `--network=host` or fixed bridge IP on Linux.
- **Migrations:** `alembic upgrade head` runs as a separate one-shot `migrator` container, manually invoked (`docker compose run --rm migrator`). NOT auto-run on app container start — protects against surprise schema changes at market open.
- **Image registry:** GHCR (private), built+pushed by `release.yml` on tag push.

## Out of scope

- **Futu PG migration** — deferred per direction; rewrite after production starts using Futu's friendlier snapshot API. Schema's `CHECK (broker IN ('IB','FUTU'))` keeps the door open. Tracker: `docs/plans/2026-04-27-futu-postgres-migration-followup.md`.
- **Linux failover host** — design supports it via the same image + `.env`, but no provisioning planned in this scope.
- **PgBouncer** — not needed today; revisit if connection contention surfaces after containers add their pool to the cluster.

## Phase 0 — Pre-deploy hygiene

**Goal:** `data/` at its real ~10MB footprint; memory entry reflects today's truth.

| #   | Action                                                                                                                                                                                             | File(s)                 | Risk |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------- | ---- |
| 0.1 | Verify `data/uw_analyze_history/`, `data/ta.duckdb`, `data/orders.duckdb`, `data/*.bak` have zero live readers (grep audit + run `python scripts/infra/dev/run_pytest_affected.py`)                | `data/`                 | Low  |
| 0.2 | Delete the 4 dead artifacts (~535MB freed)                                                                                                                                                         | `data/`                 | Low  |
| 0.3 | Update `project_postgres_migration_read_side_gap.md` memory entry — full 8-caller inventory, reframed priority ("blocks Phase 3 stateless deploy")                                                 | `~/.claude/.../memory/` | None |
| 0.4 | Tighten `test_portfolio_json_not_read.py` regex to also catch `Path(...)/"portfolio.json"` construction pattern (current regex misses it — that's why `naked_short_audit.py` etc. slipped through) | `scripts/tests/`        | Low  |
| 0.5 | Single commit: `chore: cull post-PG-migration dead artifacts + tighten guard`                                                                                                                      | —                       | Low  |

**Effort:** 1-2 hours. **Exit:** `du -sh data/ < 15M`, all tests green, memory accurate, guard catches the slipped pattern.

## Phase 1 — Containerize for remote deploy

**Goal:** Same image runs on Mac mini today, Linux box tomorrow. Bind-mount handles remaining JSON without code rewrites.

| #    | Action                                                                                                                                                                                                                                                                    | Effort                     |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------- |
| 1.1  | `docker/api.Dockerfile` — `python:3.13-slim` base, `uv sync --frozen --no-dev`, entrypoint `uvicorn xenon.api.server:app --host 0.0.0.0 --port 8321`                                                                                                                      | 1 hr                       |
| 1.2  | `docker/web.Dockerfile` — `node:22-alpine` base, multi-stage (build → runner), `next build` with `output: 'standalone'`, runtime image is just `.next/standalone` + `public` + `.next/static`                                                                             | 1 hr                       |
| 1.3  | `docker/realtime.Dockerfile` — `node:22-alpine` base, just the `ib_realtime_server.js` and its deps                                                                                                                                                                       | 30 min                     |
| 1.4  | `docker/migrator.Dockerfile` — `python:3.13-slim` + `uv sync` for alembic only, entrypoint `uv run alembic upgrade head`                                                                                                                                                  | 30 min                     |
| 1.5  | `docker-compose.yml` at repo root — 4 services, bind-mounts (`/opt/xenon/data:/app/data`, `/opt/xenon/.env:/app/.env:ro`), `restart: unless-stopped`, depends_on chain (migrator → api → web/realtime), healthchecks on api + web                                         | 1.5 hrs                    |
| 1.6  | GHCR push job in `.github/workflows/release.yml` — fires on tag, builds 4 images, tags `:X.Y.Z` (no `v` prefix; see remote-deploy.md "Tag convention") + `:latest`, pushes to `ghcr.io/<owner>/xenon-{api,web,realtime,migrator}`                                         | 1 hr                       |
| 1.7  | Provision Mac mini: install Docker Desktop or Colima, create `/opt/xenon/{data,logs}`, place `.env` (with `DATABASE_URL=...core`, `IB_GATEWAY_HOST=127.0.0.1`, `IB_GATEWAY_PORT=4001`), `docker login ghcr.io`                                                            | 30 min                     |
| 1.8  | First end-to-end smoke test on Mac mini in **PAPER mode** — `docker compose run --rm migrator` against `core_dev` first, then `docker compose up -d`, verify `curl http://<mini>:8321/health` returns `ib_gateway.port_listening: true`, `/portfolio` returns scoped data | 1 hr                       |
| 1.9  | `docs/runbooks/remote-deploy.md` — pull/start/stop/logs/rollback runbook                                                                                                                                                                                                  | 30 min                     |
| 1.10 | **24h paper burn-in** on Mac mini — full container stack runs paper trading end-to-end, validates Phase 2 migrations + container restart behavior + IB Gateway connection stability                                                                                       | (elapsed time, not effort) |
| 1.11 | **Production declaration** — switch container `.env` `DATABASE_URL` from `core_dev` to `core`, switch IB Gateway from paper:4002 to live:4001, restart compose. This is the start point of production.                                                                    | 30 min                     |

**Effort:** ~7 hrs of focused work over 1-2 days, plus 24h burn-in elapsed time.

**Exit:** Container-based prod on remote, this Mac no longer runs production processes, rollback is `docker compose pull <prev-tag> && docker compose up -d`.

**Risk:** Low — clean-slate context means no live trading to break. First containerization gets validated end-to-end on paper before any production declaration. Keep `scripts/infra/dev.sh` workable as escape hatch on the Mac mini; tag rollback target in git before each compose change.

## Phase 2 — Drain JSON readers (clean-slate batch)

**Goal:** Migrate every JSON reader to PG before production declaration. **No live trading to protect** — sequencing is by code dependency only, not by safety burn-in. PRs can land in batches, paper-validation happens at the end against the full container stack.

| #   | Reader                                                                     | Replaced with                                                               | Stakes             | Effort        | Paper-test? |
| --- | -------------------------------------------------------------------------- | --------------------------------------------------------------------------- | ------------------ | ------------- | ----------- |
| 2.1 | `naked_short_audit.py`                                                     | `xenon.account_snapshots` (latest scoped payload)                           | Gate 4 enforcement | 0.5 day       |
| 2.2 | `preflight.py:119`                                                         | `xenon.account_snapshots`                                                   | Order-block guard  | 0.25 day      |
| 2.3 | `ib_sync.py` entry_date join (3-way: trade_log + blotter + prev portfolio) | PG join across `order_submissions` + `trades` + `account_snapshots`, scoped | Sync metadata      | 1 day         |
| 2.4 | `monitor_daemon/handlers/exit_orders.py` + `fill_monitor.py`               | `xenon.fills`                                                               | Exit lifecycle     | 0.5 day       |
| 2.5 | `portfolio_adapter.py`                                                     | `xenon.account_snapshots`                                                   | Read-only adapter  | 0.25 day      |
| 2.6 | `portfolio_performance.py:309`                                             | `nav_history` table                                                         | Analytics          | 0.25 day      |
| 2.7 | `portfolio_report.py`, `leap_iv.py`                                        | `xenon.account_snapshots`                                                   | CLI audit          | 0.25 day each |
| 2.8 | `ib_sync.py:992` prev-portfolio fallback                                   | `xenon.account_snapshots`                                                   | Final cleanup      | 0.25 day      |

**Per-PR pattern (every step):**

1. Add new query to `src/xenon/db/queries/portfolio.py` (or relevant module) returning the same shape as the JSON reader.
2. Replace JSON read with the query call. **No JSON fallback** — clean-slate means the migration is the only code path; if it fails we fix forward.
3. Add unit test asserting the migrated reader works against a seeded PG (use existing PG harness).
4. End-to-end paper validation deferred to Phase 1.10 (24h burn-in) — by then the full stack is containerized and we test the whole system at once, not each PR in isolation.
5. Add a row to `docs/reference/order-path-incident-history.md` for steps 2.1-2.4 (still useful documentation even without an incident, since they migrated load-bearing readers).
6. Update `test_portfolio_json_not_read.py` and `test_dual_write_removal.py` to permanently lock out the just-migrated reader.

**After step 2.8:** `ib_sync.py` no longer writes `portfolio.json`. Remove the write site. The bind-mount can shrink to just `futu_portfolio.json` + `locks/` + `service_health/`.

**Effort:** ~3-4 days of focused work, can land as 2-3 batched PRs since there's no live trading to stagger around.

**Exit:** `data/portfolio.json`, `data/trade_log.json`, `data/blotter.json`, `data/nav_history.jsonl` all unused; tests prove zero readers; bind-mount can shrink to just `futu_portfolio.json` + `locks/` + `service_health/`.

## Phase 3 — Stateless containers

**Goal:** Remove the `/opt/xenon/data` bind-mount. Containers can be replaced/migrated without state transfer.

| #   | Action                                                                                                                                                                                                              |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 3.1 | `locks/` and `service_health/` → `tmpfs` mount (ephemeral, regenerated on container start)                                                                                                                          |
| 3.2 | `presets/`, `strategies.json`, `watchlist.json`, `flex_token_config.json` → migrate to PG `xenon.user_config` table OR keep as named bind-mount (decide based on whether config should follow user across machines) |
| 3.3 | `futu_portfolio.json` — stays bind-mounted until Phase 4                                                                                                                                                            |
| 3.4 | Cache files (analyst_ratings, option_close, menthorq_cache, etc.) → named Docker volume (regenerable, survives container restart, no host-side inspection needed)                                                   |
| 3.5 | Remove the broad `/opt/xenon/data` bind-mount; replace with the targeted mounts above                                                                                                                               |

**Effort:** ~1 day. **Exit:** `docker compose down && docker compose up -d` on a fresh host (with only PG + IB Gateway as dependencies) produces a working stack.

## Phase 4 — Futu migration (deferred)

Per direction: rewrite after production starts using Futu's friendlier snapshot API. Schema already supports `broker='FUTU'`. Tracked in `docs/plans/2026-04-27-futu-postgres-migration-followup.md`. No work in this plan.

## Recommended sequencing (clean-slate compressed)

The clean-slate framing collapses the timeline because Phase 2 no longer needs paper burn-in between each PR — burn-in happens once, against the full stack, in Phase 1.10.

| Date                       | Phase    | Work                                                                                             |
| -------------------------- | -------- | ------------------------------------------------------------------------------------------------ |
| **Sun 2026-05-03 PM**      | 0        | Cull, memory, guard tighten (1-2 hrs)                                                            |
| **Sun 2026-05-03 PM**      | 1.1-1.6  | Dockerfiles + compose + GHCR workflow (4-5 hrs)                                                  |
| **Mon 2026-05-04**         | 2.1-2.4  | Drain safety-tier readers (naked_short_audit, preflight, ib_sync entry_date, monitor_daemon)     |
| **Tue 2026-05-05**         | 2.5-2.8  | Drain analytics tier (portfolio_adapter, performance, report, leap_iv, ib_sync prev fallback)    |
| **Tue 2026-05-05 PM**      | 3        | Stateless cutover — replace broad bind-mount with targeted mounts (tmpfs locks, named cache vol) |
| **Wed 2026-05-06**         | 1.7-1.8  | Provision Mac mini + paper smoke test of full stateless stack                                    |
| **Wed-Thu 2026-05-06/07**  | 1.10     | 24h paper burn-in (elapsed) — validates full Phase 0+1+2+3 together                              |
| **Thu 2026-05-07 evening** | 1.11     | **Production declaration** after market close — flip DATABASE_URL + IB Gateway port              |
| **Future**                 | 4 (Futu) | Deferred — rewrite Futu sync after production starts                                             |

## What NOT to do

- **Don't auto-run alembic on app container start.** Migrator is a separate one-shot, manually invoked. Surprise schema changes at 09:30 ET = bad day.
- **Don't ship the full 546MB `data/` to remote.** Phase 0 is mandatory before Phase 1.
- **Don't declare production live before Phase 3 lands.** The whole point of clean-slate is starting from a stateless container artifact, not migrating to one. Phases 0→1→2→3 all gate the production declaration in Phase 1.11.
- **Don't have both this Mac and remote container writing to the same `data/` or same DB.** Dev = `core_dev` + this Mac's `data/`. Prod = `core` + remote's targeted volumes. They never share.
- **Don't ramp Futu migration into this plan.** It's a separate initiative with its own design surface.

## Open decisions

| Decision                             | Default                                                                                            | When to revisit                           |
| ------------------------------------ | -------------------------------------------------------------------------------------------------- | ----------------------------------------- |
| GHCR vs Docker Hub                   | GHCR (already in GH Actions ecosystem)                                                             | If GHCR quotas become a problem           |
| Build trigger                        | Tag-only (matches `release.yml` cadence)                                                           | If we want preview builds per PR          |
| Secrets management                   | Bind-mount `.env` (simplest)                                                                       | If we add a second prod host              |
| Docker Desktop vs Colima on Mac mini | Docker Desktop (most familiar)                                                                     | If resource overhead becomes a concern    |
| Phase 2 batching strategy            | One PR per phase (2.1-2.4 batched as one safety-tier PR; 2.5-2.8 batched as one analytics-tier PR) | If diff size becomes unreviewable — split |

## Success criteria

- [ ] Phase 0 — `du -sh data/ < 15M`, memory entry accurate, guard regex tightened
- [ ] Phase 1 build — All 4 Dockerfiles + compose + GHCR workflow exist; first image build succeeds locally
- [ ] Phase 2 — Zero readers of `data/portfolio.json`, `trade_log.json`, `blotter.json`, `nav_history.jsonl`; tests enforce
- [ ] Phase 3 — Containers stateless; `docker compose up -d` on fresh host (PG + IB Gateway only) works
- [ ] Phase 1.10 burn-in — 24h paper trading on full stateless stack, no errors
- [ ] **Production declaration (Phase 1.11) gates on all of the above.** First production process is a clean-slate stateless container.
- [ ] Memory entry `project_postgres_migration_read_side_gap.md` deleted (no longer applicable) once Phase 2 lands
