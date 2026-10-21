# deploy/ — production deploy truth

`compose.prod.yml` mirrors `/opt/xenon/compose.yml` on the macmini. `jobs/` holds the scheduled-job entries (launchd plists + `crontab.example`). The macmini system zone is Asia/Hong_Kong; every job fires at both candidate HKT times and runs only in the matching New York hour.

## Jobs

| Job | Entry | Runs from | Schedule (ET) | Lock | Creds | Volumes | Logs |
|---|---|---|---|---|---|---|---|
| nav-flex-refresh | `xenon-nav-flex-refresh` (pyproject script, `xenon.jobs.nav_flex_refresh:main`) | api image via `/bin/sh -c '[ "$(TZ=America/New_York date +%H)" = 17 ] || exit 0; exec docker-compose -f /opt/xenon/compose.yml run --rm -T --no-deps --name xenon-job-nav-flex-refresh --label ...watchtower.enable=false api xenon-nav-flex-refresh` | daily 17:30 ET = 05:30 HKT (EDT) / 06:30 HKT (EST); fires at both, `/bin/sh -c` guard runs only when NY hour is 17 | launchd single instance + fixed container name (Docker name conflict); dead same-name container removed first with `docker rm` (no force) | api `env_file` (.env, web.env): DATABASE_URL, IB_FLEX_TOKEN, IB_FLEX_NAV_QUERY_ID, XENON_TRADING_MODE=live, XENON_LIVE_ACCOUNT | api service: .env->/app/.env ro, data->/app/data | /var/log/xenon/nav-flex-refresh.{stdout,stderr}.log |
| refresh-core-test | `scripts/infra/refresh-core-test.sh` (pg_dump core_dev, pg_restore core_test) | HOST-NATIVE: needs pg_dump/pg_restore/psql, the api image has only libpq5. Script copied to /opt/xenon/bin (no checkout) | daily 04:00 ET = 16:00 HKT (EDT) / 17:00 HKT (EST); fires at both, guard runs only when NY hour is 04 | the script takes an atomic mkdir lock itself (all paths: launchd, cron, manual); second run exits 3; script refuses SRC==DST and any DST that is core_dev or lacks "test" | PGPASSFILE=/opt/xenon/.pgpass-refresh (chmod 600); does not read .env | none | /var/log/xenon/refresh-core-test.{log,stdout.log,stderr.log} |
| Futu nightly deal sync | CLI `xenon-futu-history-sync` exists, BUT the 16:30 ET weekday run is already an in-process asyncio loop in the api (`server.py::_maybe_start_futu_history_loop`, default on, `XENON_FUTU_HISTORY_LOOP=0` disables) | not scheduled externally | n/a | n/a | n/a | n/a | api container logs |
| monitor daemon | out of scope (PR-17 moves it into FastAPI) | | | | | | |

Files: `deploy/jobs/com.xenon.nav-flex-refresh.plist`, `deploy/jobs/com.xenon.refresh-core-test.plist`, `deploy/jobs/crontab.example` (cron alternative; use ONE scheduler per job).

Why `run --rm api` and not a new service: api already has the right env_file, volumes, and image. No ofelia/cron container, no new service. `run` does not publish ports (no `--service-ports`).
Why the Watchtower label override: `run` copies the service labels, including `watchtower.enable=true`. Without the override Watchtower could recreate the one-off container mid-run on a new image.


## IB clientId blocks (PR-10 deployment contract)

| Gateway | Environment | `XENON_IB_CLIENT_ID_BASE` | Host / supervisor |
|---|---|---|---|
| Live, mini `:4001` | prod | `0` (this compose file) | `/opt/xenon` Docker stack, Watchtower |
| Live, mini `:4001` (read-only) or paper, laptop `:4002` | dev | `100` (`scripts/infra/dev.sh`, `docker-compose.yml`) | MacBook |
| any | second dev stack | another base (`200`-`800`, step 100) | — or stopped |

Pool `sync`/`orders`/`data` = base+3/4/5 (`orders` places, cancels and modifies every Xenon order), relay base+10..19, `auto` base+20..49 (read/data only). The api refuses a base that does not match its database. `api` has `stop_grace_period: 60s` and Watchtower `WATCHTOWER_TIMEOUT: 60s` so the api can disconnect from IB before it is killed. Cutover, stale-socket check and `ORDERS_HALTED` recovery: `docs/runbooks/pr10-order-owner-cutover.md`.

