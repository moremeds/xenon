# Remote Deploy Runbook (Mac mini, Docker)

The authoritative runbook for shipping Xenon to the remote Mac mini
(`moremeds@100.66.147.98`). Covers first-time bootstrap, tagged
releases via GHCR, the controlled release cutover, smoke checks, rollback,
and known follow-ups.

> **Host address:** the mini is reached over **Tailscale** at
> `100.66.147.98` (it was previously the LAN IP `192.168.50.47`; all
> `ssh`/`scp` examples below use the Tailscale address). On this dev Mac a
> `Host macmini` alias in `~/.ssh/config` maps to `moremeds@100.66.147.98`,
> so `ssh macmini …` is equivalent to the `ssh moremeds@100.66.147.98 …`
> commands shown here. SSH runs a non-login shell, so brew tools aren't on
> `PATH` by default — every remote command prefixes `PATH=/opt/homebrew/bin:$PATH`
> (or `export PATH=…`) so `docker`/`docker-compose`/`colima` resolve.

**Supersedes** the former launchd-based mac-mini runbook and its `scripts/deploy/` helpers (pre-containers; removed, see git history).
The old launchd path is no longer the deploy mechanism.

## Topology

| Host                                                      | Where things live                                                                                                                                                                                                                                                                |
| --------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Mac mini                                                  | Colima (Linux/arm64 VM running Docker), 4 app containers (api / web / realtime / migrator) plus Watchtower, Postgres 17 (`:5432`, host-native, schemas `xenon` / `apex` / `events`). IB Gateway may be host-native on the mini or reachable on another machine; see "IB Gateway reachability" below. |
| This dev Mac                                              | Source of truth for builds + tag cuts. Paper IB Gateway local (`127.0.0.1:4002`).                                                                                                                                                                                                |
| GHCR `ghcr.io/moremeds/xenon-{api,web,realtime,migrator}` | Owned by user `moremeds`; tags `:X.Y.Z` (no `v` prefix — see "Tag convention" below) and `:latest`. Visibility "private". Each package has `moremeds/xenon` linked under **Manage Actions access** with role **Write**; without that link, `release.yml::ghcr-push` is denied.   |

Containers reach host-native Mac mini services through `host.docker.internal`
(Colima maps it to the host gateway). That hostname only means "the Docker
host." If IB Gateway is running on a different machine, use that machine's
LAN/Tailscale address instead, or tunnel the Gateway port onto the mini.
Postgres + IB Gateway never run in the Xenon app containers.

### IB Gateway reachability

Set `/opt/xenon/.env` based on where IB Gateway actually runs:

| IB Gateway location                         | `IB_GATEWAY_HOST`                       | Notes                                                                                                                             |
| ------------------------------------------- | --------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| Same Mac mini that runs Docker              | `host.docker.internal`                  | Container-to-host path. `XENON_TRADING_MODE=live` uses port `4001`; `paper` uses `4002`.                                          |
| Another Mac/server on LAN or Tailscale      | that machine's reachable IP or hostname | IB Gateway/TWS must allow API clients from the Mac mini or its Colima VM source IP.                                               |
| Another Mac/server but IB is localhost-only | `host.docker.internal`                  | Run an SSH tunnel on the mini, for example `ssh -N -L 4002:127.0.0.1:4002 <user>@<ib-host>`, then use `XENON_TRADING_MODE=paper`. |

Do not set `IB_GATEWAY_HOST=127.0.0.1` for Docker deployment. Inside a
container, `127.0.0.1` is the container itself.

## Layout on the mini (`/opt/xenon/`)

```
/opt/xenon/
├── compose.yml      # image refs to ghcr.io/moremeds/xenon-*; no build:
├── .env             # api/realtime/migrator env_file
├── web.env          # web env_file, also loaded by api (ANTHROPIC_API_KEY, UW_TOKEN, EXA_API_KEY, XENON_ALLOWED_HOSTS)
└── data/            # bind-mount target for /app/data (mostly empty post-PG migration)
```

