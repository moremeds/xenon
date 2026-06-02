# Dev/Prod Postgres Split — Cutover Runbook

One-time procedure to enforce the rule that **`core_dev` is written only
by the macmini Docker stack**, **`core_test` is written by dev machines**,
and the two are linked by a nightly **`core_dev → core_test` refresh**.

Companion docs:

- Policy: [`docs/architecture/production-database-strategy.md`](../architecture/production-database-strategy.md)
  § Dev/Prod DB split
- Code-side enforcement: `scripts/infra/dev.sh` core_dev guard, FastAPI
  `XENON_READ_ONLY` mode, CI guard
  `scripts/checks/no_json_write_on_order_path.py`.

---

## Pre-flight

Run from your MacBook before doing anything destructive:

- [ ] `git pull && git checkout master` — the `infra/dev-prod-db-split`
      PR must already be merged.
- [ ] `ssh macmini "docker compose ps"` — confirm the prod stack is
      running and healthy (api/web/realtime up; recent migrator run ok).
- [ ] `psql -h 100.66.147.98 -U xenon_app core_dev -c "SELECT 1;"` —
      legacy app role still has connectivity. Captures the
      "before" credentials for rollback if needed.
- [ ] Note the current alembic head — you'll re-verify it lines up
      across both DBs after the cutover.

```bash
ssh macmini "cd ~/projects/xenon && uv run alembic current"
```

## Step 1 — Create the new PG roles (macmini)

SSH to the macmini and connect as a Postgres superuser (typically
`postgres` for a Homebrew install, or the role you created during
initdb).

```sql
-- Replace <prod-pw> and <dev-pw> with strong, distinct passwords.
-- Store them in a secrets manager + ~/.pgpass per-environment.

CREATE ROLE xenon_prod WITH LOGIN PASSWORD '<prod-pw>';
CREATE ROLE xenon_dev  WITH LOGIN PASSWORD '<dev-pw>';

-- xenon_migrator probably already exists; if not:
-- CREATE ROLE xenon_migrator WITH LOGIN PASSWORD '<migrator-pw>' CREATEDB;
```

Both roles need CONNECT on both databases:

```sql
GRANT CONNECT ON DATABASE core_dev  TO xenon_prod, xenon_dev;
-- core_test does not exist yet — created in Step 2.
```

## Step 2 — Provision `core_test`

If `core_test` already exists (it may, from prior pytest runs), drop
and recreate to start fresh. If not, just create.

```sql
DROP DATABASE IF EXISTS core_test;
CREATE DATABASE core_test OWNER xenon_migrator;
GRANT CONNECT ON DATABASE core_test TO xenon_prod, xenon_dev;
```

## Step 3 — Apply alembic to `core_test`

From the macmini, point `DATABASE_URL` at `core_test` (just for this
invocation) and upgrade to head:

```bash
cd ~/projects/xenon
DATABASE_URL=postgresql+psycopg://xenon_migrator:<migrator-pw>@localhost:5432/core_test \
  uv run alembic upgrade head
```

Verify it matches `core_dev`:

```bash
DATABASE_URL=postgresql+psycopg://xenon_migrator:<migrator-pw>@localhost:5432/core_dev  uv run alembic current
DATABASE_URL=postgresql+psycopg://xenon_migrator:<migrator-pw>@localhost:5432/core_test uv run alembic current
# both should print the same revision id
```

## Step 4 — Set grants + default privileges

For `core_dev`:

```sql
\c core_dev

GRANT USAGE ON SCHEMA xenon, events TO xenon_prod, xenon_dev;

-- xenon_prod: full DML
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA xenon  TO xenon_prod;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA events TO xenon_prod;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA xenon  TO xenon_prod;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA events TO xenon_prod;

-- xenon_dev: SELECT only
GRANT SELECT ON ALL TABLES IN SCHEMA xenon  TO xenon_dev;
GRANT SELECT ON ALL TABLES IN SCHEMA events TO xenon_dev;

-- New-table defaults so the next migration's tables inherit ACL.
ALTER DEFAULT PRIVILEGES IN SCHEMA xenon
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO xenon_prod;
ALTER DEFAULT PRIVILEGES IN SCHEMA events
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO xenon_prod;
ALTER DEFAULT PRIVILEGES IN SCHEMA xenon  GRANT SELECT ON TABLES TO xenon_dev;
ALTER DEFAULT PRIVILEGES IN SCHEMA events GRANT SELECT ON TABLES TO xenon_dev;
```

