# Remote Deploy Runbook (Mac mini, Docker)

The authoritative runbook for shipping Xenon to the remote Mac mini
(`192.168.50.47`, `moremeds@…`). Covers first-time bootstrap, tagged
releases via GHCR, smoke checks, rollback, and the snags that bit us
during the v0.0.3 cutover.

**Supersedes** `docs/runbooks/mac-mini.md` (launchd-based; pre-containers).
The old launchd path is no longer the deploy mechanism.

## Topology

| Host                                                      | Where things live                                                                                                                                                                                                                                            |
| --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Mac mini                                                  | Colima (Linux/arm64 VM running Docker), 4 containers (api / web / realtime / migrator), live IB Gateway 10.45 (`:4001`, host-native), Postgres 17 (`:5432`, host-native, schemas `xenon` / `apex` / `events`).                                               |
| This dev Mac                                              | Source of truth for builds + tag cuts. Paper IB Gateway local (`127.0.0.1:4002`).                                                                                                                                                                            |
| GHCR `ghcr.io/moremeds/xenon-{api,web,realtime,migrator}` | Owned by user `moremeds`; tags `:vX.Y.Z` and `:latest`. Visibility "private". Each package must have `moremeds/xenon` added under **Manage Actions access** with role **Write** — otherwise `release.yml::ghcr-push` is denied (see "Known follow-ups #11"). |

Containers reach Postgres + IB Gateway + Futu OpenD via `host.docker.internal`
(Colima maps it to the host gateway). Postgres + IB Gateway never run in
containers.

## Layout on the mini (`/opt/xenon/`)

```
/opt/xenon/
├── compose.yml      # image refs to ghcr.io/moremeds/xenon-*; no build:
├── .env             # api/realtime/migrator env_file
├── web.env          # web env_file (CLERK_SECRET_KEY, UW_TOKEN, etc.)
└── data/            # bind-mount target for /app/data (mostly empty post-PG migration)
```

`/opt/xenon` is `moremeds:staff`-owned (one-time `sudo mkdir + chown`
during 1.7). All operational files live under it; the source repo is **not**
cloned to the mini.

## First-time bootstrap (one-shot)

Run from this dev Mac unless noted. Prereq: `gh` authenticated as the GitHub
package owner (`moremeds`) with `write:packages,read:packages` scopes on
this Mac, and `read:packages` on the mini.

```bash
# 1. /opt/xenon owned by moremeds (single sudo prompt on the mini)
ssh moremeds@192.168.50.47 'sudo mkdir -p /opt/xenon && sudo chown moremeds:staff /opt/xenon'

# 2. GHCR auth on the mini (use this Mac's classic PAT for now — see
#    "Known follow-ups" for the fine-grained PAT path)
gh auth token | ssh moremeds@192.168.50.47 \
  'PATH=/opt/homebrew/bin:$PATH cat | docker login ghcr.io -u moremeds --password-stdin'

# 3. Start Colima (uses the existing default profile: 2 CPU / 2 GiB / 100 GiB / arm64)
ssh moremeds@192.168.50.47 'PATH=/opt/homebrew/bin:$PATH colima start'

# 4. Push env files (fix DATABASE_URL host + IB_GATEWAY_HOST → host.docker.internal,
#    and append XENON_BROKER_ACCOUNT — see follow-up #9 for why this is required)
scp .env       moremeds@192.168.50.47:/opt/xenon/.env
scp web/.env   moremeds@192.168.50.47:/opt/xenon/web.env
ssh moremeds@192.168.50.47 'cd /opt/xenon && \
  sed -i "" -E "s|@192\.168\.50\.47:5432|@host.docker.internal:5432|g; \
                s|^IB_GATEWAY_HOST=.*|IB_GATEWAY_HOST=host.docker.internal|" .env && \
  ( grep -q "^XENON_BROKER_ACCOUNT=" .env || \
    echo "XENON_BROKER_ACCOUNT=<U-prefix-for-live-or-DU-prefix-for-paper>" >> .env )'

# 4a. Edit the placeholder. Without this, the api boot raises
#     `ValueError: app.state.trading_mode and app.state.account must be set`
#     and every IB-touching endpoint 500s — the lifespan guard fails closed.
ssh moremeds@192.168.50.47 'vim /opt/xenon/.env'   # set XENON_BROKER_ACCOUNT to your real account ID

# 5. compose.yml (image: refs only — see template at the bottom of this doc)
scp <local-compose.yml-template> moremeds@192.168.50.47:/opt/xenon/compose.yml
ssh moremeds@192.168.50.47 'mkdir -p /opt/xenon/data'
```

