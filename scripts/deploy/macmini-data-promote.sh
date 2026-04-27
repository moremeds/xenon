#!/usr/bin/env bash
# macmini-data-promote.sh — laptop → Mac mini full Postgres mirror.
#
# RUN FROM THE LAPTOP. Dumps local xenon_db, ships over SSH, restores onto the
# Mac mini's xenon_db. This is destructive on the target (--clean --if-exists).
#
# Usage:
#   ./scripts/deploy/macmini-data-promote.sh <ssh-host> [--confirm]
#
# Example:
#   ./scripts/deploy/macmini-data-promote.sh xenon@macmini.tail-scale.ts.net --confirm
#
# Safety:
#   - Refuses if FastAPI (8321), web (3000), or IB realtime (8765) is listening
#     on the LAPTOP — those are writers; mid-snapshot is corrupt
#   - Refuses without --confirm
#   - Saves dump to data/backups/ with timestamp before shipping (audit trail)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

SSH_HOST="${1:-}"
CONFIRM="${2:-}"
[[ -n "$SSH_HOST" ]] || { echo "usage: $0 <ssh-host> --confirm" >&2; exit 2; }

say()  { printf '\033[1;34m[promote]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[promote]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[promote] FAIL: %s\033[0m\n' "$*" >&2; exit 1; }
step() { printf '\n\033[1;36m=== %s ===\033[0m\n' "$*"; }

# ---------- Load .env for source DATABASE_URL ----------
[[ -f .env ]] || die "no .env in $REPO_ROOT"
# shellcheck disable=SC1091
set -a; source .env; set +a
[[ -n "${DATABASE_URL:-}" ]] || die "DATABASE_URL not set in .env"

DB_NAME="${XENON_DB_NAME:-xenon_db}"
DB_ROLE="${XENON_DB_ROLE:-xenon_app}"

# ---------- Refuse if local writers running ----------
step "Safety: ensure no local writers"
for port in 8321 3000 8765; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    die "port $port is in use — stop FastAPI/web/relay before promoting (would snapshot mid-write)"
  fi
done
say "no local writers listening"

# ---------- Confirm destructive op ----------
if [[ "$CONFIRM" != "--confirm" ]]; then
  warn "This will OVERWRITE Mac mini DB '$DB_NAME' on $SSH_HOST."
  warn "Re-run with --confirm to proceed."
  exit 1
fi

# ---------- Probe target DB ----------
step "Probe target Postgres on $SSH_HOST"
if ! ssh "$SSH_HOST" "command -v pg_restore" >/dev/null 2>&1; then
  die "pg_restore not on PATH on target — run macmini-bootstrap.sh first"
fi

# ---------- Local dump ----------
step "Dump local DB"
mkdir -p data/backups
TS="$(date +%Y%m%dT%H%M%S)"
DUMP_FILE="data/backups/${DB_NAME}-${TS}.dump"

# Find local pg_dump (Homebrew layout)
PG_DUMP="$(command -v pg_dump || true)"
[[ -x "$PG_DUMP" ]] || PG_DUMP="/opt/homebrew/opt/postgresql@15/bin/pg_dump"
[[ -x "$PG_DUMP" ]] || die "pg_dump not found"

"$PG_DUMP" -h localhost -U "$DB_ROLE" -Fc --no-owner --no-acl -f "$DUMP_FILE" "$DB_NAME"
say "wrote $DUMP_FILE ($(du -h "$DUMP_FILE" | awk '{print $1}'))"

# ---------- Ship + restore ----------
step "Ship + restore on $SSH_HOST"
# Stream to target, restoring with --clean so the alembic-created schema is
# replaced wholesale (matches the source's schema version).
ssh "$SSH_HOST" "pg_restore --clean --if-exists --no-owner --no-acl -h localhost -U $DB_ROLE -d $DB_NAME" < "$DUMP_FILE"

# ---------- Verify ----------
step "Verify row counts on target"
ssh "$SSH_HOST" "psql -h localhost -U $DB_ROLE $DB_NAME -c \"SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC LIMIT 20\""

step "Done"
say "Mac mini DB now mirrors laptop as of $TS"
say "Dump archived: $DUMP_FILE"
warn "If services on the Mac mini were running during restore, kickstart them now:"
warn "  ssh $SSH_HOST 'launchctl kickstart -k gui/\$UID/com.xenon.api'"