For `core_test` (mirror, flipped):

```sql
\c core_test

GRANT USAGE ON SCHEMA xenon, events TO xenon_prod, xenon_dev;

GRANT SELECT ON ALL TABLES IN SCHEMA xenon  TO xenon_prod;
GRANT SELECT ON ALL TABLES IN SCHEMA events TO xenon_prod;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA xenon  TO xenon_dev;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA events TO xenon_dev;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA xenon  TO xenon_dev;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA events TO xenon_dev;

ALTER DEFAULT PRIVILEGES IN SCHEMA xenon
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO xenon_dev;
ALTER DEFAULT PRIVILEGES IN SCHEMA events
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO xenon_dev;
ALTER DEFAULT PRIVILEGES IN SCHEMA xenon  GRANT SELECT ON TABLES TO xenon_prod;
ALTER DEFAULT PRIVILEGES IN SCHEMA events GRANT SELECT ON TABLES TO xenon_prod;
```

Sanity check (run on the macmini as `xenon_prod`):

```bash
PGPASSWORD=<prod-pw> psql -h localhost -U xenon_prod -d core_dev \
  -c "INSERT INTO xenon.dummy_test(id) VALUES (1); ROLLBACK;"
# should succeed

PGPASSWORD=<prod-pw> psql -h localhost -U xenon_prod -d core_test \
  -c "INSERT INTO xenon.dummy_test(id) VALUES (1); ROLLBACK;"
# should fail: permission denied for relation dummy_test
```

(If `dummy_test` doesn't exist, pick any real table — point is to confirm
DML succeeds on the right DB and fails on the other.)

## Step 5 — Update the macmini's `.env`

Edit `~/projects/xenon/.env` on the macmini and replace the
`DATABASE_URL` line:

```diff
- DATABASE_URL=postgresql+asyncpg://xenon_app:xenon_dev@<lan-host>:5432/core_dev
+ DATABASE_URL=postgresql+asyncpg://xenon_prod:<prod-pw>@localhost:5432/core_dev
```

Leave `DATABASE_URL_TEST` and `DATABASE_URL_PAPER` empty/commented in
the prod `.env` — the Docker stack does not run paper mode.

Restart the Docker stack so the new credentials take effect:

```bash
cd ~/projects/xenon
docker compose down
docker compose run --rm migrator       # alembic upgrade against core_dev as xenon_prod
docker compose up -d
curl -s http://localhost:8321/health | jq '.ib_gateway, .ib_pool, .trading_mode'
```

The migrator container needs `xenon_migrator` credentials, NOT
`xenon_prod`. Add a `DATABASE_URL_MIGRATOR` to `.env` if your compose
file doesn't already split this — see
[`docker-compose.yml`](../../docker-compose.yml).

## Step 6 — Install the nightly refresh LaunchAgent

```bash
# On the macmini
sudo mkdir -p /var/log/xenon
sudo chown $(whoami) /var/log/xenon

cp ~/projects/xenon/scripts/infra/launchd/com.xenon.refresh-core-test.plist \
   ~/Library/LaunchAgents/

# Edit the plist — replace the three __PLACEHOLDER__ tokens with real paths.
$EDITOR ~/Library/LaunchAgents/com.xenon.refresh-core-test.plist
#   __XENON_ROOT__   = /Users/<you>/projects/xenon
#   __HOMEBREW_PATH__ = /opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
#   __PGPASSFILE__   = /Users/<you>/.pgpass  (chmod 600, contains migrator creds)

launchctl load ~/Library/LaunchAgents/com.xenon.refresh-core-test.plist
launchctl list | grep com.xenon.refresh-core-test
```

Smoke-test the refresh (dry-run first, then real):