`/opt/xenon` is `moremeds:staff`-owned (one-time `sudo mkdir + chown`
during 1.7). All operational files live under it; the source repo is **not**
cloned to the mini.

### Query-API auth env (added 2026-06-17)

The api is **fail-closed** (see `src/xenon/api/CLAUDE.md` § Auth). Set these in
`/opt/xenon/.env` (single source — `compose.yml` injects the token into the web
container via `environment:`, and `env_file: ./.env` gives it to api/realtime/migrator):

| Var                        | Purpose                                                                                                                                                                       |
| -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `XENON_INTERNAL_API_TOKEN` | Shared secret proving `web→api` is the trusted UI (full access). `compose.yml` web service must carry `environment: XENON_INTERNAL_API_TOKEN: ${XENON_INTERNAL_API_TOKEN:-}`. |
| `XENON_QUERY_API_KEY`      | Read-only key for external scripts (send as `X-API-Key`; GET-only query paths). MUST differ from the internal token — the api refuses to boot if equal.                       |

Generate two distinct values with `openssl rand -hex 32`. `XENON_AUTH_ALLOW_DEV_OPEN`
is NEVER set in prod. After editing, recreate the containers (env_file changes need a recreate,
not a restart) with the controlled sequence: pause Watchtower, `docker-compose stop -t 60 api realtime`,
the socket check (§ Standard release flow, step 7), then `docker-compose up -d --pull never api web realtime futu-quotes`
and step 10 for Watchtower. Confirm the api startup log shows
`auth: ENFORCED`.

## First-time bootstrap (one-shot)

Run from this dev Mac unless noted. Prereq: `gh` authenticated as the GitHub
package owner (`moremeds`) with `write:packages,read:packages` scopes on
this Mac, and `read:packages` on the mini.

```bash
# 1. /opt/xenon owned by moremeds (single sudo prompt on the mini)
ssh moremeds@100.66.147.98 'sudo mkdir -p /opt/xenon && sudo chown moremeds:staff /opt/xenon'

# 2. GHCR auth on the mini (use this Mac's classic PAT for now — see
#    "Known follow-ups" for the fine-grained PAT path)
gh auth token | ssh moremeds@100.66.147.98 \
  'PATH=/opt/homebrew/bin:$PATH cat | docker login ghcr.io -u moremeds --password-stdin'

# 3. Start Colima (uses the existing default profile: 2 CPU / 2 GiB / 100 GiB / arm64)
ssh moremeds@100.66.147.98 'PATH=/opt/homebrew/bin:$PATH colima start'

# 4. Push env files (fix DATABASE_URL host, set IB reachability, and append
#    XENON_BROKER_ACCOUNT — see follow-up #9 for why this is required)
scp .env       moremeds@100.66.147.98:/opt/xenon/.env
scp web/.env   moremeds@100.66.147.98:/opt/xenon/web.env
ssh moremeds@100.66.147.98 'cd /opt/xenon && \
  sed -i "" -E "s|@100\.66\.147\.98:5432|@host.docker.internal:5432|g" .env && \
  ( grep -q "^XENON_BROKER_ACCOUNT=" .env || \
    echo "XENON_BROKER_ACCOUNT=<U-prefix-for-live-or-DU-prefix-for-paper>" >> .env )'

# 4a. Edit the placeholders. Without XENON_BROKER_ACCOUNT, the api boot raises
#     `ValueError: app.state.trading_mode and app.state.account must be set`
#     and every IB-touching endpoint 500s — the lifespan guard fails closed.
ssh moremeds@100.66.147.98 'vim /opt/xenon/.env'
# Set XENON_BROKER_ACCOUNT to your real account ID.
# Set IB_GATEWAY_HOST per "IB Gateway reachability" above.

# 5. compose.yml (image: refs only — the reviewed source is deploy/compose.prod.yml)
scp deploy/compose.prod.yml moremeds@100.66.147.98:/opt/xenon/compose.yml
ssh moremeds@100.66.147.98 'mkdir -p /opt/xenon/data'
```

After bootstrap, the mini is ready for `pull → migrator → up`.

