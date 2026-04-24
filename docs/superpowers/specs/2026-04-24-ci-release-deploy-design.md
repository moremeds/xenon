# CI, Release, and Deploy Design

**Status:** Draft (design, not yet implemented)
**Date:** 2026-04-24
**Owner:** chenxi

## Summary

Introduce a three-phase pipeline to take Xenon from "works on my laptop" to "a stable tagged version is running on a dedicated Mac mini in production."

1. **Phase 1 — CI gate on PRs.** GitHub Actions runs pytest, vitest, typecheck, and lint on every PR to master. Required checks before merge.
2. **Phase 2 — Release cut.** A semi-manual `scripts/release/cut.sh` bumps `VERSION`, finalizes the CHANGELOG, commits, and tags `vX.Y.Z`. A tag-triggered workflow re-verifies CI and publishes a GitHub Release.
3. **Phase 3 — Deploy to Mac mini.** A push-based `scripts/deploy/mac-mini.sh vX.Y.Z` SSHes to the Mac mini over Tailscale, checks out the tag into a versioned release directory, health-checks on alternate ports, atomically swaps a `current` symlink, and restarts launchd-managed services.

The Mac mini becomes the canonical host for the full stack — Next.js, FastAPI, `ib_realtime_server.js`, and IB Gateway. IB Gateway is exposed on the LAN (and via Tailscale) so the dev laptop's existing `scripts/cloud.sh` path points at the Mac mini instead of the VPS.

Versioning restarts at `0.0.1`. `VERSION` is the single source of truth; `package.json` mirrors it.

## Goals

- Master is always green. No one-off "it works on my machine" merges.
- Every prod-bound release is a tagged, CHANGELOG'd, independently-verified artifact.
- Deploys are a single command, reversible in under 10 seconds, and decoupled from the IB Gateway 2FA cold-start flow.
- Single-operator scale. No multi-tenant, no RBAC, no approval gates beyond "the operator runs the command."

## Non-goals

- Zero-downtime deploys. A ~2-second window during launchd service restart is acceptable.
- Cloudflare Tunnel / public internet exposure. Deferred to a Phase 3.5; LAN and Tailscale-only at first.
- Auto-deploy on tag. Tags create release candidates; deploys remain a deliberate operator action.
- Self-hosted CI runners. GitHub-hosted `ubuntu-latest` is sufficient for Phase 1.
- Playwright on PRs. Moves to a nightly cron workflow.

---

## Phase 1 — CI on PRs

### Workflow

`.github/workflows/ci.yml`

**Triggers:**

- `pull_request` targeting `master`
- `push` to `master` (so the master badge reflects reality post-merge)

**Concurrency:** `group: ci-${{ github.ref }}`, `cancel-in-progress: true`.

### Jobs (parallel, `ubuntu-latest`)

| Job             | Command                                                                                                           | Notes                                                                                                                                |
| --------------- | ----------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `python-tests`  | PR: `python3.13 scripts/infra/dev/run_pytest_affected.py --base origin/master`. Master push: full `pytest` suite. | `fetch-depth: 0` for diff. Pip cache keyed on `requirements.txt` + `pyproject.toml` hash.                                            |
| `web-tests`     | `cd web && npm test`                                                                                              | Vitest. `ASSISTANT_MOCK=1` already in the npm script. npm cache keyed on `web/package-lock.json` hash.                               |
| `web-typecheck` | `cd web && npm run typecheck`                                                                                     | `tsc --noEmit`.                                                                                                                      |
| `web-lint`      | `cd web && npm run lint`                                                                                          | ESLint on `app components lib`.                                                                                                      |
| `dead-code`     | Existing dead-code scan                                                                                           | **Advisory for 2 weeks** (`continue-on-error: true`). Flip to required once the 174-item backlog is drained or `.deadcodeignore`-ed. |

### Branch protection on master

Required checks: `python-tests`, `web-tests`, `web-typecheck`, `web-lint`. `dead-code` is visible but non-blocking until flipped.

### Secrets

**None required.** Tests must skip cleanly when `UW_TOKEN` / `CLERK_*` / `MENTHORQ_*` are absent. Any test that silently reaches for production credentials is a bug — fix the test, don't add the secret to CI.

### Playwright (nightly, not on PRs)

`.github/workflows/nightly.yml` — cron `0 9 * * *` UTC, master only. Runs `cd web && npx playwright test`. On failure, opens (or updates) a GitHub issue labeled `nightly-playwright`. Playwright browsers cached by OS + Playwright version.

### Failure policy

No auto-retry. Flaky tests are bugs; they get fixed, not retried. A red check disables the merge button.

---

## Phase 2 — Release cut

### One-time reset

As part of rolling out Phase 2: set `VERSION=0.0.1` and `package.json` root `version=0.0.1`. `VERSION` is authoritative from this point forward; Python reads `VERSION` via a small `xenon.version` helper, Node reads `VERSION` via a build-time script that injects it into `package.json`. No drift is acceptable; CI enforces consistency (a lint step that fails if the two disagree).