After bootstrap, the mini is ready for `pull → migrator → up`.

## Standard release flow

1. **Cut + tag** on this Mac (PR-merged `release: vX.Y.Z` on master, then
   tag locally and push):

   ```bash
   git fetch origin
   git tag -a vX.Y.Z <merged-master-sha> -m "vX.Y.Z

   <CHANGELOG section>"
   git push origin vX.Y.Z
   ```

2. **`release.yml` `ghcr-push` matrix fires** and publishes 4 images to GHCR
   with `:vX.Y.Z` + `:latest` tags. ⚠️ The `verify` job is currently broken
   on the runner (no Postgres service) — see "Known follow-ups". Until that
   lands, do step 2 manually from this Mac:

   ```bash
   for img in api web realtime migrator; do
     docker tag xenon-${img}:dev ghcr.io/moremeds/xenon-${img}:vX.Y.Z
     docker tag xenon-${img}:dev ghcr.io/moremeds/xenon-${img}:latest
     docker push ghcr.io/moremeds/xenon-${img}:vX.Y.Z
     docker push ghcr.io/moremeds/xenon-${img}:latest
   done
   ```

3. **Pull + restart on the mini**:

   ```bash
   ssh moremeds@192.168.50.47 'PATH=/opt/homebrew/bin:$PATH; cd /opt/xenon && \
     for img in api web realtime migrator; do \
       docker pull ghcr.io/moremeds/xenon-${img}:vX.Y.Z; \
     done && \
     docker-compose --profile migrate run --rm migrator && \
     docker-compose up -d'
   ```

   Note: the mini uses **`docker-compose`** (hyphenated v5.1.3 from brew),
   not the `docker compose` plugin. Use the hyphenated form on the mini;
   the dev Mac's `docker compose` plugin works equivalently.

4. **Smoke** (from the mini):

   ```bash
   ssh moremeds@192.168.50.47 'PATH=/opt/homebrew/bin:$PATH; \
     docker-compose -f /opt/xenon/compose.yml ps && \
     curl -s http://localhost:8321/health && \
     curl -sI http://localhost:3000/sign-in | head -1'
   ```

   Expected:
   - `docker-compose ps` → `api` and `web` `Up (healthy)`, `realtime` `Up`.
   - `/health` → 200 with valid JSON.
     - `ib_gateway.port_listening: true` only when the live Gateway at
       `host.docker.internal:4001` is up. **Currently expected to be `false`** until
       the Gateway is started on the mini.
   - `/sign-in` → 200.

## Auto-start on mini boot

Colima is registered as a brew service so it launches at user login:

```bash
ssh moremeds@192.168.50.47 'PATH=/opt/homebrew/bin:$PATH brew services start colima'
# plist lands at ~/Library/LaunchAgents/homebrew.mxcl.colima.plist
```

Combined with the `restart: unless-stopped` policy on all three long-running
services, a mini reboot recovers the stack without operator action — provided
moremeds either auto-logs in or someone logs in interactively. To check
auto-login:

```bash
ssh moremeds@192.168.50.47 'sudo defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser 2>/dev/null'
```

To disable Colima auto-start later: `brew services stop colima`.

## Rollback

```bash
ssh moremeds@192.168.50.47 'PATH=/opt/homebrew/bin:$PATH; cd /opt/xenon && \
  docker-compose down && \
  for img in api web realtime migrator; do \
    docker pull ghcr.io/moremeds/xenon-${img}:<prev-tag>; \
    docker tag ghcr.io/moremeds/xenon-${img}:<prev-tag> ghcr.io/moremeds/xenon-${img}:v0.0.3; \
  done && \
  docker-compose up -d'
```

(The `tag <prev>:v0.0.3` step makes the existing `compose.yml` (which pins
`v0.0.3`) resolve to the rollback image without editing the file. Cleaner
long-term: `image: ghcr.io/.../xenon-${SVC}:${XENON_VERSION:-latest}`

- `XENON_VERSION` env in `.env` — captured as a follow-up.)

## Logs + diagnostics

```bash
# Live tail of one service:
ssh moremeds@192.168.50.47 'PATH=/opt/homebrew/bin:$PATH; cd /opt/xenon && docker-compose logs -f api'

# Last 100 lines from all services:
ssh moremeds@192.168.50.47 'PATH=/opt/homebrew/bin:$PATH; cd /opt/xenon && docker-compose logs --tail 100'

# Container resource usage:
ssh moremeds@192.168.50.47 'PATH=/opt/homebrew/bin:$PATH; docker stats --no-stream'
```

## Known follow-ups

