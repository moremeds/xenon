#!/usr/bin/env bash
# refresh-core-test.sh — Nightly mirror core_dev → core_test.
#
# Runs on the macmini at 04:00 ET (08:00 UTC) via the LaunchAgent at
# scripts/infra/launchd/com.xenon.refresh-core-test.plist.
#
# Why this exists: dev MacBooks write core_test; prod (Docker) writes
# core_dev. To give dev sessions realistic data without bidirectional
# replication, we dump prod nightly and restore over the dev DB. Any
# in-progress local dev migrations are wiped — re-apply them after
# refresh.
#
# Connection details come from PGSERVICE entries or env vars set by the
# LaunchAgent plist (PGHOST, PGUSER, PGPASSFILE). The script does NOT
# read .env so it stays valid when run from launchd (which has no shell
# session).
#
# Usage:
#   ./scripts/infra/refresh-core-test.sh          # restore
#   ./scripts/infra/refresh-core-test.sh --dry    # dump only, no restore
#
# Exit codes:
#   0 — success
#   1 — dump or restore failed
#   2 — invalid arguments / missing required env

set -euo pipefail

LOG_DIR="${XENON_REFRESH_LOG_DIR:-/var/log/xenon}"
LOG_FILE="$LOG_DIR/refresh-core-test.log"

SRC_DB="${XENON_REFRESH_SRC:-core_dev}"
DST_DB="${XENON_REFRESH_DST:-core_test}"
PG_HOST="${PGHOST:-localhost}"
PG_PORT="${PGPORT:-5432}"
PG_USER="${PGUSER:-xenon_migrator}"

DRY=0
for arg in "$@"; do
  case "$arg" in
    --dry|-n) DRY=1 ;;
    -h|--help)
      sed -n '2,25p' "$0"
      exit 0
      ;;
    *)
      echo "FATAL: unknown argument '$arg'" >&2
      exit 2
      ;;
  esac
done

mkdir -p "$LOG_DIR"

log() {
  printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$LOG_FILE"
}

log "=== refresh start: $SRC_DB -> $DST_DB (host=$PG_HOST:$PG_PORT user=$PG_USER, dry=$DRY) ==="

# Sanity: never run with src == dst. Cheap typo guard.
if [[ "$SRC_DB" == "$DST_DB" ]]; then
  log "FATAL: src and dst databases are identical ($SRC_DB) — aborting."
  exit 2
fi

# The PG client tools (pg_dump, pg_restore) consume PGPASSWORD or a
# PGPASSFILE. Prefer PGPASSFILE so the password never lands in process
# args or env logs.
if [[ -z "${PGPASSFILE:-}" && -z "${PGPASSWORD:-}" ]]; then
  log "WARN: neither PGPASSFILE nor PGPASSWORD set — relying on .pgpass in HOME."
fi

if [[ "$DRY" == "1" ]]; then
  # Stream to /dev/null so we still exercise the connection + permissions.
  if pg_dump -Fc -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$SRC_DB" > /dev/null 2>>"$LOG_FILE"; then
    log "DRY: pg_dump of $SRC_DB succeeded — restore would have followed."
    exit 0
  else
    log "DRY: pg_dump of $SRC_DB FAILED."
    exit 1
  fi
fi

# Production path: pipe the custom-format dump straight into pg_restore.
# --clean --if-exists drops existing objects in the destination so the
# schema matches the source exactly (handles dropped tables, renamed
# columns, etc).
if pg_dump -Fc -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$SRC_DB" 2>>"$LOG_FILE" \
   | pg_restore --clean --if-exists --no-owner --no-privileges \
       -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$DST_DB" 2>>"$LOG_FILE"; then
  log "=== refresh ok: $SRC_DB -> $DST_DB ==="
  exit 0
else
  log "=== refresh FAILED — inspect $LOG_FILE ==="
  exit 1
fi