### Script

`scripts/release/cut.sh` — interactive, laptop-local.

### Preflight (abort if any fails)

1. On `master`, clean working tree, up to date with `origin/master`.
2. Latest commit on `origin/master` has a green CI run (`gh run list --branch master --limit 1 --json conclusion` → `success`).
3. `CHANGELOG.md` has a non-empty `## [Unreleased]` section.
4. No existing tag matches the target version.

### Interactive bump

Shows current `VERSION` and asks: `patch / minor / major / custom`. Default `patch`. Prints a diff preview of the intended changes and asks `y/N`.

### On confirm

1. Rewrite `VERSION` → new version.
2. Rewrite `CHANGELOG.md`: insert `## [X.Y.Z] — YYYY-MM-DD` below `## [Unreleased]` and move bullets under the new heading. Reset `[Unreleased]` to empty.
3. Rewrite `package.json` root `version` to match.
4. Commit: `release: vX.Y.Z`.
5. Annotated tag: `git tag -a vX.Y.Z -m "<CHANGELOG section for X.Y.Z>"`.
6. Print `git push origin master --follow-tags` but **do not run it.** Operator reviews and pushes manually.

### Tag-triggered workflow

`.github/workflows/release.yml`, `push: { tags: [v*] }`. Two jobs:

1. `verify` — re-runs the full Phase 1 suite against the tag ref.
2. `publish` — after `verify` succeeds: `gh release create vX.Y.Z --notes-file <(extract CHANGELOG section)`. Marks as latest. No artifacts attached.

### Rollback of a bad release cut (pre-deploy)

```
git tag -d vX.Y.Z && git push --delete origin vX.Y.Z
gh release delete vX.Y.Z
git revert <release-commit-sha>
```

Then re-run `cut.sh`. Documented in `docs/runbooks/release.md`.

---

## Phase 3 — Deploy to Mac mini

### Host

`xenon-mini.local` on LAN, Tailscale for remote access. SSH as user `xenon`. No inbound public access in Phase 3 (Cloudflare Tunnel deferred).

### Directory layout

```
/opt/xenon/
  releases/
    v0.0.1/                  # full git worktree at tag
    v0.0.2/
  current -> releases/v0.0.2 # atomic swap target
  shared/
    .env                     # root secrets (MENTHORQ_*, MASSIVE_*, CLERK_*, R2_*)
    web/.env                 # web secrets (ANTHROPIC, UW_TOKEN, EXA, CEREBRAS, CLERK)
    data/                    # trend_scan.duckdb, trade_log.json, reconciliation.json
    logs/
    venv/                    # persistent Python venv
    web-node_modules/        # persistent node_modules (symlinked into each release)
  ib-gateway/                # separate docker-compose project, independent lifecycle
  .git-cache/                # bare bookkeeping repo for fast fetches
```

Releases are full checkouts. `current` is a symlink, swapped atomically with `mv -Tf`. `shared/` persists across releases — secrets and data are never clobbered. `venv/` and `web-node_modules/` are shared because lockfile-unchanged reinstalls are wasteful.

### Deploy script

`scripts/deploy/mac-mini.sh vX.Y.Z` — runs on laptop, SSHes to Mac mini over Tailscale.

Sequence:

1. **Verify tag published.** `gh release view vX.Y.Z` succeeds; abort otherwise.
2. **Fetch into bookkeeping repo** at `/opt/xenon/.git-cache`, check out worktree to `releases/vX.Y.Z/`.
3. **Symlink shared state:** `.env`, `web/.env`, `data/`, `logs/`, `web/node_modules`, and `venv/` symlinked from `shared/` into the new release.
4. **Build deps:**
   - `cd web && npm ci && npm run build` (npm ci no-ops when lockfile matches).
   - `source /opt/xenon/shared/venv/bin/activate && pip install -r requirements.txt`.
5. **Pre-swap health check on alternate ports.** Launch the stack with `XENON_API_PORT=8322`, `PORT=3001`. Poll `/health` on the API and `/` on Next.js for up to 60s. On failure → abort, leave `current` untouched.
6. **Atomic swap:** `ln -sfn releases/vX.Y.Z current.new && mv -Tf current.new current`.
7. **Restart app services via launchd:** `launchctl kickstart -k gui/$UID/xenon.web`, `xenon.api`, `xenon.ib-realtime`. **Do not touch `xenon.ib-gateway`** — decoupled lifecycle.
8. **Post-swap verification:** poll real ports (`:8321/health`), assert `ib_gateway.port_listening: true`, assert `GET /version` returns the new tag. (A new endpoint on FastAPI — see "New code required" below.)
9. **On success:** prune to last 3 releases. Append a record to `shared/logs/deploys.jsonl`: `{ts, version, previous, actor, outcome}`.
10. **On failure after swap:** auto-rollback — re-point `current` at previous release, kickstart services, exit non-zero with the failure reason.

### Rollback

`scripts/deploy/mac-mini.sh v0.0.1` — same flow, fast path when the release directory already exists (build steps no-op, symlink swap + kickstart). End-to-end under 10 seconds.

