#!/usr/bin/env bash
# nav-flex-refresh.sh — Daily IB Flex NAV refresh wrapper.
#
# Runs on the macmini at 17:30 ET via the LaunchAgent at
# scripts/infra/launchd/com.xenon.nav-flex-refresh.plist.
#
# Why this exists: the plist passes only PATH + XENON_ROOT. This script
# sources .env from the checkout so DB creds + IB_FLEX_TOKEN come from
# the operator's already-configured file rather than the plist (which
# is checked in and cannot carry secrets). Same pattern as
# refresh-core-test.sh.
#
# Per the [[flex-is-reconciliation-not-history]] architecture, the saved
# Flex query (IB_FLEX_NAV_QUERY_ID=1529248) should be a rolling ~2-week
# period so a single missed run is absorbed by the next.
#
# Usage:
#   ./scripts/infra/nav-flex-refresh.sh           # invoke the CLI
#   ./scripts/infra/nav-flex-refresh.sh --dry     # source .env, do not run

set -euo pipefail

LOG_DIR="${XENON_NAV_REFRESH_LOG_DIR:-/var/log/xenon}"
LOG_FILE="$LOG_DIR/nav-flex-refresh.log"

XENON_ROOT="${XENON_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
ENV_FILE="${XENON_ENV_FILE:-$XENON_ROOT/.env}"

DRY=0
for arg in "$@"; do
  case "$arg" in
    --dry|-n) DRY=1 ;;
    -h|--help)
      sed -n '2,18p' "$0"
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

log "=== nav-flex-refresh start: XENON_ROOT=$XENON_ROOT env=$ENV_FILE dry=$DRY ==="

if [[ ! -f "$ENV_FILE" ]]; then
  log "FATAL: env file not found: $ENV_FILE"
  exit 2
fi

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

# Default to live for the prod scheduled run; operators can override
# XENON_TRADING_MODE in .env for paper.
export XENON_TRADING_MODE="${XENON_TRADING_MODE:-live}"

if [[ "$DRY" == "1" ]]; then
  log "DRY: env sourced, mode=$XENON_TRADING_MODE — exiting before CLI invocation."
  exit 0
fi

cd "$XENON_ROOT"
if uv run xenon-nav-flex-refresh 2>&1 | tee -a "$LOG_FILE"; then
  log "=== nav-flex-refresh ok ==="
  exit 0
else
  rc=$?
  log "=== nav-flex-refresh FAILED rc=$rc — inspect $LOG_FILE ==="
  exit $rc
fi