## Operator rollout checklist

Names and booleans only: never print secret values. `docker-compose` below is `/opt/homebrew/bin/docker-compose`; run `export PATH=/opt/homebrew/bin:$PATH` first. The repo copy is on the laptop; the mini path is `/opt/xenon`.

A. Staged compose validation and backup
1. From the laptop: `scp deploy/compose.prod.yml macmini:/opt/xenon/compose.new.yml`.
2. On the mini, first put the webhook variable in place. Check whether it is already defined, printing only a count: `grep -c '^WATCHTOWER_NOTIFICATION_URL=' /opt/xenon/.env` (expect `1`). If it prints `0`, add `WATCHTOWER_NOTIFICATION_URL=<current ntfy URL from the live compose>` to `/opt/xenon/.env` (an unquoted value; never echo it; step 3 checks the effective value). Also confirm `GHCR_USER`, `GHCR_PAT`, `XENON_INTERNAL_API_TOKEN` exist there (names only: `grep -c '^NAME=' .env`).
3. `cd /opt/xenon && docker-compose -f compose.new.yml config -q` (must exit 0; needs `.env`, `web.env` and the variables from step 2). Then check that the effective webhook is a non-empty string, printing only a boolean: `docker-compose -f compose.new.yml config --format json | jq -e '.services.watchtower.environment.WATCHTOWER_NOTIFICATION_URL | type == "string" and (gsub("\\s"; "") | length > 0)'` prints `true` and exits 0. Stop on `false` or a non-zero exit. Then check the PR-10 values as rendered, printing only a boolean:
   `docker-compose -f compose.new.yml config --format json | jq -e '(.services.api.environment.XENON_IB_CLIENT_ID_BASE == "0") and (.services.realtime.environment.XENON_IB_CLIENT_ID_BASE == "0") and (.services.api.environment.IB_REALTIME_STATUS_URL == "http://realtime:8765/status") and (.services.watchtower.environment.WATCHTOWER_TIMEOUT == "60s") and (.services.api.stop_grace_period | IN("60s", "1m0s"))'` prints `true`. (The rendered form of the 60s duration differs between docker-compose versions; both forms are accepted.) Stop on `false`.
4. Back up: `cp -p compose.yml compose.yml.bak-$(date +%Y%m%d%H%M)`.
5. Diff without printing the webhook URL: `diff <(grep -v WATCHTOWER_NOTIFICATION_URL compose.yml) <(grep -v WATCHTOWER_NOTIFICATION_URL compose.new.yml)`. Expected differences (PR-10 staging), and nothing else:
   - header and comment lines;
   - `api.environment.XENON_IB_CLIENT_ID_BASE: "0"` and `realtime.environment.XENON_IB_CLIENT_ID_BASE: "0"` added;
   - `api.environment.IB_REALTIME_STATUS_URL: http://realtime:8765/status` added;
   - `api.stop_grace_period: 60s` added;
   - `watchtower.environment.WATCHTOWER_TIMEOUT: 60s` added;
   - `WATCHTOWER_NOTIFICATION_URL` becomes `${WATCHTOWER_NOTIFICATION_URL}` interpolation (filtered out of this diff by design; step 3 checked the effective value).

   Explain every other difference before you continue; if you cannot, stop.
6. Only then: `mv compose.new.yml compose.yml && docker-compose up -d --pull never` (`--pull never`: no image changes in this step), and do section A only when no release is in flight.

B. Rollback
- `cp -p compose.yml.bak-<stamp> compose.yml && docker-compose up -d`. This restores the previous definition. Images are `:latest`; Watchtower still tracks them, so a rollback of the file does not pin an image.
- Rolling back across PR-10 (single order owner): follow `docs/runbooks/pr10-order-owner-cutover.md` § 7 (pause Watchtower, dispose of orders placed by clientId 4, pin and verify the previous image, stop/check sockets/start).
- Jobs: `launchctl bootout gui/$(id -u)/com.xenon.nav-flex-refresh` and the same for `com.xenon.refresh-core-test`; `rm` the two plists from `~/Library/LaunchAgents`.

