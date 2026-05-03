# Multi-service Postgres: Xenon + apex shared instance

**Status:** design — pending Phase 1 (remote operator) before cutover
**Date:** 2026-05-03
**Owner:** chenxi
**Related:** PR #81 (ib_async migration — same infra cleanup wave)

## Context

Two parallel infrastructure moves are happening together:

1. **IB Gateway** is moving from local Docker on this Mac → remote server at `192.168.50.47:4001` (live) so the Gateway can run unattended for live trading. Paper Gateway stays local on `127.0.0.1:4002` for fast dev iteration. (Already wired in `scripts/infra/dev.sh` per-mode; pending IB Gateway-side `pg_hba` analogue, the Trusted IPs list.)

2. **Postgres** is moving from local Homebrew on this Mac → the same remote server (`192.168.50.47:5432`). Once moved, **`apex`, our signal service, will share that Postgres**. apex and Xenon need clean isolation but also need to communicate — signal-arrival (apex→Xenon) and outcome-feedback (Xenon→apex).

The status quo (single-tenant `xenon_db` on local Mac) doesn't extend cleanly to the shared-instance world. The new database — named `core` to reflect its multi-service role — is the design captured here.

## Decision

**Single Postgres database `core`, three schemas, two service roles, one shared event channel.**

```
core (Postgres on 192.168.50.47:5432)
├── schema: xenon          owner: xenon_app    Xenon-only writes
├── schema: apex           owner: apex_app     apex-only writes
├── schema: events         shared writeable     LISTEN/NOTIFY outbox
└── schema: public         (extensions only, otherwise empty)
```

| Why this shape               | Alternative                                               | Why we rejected it                                                                                                                                                        |
| ---------------------------- | --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Schemas (not separate DBs)   | Two databases (`core` + `apex_db`)                        | Cross-service joins require dblink/FDW or app-level join. We want `apex.signals JOIN xenon.order_submissions` to be a plain SQL join.                                     |
| Schemas (not table prefix)   | All tables in one schema with `xenon_*` / `apex_*` prefix | Permission model conflates services — apex_app would have grants on tables it shouldn't see. Prefix-based naming also pollutes autocomplete and breaks Alembic isolation. |
| Schema-per-service ownership | Single `db_owner` role, ACLs per table                    | Per-table ACLs drift; per-schema ownership is one-line lockdown.                                                                                                          |
| Single DB                    | Two Postgres instances                                    | Operational overhead, duplicate connection pools, no cross-service consistency.                                                                                           |

**Communication primitive: `events.outbox`.** Already exists for Xenon's fill notifications (`fill.recorded`, `fill.commission_updated`). Both services INSERT to it inside their own transactions; reactive consumers LISTEN on the channel. The durable-record-plus-NOTIFY pattern is the standard outbox pattern — payload is small, the receiver re-reads the source-of-truth table (`apex.signals` etc.) for the actual data.

## Roles & grants

```sql
-- xenon_app already exists (current local DB)
-- Just add apex_app and the cross-service grants

CREATE ROLE apex_app LOGIN PASSWORD '<set during Phase 2.5>';

CREATE SCHEMA apex AUTHORIZATION apex_app;

-- apex can write its own schema fully
GRANT USAGE, CREATE ON SCHEMA apex TO apex_app;
ALTER DEFAULT PRIVILEGES FOR ROLE apex_app IN SCHEMA apex
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO apex_app;

-- apex reads selected xenon tables for context (whitelist explicitly)
GRANT USAGE ON SCHEMA xenon TO apex_app;
GRANT SELECT ON xenon.account_snapshots TO apex_app;
GRANT SELECT ON xenon.order_submissions TO apex_app;
GRANT SELECT ON xenon.order_fills TO apex_app;
GRANT SELECT ON xenon.regime_state TO apex_app;
-- explicitly NOT granting INSERT/UPDATE/DELETE on xenon.*
-- additions to this whitelist require a doc PR + review

-- both services emit + consume on the shared outbox
GRANT USAGE ON SCHEMA events TO apex_app, xenon_app;
GRANT SELECT, INSERT ON events.outbox TO apex_app, xenon_app;
GRANT USAGE ON SEQUENCE events.outbox_id_seq TO apex_app, xenon_app;
```

