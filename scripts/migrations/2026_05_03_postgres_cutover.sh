#!/usr/bin/env bash
# 2026_05_03_postgres_cutover.sh
#
# Drives Phases 2-3 of the multi-service Postgres migration:
#   2.   pg_dump local → pg_restore remote
#   2.5  apex schema + role bootstrap on remote
#   3.   .env update preview (does NOT auto-commit)
#
# PREREQUISITE: Phase 1 must be complete on the remote — pg_hba allows
# this Mac, listen_addresses=*, postgresql reloaded, xenon_app role +
# xenon_db / xenon_test databases created. Verify with:
#
#   psql -h 192.168.50.47 -U xenon_app xenon_db -c "SELECT 1"
#
# Design doc: docs/plans/2026-05-03-multi-service-postgres-design.md
#
# Usage:
#   scripts/migrations/2026_05_03_postgres_cutover.sh                     # dry-run (probe only)
#   scripts/migrations/2026_05_03_postgres_cutover.sh --apply             # actually run phases 2 + 2.5
#   scripts/migrations/2026_05_03_postgres_cutover.sh --apply --skip-apex # skip phase 2.5 (xenon-only)

set -euo pipefail

REMOTE_HOST="${POSTGRES_REMOTE_HOST:-192.168.50.47}"
REMOTE_PORT="${POSTGRES_REMOTE_PORT:-5432}"
LOCAL_HOST="${POSTGRES_LOCAL_HOST:-localhost}"
LOCAL_PORT="${POSTGRES_LOCAL_PORT:-5432}"
DB_NAME="${XENON_DB_NAME:-xenon_db}"
DB_USER="${XENON_DB_USER:-xenon_app}"
DUMP_FILE="${DUMP_FILE:-/tmp/xenon_db_$(date +%Y%m%d_%H%M%S).dump}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

APPLY=0
SKIP_APEX=0
for arg in "$@"; do
  case "$arg" in
    --apply)     APPLY=1 ;;
    --skip-apex) SKIP_APEX=1 ;;
    *)
      echo "Unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

log()  { printf '\033[32m[cutover]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[cutover]\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[31m[cutover]\033[0m %s\n' "$*" >&2; }

# ── Phase 1 verification ──────────────────────────────────────────────
log "Phase 1 verification: probing remote $REMOTE_HOST:$REMOTE_PORT…"
if ! PGCONNECT_TIMEOUT=5 psql -h "$REMOTE_HOST" -p "$REMOTE_PORT" -U "$DB_USER" "$DB_NAME" -tA -c "SELECT 1" > /dev/null 2>&1; then
  err "Cannot reach remote Postgres as $DB_USER on $DB_NAME."
  err "Phase 1 must be complete first:"
  err "  - pg_hba.conf: host all all 192.168.50.0/24 scram-sha-256"
  err "  - postgresql.conf: listen_addresses = '*'"
  err "  - sudo systemctl reload postgresql"
  err "  - createuser xenon_app --pwprompt"
  err "  - createdb -O xenon_app xenon_db"
  err "  - createdb -O xenon_app xenon_test"
  exit 1
fi
log "  Remote $DB_USER@$REMOTE_HOST:$DB_NAME reachable."

# ── Local data snapshot ───────────────────────────────────────────────
log "Local Postgres snapshot:"
psql -h "$LOCAL_HOST" -p "$LOCAL_PORT" -U "$DB_USER" "$DB_NAME" -tA <<'SQL' 2>/dev/null | sed 's/^/  /'
SELECT format('  size: %s', pg_size_pretty(pg_database_size(current_database())));
SELECT format('  order_submissions: %s', count(*)) FROM xenon.order_submissions;
SELECT format('  regime_overrides:  %s', count(*)) FROM xenon.regime_overrides;
SELECT format('  order_fills:       %s', count(*)) FROM xenon.order_fills;
SELECT format('  order_events:      %s', count(*)) FROM xenon.order_events;
SELECT format('  account_snapshots: %s', count(*)) FROM xenon.account_snapshots;
SELECT format('  trades:            %s', count(*)) FROM xenon.trades;
SELECT format('  nav_history:       %s', count(*)) FROM xenon.nav_history;
SQL

# ── Dry run gate ──────────────────────────────────────────────────────
if [[ "$APPLY" == "0" ]]; then
  log ""
  log "Dry-run complete. To execute Phase 2 (data migration) + 2.5 (apex bootstrap):"
  log "  scripts/migrations/2026_05_03_postgres_cutover.sh --apply"
  log ""
  log "After apply, perform Phase 3 (cutover) manually:"
  log "  1. Stop FastAPI (kill scripts/infra/dev.sh)"
  log "  2. Update .env DATABASE_URL/DATABASE_URL_TEST to point at $REMOTE_HOST:$REMOTE_PORT"
  log "  3. uv run alembic upgrade head"
  log "  4. Restart scripts/infra/dev.sh and smoke test /health, /portfolio, /orders"
  exit 0