These tripped us during v0.0.3 and need real fixes before v0.0.4:

1. **`release.yml::verify` job has no Postgres service** → full pytest
   suite errors out with `connection refused` on `127.0.0.1:5432`. The PG
   guard in `conftest.py` skips PG-backed _unit_ tests when offline, but
   the wizard route + schema_scope tests use a different fixture path that
   doesn't honor the skip. Either add a `services: postgres:` block to
   the verify job and seed via alembic, or scope the test selection like
   `ci.yml`'s `python-tests` does.

2. **Mini's GHCR auth is on this Mac's classic PAT.** Worked for
   bootstrap but isn't durable. The mini's own fine-grained PAT
   (`github_pat_11AAI…`) lacks "Packages: read" permission on the linked
   `moremeds/xenon` repo. Either:
   - Regenerate the mini's fine-grained PAT with `Packages: read` permission
     for the `moremeds/xenon` repo, then `gh auth refresh` + re-`docker login`.
   - Or generate a classic PAT with `read:packages` and feed it to
     `docker login` on the mini.

3. **`release.yml::ghcr-push` doesn't pass `NEXT_PUBLIC_*` build-args.**
   The web image baked at CI has empty `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`
   in its bundle. v0.0.3 was pushed manually from this Mac with the PK
   inlined, so it works for now. Fix: add the PK as a GHA secret + thread
   through `build-args:` in the ghcr-push step.

4. **Sizing.** Colima default is 2 CPU / 2 GiB. Tight for 4 simultaneous
   containers; web alone can spike. If OOMKills appear or web becomes
   sluggish: `colima stop && colima start --cpu 4 --memory 6`.

5. **No auto-login on the mini yet** — Colima starts via brew/launchd at _user login_, not at boot. If the mini reboots and no one logs in, the stack stays down. If you want unattended recovery, enable auto-login via System Settings → Users & Groups → "Automatic login" (or `sysadminctl -autologin set …` from a shell with admin sudo). Mind the security trade-off: anyone with physical access skips the login prompt.

6. **`/opt/xenon/logs/`** isn't mounted yet. Container stdout still works
   (`docker-compose logs`), but persistent file logs go nowhere. Add a
   `./logs:/app/logs` bind-mount once persistent logging is needed.

7. **Full live promotion (Phase 1.11)** still waits on a 24h burn-in. Today
   we shipped _half-live_: live trading mode + live IB pointing, but
   `DATABASE_URL=core_dev`. To flip to full prod: edit `/opt/xenon/.env`
   `DATABASE_URL` from `core_dev` to `core`, restart, and stop this Mac's
   API to avoid double-writers.

8. **Compose plugin parity.** Mini uses `docker-compose` (hyphen, v5.1.3
   via brew) because the docker plugin's compose subcommand isn't
   installed. Either install `docker-compose-plugin` for the mini's docker,
   or accept hyphenated as the prod surface.

9. **Container deploy bypasses `dev.sh`'s broker-account resolution.**
   `scripts/infra/dev.sh` reads `XENON_PAPER_ACCOUNT` / `XENON_LIVE_ACCOUNT`
   from `.env` and resolves `XENON_BROKER_ACCOUNT` based on `XENON_TRADING_MODE`
   before exec'ing uvicorn. The Docker compose path doesn't run that script —
   `env_file:` is a straight pass-through. So the container env file must
   set `XENON_BROKER_ACCOUNT` directly (the bootstrap above adds a
   placeholder; you fill in the real value). v0.0.3 deploy hit this exactly:
   api boot succeeded but every IB-backed endpoint raised
   `ValueError: app.state.trading_mode and app.state.account must be set`.
   Proper fix is one of:
   - Docker entrypoint script in `docker/api.Dockerfile` that mirrors
     `dev.sh`'s mode→account resolution before exec'ing uvicorn.
   - Compose-time interpolation: `XENON_BROKER_ACCOUNT=${XENON_LIVE_ACCOUNT}`
     with profile-based overrides for paper. Less robust because compose
     can't pick the mode-specific key cleanly without a profile per mode.