## Tag convention

GHCR image tags use **`X.Y.Z` (no `v` prefix)** — `release.yml::ghcr-push`
strips the `v` from the git ref via `${GITHUB_REF_NAME#v}` so the image
tag matches the contents of `VERSION` and `package.json` (which never
have `v`). This matches Docker Hub norms (`postgres:16-alpine`,
`node:20.11`).

| Surface        | Form     | Example  |
| -------------- | -------- | -------- |
| Git tag        | `vX.Y.Z` | `v0.0.4` |
| `VERSION` file | `X.Y.Z`  | `0.0.4`  |
| `package.json` | `X.Y.Z`  | `0.0.4`  |
| GHCR image tag | `X.Y.Z`  | `0.0.4`  |
| GitHub Release | `vX.Y.Z` | `v0.0.4` |

**Legacy exception:** `:v0.0.3` exists on each package because v0.0.3
was bootstrapped via manual `docker push` from the dev Mac (the workflow
was failing at the time). Don't reference `:v0.0.3` in new flows;
treat it as a grandfathered rollback target only. v0.0.4 onwards
follows the convention above.

## Standard release flow

Deploy happens only on a `v*` tag push (merging a PR never deploys). The api, web and
realtime services track `:latest` and Watchtower polls GHCR every 60 s, so an
uncontrolled tag push lets Watchtower recreate the api on its own, with no
stale-socket check. Use this controlled cutover (it was used for v0.11.0 and v0.12.0).
`docker-compose` below is the hyphenated v5 binary from brew (the mini has no `docker compose`
plugin). Every remote command needs `PATH=/opt/homebrew/bin:$PATH`.

1. **Cut and merge.** Follow root `CLAUDE.md` § CI / Release: `scripts/release/cut.sh`
   makes the `release: vX.Y.Z` commit, the commit moves to a `release/vX.Y.Z` branch,
   and the release PR merges when CI is green. Never push `master`. If the release
   changes `deploy/compose.prod.yml`, stage and validate the new file now with
   `deploy/README.md` § Operator rollout checklist, section A. Do not apply it yet:
   Watchtower recreates containers with their old config and never reads `compose.yml`,
   and `docker-compose up -d` would recreate the api before the socket check. Step 8
   applies it.

2. **Pause Watchtower on the mini and confirm it is stopped, BEFORE the tag push**, so it
   cannot race the cutover:

   ```bash
   ssh moremeds@100.66.147.98 'PATH=/opt/homebrew/bin:$PATH; docker stop xenon-watchtower-1 && \
     docker inspect -f "{{.State.Running}}" xenon-watchtower-1'   # must print false
   ```

   Do not continue to step 3 until it prints `false`.

3. **Tag the merge commit and push the tag alone** (never `git push origin master`):

   ```bash
   git fetch origin
   git tag -a vX.Y.Z -m vX.Y.Z <merge-commit-sha>
   git push origin refs/tags/vX.Y.Z
   ```

   The tag push fires `.github/workflows/release.yml`.

4. **Wait for the `Release` run of this tag's commit** and check its conclusion. Do not look at
   "the latest run": another release or a prerelease can be newer. `--commit` filters runs by SHA:

   ```bash
   REV=$(git rev-parse vX.Y.Z^{commit})
   gh run list --repo moremeds/xenon --workflow Release --commit "$REV" --json databaseId,status,conclusion
   gh run watch <databaseId> --repo moremeds/xenon      # then list again
   ```

   The run must show `"conclusion":"success"`. Its jobs:
   - `verify` — full pytest + npm test + typecheck + lint + version-sync
     against an ephemeral Postgres service container.
   - `publish` — GitHub Release at `vX.Y.Z`, body extracted from `CHANGELOG.md`.
   - `ghcr-push` (matrix × 4) — builds + pushes each image at
     `ghcr.io/moremeds/xenon-{api,web,realtime,migrator}:X.Y.Z` (immutable version tag).
   - `promote-latest` — after every `ghcr-push` leg passed, moves `:latest` of all four
     images to `:X.Y.Z` (skipped for prerelease tags). The cutover below does not depend on it.