C. Post-up checks
- `docker-compose ps`: api, web, realtime `healthy`/`Up`, watchtower `Up`.
- `curl -fsS http://127.0.0.1:8321/health` and `curl -fsSI http://127.0.0.1:3000/ | head -1`.
- Watchtower: `docker logs xenon-watchtower-1 --tail 20` shows no auth or webhook error. Confirm the ntfy topic receives a notification on the next real update (the template is silent on no-op cycles); one way: after the next release, check the topic.

D. Migrator/schema gate (every release that carries a migration)
`release.yml` pushes `:VER` and then promotes `:latest` for api, web, realtime and migrator in one run. Watchtower recreates api as soon as `:latest` moves, and it never runs the migrator. So hold Watchtower for the whole release and migrate before any api is recreated. Never rely on Watchtower for schema.
1. BEFORE pushing the release tag: `docker stop xenon-watchtower-1`. While it is stopped nothing deploys; the running api stays on the old image.
2. After `release.yml` is green, pull and verify the immutable version tag (`VER` = the release version without the `v`):
   `docker pull ghcr.io/moremeds/xenon-migrator:VER`
   `docker image inspect ghcr.io/moremeds/xenon-migrator:VER --format '{{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "org.opencontainers.image.revision"}} {{json .RepoDigests}}'`
   - The version label must equal `VER`, and the revision must equal the release commit (`git rev-parse vVER^{commit}` on the laptop).
   - The digest in `RepoDigests` must equal `docker buildx imagetools inspect ghcr.io/moremeds/xenon-migrator:VER` (or the migrator digest in `/opt/xenon/release-manifest.json`, if that file lists it).
3. Point the compose migrator at the verified image: `docker tag ghcr.io/moremeds/xenon-migrator:VER ghcr.io/moremeds/xenon-migrator:latest`.
4. `docker-compose --profile migrate run --rm --pull never migrator alembic current`, then `... alembic heads`. If they differ, run `docker-compose --profile migrate run --rm --pull never migrator` (this upgrades the schema; `--pull never` keeps the verified local alias) and repeat `alembic current` until it equals the head.
5. Only after the schema is at head, roll out the api with the controlled cutover from `docs/runbooks/remote-deploy.md` § Standard release flow, steps 5-8 (`docker-compose pull api web realtime`, `docker-compose stop -t 60 api realtime`, the gateway socket check, `docker-compose up -d --pull never`). The last command recreates Watchtower too; check `docker inspect -f '{{.State.Running}}' xenon-watchtower-1` prints `true`. Then section C.
6. If the migration fails: leave Watchtower stopped (the old api keeps running on the old schema), and fix forward or restore before restarting it.

E. Before ANY job run (scheduled install or manual): read the effective settings inside the container. `web.env` overrides `.env` for the api service, so read them from the container, not from the files. Names and booleans only. The operator exports the expected account first (`export EXPECTED_ACCOUNT=<the intended live account, the U...31 account>`; a plain assignment is not passed to `-e EXPECTED_ACCOUNT`); the container compares by exact equality and prints a boolean:
```
cd /opt/xenon && docker-compose -f compose.yml run --rm -T --no-deps -e EXPECTED_ACCOUNT --label com.centurylinklabs.watchtower.enable=false api python -c '
import os
from urllib.parse import urlparse
db = urlparse(os.environ.get("DATABASE_URL", "")).path.lstrip("/")
acct = os.environ.get("XENON_BROKER_ACCOUNT") or os.environ.get("XENON_LIVE_ACCOUNT") or ""
expected = os.environ.get("EXPECTED_ACCOUNT", "")
print("db_name_is_core_dev:", db == "core_dev")
print("trading_mode_is_live:", os.environ.get("XENON_TRADING_MODE") == "live")
print("account_masked:", (acct[:1] + "..." + acct[-2:]) if acct else "MISSING")
print("account_matches_expected:", bool(expected) and acct == expected)
print("read_only_absent_or_0:", os.environ.get("XENON_READ_ONLY", "0") in ("", "0"))
print("flex_token_set:", bool(os.environ.get("IB_FLEX_TOKEN")), "nav_query_id_set:", bool(os.environ.get("IB_FLEX_NAV_QUERY_ID")))
'
```
Every boolean line must read `True` (account_masked shows the masked form). Also confirm the CLI exists: `... api sh -c 'command -v xenon-nav-flex-refresh'`. Stop if any value is wrong.