fi

# ── Phase 2: dump + restore ───────────────────────────────────────────
log ""
log "Phase 2: pg_dump local → pg_restore remote"
log "  dumping $LOCAL_HOST:$LOCAL_PORT/$DB_NAME → $DUMP_FILE"
pg_dump -h "$LOCAL_HOST" -p "$LOCAL_PORT" -U "$DB_USER" -Fc -f "$DUMP_FILE" "$DB_NAME"
DUMP_SIZE=$(du -h "$DUMP_FILE" | cut -f1)
log "  dump complete: $DUMP_SIZE"

log "  restoring → $REMOTE_HOST:$REMOTE_PORT/$DB_NAME"
pg_restore \
  -h "$REMOTE_HOST" -p "$REMOTE_PORT" -U "$DB_USER" \
  -d "$DB_NAME" \
  --no-owner --no-acl \
  --if-exists --clean \
  --exit-on-error \
  "$DUMP_FILE"
log "  restore complete."

# ── Phase 2 verification: row counts ──────────────────────────────────
log ""
log "Verifying row counts match…"
TABLES=(order_submissions regime_overrides order_fills order_events account_snapshots trades nav_history)
ALL_MATCH=1
for tbl in "${TABLES[@]}"; do
  LOCAL=$(psql -h "$LOCAL_HOST"  -p "$LOCAL_PORT"  -U "$DB_USER" "$DB_NAME" -tA -c "SELECT count(*) FROM xenon.$tbl" 2>/dev/null)
  REMOTE=$(psql -h "$REMOTE_HOST" -p "$REMOTE_PORT" -U "$DB_USER" "$DB_NAME" -tA -c "SELECT count(*) FROM xenon.$tbl" 2>/dev/null)
  if [[ "$LOCAL" == "$REMOTE" ]]; then
    log "  $tbl: $LOCAL (match)"
  else
    err "  $tbl: local=$LOCAL remote=$REMOTE (MISMATCH)"
    ALL_MATCH=0
  fi
done
if [[ "$ALL_MATCH" == "0" ]]; then
  err "Row count mismatch — investigate before proceeding to Phase 2.5"
  exit 1
fi
log "All row counts match."

# ── Phase 2.5: apex bootstrap ─────────────────────────────────────────
if [[ "$SKIP_APEX" == "1" ]]; then
  warn "Skipping Phase 2.5 (--skip-apex). Run it later with:"
  warn "  psql -h $REMOTE_HOST -U postgres $DB_NAME \\"
  warn "       -v apex_password='<set>' \\"
  warn "       -f $REPO_ROOT/scripts/migrations/2026_05_03_apex_schema_setup.sql"
else
  log ""
  log "Phase 2.5: apex schema + role bootstrap"
  log "  this requires postgres superuser on the remote."
  read -r -s -p "[cutover] Enter password for apex_app role: " APEX_PASSWORD
  echo
  if [[ -z "$APEX_PASSWORD" ]]; then
    err "Empty apex_app password — aborting Phase 2.5."
    exit 1
  fi
  PGPASSWORD="${POSTGRES_SUPERUSER_PASSWORD:-}" psql \
    -h "$REMOTE_HOST" -p "$REMOTE_PORT" -U postgres "$DB_NAME" \
    -v "apex_password=$APEX_PASSWORD" \
    -f "$REPO_ROOT/scripts/migrations/2026_05_03_apex_schema_setup.sql"
  log "Phase 2.5 complete."
fi

# ── Phase 3 instructions ──────────────────────────────────────────────
log ""
log "Phases 2 + 2.5 complete. Phase 3 (cutover) is manual — your call when:"
log ""
log "  1. Stop the dev stack (Ctrl-C scripts/infra/dev.sh)"
log "  2. Update .env:"
log "       DATABASE_URL=postgresql+asyncpg://$DB_USER:xenon_dev@$REMOTE_HOST:$REMOTE_PORT/$DB_NAME"
log "       DATABASE_URL_TEST=postgresql+asyncpg://$DB_USER:xenon_dev@$REMOTE_HOST:$REMOTE_PORT/xenon_test"
log "  3. uv run alembic upgrade head    # no-op if schemas match"
log "  4. scripts/infra/dev.sh paper     # smoke test"
log "  5. curl http://localhost:8321/health | jq"
log ""
log "Rollback (if anything's off): swap DATABASE_URL back to localhost. Local Postgres"
log "is unchanged — keep it running 48h as the safety net before stopping it."
log ""
log "Dump file kept at $DUMP_FILE (delete after Phase 4 confirms clean operation)."
