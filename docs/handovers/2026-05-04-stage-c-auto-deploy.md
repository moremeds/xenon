# Stage C — Tag-to-mini auto-deploy

**Goal:** Pushing `vX.Y.Z` should result in the mini running that version with no
operator action beyond the tag itself. Today the chain breaks in four places:

0. **GHCR per-package ACL** — the 4 packages were bootstrapped via manual
   `docker push` with a PAT, so GHCR never auto-linked `moremeds/xenon` as
   an authorized writer. Every workflow push is denied. Hit during the
   v0.0.4 cut (run [25296418217](https://github.com/moremeds/xenon/actions/runs/25296418217)).
1. `release.yml::verify` fails on the runner (no Postgres) → `ghcr-push` skipped.
2. `release.yml::ghcr-push` doesn't pass `NEXT_PUBLIC_*` build-args → the
   web image baked at CI has empty Clerk keys.
3. Even if 0+1+2 land, the mini doesn't know about new images — operator has
   to `ssh + pull + up -d` by hand.

This doc fixes all four. Item 0 is a one-time UI step on each package's
settings page; items 1+2 are repo-side PRs you (the dev Mac operator)
merge; item 3 is a one-shot config on the mini that you can copy into a
terminal there.

> **Status (2026-05-04):** Items 1+2 landed in PR #91 (`release: v0.0.4`).
> Item 0 was discovered when the v0.0.4 tag fired and `ghcr-push` denied
> all 4 matrix jobs. Item 3 (Watchtower) still pending mini-side.

## Item 0 — GHCR per-package ACL (one-time)

### Problem

`release.yml::ghcr-push` declares `permissions: packages: write` and the
run log confirms `Packages: write` on the `GITHUB_TOKEN`. Login succeeds.
The push still fails with `denied: permission_denied: write_package`.

GHCR has two permission layers:

1. **Token scope** — granted by the workflow YAML (`permissions:` block).
2. **Per-package ACL** — managed under each package's "Manage Actions
   access" settings. When a workflow successfully pushes a package for
   the first time, GHCR auto-links the source repo here. **There is no
   public REST or GraphQL API to manage this** — UI only.

The Xenon packages (`xenon-{api,web,realtime,migrator}`) were created by
manual `docker push` from this Mac on 2026-05-03 because `release.yml::verify`
was failing (item 1 wasn't fixed yet). That manual path bypasses the
auto-link, leaving each package with no authorized repos. Every
workflow-driven push is denied until a human grants access.

### Fix

For each of the 4 packages, visit the settings page and grant
`moremeds/xenon` write access:

- `https://github.com/users/moremeds/packages/container/xenon-api/settings`
- `https://github.com/users/moremeds/packages/container/xenon-web/settings`
- `https://github.com/users/moremeds/packages/container/xenon-realtime/settings`
- `https://github.com/users/moremeds/packages/container/xenon-migrator/settings`

On each page → **Manage Actions access** → **Add Repository** → search
`xenon` → select `moremeds/xenon` → role **Write** → save.

After all 4 are linked, re-run the failed jobs without re-tagging:

```bash
gh run rerun 25296418217 --failed --repo moremeds/xenon
```

(Or for any future blocked tag, swap the run id.)

### Test plan

- After the link is applied, `gh run rerun --failed` should publish all
  4 images. Verify with `gh api user/packages/container/xenon-api/versions`
  — a fresh `:v0.0.4` digest should appear, dated within minutes.

### Cost

Zero code change. ~30 seconds of UI clicks per package, one-time per
package's lifetime. Future tags publish without operator intervention.

### Prevention

Don't bootstrap GHCR packages via manual `docker push`. Either push
the first version from a working `release.yml` (auto-link on success),
or link the repo immediately after the first manual push. Once linked,
the ACL persists across version bumps.

## Item 1 — `release.yml::verify` Postgres service

### Problem

`release.yml::verify` runs `uv run pytest` against the full suite. Tests
under `src/xenon/db/tests/` and `src/xenon/api/tests/test_wizard_routes.py`
require a live Postgres at `127.0.0.1:5432`. The runner doesn't have one.
Net: `282 failed / 94 errors` on every tag push.

The mini's Postgres is on the LAN at `192.168.50.47:5432`, not reachable
from a GitHub-hosted runner. So we use the standard GHA pattern: spin up a
**temporary Postgres service container** inside the runner, run alembic
against it, then run the tests.

### Fix

Append a `services:` block to the `verify` job. Inside the job, add an
alembic step before pytest. Set `DATABASE_URL` and `DATABASE_URL_TEST` to
the service's localhost endpoint.

Concretely, in `.github/workflows/release.yml`:

```yaml
verify:
  runs-on: ubuntu-latest
  services:
    postgres:
      image: postgres:17-alpine
      env:
        POSTGRES_USER: xenon_app
        POSTGRES_PASSWORD: xenon_dev
        POSTGRES_DB: core_test
      ports: ["5432:5432"]
      options: >-
        --health-cmd "pg_isready -U xenon_app"
        --health-interval 5s
        --health-timeout 3s
        --health-retries 10
  env:
    DATABASE_URL: postgresql+asyncpg://xenon_app:xenon_dev@127.0.0.1:5432/core_test
    DATABASE_URL_TEST: postgresql+asyncpg://xenon_app:xenon_dev@127.0.0.1:5432/core_test
  steps:
    - uses: actions/checkout@v4
      with: { fetch-depth: 0 }
    - uses: astral-sh/setup-uv@v5
      with: { enable-cache: true, cache-dependency-glob: uv.lock }
    - run: uv python install 3.13
    - run: uv sync --frozen --extra test
    # NEW: prep schemas + run migrations against the service Postgres
    - name: Bootstrap schemas
      run: |
        psql "$DATABASE_URL_TEST" -c "CREATE SCHEMA IF NOT EXISTS xenon;"
        psql "$DATABASE_URL_TEST" -c "CREATE SCHEMA IF NOT EXISTS apex;"
        psql "$DATABASE_URL_TEST" -c "CREATE SCHEMA IF NOT EXISTS events;"
    - run: uv run alembic upgrade head
    - run: uv run pytest
    # ... rest unchanged (npm install, typecheck, lint, vitest, version_sync_check)
```

### Test plan

- Push a `v0.0.4-rc1` tag to a draft branch (or use `workflow_dispatch` —
  but the existing trigger is tags-only; for testing, temporarily add
  `workflow_dispatch:` and remove after).
- `verify` should land green; `ghcr-push` should run.

### Cost

~25 lines added to `release.yml`. Zero impact on `ci.yml` (which already
has its own postgres service in `python-tests`).

## Item 2 — `NEXT_PUBLIC_*` build-args in `ghcr-push`

### Problem

`docker/web.Dockerfile` accepts `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` and
`NEXT_PUBLIC_IB_REALTIME_WS_URL` as build-args, but the `ghcr-push` step
in `release.yml` doesn't pass them. Result: the GHCR web image has empty
strings inlined in its bundle, breaking client-side Clerk on every
CI-built tag.

### Fix

Two pieces:

**(a) Add the publishable key as a repo secret.** GitHub repo → Settings →
Secrets and variables → Actions → New repository secret:

- Name: `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`
- Value: the `pk_test_…` (or `pk_live_…` when you graduate) from
  `web/.env`. Note: this _is_ a publishable key — safe-ish to embed,
  but treating as a secret keeps the value out of repo / logs.

(Skip `NEXT_PUBLIC_IB_REALTIME_WS_URL` — its empty default works on
`localhost`-served pages and there's no production-shape value yet.)

**(b) Thread it through `ghcr-push`** in `release.yml`. The matrix entry
for `web` needs build-args; other images skip the block:

```yaml
- uses: docker/build-push-action@v5
  with:
    context: .
    file: ${{ matrix.dockerfile }}
    platforms: linux/arm64
    push: true
    tags: |
      ${{ steps.meta.outputs.image }}:${{ steps.meta.outputs.version }}
      ${{ steps.meta.outputs.image }}:latest
    labels: |
      org.opencontainers.image.source=https://github.com/${{ github.repository }}
      org.opencontainers.image.revision=${{ steps.meta.outputs.revision }}
      org.opencontainers.image.version=${{ steps.meta.outputs.version }}
    # NEW: web-only build-args
    build-args: |
      NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=${{ matrix.image == 'web' && secrets.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY || '' }}
    cache-from: type=gha,scope=xenon-${{ matrix.image }}
    cache-to: type=gha,scope=xenon-${{ matrix.image }},mode=max
```

The `matrix.image == 'web'` ternary keeps the build-arg empty for the other
3 images (which don't read it anyway) so a single matrix definition still
works.

### Test plan

- After the v0.0.4 tag fires `ghcr-push`, pull the new web image on the
  mini, restart, browse `/sign-in` from the LAN host. The Clerk SignIn
  component should mount client-side (script tag for `clerk.js` should
  appear in the rendered HTML, not an empty bundle).

### Cost

~3 lines in `release.yml` + 1 GHA secret. No impact on local builds —
they still source from the host `.env` via compose interpolation.

## Item 3 — Pull-and-restart on the mini (Watchtower)

### Problem

The mini has no mechanism to pull new GHCR tags after a release. Every
deploy still requires the operator to `ssh + docker pull + docker-compose
up -d`.

### Decision: Watchtower

Three options were on the table — Watchtower, self-hosted GHA runner,
repo-dispatch listener. **Watchtower wins** for this scope because:

- Single OSS container, ~15 LOC of compose addition.
- Already authenticated against GHCR via the mini's existing `~/.docker/config.json`.
- Polls every 60s by default — well within "tag → live" SLA needs.
- Honors `restart: unless-stopped` so the post-update state is identical
  to a manual restart.
- No secrets to provision, no GH webhook to wire.

Trade-off: any digest change pulled is auto-applied — no manual gate. Mitigated
by Watchtower's `--label-enable` so only services tagged with
`com.centurylinklabs.watchtower.enable=true` are watched. Migrator stays
out (no auto-runs at app start — that's a load-bearing rule from the
plan).

### What to do on the mini

These are the exact commands to paste in a shell on the mini (or via
`ssh moremeds@192.168.50.47`):

```bash
# 1. Add Watchtower as a service in /opt/xenon/compose.yml.
#    Append the block below — does NOT replace existing services.
cat >> /opt/xenon/compose.yml <<'YAML'

  watchtower:
    image: containrrr/watchtower:1.7.1
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ~/.docker/config.json:/config.json:ro
    environment:
      WATCHTOWER_POLL_INTERVAL: 60
      WATCHTOWER_LABEL_ENABLE: "true"
      WATCHTOWER_CLEANUP: "true"
      WATCHTOWER_INCLUDE_RESTARTING: "true"
      WATCHTOWER_LIFECYCLE_HOOKS: "true"
      WATCHTOWER_NOTIFICATIONS_LEVEL: info
      DOCKER_CONFIG: /
    labels:
      com.centurylinklabs.watchtower.enable: "false"
YAML

# 2. Add the opt-in label to the three long-running services. Watchtower
#    is label-gated so only api/web/realtime get auto-updated; migrator stays
#    out (no auto-alembic on tag bump).
#
# Edit /opt/xenon/compose.yml. Under each of the api, web, realtime
# services, add a `labels:` block (or append to existing one):
#
#   labels:
#     com.centurylinklabs.watchtower.enable: "true"
#
# Easiest with sed (idempotent):
python3 <<'PY'
import re
from pathlib import Path
p = Path("/opt/xenon/compose.yml")
text = p.read_text()
for svc in ("api", "web", "realtime"):
    pat = rf"(  {svc}:\n(?:    .*\n)*?)(    restart: unless-stopped)"
    label_block = '    labels:\n      com.centurylinklabs.watchtower.enable: "true"\n'
    if 'com.centurylinklabs.watchtower.enable' not in re.search(pat, text).group(0):
        text = re.sub(pat, r'\1' + label_block + r'\2', text, count=1)
p.write_text(text)
print("labels added")
PY

# 3. Validate + apply.
cd /opt/xenon
docker-compose config > /dev/null && echo "OK"
docker-compose up -d watchtower

# 4. Verify Watchtower is watching only the 3 services.
docker logs $(docker ps -qf name=watchtower) 2>&1 | tail -20
# Expected log line: "Watching 3 containers"
```

After step 3, every 60s Watchtower checks GHCR for newer digests on
`ghcr.io/moremeds/xenon-{api,web,realtime}:v0.0.3` (matches whatever tag
is pinned in compose.yml). When a digest changes, it pulls + recreates
the container in place.

### "But the tag in compose.yml is pinned"

Right — Watchtower watches the _digest_, not the tag. If you re-tag
`:v0.0.3` in GHCR (which you shouldn't), Watchtower notices. For new
versions you still need to bump the tag in compose.yml _or_ switch
compose.yml to use `:latest` and let Watchtower handle the version dance:

```yaml
image: ghcr.io/moremeds/xenon-api:latest
```

**Recommendation**: switch to `:latest` once items 1+2 land. Operator's
deploy then becomes "push tag → done" — Watchtower picks up `:latest`
on the mini within 60s.

Until then: bump the tag in compose.yml manually, `docker-compose up -d`,
done. Watchtower still helps with sub-tag digest changes (e.g., security
patches retagged in place).

### Migrator handling

Migrator is _not_ watched (the `enable: "false"` label on Watchtower
itself is a no-op convention; what actually keeps migrator out is the
absence of `enable: "true"` on its service definition + Watchtower's
`WATCHTOWER_LABEL_ENABLE: "true"` opt-in mode).

For schema changes on a new tag, the operator still runs:

```bash
ssh moremeds@192.168.50.47 'PATH=/opt/homebrew/bin:$PATH; cd /opt/xenon && \
  docker-compose --profile migrate run --rm migrator'
```

This is intentional per `docs/plans/2026-05-03-pg-migration-completion-and-remote-deploy.md`
"Don't auto-run alembic on app container start. Migrator is a separate
one-shot, manually invoked. Surprise schema changes at 09:30 ET = bad day."

### Test plan

- Land item 1 (Postgres in verify) and item 2 (build-args).
- Push `v0.0.4-rc1` tag.
- Watch `release.yml::ghcr-push` succeed for all 4 images.
- On mini: `docker logs <watchtower>` should show "Found new ... image, restarting..."
  within 60s of the push.
- `docker ps` on mini → containers show new image digests.
- `curl http://localhost:8321/health` on mini → still 200, version surfaces if
  there's a version field, else just confirms responsiveness.

### Cost

- ~25 lines added to `/opt/xenon/compose.yml` (Watchtower service + labels).
- No new secrets. No GH webhook.
- Watchtower image: ~17 MB.
- Network: 1 GHCR HEAD request per service per 60s = trivial.

## Order of operations

| Step | Where   | What                                                       | Blocks                                                                     |
| ---- | ------- | ---------------------------------------------------------- | -------------------------------------------------------------------------- |
| 1    | Dev Mac | PR: `release.yml::verify` Postgres service                 | Item 2's CI test                                                           |
| 2    | Dev Mac | PR: `release.yml::ghcr-push` build-args + repo secret      | Test only after item 1 lands                                               |
| 3    | Mini    | Apply Watchtower compose addition (commands above)         | None — independent                                                         |
| 4    | Dev Mac | Cut `v0.0.4` to validate the full pipeline end-to-end      | Items 1+2+3 all done                                                       |
| 5    | Dev Mac | Update `docs/runbooks/remote-deploy.md` "Known follow-ups" | Strikes out items 1, 2, 9 (Clerk LAN host can be addressed in same window) |

Items 1+2 can land as one PR or two; either is fine. Item 3 doesn't need
a PR — it's mini-side config. After step 4 validates, Stage C is done and
the deploy story is "push tag → mini runs new images within 60s of the
ghcr-push job finishing."

## Out of scope (deferred)

- **Self-hosted runner on the mini.** Would let CI verify run against
  the mini's actual Postgres + IB Gateway + Futu, but security overhead
  (must disable runs from forks, segregate from system) outweighs the
  benefit for solo dev. Revisit if a second contributor joins.
- **Repo-dispatch webhook listener on the mini.** Cleaner trigger than
  60s polling but needs an HTTPS endpoint, ngrok-or-equivalent, and
  signature verification. Not worth the complexity at current scale.
- **Rolling deploy / blue-green.** Watchtower restarts in place. Brief
  downtime per service during recreate (~2–5s). Acceptable for a
  single-operator system.
- **Health-aware deploy.** Watchtower doesn't gate on post-restart
  healthchecks. If a new image is broken, the container restart-loops
  per `unless-stopped`. Mitigation: smoke against staging tag (e.g.,
  `v0.0.4-rc1`) before bumping the prod-tracking tag.