**The whitelist is the contract.** When apex needs a new xenon table, it goes through review — never `GRANT SELECT ON ALL TABLES IN SCHEMA xenon`.

## Communication patterns

### Pattern 1 — apex → Xenon (signal arrival)

```
apex.signals                                 (durable ledger — survives restarts)
  ↓ same-txn
events.outbox channel='apex.signal'          (NOTIFY wake-up)
  ↓ pg_notify
xenon listener (existing outbox consumer)
  ↓ reads apex.signals WHERE consumed_at IS NULL
xenon dispatches signal through the Four Gates
  ↓ if gates pass
xenon.order_submissions                       (FK-by-convention: client_attempt_id = apex.signals.signal_id)
```

**Why both `apex.signals` AND `events.outbox`:**

- `apex.signals` is the durable record. Queryable forever ("what did apex say on 2026-05-03?"), survives consumer restarts, marked `consumed_at` when Xenon acts.
- `events.outbox` is the wake-up bell. Sub-second NOTIFY to Xenon's reactive consumer. Missed during downtime is fine — on restart, Xenon's consumer re-reads `apex.signals WHERE consumed_at IS NULL`. Idempotent.

**This is the canonical outbox pattern.** Same shape as Xenon's existing `fill.recorded` channel.

### Pattern 2 — Xenon → apex (outcome feedback)

apex needs to know "did my signal turn into a fill at what price?" Two options considered:

- **Pull (default):** apex queries `xenon.order_submissions JOIN xenon.order_fills` filtered by `client_attempt_id IN (signal_ids it cares about)`. Read-only grant, simple, no coordination, ~1s latency acceptable.
- **Push:** Xenon emits `events.outbox` channel `xenon.fill` → apex listens. Same pattern, opposite direction. Reserved for if/when apex's outcome tracking becomes real-time-critical.

**Default: pull.** Add push later if outcome latency becomes a bottleneck.

### Pattern 3 — Shared reference data

NAV, regime tier, current open orders. Pure read on `xenon.account_snapshots`, `xenon.regime_state`, `xenon.order_submissions`. Whitelisted via grants above. Both services see consistent state because they're in the same DB.

## Schema sketch — `apex.signals`

```sql
CREATE TABLE apex.signals (
    signal_id           UUID         PRIMARY KEY,
    ticker              TEXT         NOT NULL,
    structure_type      TEXT         NOT NULL,    -- bull_call_spread, long_put, hedge, etc.
    direction           TEXT         NOT NULL,    -- 'long' | 'short' | 'hedge'
    conviction          NUMERIC(5,4),             -- 0..1 score
    evidence            JSONB        NOT NULL,    -- flow data, supporting metrics
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ  NOT NULL,
    consumed_at         TIMESTAMPTZ,              -- xenon sets when it dispatches
    consumed_outcome    TEXT,                     -- 'placed' | 'gated_out' | 'expired' | 'rejected'
    consumed_attempt_id TEXT                      -- = xenon.order_submissions.client_attempt_id (convention, not FK)
);

CREATE INDEX ON apex.signals (consumed_at) WHERE consumed_at IS NULL;
CREATE INDEX ON apex.signals (ticker, created_at DESC);
CREATE INDEX ON apex.signals (consumed_attempt_id) WHERE consumed_attempt_id IS NOT NULL;
```

**Why FK-by-convention, not enforced FK:**

- `consumed_attempt_id` is the join key linking back to Xenon's order ledger
- A real FK to `xenon.order_submissions` would block apex's row deletion if Xenon ever wanted to clean up old order rows, and tightens the cross-service coupling we're trying to keep clean
- The convention is enforced at the application layer, audited via lineage queries

