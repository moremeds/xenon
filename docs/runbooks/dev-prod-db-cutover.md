# Dev/Prod Postgres Split — Limited `xenon_dev` Role (option C)

Goal: a laptop cannot write prod `core_dev`. Prod services (api, realtime,
migrator) stay on `xenon_app`: no prod restart and no prod credential change
in steps 1-5. The earlier design with `xenon_prod` and `xenon_migrator` roles
is not planned anymore.

Policy: [`docs/architecture/production-database-strategy.md`](../architecture/production-database-strategy.md)
§ Dev/Prod DB Split. Code-side guards: `scripts/infra/dev.sh` core_dev guard,
`XENON_READ_ONLY`, CI write-side guard.

## Status (2026-10-21)

| Step | What                                                         | State                                          |
| ---- | ------------------------------------------------------------ | ---------------------------------------------- |
| 1    | Create role `xenon_dev`                                      | DONE 2026-10-21                                |
| 2    | `core_dev` read-only grants for `xenon_dev`                  | DONE 2026-10-21                                |
| 3    | `core_test`: drop stale schemas, `xenon_dev` becomes owner   | PENDING. Drops data: needs the user's confirmation |
| 4    | Refresh dry run, then real run, as `xenon_dev`               | PENDING (needs step 3 for the real run)        |
| 5    | Laptop `.env` uses `xenon_dev`                               | PENDING (until then laptops use `xenon_app`)   |
| 6    | Optional: rotate the `xenon_app` password                    | PENDING                                        |

Until step 5, a laptop that still holds a `xenon_app` URL can write prod.
Step 6 is the only full fix.

## Where to run

Run the SQL on the Mac mini as the Postgres superuser (`moremeds`) with host
`psql`. There is no source checkout on the mini. The refresh script comes from
the repo and is copied to `/opt/xenon/bin/` (`deploy/README.md` § G). Never
print a password; use `\password` and the `.pgpass` files.

## Step 1: create the role (DONE)

```sql
CREATE ROLE xenon_dev LOGIN NOSUPERUSER NOCREATEDB;
\password xenon_dev
```

Store the password in the laptop `.env` and in `/opt/xenon/.pgpass-refresh`
(chmod 600, line for `localhost:5432`).

## Step 2: `core_dev` read-only grants (DONE)

Connect to `core_dev` (`\c core_dev`). These only add privileges.

```sql
GRANT CONNECT ON DATABASE core_dev TO xenon_dev;
GRANT USAGE ON SCHEMA xenon, events TO xenon_dev;
GRANT SELECT ON ALL TABLES    IN SCHEMA xenon, events TO xenon_dev;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA xenon, events TO xenon_dev;
ALTER DEFAULT PRIVILEGES FOR ROLE xenon_app IN SCHEMA xenon, events
  GRANT SELECT ON TABLES TO xenon_dev;
ALTER DEFAULT PRIVILEGES FOR ROLE xenon_app IN SCHEMA xenon, events
  GRANT SELECT ON SEQUENCES TO xenon_dev;
```

## Step 3: `core_test` ownership (PENDING, drops data)

Needs the user's confirmation phrase. `core_test` is a mirror, but the drop
removes its current `xenon` and `events` schemas (owned by `xenon_app`, alembic
`2026_06_22`). `dev.sh` runs `alembic upgrade head` on `core_test`, so
`xenon_dev` must own the objects.

```sql
\c core_test
DROP SCHEMA IF EXISTS xenon  CASCADE;
DROP SCHEMA IF EXISTS events CASCADE;
ALTER DATABASE core_test OWNER TO xenon_dev;
```

Do NOT use `REASSIGN OWNED BY xenon_app`: it also moves cluster-level objects,
such as the `core_dev` database itself.

## Step 4: refresh as `xenon_dev`

Follow `deploy/README.md` § G. The script defaults to `PGUSER=xenon_dev` and
dumps only `-n xenon -n events` (the limited role cannot read other apps'
schemas). Run `--dry` first, then the real run. The real run recreates both
schemas owned by `xenon_dev`.