5. **Pull the release images by version tag, verify them, and alias them to `:latest` locally.**
   Compose uses `:latest` and `--pull never`, so the alias is what runs. A floating
   `:latest` pull could be a different release. `VER` is the version without the `v`; `REV` is the
   commit from step 4. Add `migrator` to the list only when the release carries an Alembic migration.
   Each image must report `VER REV` (its `org.opencontainers.image.version` and `.revision` labels);
   the loop stops on the first failed pull, label mismatch or tag. Continue only when every image printed `ok`:

   ```bash
   ssh moremeds@100.66.147.98 'VER=X.Y.Z REV=<full sha> PATH=/opt/homebrew/bin:$PATH bash -s' <<'EOF'
   for s in api web realtime; do          # add: migrator
     img=ghcr.io/moremeds/xenon-$s
     docker pull "$img:$VER" >/dev/null || { echo "PULL FAILED $s"; exit 1; }
     got=$(docker image inspect "$img:$VER" --format '{{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "org.opencontainers.image.revision"}}')
     [ "$got" = "$VER $REV" ] || { echo "MISMATCH $s: $got"; exit 1; }
     docker tag "$img:$VER" "$img:latest" || { echo "TAG FAILED $s"; exit 1; }
     echo "ok $s $got"
   done
   EOF
   ```

6. **Migrate, only when the release carries an Alembic migration.** Watchtower never runs the
   migrator. Run it once, on the image aliased in step 5, with the gate in
   `deploy/README.md` § D step 3 (`alembic current`, `alembic heads`, one
   `docker-compose --profile migrate run --rm --pull never migrator`). Do not pull or run any other migrator image.

7. **Stop api and realtime with the full grace period, then confirm no leftover gateway sockets.**
   The api disconnects its IB connections on SIGTERM; 60 s matches `stop_grace_period`.
   The socket count must be `0` (`grep -c` prints `0` and exits 1 when nothing matches; that is fine):

   ```bash
   ssh moremeds@100.66.147.98 'PATH=/opt/homebrew/bin:$PATH; cd /opt/xenon && docker-compose stop -t 60 api realtime'
   ssh moremeds@100.66.147.98 'lsof -nP -iTCP:4001 -sTCP:ESTABLISHED 2>/dev/null | grep -c limactl'
   ```

   If the count is not `0`, do not start the api. Follow `docs/runbooks/pr10-order-owner-cutover.md` § 4.

8. **Start the stack on the aliased images.** If step 1 staged a new compose file, move it into place
   first: `cd /opt/xenon && mv compose.new.yml compose.yml` (the backup from section A step 4 stays).
   `--pull never` keeps the images from step 5. Name the app services: a bare `up -d` also starts
   Watchtower, which would replace the aliased images with whatever registry `:latest` is (step 10):

   ```bash
   ssh moremeds@100.66.147.98 'PATH=/opt/homebrew/bin:$PATH; cd /opt/xenon && docker-compose up -d --pull never api web realtime futu-quotes'
   ```

   A transient `[health] realtime /status unreachable` traceback in the api log right after
   `up` is expected: the api starts before realtime.

9. **Smoke** (from the mini):

   ```bash
   ssh moremeds@100.66.147.98 'PATH=/opt/homebrew/bin:$PATH; \
     docker-compose -f /opt/xenon/compose.yml ps && \
     curl -s http://localhost:8321/health && \
     curl -sI http://localhost:3000/ | head -1'
   ```

   Expected:
   - `docker-compose ps` → `api` and `web` `Up (healthy)`, `realtime` `Up`; `watchtower` still stopped until step 10.
   - `/health` → 200 with valid JSON:
     - `status` is `ok` (not `orders_halted`).
     - `ib_pool.sync`, `ib_pool.orders`, `ib_pool.data` each `connected: true`
       (`broker_readiness` is `unknown` until the first open-orders round trip, then `ready`).
     - `orders_owner.client_id` is `4`, `halted` is `false`.
     - `ib_gateway.port_listening: true` only when the configured Gateway
       (`IB_GATEWAY_HOST` plus the port derived from `XENON_TRADING_MODE`) is reachable.
   - `/` → 200.