**End-to-end traceability** with one query:

```sql
SELECT s.signal_id, s.ticker, s.structure_type, s.created_at AS signaled_at,
       o.state, o.ib_order_id, f.fill_price, f.commission
FROM apex.signals s
LEFT JOIN xenon.order_submissions o ON o.client_attempt_id = s.consumed_attempt_id
LEFT JOIN xenon.order_fills f ON f.submission_id = o.submission_id
WHERE s.created_at > now() - interval '7 days';
```

apex's other tables (signal scoring runs, archive, model versions) follow the same pattern: own them in `apex.*`, expose via apex's own API to Xenon if needed.

## Per-service Alembic

Each service runs its own Alembic with isolated version tracking:

```python
# apex's alembic env.py
context.configure(
    connection=connection,
    target_metadata=apex_metadata,
    version_table_schema='apex',         # apex.alembic_version, not public.alembic_version
    include_schemas=True,
    include_object=lambda obj, name, type_, refl, compare:
        not (type_ == 'table' and obj.schema != 'apex'),
)
```

```python
# xenon's alembic env.py — already configured similarly for xenon
# version_table_schema='xenon' for the same reason
```

**Critical:** the `include_object` filter ensures apex's autogenerate sees only `apex.*` tables. Without it, apex's autogenerate would see Xenon's tables (because they're in the same DB) and try to "drop" them as if they were stale.

## Migration plan

### Phase 1 — Remote setup (operator: chenxi, on `192.168.50.47`)

```bash
# 1a. Find configs
sudo -u postgres psql -tA -c "SHOW hba_file;"            # path to pg_hba.conf
sudo -u postgres psql -tA -c "SHOW config_file;"          # path to postgresql.conf
sudo -u postgres psql -tA -c "SELECT version();"          # confirm >= 17

# 1b. Edit postgresql.conf — listen externally
listen_addresses = '*'

# 1c. Edit pg_hba.conf — allow LAN
host    all    all    192.168.50.0/24    scram-sha-256

# 1d. Reload
sudo systemctl reload postgresql      # or: brew services restart postgresql@17 on macOS

# 1e. Create xenon_app role + databases
sudo -u postgres createuser xenon_app --pwprompt        # password = xenon_dev (current .env)
sudo -u postgres createdb -O xenon_app core
sudo -u postgres createdb -O xenon_app core_test
```

**Verification (from this Mac):**

```bash
psql -h 192.168.50.47 -U xenon_app core -c "SELECT 1"   # expect "1"
```

### Phase 2 — Data migration (driver: this Mac)

```bash
# 2a. Dump local
pg_dump -h localhost -U xenon_app -Fc -f /tmp/core.dump core

# 2b. Restore to remote
pg_restore -h 192.168.50.47 -U xenon_app -d core --no-owner --no-acl /tmp/core.dump

# 2c. Verify row counts match
for tbl in order_submissions regime_overrides order_fills order_events account_snapshots trades nav_history; do
  echo -n "$tbl: local="
  psql -h localhost      -U xenon_app core -tA -c "SELECT count(*) FROM xenon.$tbl"
  echo -n " remote="
  psql -h 192.168.50.47  -U xenon_app core -tA -c "SELECT count(*) FROM xenon.$tbl"
done
```

Expected baseline (snapshot taken 2026-05-03):

- `order_submissions`: 9
- `regime_overrides`: 0
- `order_fills`: 8
- `order_events`: 9
- `account_snapshots`: 6120
- `trades`: 4
- `nav_history`: 25

### Phase 2.5 — apex bootstrap (driver: this Mac)

Run `scripts/migrations/2026_05_03_apex_schema_setup.sql` against the remote (full SQL above under "Roles & grants").

```bash
psql -h 192.168.50.47 -U postgres core -f scripts/migrations/2026_05_03_apex_schema_setup.sql
```

apex's own table creation (`apex.signals`, etc.) is owned by apex's repo, run from apex's Alembic when ready.

