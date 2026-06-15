# Mac Mini Production Runbook

> **⚠️ Superseded by [`docs/runbooks/remote-deploy.md`](remote-deploy.md)** as
> of v0.0.3 (2026-05-04). The Mac mini deploy is now Docker / Colima-based;
> the launchd path documented below is no longer the operative deploy
> mechanism. Kept here as a reference for the pre-containerization design
> until the launchd plists / `scripts/deploy/macmini-*.sh` helpers are
> retired in a follow-up.

The single source of truth for the Mac mini that hosts Xenon production.
Covers first-time bootstrap, recurring tagged deploys, database promotion from
the laptop, rollback, health checks, and troubleshooting.

Paired scripts (all in `scripts/deploy/`):

| Script                    | Where it runs | When                                  |
| ------------------------- | ------------- | ------------------------------------- |
| `macmini-bootstrap.sh`    | Mac mini      | Once, on a fresh host                 |
| `macmini-data-promote.sh` | **Laptop**    | Once, after bootstrap, to mirror data |
| `macmini-prod.sh`         | Mac mini      | Every tagged release                  |

Paired launchd templates (rendered into `~/Library/LaunchAgents/`):
`config/templates/com.xenon.{api,web,ib-realtime}.plist.template`.

---

## 1. Operating Model

| Host     | Role                                                                                                                                                                        |
| -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Mac mini | Production. Runs Next.js (3000), FastAPI (8321), IB realtime relay (8765), live IB Gateway/TWS (4001 live / 4002 paper), Postgres `xenon_db`. **Only live-trading writer.** |
| Laptop   | Development. Tests, paper workflows, releases. Pushes tagged builds to the Mac mini; never writes production state directly.                                                |

Live raw IB access stays private to the Mac mini. Laptop dev may use paper IB
directly. If laptop dev later needs live market data, expose a read-only Xenon
data stream after a separate hardening pass — do **not** expose the raw live IB
API port as the default path.

Production services:

| Service           | Command                                                            | Port        | Exposure                       |
| ----------------- | ------------------------------------------------------------------ | ----------- | ------------------------------ |
| Next.js web       | `npm run start -- -H 127.0.0.1 -p 3000`                            | 3000        | LAN/Tailscale or reverse proxy |
| FastAPI backend   | `uv run uvicorn xenon.api.server:app --host 127.0.0.1 --port 8321` | 8321        | localhost only                 |
| IB realtime relay | `node scripts/infra/ib_realtime/ib_realtime_server.js`             | 8765        | localhost initially            |
| IB Gateway/TWS    | managed by IBC outside app release                                 | 4001 / 4002 | live private to Mac mini       |

App services run under `launchd` (KeepAlive on crash). IB Gateway is supervised
separately by IBC because it has its own login, 2FA, and trading-session
lifecycle.

---

## 2. Pre-Flight (manual, ~10 min)

Do these on the Mac mini before running the bootstrap script:

1. **macOS first-run** — sign in with Apple ID, enable FileVault, enable
   automatic security updates.
2. **Tailscale** — install the app and join your tailnet. The laptop reaches
   the Mac mini over Tailscale for SSH and `macmini-data-promote.sh`.
3. **Remote login** — System Settings → General → Sharing → enable Remote Login.
4. **GitHub SSH key**:
   ```bash
   ssh-keygen -t ed25519 -C "$(whoami)@$(hostname)"
   cat ~/.ssh/id_ed25519.pub        # paste at https://github.com/settings/keys
   ```
5. **IBKR mobile app** — install on your phone for 2FA approval on the first IB
   Gateway login.

---

## 3. One-Click Bootstrap

From a Terminal on the Mac mini:

```bash
# Option A — pull standalone, before the repo is cloned
curl -fsSL https://raw.githubusercontent.com/lcxxcllcx/xenon/master/scripts/deploy/macmini-bootstrap.sh -o /tmp/bootstrap.sh
bash /tmp/bootstrap.sh

# Option B — already cloned
cd ~/projects/xenon
./scripts/deploy/macmini-bootstrap.sh
```

Env-var overrides (defaults shown):

```bash
XENON_HOME=~/projects/xenon          # repo location
XENON_REPO=git@github.com:lcxxcllcx/xenon.git
XENON_BRANCH=master                  # branch or tag
XENON_TRADING_MODE=paper             # initial mode embedded in plists
XENON_PG_VERSION=17
XENON_NODE_VERSION=22
XENON_DB_NAME=xenon_db
XENON_DB_ROLE=xenon_app
```