10. **Resume Watchtower only when registry `:latest` is this release.** Watchtower deploys whatever
    registry `:latest` points at; `--pull never` does not constrain it. For a prerelease (no
    `promote-latest`) or when a newer release moved `:latest`, the registry differs from the
    selected release. Pulling `:latest` only moves the local alias; running containers keep their image.
    Do not push another release tag while a cutover is in progress.

    ```bash
    ssh moremeds@100.66.147.98 'VER=X.Y.Z REV=<full sha> PATH=/opt/homebrew/bin:$PATH bash -s' <<'EOF'
    for s in api web realtime; do
      img=ghcr.io/moremeds/xenon-$s
      docker pull "$img:latest" >/dev/null || { echo "PULL FAILED $s"; exit 1; }
      got=$(docker image inspect "$img:latest" --format '{{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "org.opencontainers.image.revision"}}')
      if [ "$got" != "$VER $REV" ]; then
        docker tag "$img:$VER" "$img:latest" || { echo "RESTORE FAILED $s: local :latest is NOT $VER; do not recreate any service"; exit 2; }
        echo "KEEP WATCHTOWER STOPPED: registry :latest of $s is $got (local :latest restored to $VER)"; exit 1
      fi
    done
    cd /opt/xenon && docker-compose up -d --pull never watchtower && echo "watchtower resumed"
    EOF
    ```

    If it prints `KEEP WATCHTOWER STOPPED`, leave Watchtower stopped (the local alias is restored to
    `:VER`) until a stable release is promoted to `:latest`, then repeat this step. If it prints
    `RESTORE FAILED`, do not recreate any service: rerun step 5 for that image (it re-tags `:VER` and
    must print `ok`) before anything else.

   The full post-deploy checks are in `docs/runbooks/pr10-order-owner-cutover.md` § 5.

## Auto-start on mini boot

Colima is registered as a brew service so it launches at user login:

```bash
ssh moremeds@100.66.147.98 'PATH=/opt/homebrew/bin:$PATH brew services start colima'
# plist lands at ~/Library/LaunchAgents/homebrew.mxcl.colima.plist
```

Combined with the `restart: unless-stopped` policy on all three long-running
services, a mini reboot recovers the stack without operator action — provided
moremeds either auto-logs in or someone logs in interactively. To check
auto-login:

```bash
ssh moremeds@100.66.147.98 'sudo defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser 2>/dev/null'
```

To disable Colima auto-start later: `brew services stop colima`.

## Rollback

The services run on `:latest`, so a rollback re-points `:latest` at the previous version
on the mini and restarts. Migrations are not reverted. Every release tag keeps its
immutable `:X.Y.Z` image (`PREV` below is the previous version without the `v`; for the legacy
v0.0.3 target use `v0.0.3`, see "Tag convention").

1. Pause Watchtower, or it pulls the bad `:latest` again: `docker stop xenon-watchtower-1`.
2. Pull and verify the previous image (labels and digest as in `deploy/README.md` § D step 2),
   then re-point the local alias for each service that changed:

   ```bash
   docker pull ghcr.io/moremeds/xenon-api:PREV
   docker tag ghcr.io/moremeds/xenon-api:PREV ghcr.io/moremeds/xenon-api:latest   # same for web / realtime
   ```

3. `docker-compose stop -t 60 api realtime`, run the stale-socket check (step 7 of the
   release flow), then `docker-compose up -d --pull never api web realtime futu-quotes`. Name the
   services: a bare `up -d` also recreates Watchtower, which would pull the bad `:latest` again.
4. Resume Watchtower (`docker start xenon-watchtower-1`) only after the registry `:latest`
   is no longer the bad release (publish a fixed release first).

Rolling back across PR-10 (single order owner) has extra steps (order inventory, ownership):
`docs/runbooks/pr10-order-owner-cutover.md` § 7.

## Logs + diagnostics