F. nav-flex-refresh: manual unguarded run and evidence (REAL WRITES)
```
cd /opt/xenon && docker rm xenon-job-nav-flex-refresh 2>/dev/null; docker-compose -f /opt/xenon/compose.yml run --rm -T --no-deps --name xenon-job-nav-flex-refresh --label com.centurylinklabs.watchtower.enable=false api xenon-nav-flex-refresh
```
Effects: calls the live IB Flex web service with the live token; UPSERTs `xenon.nav_history` rows (`source='close'`) and `xenon.ib_cash_flow` rows into the DB named by the container env (step E). Evidence the CLI ran: output lines `xenon-nav-flex-refresh: mode=live account=...` and `fetched N NAV rows`, exit code 0 (1 empty fetch, 2 not configured, 3 read-only), and NO `PG persist failed` line. The CLI prints "fetched N NAV rows" even when the DB write failed, so also check storage afterwards (read-only):
```
PGOPTIONS="-c default_transaction_read_only=on" psql -h 100.66.147.98 -U xenon_prod core_dev -v acct="$EXPECTED_ACCOUNT" -f - <<'SQL'
SELECT max(date), count(*), max(source)
  FROM xenon.nav_history
 WHERE broker = 'IB' AND account_env = 'live' AND broker_account = :'acct' AND source = 'close';
SQL
```
(Expect `max(date)` to be the last trading day and `count` greater than 0. An empty result for the scope is a failure even if the CLI exited 0.) If the Flex token is expired (spec F6, unverified) the exit code is 1.
- `launchctl kickstart gui/$(id -u)/com.xenon.nav-flex-refresh` is NOT a test of the job: outside 17:00-17:59 New York time the guard exits 0 before Docker starts and nothing runs. Use the unguarded command above.

G. core_test refresh
- Copy `scripts/infra/refresh-core-test.sh` to `/opt/xenon/bin/`, create `/opt/xenon/.pgpass-refresh` (chmod 600).
- Dry (reads core_dev, writes nothing): `PGHOST=localhost PGUSER=xenon_migrator PGPASSFILE=/opt/xenon/.pgpass-refresh /opt/xenon/bin/refresh-core-test.sh --dry`. Evidence: log line `DRY: pg_dump of core_dev succeeded`.
- Real (REAL WRITES: DROPs the `xenon` and `events` schemas in core_test and restores core_dev into it): same command without the dry argument. Evidence: `=== refresh ok: core_dev -> core_test ===` in `/var/log/xenon/refresh-core-test.log`, exit 0. The script refuses any destination that is core_dev or lacks "test", and a second concurrent run exits 3. A stale lock (crashed run) is never reclaimed automatically: the log line prints the recovery. Check the pid with `ps -p <pid>` and that no `refresh-core-test.sh` is running, then `rm -rf /tmp/xenon-refresh-core-test.lock.d` by hand.
- Same kickstart caveat: outside 04:00-04:59 New York time the plist guard makes kickstart a no-op.

H. Install schedule: `sudo mkdir -p /var/log/xenon && sudo chown $(whoami) /var/log/xenon`; copy both plists to `~/Library/LaunchAgents`, `plutil -lint`, `launchctl bootstrap gui/$(id -u) <plist>`. Do NOT change the host time zone (Asia/Hong_Kong; shared host). Check the LaunchAgent can reach Docker: `launchctl asuser $(id -u) docker ps` (unverified path).

I. Recovery from a failed job start (fixed container name)
- Symptom: `Conflict. The container name "/xenon-job-nav-flex-refresh" is already in use`.
- `docker ps -a --filter name=xenon-job-nav-flex-refresh --format '{{.ID}} {{.Status}}'` then `docker inspect -f '{{.State.Status}} {{.State.ExitCode}} {{.State.Error}}' xenon-job-nav-flex-refresh`.
- `running`: a job is active. Leave it alone; wait for it.
- `created` or `exited` or `dead`: read `docker logs xenon-job-nav-flex-refresh --tail 50`, then `docker rm xenon-job-nav-flex-refresh` (no `-f`).
- The scheduled command already runs `docker rm xenon-job-nav-flex-refresh` before each run. `docker rm` cannot remove a running container, so an active job is never touched and still blocks a second run through the name conflict.


## Not verified on the mini

Unverified: that `docker` is on the LaunchAgent PATH (`/opt/homebrew/bin:/usr/local/bin` are set), the real behavior of `docker rm` on the mini's Docker version, and the effective-settings snippet inside the real image (written from the code, not run in the image).