### launchd services

Plists checked into `deploy/launchd/`, installed once via the bootstrap script.

- `xenon.web.plist` — `WorkingDirectory: /opt/xenon/current/web`, `ProgramArguments: [npm, run, start]`, `KeepAlive: true`, stdout/stderr to `shared/logs/web.log`.
- `xenon.api.plist` — `/opt/xenon/shared/venv/bin/python3.13 -m uvicorn xenon.api.server:app --host 127.0.0.1 --port 8321 --app-dir /opt/xenon/current/src`.
- `xenon.ib-realtime.plist` — `node /opt/xenon/current/scripts/infra/ib_realtime/ib_realtime_server.js`.
- `xenon.ib-gateway.plist` — `docker compose -f /opt/xenon/ib-gateway/docker-compose.yml up`. Independent of app deploys.

`EnvironmentVariables` loaded via a small shim that sources `/opt/xenon/shared/.env` (launchd doesn't parse `.env` natively).

### IB Gateway on LAN

`ib-gateway/docker-compose.override.yml` binds `4002:4002` to `0.0.0.0` (default was `127.0.0.1`). Dev laptop reaches IB Gateway at `xenon-mini.local:4002` or its Tailscale IP. `scripts/cloud.sh` on the laptop is updated to point at the new host.

### New code required

- **`GET /version`** on FastAPI — reads `/opt/xenon/current/VERSION`, returns `{version, commit, deployed_at}`. Load-bearing: without it, post-swap verification cannot distinguish "new release live" from "old release happens to be healthy."
- **`web/package.json` must have `"start": "next start"`** — confirm in Phase 3 prerequisites; add if absent.
- **`xenon.version` helper** in Python and a matching Node read — both resolve `VERSION` at the repo root.

### Bootstrap (one-time, per Mac mini)

`scripts/deploy/mac-mini-bootstrap.sh`. Installs Homebrew deps (node, python@3.13, docker, cloudflared, tailscale), creates `/opt/xenon/` layout, installs launchd plists, prompts for `.env` paths to copy into `shared/`. Re-runnable (idempotent). Documented in `docs/runbooks/mac-mini-provision.md`, which also lists every required env var and its source.

---

## Testing strategy

- **Phase 1 is self-testing** — the first PR that adds `ci.yml` will either be green (✓) or expose latent test failures (which become follow-up fixes before merging the CI PR itself).
- **Phase 2 dry-run** — cut a `v0.0.1-rc1` tag, walk it through the tag workflow, confirm GitHub Release appears, then delete both.
- **Phase 3 dry-run** — first real deploy is `v0.0.1` to the Mac mini. Manual QA of the web UI and the `/health` endpoint per CLAUDE.md Startup Checklist. Second deploy (`v0.0.2`) tests the rollback path: deploy, then redeploy `v0.0.1`, confirm stack is back.

## Rollout order

1. PR 1 — `ci.yml` + nightly Playwright workflow. No deploy involvement.
2. PR 2 — `scripts/release/cut.sh`, `release.yml`, VERSION/package.json reset to `0.0.1`, CHANGELOG migration.
3. PR 3 — `scripts/deploy/mac-mini.sh`, `scripts/deploy/mac-mini-bootstrap.sh`, launchd plists in `deploy/launchd/`, `GET /version` endpoint, `web/package.json start` script, `scripts/cloud.sh` repoint.

Each PR lands on master independently and can be reverted cleanly.

## Risks and mitigations

| Risk                                                                               | Mitigation                                                                                                                                                                 |
| ---------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| IB Gateway 2FA cold start blocks deploys                                           | App deploys don't restart the gateway. Gateway has its own launchd job and its own bounce command.                                                                         |
| First Phase 1 run is red due to latent test failures                               | Fix or `.skip` tests in the same PR that adds `ci.yml`. Acceptable to disable a small number with a tracked follow-up issue, but not to ship CI that is broken on day one. |
| `reqTickersAsync` hang on index options (per memory) breaks post-swap health check | `/health` endpoint must not touch that code path. Add a regression test that asserts `/health` completes under 2s without any IB quote subscriptions.                      |
| `shared/` gets corrupted, breaks all releases                                      | `shared/data/` is backed up nightly to R2 via a small launchd job (separate concern, stub out in Phase 3, track as follow-up).                                             |
| Operator deploys from a dirty laptop checkout                                      | `deploy/mac-mini.sh` asserts `git status --porcelain` is empty on the laptop before SSHing out.                                                                            |
| GitHub Actions outage delays a release                                             | Release cut is local; only the tag workflow (verify + GitHub Release) depends on Actions. Deploys don't depend on Actions at all.                                          |

## Open items (to confirm before writing the implementation plan)

- Cloudflare Tunnel rollout (Phase 3.5): track as a separate spec, not yet written.
- Nightly R2 backup of `shared/data/`: same — separate spec, mentioned above as a risk mitigation.
- Dead-code hard-gate flip date: put a calendar reminder for +2 weeks after Phase 1 merges.