```bash
# Dry: dump only
~/projects/xenon/scripts/infra/refresh-core-test.sh --dry
tail -n 20 /var/log/xenon/refresh-core-test.log

# Real
~/projects/xenon/scripts/infra/refresh-core-test.sh
```

The first real refresh wipes the freshly-migrated `core_test` and
replaces it with the contents of `core_dev`. The alembic state will
remain in sync because both DBs were already at the same head.

## Step 7 — Update each dev machine's `.env`

On every MacBook / dev laptop:

```diff
- DATABASE_URL=postgresql+asyncpg://xenon_app:xenon_dev@<lan-host>:5432/core_dev
+ DATABASE_URL=postgresql+asyncpg://xenon_dev:<dev-pw>@100.66.147.98:5432/core_test
- DATABASE_URL_TEST=postgresql+asyncpg://xenon_app:xenon_dev@<lan-host>:5432/core_test
+ DATABASE_URL_TEST=postgresql+asyncpg://xenon_dev:<dev-pw>@100.66.147.98:5432/core_test
- DATABASE_URL_PAPER=postgresql+asyncpg://xenon_app:xenon_dev@127.0.0.1:5432/core_dev
+ DATABASE_URL_PAPER=postgresql+asyncpg://xenon_dev:<dev-pw>@127.0.0.1:5432/core_test
- DATABASE_URL_TEST_PAPER=postgresql+asyncpg://xenon_app:xenon_dev@127.0.0.1:5432/core_test
+ DATABASE_URL_TEST_PAPER=postgresql+asyncpg://xenon_dev:<dev-pw>@127.0.0.1:5432/core_test
```

Verify:

```bash
./scripts/infra/dev.sh paper
# Expect: [dev.sh] Trading mode: paper → IB Gateway 127.0.0.1:4002
#         [dev.sh] Applying alembic migrations…
#         (no FATAL)

./scripts/infra/dev.sh live
# Expect: [dev.sh] Trading mode: live  → IB Gateway <host>:4001
#         [dev.sh] dev.sh live: XENON_READ_ONLY=1 — order placement and ib_sync writes are disabled.
```

If a stale env exports a `core_dev` URL, you'll see:

```
[dev.sh] FATAL: dev.sh refuses to start against core_dev.
[dev.sh]   core_dev is the prod DB — written only by the macmini Docker stack.
```

## Step 8 — Drop the legacy `xenon_app` role (optional, after a soak)

Once every machine is confirmed on the new credentials, the old
`xenon_app` role can be revoked. Give it ~1 week of observed-no-traffic
first:

```sql
-- macmini, as postgres superuser
REVOKE ALL ON ALL TABLES IN SCHEMA xenon, events FROM xenon_app;
REVOKE ALL ON SCHEMA xenon, events FROM xenon_app;
REVOKE ALL ON DATABASE core_dev, core_test FROM xenon_app;
DROP ROLE xenon_app;
```

## Rollback

If anything goes wrong:

1. Revert each machine's `.env` `DATABASE_URL` to the old
   `xenon_app:xenon_dev@<host>:5432/core_dev` form.
2. On the macmini: `launchctl unload ~/Library/LaunchAgents/com.xenon.refresh-core-test.plist`.
3. Restart Docker stack with the old creds.

No data is destroyed by this cutover until Step 8. `core_dev` is never
written by anything other than what already wrote to it; new roles are
just additional access paths. Reverting to `xenon_app` is safe at any
prior step.

## Verification checklist

- [ ] `xenon_prod` can write `core_dev`, cannot write `core_test`
- [ ] `xenon_dev` can write `core_test`, cannot write `core_dev`
- [ ] Docker stack on macmini connects with `xenon_prod` and serves
      `/health` ok
- [ ] `./scripts/infra/dev.sh paper` from a MacBook reaches alembic and
      logs no FATAL
- [ ] `./scripts/infra/dev.sh live` exports XENON_READ_ONLY=1 and the
      banner appears in stderr
- [ ] Nightly refresh ran once successfully (check
      `/var/log/xenon/refresh-core-test.log`)
- [ ] Alembic head matches on both DBs