10. **Clerk dev origin only allows `localhost:3000`.** The `pk_test_*` key
    in `web/.env` is bound to a Clerk Development instance which is
    **hardcoded to `localhost`**. Clerk dev instances do not support adding
    arbitrary hosts via the dashboard — the "Domains" UI for a dev instance
    is read-only or limited to localhost variants. Visiting the web app at
    `http://192.168.50.47:3000` triggers Clerk's dev-browser handshake to
    redirect back to `localhost:3000`, breaking sign-in from the LAN.
    Workarounds:
    - **Quick (current state):** append `XENON_DISABLE_AUTH=1` to
      `/opt/xenon/web.env` and `docker-compose up -d --force-recreate web`.
      Bypasses Clerk middleware entirely; combine with default-private
      route gating (PR #90) so portfolio/orders/journal still gate at the
      app layer (you'd need a logged-in session — but with auth bypassed
      that's not enforced; effectively all routes serve while bypass is on).
    - **Network-level auth:** put the deploy on a Tailscale tailnet so
      only your devices can reach it. Drop the public-internet attack
      surface entirely; auth bypass becomes safe again. Does not need a
      domain or Clerk Production.
    - **Real fix:** buy any cheap domain (`.xyz`, `.click` ~ $1-2/yr),
      create a Clerk Production instance, verify the domain via TXT
      record, swap `pk_test_*` → `pk_live_*` in the `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`
      GHA secret, redeploy. Public domain + real auth.

11. **GHCR packages need explicit per-repo ACL — not auto-linked when
    bootstrapped manually.** Hit during v0.0.4 (run
    [25296418217](https://github.com/moremeds/xenon/actions/runs/25296418217)):
    `release.yml::ghcr-push` failed all 4 matrix jobs with
    `denied: permission_denied: write_package`, even though the workflow
    declares `permissions: packages: write` and the run log confirms the
    `GITHUB_TOKEN` was granted `Packages: write`.

    Cause: GHCR has two permission layers. The `GITHUB_TOKEN` scope is
    one; the package's per-repo ACL ("Manage Actions access") is the
    other. When a workflow successfully pushes a package for the first
    time, GHCR auto-links the source repo. The Xenon packages were
    created on 2026-05-03 by **manual `docker push` from this Mac with a
    classic PAT** (see follow-up #2 above), so the auto-link never
    happened. Every subsequent workflow push is denied because
    `moremeds/xenon` is not listed under the package's Actions access.

    There is no public REST or GraphQL API for managing per-repo
    Actions access on user-owned packages — UI only. Fix is one-time per
    package:
    - Visit `https://github.com/users/moremeds/packages/container/xenon-<api|web|realtime|migrator>/settings`
    - Scroll to **Manage Actions access** → **Add Repository**
    - Search `xenon`, select `moremeds/xenon`, role **Write**, save.
    - Repeat for the other 3 packages.

    After the link is in place, re-run the failed jobs:

    ```bash
    gh run rerun <run-id> --failed --repo moremeds/xenon
    ```

    Permanent prevention: don't bootstrap GHCR packages via manual
    `docker push`. Either (a) push the first version from a working
    `release.yml` (auto-links on success), or (b) link the repo
    immediately after the first manual push. Once linked, every future
    tag release publishes without the operator touching anything.

## Reference: `/opt/xenon/compose.yml` template

```yaml
# Pulls 4 images from ghcr.io/moremeds/xenon-* (no build steps).
# Postgres + IB Gateway live host-native; containers reach both via
# host.docker.internal.

services:
  migrator:
    image: ghcr.io/moremeds/xenon-migrator:v0.0.3
    profiles: ["migrate"]
    env_file: [./.env]
    volumes: [./.env:/app/.env:ro]
    extra_hosts: ["host.docker.internal:host-gateway"]
    restart: "no"

  api:
    image: ghcr.io/moremeds/xenon-api:v0.0.3
    env_file: [./.env]
    environment:
      FUTU_OPEND_HOST: host.docker.internal
    ports: ["8321:8321"]
    volumes: [./.env:/app/.env:ro, ./data:/app/data]
    extra_hosts: ["host.docker.internal:host-gateway"]
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8321/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
    restart: unless-stopped

  web:
    image: ghcr.io/moremeds/xenon-web:v0.0.3
    env_file: [./web.env]
    environment:
      XENON_API_URL: http://api:8321
    ports: ["3000:3000"]
    extra_hosts: ["host.docker.internal:host-gateway"]
    depends_on: { api: { condition: service_healthy } }
    healthcheck:
      test: ["CMD", "wget", "--spider", "-q", "http://127.0.0.1:3000/sign-in"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 45s
    restart: unless-stopped

  realtime:
    image: ghcr.io/moremeds/xenon-realtime:v0.0.3
    env_file: [./.env]
    ports: ["8765:8765"]
    volumes: [./.env:/app/.env:ro]
    extra_hosts: ["host.docker.internal:host-gateway"]
    depends_on: { api: { condition: service_healthy } }
    restart: unless-stopped
```

Bump the `v0.0.3` literal to the current release tag at deploy time.
