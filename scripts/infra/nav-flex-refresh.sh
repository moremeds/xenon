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

# Pass-2 T7 — no silent default. The macmini runs both paper and live
# stacks; silently defaulting to `live` risks scope-collision when `.env`
# is misconfigured. The prod runbook (docs/runbooks/nav-flex-refresh.md)
# sets XENON_TRADING_MODE=live explicitly in the macmini .env.
if [[ -z "${XENON_TRADING_MODE:-}" ]]; then
  log "FATAL: XENON_TRADING_MODE not set. Source .env first (and ensure it sets the mode), or set the var explicitly."
  exit 2
fi
case "$XENON_TRADING_MODE" in
  live|paper) ;;
  *)
    log "FATAL: invalid XENON_TRADING_MODE=$XENON_TRADING_MODE (must be 'live' or 'paper')."
    exit 2
    ;;
esac

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