```bash
# Live tail of one service:
ssh moremeds@100.66.147.98 'PATH=/opt/homebrew/bin:$PATH; cd /opt/xenon && docker-compose logs -f api'

# Last 100 lines from all services:
ssh moremeds@100.66.147.98 'PATH=/opt/homebrew/bin:$PATH; cd /opt/xenon && docker-compose logs --tail 100'

# Container resource usage:
ssh moremeds@100.66.147.98 'PATH=/opt/homebrew/bin:$PATH; docker stats --no-stream'
```

## Known follow-ups

Open items as of v0.0.4. Items resolved during the v0.0.4 cut have been
removed (`release.yml::verify` Postgres service, GHCR per-package ACL —
landed via PR #91 + PR #94).

1. **Mini's GHCR auth is on this Mac's classic PAT.** Worked for
   bootstrap but isn't durable. The mini's own fine-grained PAT
   (`github_pat_11AAI…`) lacks "Packages: read" permission on the linked
   `moremeds/xenon` repo. Either:
   - Regenerate the mini's fine-grained PAT with `Packages: read` permission
     for the `moremeds/xenon` repo, then `gh auth refresh` + re-`docker login`.
   - Or generate a classic PAT with `read:packages` and feed it to
     `docker login` on the mini.

2. **Sizing.** Colima default is 2 CPU / 2 GiB. Tight for 4 simultaneous
   containers; web alone can spike. If OOMKills appear or web becomes
   sluggish: `colima stop && colima start --cpu 4 --memory 6`.

3. **No auto-login on the mini yet** — Colima starts via brew/launchd at _user login_, not at boot. If the mini reboots and no one logs in, the stack stays down. If you want unattended recovery, enable auto-login via System Settings → Users & Groups → "Automatic login" (or `sysadminctl -autologin set …` from a shell with admin sudo). Mind the security trade-off: anyone with physical access skips the login prompt.

4. **`/opt/xenon/logs/`** isn't mounted yet. Container stdout still works
   (`docker-compose logs`), but persistent file logs go nowhere. Add a
   `./logs:/app/logs` bind-mount once persistent logging is needed.

6. **Compose plugin parity.** Mini uses `docker-compose` (hyphen, v5.1.3
   via brew) because the docker plugin's compose subcommand isn't
   installed. Either install `docker-compose-plugin` for the mini's docker,
   or accept hyphenated as the prod surface.

7. **Container deploy bypasses `dev.sh`'s broker-account resolution.**
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

## Resolved during v0.0.4

Kept here as institutional memory — these are no longer open.

- **`release.yml::verify` had no Postgres service** → fixed in PR #91:
  ephemeral `postgres:16-alpine` service container + alembic step
  precede pytest in the verify job.
- **GHCR packages need explicit per-repo ACL — not auto-linked when
  bootstrapped manually.** Hit during the first v0.0.4 attempt
  ([run 25296418217](https://github.com/moremeds/xenon/actions/runs/25296418217)):
  `ghcr-push` was denied `write_package` even with `Packages: write`
  on the token. Cause: GHCR has two permission layers — the
  `GITHUB_TOKEN` scope **and** the package's per-repo ACL ("Manage
  Actions access"). When a workflow pushes a package for the first
  time, GHCR auto-links the source repo. The Xenon packages were
  bootstrapped via manual `docker push` from this Mac on 2026-05-03,
  bypassing the auto-link. Resolved by adding `moremeds/xenon` with
  Write to each package's "Manage Actions access" page (UI only — no
  public API). Permanent prevention: don't bootstrap via manual
  `docker push`. Push the first version from a working `release.yml`
  so GHCR auto-links on success, or link the repo immediately after
  any manual bootstrap.

## Reference: `/opt/xenon/compose.yml`

The reviewed source is `deploy/compose.prod.yml`; `/opt/xenon/compose.yml` must match it
(`diff deploy/compose.prod.yml /opt/xenon/compose.yml`). Change prod by changing that file in a
PR first, then stage it with `deploy/README.md` § Operator rollout checklist, section A.