### Phase 3 — Xenon cutover (driver: this Mac)

```bash
# 3a. Stop FastAPI gracefully
# (kill running scripts/infra/dev.sh)

# 3b. Update .env
DATABASE_URL=postgresql+asyncpg://xenon_app:xenon_dev@192.168.50.47:5432/core
DATABASE_URL_TEST=postgresql+asyncpg://xenon_app:xenon_dev@192.168.50.47:5432/core_test

# 3c. Apply migrations against remote (no-op if schema matches)
uv run alembic upgrade head

# 3d. Smoke test
curl -s http://localhost:8321/health | jq
curl -s http://localhost:8321/portfolio | jq '.account_env, .net_liquidation'
curl -s http://localhost:8321/orders | jq 'length'
```

### Phase 4 — Rollback safety net

- Keep local Postgres running for 48 hours after cutover
- If any issue post-cutover: swap `DATABASE_URL` back in `.env`, restart FastAPI — instant rollback, zero data loss
- After 48h of clean operation: stop local Postgres (`brew services stop postgresql@17`)

## Operational concerns

### Backups

- Configure `pg_dump` daily on the remote, retain 30 days
- Recommend WAL archiving for point-in-time recovery if data volume grows

### Observability

```sql
-- Per-schema write traffic
SELECT schemaname, n_tup_ins, n_tup_upd, n_tup_del
FROM pg_stat_user_tables
ORDER BY n_tup_ins + n_tup_upd + n_tup_del DESC;

-- Connection mix
SELECT usename, count(*) FROM pg_stat_activity GROUP BY usename;
```

Expect to see `xenon_app` and `apex_app` as separate connection users; trivial isolation observability.

### Upgrades

Each service's Alembic is independent — apex's migrations don't pause Xenon, and vice versa. The `version_table_schema` separation is the linchpin.

### Failure mode: apex emits malformed signal

apex transactional INSERT to `apex.signals` + `events.outbox` either both commit or neither do. Xenon's consumer reads the durable signal — bad payload → consumed_outcome='rejected', logged. apex's signal stays as a forensic record.

### Failure mode: Xenon down when signal arrives

NOTIFY is missed; durable signal stays in `apex.signals` with `consumed_at IS NULL`. On Xenon restart, the consumer re-reads pending signals and dispatches. Standard outbox guarantee.

## Open questions

1. **apex signal expiry** — what's the policy for `apex.signals.expires_at`? Suggest defaulting to 4 hours from `created_at` (intraday signal); apex sets explicitly if longer.
2. **`events.outbox` ownership** — left to `postgres` superuser today; should we create a dedicated `events_app` role for tighter least-privilege? Probably not worth it for a 2-service system.
3. **apex's read whitelist** — initial four tables (`account_snapshots`, `order_submissions`, `order_fills`, `regime_state`). Add as needed via doc PR.
4. **Connection pooling** — single-instance Postgres serves both services. If/when contention shows up, consider PgBouncer in transaction-pooling mode.

## Acceptance criteria

- [ ] Phase 1 complete: `psql -h 192.168.50.47 -U xenon_app core -c "SELECT 1"` succeeds from this Mac
- [ ] Phase 2 complete: row counts match exactly between local and remote for all xenon.\* tables
- [ ] Phase 2.5 complete: `apex_app` role exists, `apex` schema exists, grants verified via `\dn+` and `\dp xenon.*`
- [ ] Phase 3 complete: FastAPI starts cleanly against remote DB, `/health` returns ok, `/portfolio` and `/orders` return non-empty
- [ ] Phase 4 complete: 48h of clean operation, local Postgres stopped, no rollback needed

## References

- Existing Xenon schema: `src/xenon/db/schema.py`
- Outbox helper: `src/xenon/db/events.py`
- Broker-account scope policy: `docs/architecture/production-database-strategy.md`
- IB Gateway remote setup (parallel work): `scripts/infra/dev.sh` — paper local, live remote
