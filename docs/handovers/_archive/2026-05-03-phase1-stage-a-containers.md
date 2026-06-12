# Handover — Phase 1 Stage A: containerize Xenon for remote deploy

> **For the next session:** read this file end-to-end before touching anything. It is self-contained — you do not need the prior conversation.

## Simple goal in one paragraph

Build the four Docker images (`api`, `web`, `realtime`, `migrator`) plus a `docker-compose.yml` that runs all of them as one local stack on **this Mac** (the dev workspace at `/Users/chenxi/projects/xenon`). Add a tag-triggered GHCR push job to `.github/workflows/release.yml` so the same images can later be pulled on the remote Mac mini. The work is **only complete** when you have actually run `docker compose up -d` locally, hit `curl http://localhost:8321/health` and `curl http://localhost:3000`, and confirmed both come back green. Do **not** push to GHCR, do **not** touch the Mac mini, do **not** declare anything production. This is build + local smoke only.

## Authoritative specs (read first)

In this order:

1. `docs/plans/2026-05-03-pg-migration-completion-and-remote-deploy.md` — the umbrella plan. **Read § "Phase 1 — Containerize for remote deploy" carefully**, especially items 1.1–1.6 (Stage A) and § "What NOT to do".
2. `docs/plans/2026-05-03-pg-migration-clean-cutoff.md` — context for what just shipped (PR #84) so you know the runtime is already PG-only.
3. `CLAUDE.md` — § "Local vs. Remote (post-2026-05-03 split)" (process map: ports, hosts, DBs) + § "Credentials" (env vars).
4. `src/xenon/api/CLAUDE.md` — FastAPI port 8321, health-probe semantics, IB Gateway routing (host-native).
5. `web/CLAUDE.md` — Next.js conventions.
6. `.github/workflows/release.yml` — existing release workflow you will extend with the GHCR push job. Do not break the existing verify+release jobs.
7. `scripts/infra/dev.sh` — the current dev launcher; use it as the env-var parity reference (don't ship anything that requires env vars `dev.sh` doesn't already export).
8. `pyproject.toml`, `web/package.json`, `web/next.config.mjs`, `scripts/infra/ib_realtime/ib_realtime_server.js` — the actual entrypoints + their config.

## Architectural decisions (locked in — do not relitigate)

These are from the plan; quoted here so you don't have to re-read § "Architectural decisions":

- **Images:** 4 separate images — `api` (FastAPI/Python), `web` (Next.js/Node), `realtime` (Node/`ib_realtime_server.js`), `migrator` (Python/alembic).
- **Base images:** `python:3.13-slim` for api+migrator; `node:22-alpine` for web+realtime.
- **Web build:** multi-stage with `next build` and `output: 'standalone'`. The runtime stage carries only `.next/standalone` + `public` + `.next/static`.
- **Migrator:** separate one-shot. **NEVER auto-run on app container start.** The compose file lists migrator as a service with `profiles: ["migrate"]` (or equivalent) so `docker compose up -d` does not trigger it. Run manually: `docker compose run --rm migrator`.
- **IB Gateway:** stays **host-native** (Java GUI, not containerizable). Containers reach it via `host.docker.internal:4001` (live) or `host.docker.internal:4002` (paper) on Mac. On Linux this becomes a fixed bridge IP — out of scope for this work.
- **Postgres:** **external**, already running at `192.168.50.47:5432`. Containers connect over the LAN. Do not put Postgres in compose.
- **State:** bind-mount `/opt/xenon/data:/app/data` is the **remote Mac mini** layout. Locally, just bind-mount `./data:/app/data` (or skip — most readers are now PG-backed after PR #84).
- **`.env`:** bind-mount the root `.env` read-only into the api + migrator + realtime containers. Do **NOT** copy `.env` into images. Web reads `web/.env` separately at build time for `NEXT_PUBLIC_*` vars and at runtime via `env_file:` in compose.
- **Image registry:** GHCR (private). Push job is **tag-triggered only** (extends `release.yml`). Do not add a per-PR build trigger in this PR.

## Concrete tasks

Do these in order. Each is a TDD-ish loop: write file → build it → run it → check it works → commit.

### Task 1 — `docker/api.Dockerfile`

Multi-stage. Stage 1: builder using `python:3.13-slim`, install `uv`, run `uv sync --frozen --no-dev`. Stage 2: runtime image, copy the `.venv` from builder, copy `src/`, `pyproject.toml`, `uv.lock`, `alembic.ini` (if needed by app). Entrypoint: `.venv/bin/uvicorn xenon.api.server:app --host 0.0.0.0 --port 8321`.

**Verify locally:**

```bash
docker build -f docker/api.Dockerfile -t xenon-api:dev .
docker run --rm -p 8321:8321 --env-file .env xenon-api:dev &
sleep 5
curl -s http://localhost:8321/health | jq '.ib_gateway.port_listening, .ok'
docker stop $(docker ps -q --filter ancestor=xenon-api:dev)
```

Acceptance: `/health` returns `200`. `ib_gateway.port_listening` may be `false` (no gateway running locally is fine); the API itself must be up. Do not gate the build on a running gateway.

### Task 2 — `docker/web.Dockerfile`

Multi-stage. Stage 1: `node:22-alpine` builder, `cd web && npm ci && npm run build`. Stage 2: runtime, copy `.next/standalone`, `public`, `.next/static`. Entrypoint: `node web/server.js` (the standalone output).

**Critical:** `web/next.config.mjs` currently does NOT have `output: 'standalone'`. Add it:

```js
const nextConfig = {
  output: "standalone",
  outputFileTracingRoot: resolve(__dirname, ".."),
  // ...
};
```

The `outputFileTracingRoot` already exists; verify it points at the repo root so `next build` picks up the parent `package-lock.json` correctly.

**Verify locally:**

```bash
docker build -f docker/web.Dockerfile -t xenon-web:dev .
docker run --rm -p 3000:3000 -e PORT=3000 xenon-web:dev &
sleep 5
curl -sI http://localhost:3000 | head -1
docker stop $(docker ps -q --filter ancestor=xenon-web:dev)
```

Acceptance: HTTP `200` or `307`/`308` (redirect to sign-in is fine — middleware is doing its job).

### Task 3 — `docker/realtime.Dockerfile`

`node:22-alpine`. Copy `scripts/infra/ib_realtime/ib_realtime_server.js` and any deps it needs (check the file's `require`s — it likely needs `ws`, `dotenv`, etc. — install via a focused `package.json` you co-locate in `scripts/infra/ib_realtime/` if one doesn't exist, or carry the parent `node_modules` minimally). Entrypoint: `node ib_realtime_server.js`.

**Read first:** `scripts/infra/ib_realtime/ib_realtime_server.js` to see exactly what it needs (env vars, ports, dependencies). Do not guess.

**Verify locally:**

```bash
docker build -f docker/realtime.Dockerfile -t xenon-realtime:dev .
docker run --rm --env-file .env xenon-realtime:dev &
sleep 3
docker logs $(docker ps -q --filter ancestor=xenon-realtime:dev) | tail -20
docker stop $(docker ps -q --filter ancestor=xenon-realtime:dev)
```

Acceptance: container starts, logs show "listening" or equivalent, no immediate crash. It is OK for the relay to be unable to connect to a real IB Gateway locally — verify it does not crash-loop.

### Task 4 — `docker/migrator.Dockerfile`

Same `python:3.13-slim` base as api but minimal — only what alembic needs. Copy `src/`, `pyproject.toml`, `uv.lock`, `alembic.ini` (if at repo root), `src/xenon/db/migrations/`. Entrypoint: `uv run alembic upgrade head`.

**Verify locally against `core_dev`:**

```bash
docker build -f docker/migrator.Dockerfile -t xenon-migrator:dev .
docker run --rm --env-file .env xenon-migrator:dev
```

Acceptance: `INFO  [alembic.runtime.migration] Will assume transactional DDL.` followed by either "no upgrade needed" (if `core_dev` is already at `9f2c4a1d8e57`) or the upgrade trace. Exit code `0`. **Must NOT run against `core` (live) — `.env` `DATABASE_URL` already points at `core_dev` per `CLAUDE.md` § Credentials, but verify before running.**

### Task 5 — `docker-compose.yml` at repo root

Four services (`api`, `web`, `realtime`, `migrator`). Migrator under a profile so `up` doesn't touch it. Bind-mount `./data:/app/data` and `./.env:/app/.env:ro` for api+realtime+migrator. `web` uses `env_file: web/.env` and gets the necessary `NEXT_PUBLIC_*` vars at build time (you may need build-args for those — read `web/CLAUDE.md`).

Healthchecks:

- `api`: `curl -f http://localhost:8321/health || exit 1`, interval 30s, start_period 20s
- `web`: `wget --spider -q http://localhost:3000 || exit 1`, interval 30s, start_period 30s
- `realtime`: skip or use a process check; the relay does not expose HTTP

`depends_on` chain: api depends on a successful migrator profile run (manual), web + realtime depend on api healthy. **Use `depends_on: { api: { condition: service_healthy } }` syntax.**

Restart policy: `unless-stopped` for api, web, realtime. Migrator: `restart: "no"`.

Network: default bridge. Set `extra_hosts: ["host.docker.internal:host-gateway"]` on linux compatibility (no-op on Mac).

**Verify locally — this is the Stage A acceptance gate:**

```bash
# 1. Build all images
docker compose build

# 2. Run the migrator first (one-shot, against core_dev)
docker compose run --rm migrator

# 3. Bring up the stack
docker compose up -d

# 4. Wait for healthchecks
sleep 30

# 5. Inspect status — all of api, web, realtime should be healthy or running
docker compose ps

# 6. Probe endpoints — these are the exit checks
curl -s http://localhost:8321/health | jq
curl -sI http://localhost:3000 | head -1
docker compose logs api --tail 30
docker compose logs web --tail 30
docker compose logs realtime --tail 30

# 7. Tear down cleanly
docker compose down
```

**Acceptance for Stage A:**

- [ ] `docker compose build` exits 0 for all 4 images
- [ ] `docker compose run --rm migrator` exits 0 against `core_dev`
- [ ] `docker compose up -d` brings all 3 long-running services to `running` (api + web `healthy`)
- [ ] `curl http://localhost:8321/health` returns `200` with valid JSON
- [ ] `curl -I http://localhost:3000` returns `200`/`3xx`
- [ ] No service is in `restarting` / crash-loop after 60s
- [ ] `docker compose down` cleans up cleanly

### Task 6 — `.github/workflows/release.yml` GHCR push job

Append (don't replace) a new job that runs after the existing `verify` job succeeds, on tag pushes only. It builds all 4 images and pushes to `ghcr.io/<owner>/xenon-{api,web,realtime,migrator}` with tags `:vX.Y.Z` (from the git tag) and `:latest`.

Use `docker/build-push-action@v5` with `docker/login-action@v3` (auth via `GITHUB_TOKEN` and `permissions: { packages: write, contents: read }`). The job needs `permissions: packages: write` at the top of release.yml or scoped to the new job.

**Do NOT trigger this job in this PR.** Verify by:

```bash
# Validate the YAML is structurally OK
gh workflow view release.yml --yaml 2>&1 | head -20
# Ensure no other trigger fires it (no push: branches, no PR trigger)
grep -A5 "^on:" .github/workflows/release.yml
```

The existing trigger is `push: tags: ["v*"]`. Keep it that way. Do not add `workflow_dispatch` either — Stage B will manually tag a release when it's time.

## What NOT to do

- **Do not push to GHCR.** No tag, no manual workflow dispatch. Just have the workflow ready.
- **Do not touch the Mac mini.** Stage B (1.7–1.9) is a separate session.
- **Do not auto-run alembic on app container start.** Migrator is its own profile.
- **Do not modify `scripts/infra/dev.sh`.** It stays as the dev escape hatch.
- **Do not modify `.env` or `web/.env`.** Both carry credentials. Read them with `env_file:`.
- **Do not change `pyproject.toml` or `web/package.json` dependencies.** If a Dockerfile fails because something's missing, fix the Dockerfile, not the dep manifest.
- **Do not commit `data/`** (it's in `.gitignore` already; the bind-mount is for runtime, not for shipping data into the image).
- **Do not include the existing `data/` contents in the image build** — copy `src/`, `web/`, `scripts/infra/ib_realtime/`, manifests only. Use `.dockerignore` aggressively.
- **Do not start work on Phase 2 stragglers** (preflight, portfolio_adapter, leap_iv, ib_sync prev fallback). Different PR.
- **Do not push directly to `master`.** Always branch + PR. See "Branching + PR" below.
- **Never add `Co-Authored-By: Claude` trailer to commits** (per `~/.claude/CLAUDE.md`).

## Branching + PR

1. Branch off `master` (PR #84 should have merged by the time you start). If it hasn't, branch off `feat/pg-cutoff-prereq-fixtures` and rebase later.
2. Branch name: `feat/phase1-stage-a-containers`.
3. One PR per the user's preference: bundle all 6 tasks (4 Dockerfiles + compose + workflow) into a single PR with a clear test plan.
4. PR body must include the **Acceptance for Stage A** checklist filled in with the real curl outputs you observed locally.
5. Per the user's standing rule: **never `git push origin master` directly.** Push the branch, open the PR via `gh pr create`, let CI run, then the user merges via UI.

## Open decisions you may need to make

| Decision                                                                    | Recommended default                                      | When to ask the user                     |
| --------------------------------------------------------------------------- | -------------------------------------------------------- | ---------------------------------------- |
| Docker buildx multi-arch (linux/arm64 + linux/amd64)                        | linux/arm64 only for now (Mac mini is Apple silicon)     | If they want cross-platform from day one |
| `web` standalone build env vars (e.g., `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`) | Build-args via compose `args:`                           | If they want runtime injection instead   |
| Migrator profile name                                                       | `profiles: ["migrate"]`                                  | —                                        |
| Realtime restart policy on IB Gateway loss                                  | `unless-stopped`                                         | If they want a custom backoff            |
| Image labels (OCI metadata)                                                 | Add `org.opencontainers.image.{source,revision,version}` | —                                        |

If a decision is genuinely blocking, stop and ask in chat. Don't guess.

## Failure-mode triage

- **`uv sync --frozen --no-dev` fails in Docker:** check that `uv.lock` matches `pyproject.toml`; rebuild lock with `uv lock` if drift.
- **Web build fails on missing parent lockfile:** the `outputFileTracingRoot` config is already in `next.config.mjs`; ensure your `docker build` context is repo root, not `web/`.
- **Realtime crash-loops:** read its env-var requirements; common cause is missing `IB_REALTIME_PORT` or similar. Check `scripts/infra/ib_realtime/ib_realtime_server.js` top-of-file.
- **Migrator can't reach Postgres:** containers from `docker compose` see `192.168.50.47` as a normal LAN host. If your Mac firewall blocks outbound, that's local-network config, not a Docker problem.
- **`/health` returns `503` because `ib_gateway.port_listening: false`:** this is fine — there's no gateway running on this Mac in the test scenario. The api itself must respond `200` on the route; the probe sub-field is informational.

## Exit handoff (what to write back to the user)

When you're done, paste:

1. The PR URL.
2. The full output of step 6 of Task 5 (compose ps + curl health + curl -I 3000), so the user can see the stack actually came up.
3. Any open decisions you made on autopilot (so the user can challenge them).
4. The next-session pointer: "Stage B (1.7–1.9) is on the Mac mini — separate session, needs VNC/SSH access."

Do not declare Stage A done until those four bullets are written.