The script is **idempotent**. Re-running after a partial failure picks up where
it left off. Each step prints `✓` (done), `↩ skip` (already done), or fails
loudly with a `FAIL` reason.

### What it does, in order

1. **Preflight** — verify macOS, Apple Silicon
2. **Xcode CLT** — triggers the GUI installer if missing; you re-run after it finishes
3. **Homebrew** — install if missing, source `brew shellenv`
4. **Brew packages** — `uv`, `node@22`, `postgresql@17`, `git`, `gh`
5. **Postgres service** — `brew services start`, wait for socket
6. **DB role + DB** — creates `xenon_app` (prompts for password) and `xenon_db`
7. **Repo clone** — `git@github.com:lcxxcllcx/xenon.git` → `$XENON_HOME`,
   checkout `$XENON_BRANCH`. Aborts with SSH-key guidance if clone fails
8. **`.env` scaffold** — copies from `.env.example`, injects `DATABASE_URL`
   and a fresh `XENON_QUOTE_TOKEN_SECRET`. Tells you which fields still need
   manual values
9. **`uv sync --frozen --extra test`** — Python deps from the lockfile
10. **`alembic upgrade head`** — schema to HEAD
11. **`npm install` + `npm run build`** — Next.js production build
12. **launchd plists** — render the three templates from `config/templates/`
    into `~/Library/LaunchAgents/`, `launchctl load` each
13. **Health checks** — `curl 127.0.0.1:8321/health` and `127.0.0.1:3000` with
    retry; report UP/DOWN

### What it does NOT do

- Apple ID / FileVault / iCloud
- GitHub SSH key generation (it tells you to do this if clone fails)
- IB Gateway / TWS / IBC install — separate `scripts/infra/setup_ibc.sh`
- Secret values (Clerk, R2, Anthropic, UW, MenthorQ) — you fill `.env`
- Database content — see § 4

---

## 4. Post-Bootstrap (in order)

### 4.1. Fill the secrets

Bootstrap creates `.env` and `web/.env` with empty fields. Fill them, then
kickstart the services so they pick up the new values:

```bash
$EDITOR ~/projects/xenon/.env       # CLERK_*, R2_*, ALLOWED_USER_IDS, IB_FLEX_TOKEN
$EDITOR ~/projects/xenon/web/.env   # ANTHROPIC_API_KEY, UW_TOKEN, EXA_API_KEY (optional), NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY, CLERK_SECRET_KEY

launchctl kickstart -k gui/$UID/com.xenon.api
launchctl kickstart -k gui/$UID/com.xenon.web
launchctl kickstart -k gui/$UID/com.xenon.ib-realtime
```

Reference: `CLAUDE.md` § Credentials for the canonical list of which secret
goes where.

### 4.2. Promote data from the laptop (full mirror)

This is the cutover step: **laptop is the de-facto production until this runs**;
after it runs the Mac mini is.

```bash
# on the LAPTOP (not the Mac mini):
cd ~/projects/xenon

# stop laptop writers first — promote refuses to run if any are listening
launchctl unload ~/Library/LaunchAgents/com.xenon.api.plist 2>/dev/null || true
pkill -f "uvicorn xenon.api.server" || true
pkill -f "next-server" || true
pkill -f "ib_realtime_server.js" || true

./scripts/deploy/macmini-data-promote.sh xenon@<macmini-tailscale-host> --confirm
```

What it does:

1. Asserts no laptop writer is listening on 8321/3000/8765 (mid-write snapshot guard)
2. Runs `pg_dump -Fc --no-owner --no-acl` against local `xenon_db`
3. Saves a timestamped archive to `data/backups/xenon_db-<TS>.dump`
4. Streams over SSH and runs `pg_restore --clean --if-exists --no-owner --no-acl`
5. Prints row counts on the target so you can eyeball-verify

`-Fc` (custom format) is forward-compatible across Postgres major versions, so
a future laptop upgrade to Postgres 17 won't break this path.

After promote, the laptop's DB is now stale. Either repurpose it as paper-only
(re-tag rows via `XENON_TRADING_MODE=paper` and a one-shot UPDATE) or wipe and
let alembic recreate from scratch.

### 4.3. Install IB Gateway + IBC

For unattended 24/7 operation, IB Gateway needs IBC to handle restarts. The
first login is interactive (2FA on the IBKR mobile app):

```bash
cd ~/projects/xenon
./scripts/infra/setup_ibc.sh
```

After it's working, `config/com.xenon.ibc-gateway.plist` keeps the gateway
alive across daily restarts.

### 4.4. Smoke test (paper mode)

Bootstrap defaults to paper. Verify before flipping live:

```bash
curl -fsS http://127.0.0.1:8321/health | jq .
curl -fsS http://127.0.0.1:3000        # should return HTML
tail -n 50 ~/projects/xenon/logs/api.err.log
```

Open the web UI in a browser via Tailscale, run the wizard against paper, place
a paper order, watch it flow through end-to-end. **No live trading until the
four gates in `CLAUDE.md` are demonstrably green on this host.**

### 4.5. Flip to live (when ready)

```bash
$EDITOR ~/projects/xenon/.env       # XENON_TRADING_MODE=live

# also update the plists' embedded mode (re-render via bootstrap, idempotent):
XENON_TRADING_MODE=live ./scripts/deploy/macmini-bootstrap.sh
```

The second bootstrap run skips everything except the plist render+reload.

---

## 5. Recurring Deploys

Production deploys only from immutable version tags.

```text
push tag vX.Y.Z
  → GitHub Actions Release workflow runs
  → Python and web verification pass
  → GitHub Release is published
  → operator (or future GHA deploy job) SSHes to Mac mini
  → ./scripts/deploy/macmini-prod.sh vX.Y.Z
  → uv sync · npm install · npm run build · alembic upgrade head
  → launchctl kickstart all three services
  → health checks pass (or auto-rollback to previous tag)
```

Do **not** deploy every `master` push to the live-trading host.

### 5.1. Deploy command

```bash
./scripts/deploy/macmini-prod.sh vX.Y.Z
```

Behavior:

- Records the current tag as `PREV_TAG` for rollback
- Refuses to run if working tree is dirty
- `git fetch --tags && git checkout $TAG`
- `uv sync --frozen --extra test`
- `npm install` at root and `web/`, `npm run build`
- `alembic upgrade head` (forward-only)
- `launchctl kickstart -k` for `com.xenon.{api,web,ib-realtime}`
- Health-check `127.0.0.1:8321/health` and `127.0.0.1:3000` with retry
- **Auto-rollback to `PREV_TAG` on health failure** — full rebuild + re-kickstart;
  appends the result to `logs/deploy.log`

### 5.2. Manual rollback

```bash
./scripts/deploy/macmini-prod.sh vX.Y.<previous>
```

Same script, previous tag. Do **not** rollback by editing files in place — the
host should always run a known git tag.

### 5.3. Future GitHub Actions deploy job

Open follow-up: extend `.github/workflows/release.yml` after `verify`/`publish`:

```yaml
deploy-production:
  needs: [verify, publish]
  environment: production
  runs-on: ubuntu-latest
  steps:
    - name: Deploy to Mac mini
      run: ssh xenon@mac-mini 'cd ~/projects/xenon && ./scripts/deploy/macmini-prod.sh ${{ github.ref_name }}'
```

Use GitHub `environment: production` with manual approval at first. Keep
production secrets on the Mac mini, not in GHA, unless a specific secret is
required to connect.

---

## 6. launchd Service Architecture

Three long-running agents in `~/Library/LaunchAgents/`. All use:

- `RunAtLoad=true` — start at session login
- `KeepAlive={SuccessfulExit=false, Crashed=true}` — restart only on crash, not
  on clean shutdown (so `kickstart` during deploy doesn't fight a relaunch)
- `ThrottleInterval=10` — cap relaunches at 1 per 10 s (prevents tight loops on
  a bad `.env`)
- `ProcessType=Background`
- Stdout/stderr to `logs/<service>.{out,err}.log`

Templates live in `config/templates/` and are rendered with `sed` substituting
`__PROJECT_DIR__`, `__USER__`, `__BREW_PREFIX__`, `__UV_BIN__`, `__NODE_BIN__`,
`__NPM_BIN__`, `__TRADING_MODE__`. Re-running bootstrap re-renders and reloads
the plists, picking up changed binary paths or trading mode.

| Label                   | Service                                   | Port      |
| ----------------------- | ----------------------------------------- | --------- |
| `com.xenon.api`         | uvicorn → FastAPI                         | 8321      |
| `com.xenon.web`         | next start                                | 3000      |
| `com.xenon.ib-realtime` | node ib_realtime_server.js                | 8765      |
| `com.xenon.ibc-gateway` | IBC → IB Gateway (separate, pre-existing) | 4001/4002 |

Inspect:

```bash
launchctl list | grep com.xenon
launchctl print gui/$UID/com.xenon.api | head -40
tail -f ~/projects/xenon/logs/api.err.log
```

---

## 7. Health Checks

Minimum after every deploy:

```bash
curl -f http://127.0.0.1:8321/health
curl -f http://127.0.0.1:3000
```

Operational checks before market use:

- FastAPI `/health` reports IB Gateway and IB pool health
- Clerk production auth is configured (`CLERK_JWKS_URL`, `CLERK_ISSUER`)
- `ALLOWED_USER_IDS` contains production user IDs
- live/paper mode is intentional (`XENON_TRADING_MODE` matches IB Gateway port)
- no dev auth bypass enabled (`XENON_DISABLE_AUTH` unset)
- `logs/` is writable
- nightly `pg_dump` backup is current

---

## 8. Database Strategy

Detailed design: [`../architecture/production-database-strategy.md`](../architecture/production-database-strategy.md).

Short version:

- Mac mini is the production writer for live execution state
- Local dev never writes production execution tables directly
- Postgres owns indexed operational and metadata state
- R2 holds large immutable parquet/artifact objects behind explicit Postgres
  manifests

Every execution and portfolio row carries `(broker, account_env, broker_account)`
columns (alembic revision `27a1d085c2cd`) so paper and live data never blend
in a shared Postgres. Resolve scope via `AccountScope`
(`src/xenon/execution/account_scope.py`); FastAPI depends on
`get_account_scope`, sync subprocesses read `XENON_TRADING_MODE` +
`XENON_BROKER_ACCOUNT`.

Backups: `data/backups/` locally on the Mac mini (one-shot via the data-promote
script). Open follow-up: nightly `pg_dump` → R2.

---

## 9. Troubleshooting

| Symptom                                                | First check                                                                                     |
| ------------------------------------------------------ | ----------------------------------------------------------------------------------------------- |
| `git clone` fails during bootstrap                     | GitHub SSH key not added — pre-flight § 2.4                                                     |
| `uv sync` errors on a wheel                            | `xcode-select -p` returns nothing — re-run after CLT install completes                          |
| `alembic upgrade head` errors                          | `DATABASE_URL` malformed in `.env`, or Postgres not running (`brew services list`)              |
| Health check fails for `api`                           | `tail logs/api.err.log` — usually a missing `.env` value                                        |
| Health check fails for `web`                           | `tail logs/web.err.log` — usually missing `web/.env` value or build cache mismatch              |
| launchd agent won't load                               | `launchctl error <exit-code>` — check XML validity in the rendered plist                        |
| Service flaps every 10s                                | bad `.env` causes startup crash → `KeepAlive.Crashed=true` retries; fix `.env` then `kickstart` |
| `macmini-data-promote.sh` refuses with "port X in use" | a laptop writer is still listening; stop it before promoting                                    |
| Deploy auto-rolled-back                                | check `logs/deploy.log` for the failed tag, then `tail logs/api.err.log` for the cause          |

---

## 10. Why These Defaults

- **`~/projects/xenon`** matches the laptop layout, so muscle memory transfers.
- **Postgres 16** is the current Homebrew default and matches the laptop. The
  pg_dump custom format is forward-compatible, so a future Postgres 17 upgrade
  on the laptop won't break promote.
- **Paper mode at first boot** keeps Gate 4 (no naked shorts) and Gate 3
  (sizing) inert until you've smoke-tested. Flipping to live is one env-var
  edit + one bootstrap re-run, but it's intentionally not the default.
- **IB Gateway separated from bootstrap** — its install needs interactive 2FA
  which can't be automated, and treating it as a separate concern lets the
  bootstrap be re-run without disturbing a working gateway session.
- **`KeepAlive` as a dict (`SuccessfulExit=false, Crashed=true`)** rather than
  plain `true` — respects deploy-time clean shutdowns, only fights crashes.
- **`pg_dump -Fc` over plain SQL** — streams compressed (~10× smaller),
  supports `pg_restore --clean --if-exists` for idempotent restore, and lets
  you selectively skip tables if cutover grows complex.

---

## 11. Open Follow-Ups

- Add `deploy-production` job to `.github/workflows/release.yml` so tag pushes
  SSH into the Mac mini and invoke `scripts/deploy/macmini-prod.sh`.
- Decide whether Postgres stays on the Mac mini long-term or splits to a
  dedicated DB host once execution volume grows.
- Design R2 dataset manifest tables for existing parquet historical data.
- Harden a read-only live market data stream if laptop dev needs live data
  from production.
- Nightly `pg_dump` backup → R2 (currently dump-only into `data/backups/`).
- Releases-directory hardening: keep a `releases/<tag>/` directory of timestamped
  checkouts and a `current` symlink for instant pointer-flip rollback (current
  rollback re-runs the full build cycle).
