#!/usr/bin/env bash
# refresh-core-test.sh — Nightly mirror core_dev → core_test.
#
# Runs on the macmini at 04:00 ET (08:00 UTC) via the LaunchAgent at
# deploy/jobs/com.xenon.refresh-core-test.plist.
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
# Safety (checked before any PG call):
#   - SRC and DST must differ.
#   - DST must contain "test" and must not be core_dev. A reversed or
#     mistyped XENON_REFRESH_DST can never wipe prod.
#   - One run at a time: an atomic mkdir lock (XENON_REFRESH_LOCK_DIR) covers
#     every entry path (launchd, cron, manual). A second run exits 3 and
#     calls no PG command. A stale lock (crashed run, SIGKILL) is NOT
#     reclaimed automatically: the log line says how to remove it by hand.
#
# Test hook: XENON_REFRESH_PG_BIN (a directory) is put first on PATH after the
# Homebrew probe, so tests can supply stub pg_dump / pg_restore / psql.
#
# Exit codes:
#   0 — success
#   1 — dump or restore failed
#   2 — invalid arguments / missing required env / refused destination
#   3 — another refresh holds the lock (nothing was run)

set -euo pipefail

LOG_DIR="${XENON_REFRESH_LOG_DIR:-/var/log/xenon}"
LOG_FILE="$LOG_DIR/refresh-core-test.log"

SRC_DB="${XENON_REFRESH_SRC:-core_dev}"
DST_DB="${XENON_REFRESH_DST:-core_test}"
PG_HOST="${PGHOST:-localhost}"
PG_PORT="${PGPORT:-5432}"
PG_USER="${PGUSER:-xenon_migrator}"

# Prefer the highest-version PG client tools available. pg_dump must be
# >= the server's major version (a 15.x client cannot dump a 17.x
# server — fails with "aborting because of server version mismatch").
# Probe homebrew slots from newest to oldest; first hit wins. Falls
# through to the system PATH when nothing is installed under
# /opt/homebrew (e.g. on the macmini if PG ships via a different path).
for _pg_major in 18 17 16 15; do
  _pg_bin="/opt/homebrew/opt/postgresql@${_pg_major}/bin"
  if [[ -x "$_pg_bin/pg_dump" ]]; then
    PATH="$_pg_bin:$PATH"
    break
  fi
done
unset _pg_major _pg_bin
if [[ -n "${XENON_REFRESH_PG_BIN:-}" ]]; then
  PATH="$XENON_REFRESH_PG_BIN:$PATH"
fi

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

# Destination guard: never write to prod. DST must look like a test DB.
if [[ "$DST_DB" == "core_dev" || "$DST_DB" != *test* ]]; then
  log "FATAL: refusing destination '$DST_DB' (must contain 'test' and not be core_dev) — aborting."
  exit 2
fi

# Single-run lock. mkdir is atomic. There is NO automatic stale-lock reclaim:
# two racing reclaimers could both enter PG. If the lock exists, exit 3 and let
# the operator recover by hand. The EXIT trap is installed only after we own
# the lock, and it removes the lock only while its pid file still holds our PID,
# so it can never delete a successor's lock.
LOCK_DIR="${XENON_REFRESH_LOCK_DIR:-/tmp/xenon-refresh-core-test.lock.d}"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  _holder="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  log "another refresh holds $LOCK_DIR (pid ${_holder:-unknown}) — exiting without running."
  log "If you believe it is stale: check that pid (ps -p ${_holder:-<pid>}) and that no refresh-core-test.sh is running; only then remove it by hand: rm -rf $LOCK_DIR"
  exit 3
fi
echo "$$" > "$LOCK_DIR/pid"
_release_lock() {
  if [[ "$(cat "$LOCK_DIR/pid" 2>/dev/null || true)" == "$$" ]]; then
    rm -rf "$LOCK_DIR"
  fi
}
trap _release_lock EXIT

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

# pg_restore --clean drops objects one-by-one, but a referenced table
# (e.g. xenon.futu_trades) cannot be dropped while other objects depend
# on it — pg_restore lacks CASCADE semantics for that path. Pre-drop
# the application schemas with CASCADE so the restore lands on a clean
# slate. Failure here is a warning (the restore may still succeed for
# objects that don't conflict), not fatal — log loudly and continue.
log "Pre-drop: dropping xenon + events schemas with CASCADE."
if ! psql -v ON_ERROR_STOP=1 -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$DST_DB" \
     -c "DROP SCHEMA IF EXISTS xenon CASCADE;" \
     -c "DROP SCHEMA IF EXISTS events CASCADE;" 2>>"$LOG_FILE"; then
  log "WARN: pre-drop failed — restore may emit residual 'already exists' / FK errors."
fi

# Production path: pipe the custom-format dump straight into pg_restore.
# --clean --if-exists drops anything the pre-drop missed (extensions,
# the public-schema objects we don't pre-drop, etc).
if pg_dump -Fc -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$SRC_DB" 2>>"$LOG_FILE" \
   | pg_restore --clean --if-exists --no-owner --no-privileges \
       -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$DST_DB" 2>>"$LOG_FILE"; then
  log "=== refresh ok: $SRC_DB -> $DST_DB ==="
  exit 0
else
  log "=== refresh FAILED — inspect $LOG_FILE ==="
  exit 1
fi