```bash
PGHOST=localhost PGUSER=xenon_dev PGPASSFILE=/opt/xenon/.pgpass-refresh \
  /opt/xenon/bin/refresh-core-test.sh --dry
PGHOST=localhost PGUSER=xenon_dev PGPASSFILE=/opt/xenon/.pgpass-refresh \
  /opt/xenon/bin/refresh-core-test.sh
```

## Step 5: laptop `.env`

Set `DATABASE_URL` to the remote `core_test` as `xenon_dev` (see
`.env.example`). Remove the `xenon_app` URL from the active `.env`. Read-only
prod checks use `xenon_dev` too.

`DATABASE_URL_TEST` does not move to the remote role. Laptops run pytest on a
LOCAL Postgres `*_test` database owned by the local OS user. That user has
CREATEDB, so `pytest -n` can clone worker DBs; `xenon_dev` has no CREATEDB.

## Step 6 (optional): rotate the `xenon_app` password

Old copies of the password (`.env` backups, scratch files) stop working. This
changes the prod `.env` and needs a controlled api/realtime restart: follow
`docs/runbooks/remote-deploy.md` § Standard release flow steps 7-9 (stop with
the full grace period, start on the aliased images with `--pull never`, smoke).

```sql
\password xenon_app
```

## Verification

Run on the mini. `has_table_privilege` changes nothing.

```bash
export PGHOST=localhost PGUSER=xenon_dev PGPASSFILE=/opt/xenon/.pgpass-refresh
psql core_dev -c "SELECT count(*) FROM xenon.order_submissions"        # works
psql core_dev -c "SELECT has_table_privilege('xenon_dev','xenon.order_submissions','INSERT,UPDATE,DELETE')"  # f
psql core_dev -c "CREATE TABLE xenon.__probe(i int)"                   # permission denied
```

- After step 4: log line `=== refresh ok: core_dev -> core_test ===` in `/var/log/xenon/refresh-core-test.log`.
- After step 5: `scripts/infra/dev.sh paper` boots and migrates `core_test`.
- After step 5: `uv run pytest -q scripts/tests/test_test_db_guard.py` passes (the local test DB differs from `DATABASE_URL`).
- Prod `/health` is unchanged (no restart in steps 1-5).

## Rollback

`core_test` is a mirror; the refresh rebuilds it. Do the steps in this order.
Dropping the role first would break laptops and the refresh.

1. Stop dev sessions (`dev.sh`). If the refresh LaunchAgent is installed, boot
   it out (`launchctl bootout gui/$(id -u)/com.xenon.refresh-core-test`).
2. Give the laptop `DATABASE_URL` a working credential again. Before step 6:
   `xenon_app` from the backup the operator kept. After step 6: the current
   `xenon_app` password from the operator's secret workflow, never an old one.
3. Refresh: set `PGUSER` to `xenon_app` in the plist and put a `xenon_app` line
   in `/opt/xenon/.pgpass-refresh`, or leave the job uninstalled. The script
   default is `xenon_dev`, so the plist or cron line must set `PGUSER`.
4. Only then run the SQL that removes the role:

```sql
-- core_dev
\c core_dev
ALTER DEFAULT PRIVILEGES FOR ROLE xenon_app IN SCHEMA xenon, events REVOKE SELECT ON TABLES FROM xenon_dev;
ALTER DEFAULT PRIVILEGES FOR ROLE xenon_app IN SCHEMA xenon, events REVOKE SELECT ON SEQUENCES FROM xenon_dev;
REVOKE ALL ON ALL TABLES    IN SCHEMA xenon, events FROM xenon_dev;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA xenon, events FROM xenon_dev;
REVOKE USAGE ON SCHEMA xenon, events FROM xenon_dev;
REVOKE CONNECT ON DATABASE core_dev FROM xenon_dev;
-- core_test (only if step 3 ran): give ownership back, then drop the role
\c core_test
ALTER DATABASE core_test OWNER TO xenon_app;
REASSIGN OWNED BY xenon_dev TO xenon_app;   -- per database: run in core_test only
DROP OWNED BY xenon_dev;
\c core_dev
DROP OWNED BY xenon_dev;
DROP ROLE xenon_dev;
```

5. Verify: the laptop connects to `core_test`; the refresh `--dry` passes with
   the restored role (`PGUSER=xenon_app`); prod `/health` is unchanged.
