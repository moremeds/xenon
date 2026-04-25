#!/usr/bin/env bash
# dev.sh — primary dev launcher. Reads XENON_TRADING_MODE, derives the IB
# Gateway port (4001 live, 4002 paper), probes that the native Gateway is
# logged in for that mode, then starts FastAPI + Next dev.
#
# Usage:
#   ./scripts/infra/dev.sh              # mode from .env (default: paper)
#   ./scripts/infra/dev.sh paper        # override per-invocation
#   ./scripts/infra/dev.sh live
#
# Does NOT edit .env. Does NOT start IB Gateway — that is manual for v1.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

log_info() { printf '\033[32m[dev.sh]\033[0m %s\n' "$*"; }
log_warn() { printf '\033[33m[dev.sh]\033[0m %s\n' "$*" >&2; }
log_err()  { printf '\033[31m[dev.sh]\033[0m %s\n' "$*" >&2; }

# 1. Resolve mode: arg > .env > default paper
MODE="${1:-}"
if [[ -z "$MODE" && -f "$ENV_FILE" ]]; then
  MODE="$(grep -E '^XENON_TRADING_MODE=' "$ENV_FILE" | tail -n1 | cut -d= -f2- | tr -d '"' | tr -d "'" | xargs || true)"
fi
MODE="${MODE:-paper}"
MODE="$(echo "$MODE" | tr '[:upper:]' '[:lower:]' | xargs)"

case "$MODE" in
  paper) PORT=4002 ;;
  live)  PORT=4001 ;;
  *)
    log_err "Invalid mode '$MODE' — must be 'paper' or 'live'."
    exit 2
    ;;
esac

log_info "Trading mode: $MODE  →  IB Gateway port $PORT"

# 2. Probe the Gateway port. Bail out with a clear message if not listening.
if ! (exec 3<>/dev/tcp/127.0.0.1/"$PORT") 2>/dev/null; then
  log_err "IB Gateway is NOT listening on 127.0.0.1:$PORT."
  log_err "Launch IB Gateway in '$MODE' mode (Login → ${MODE^^}) and re-run."
  exit 3
fi
exec 3<&- 2>/dev/null || true
exec 3>&- 2>/dev/null || true
log_info "IB Gateway port $PORT is listening."

# 3. Start FastAPI + Next dev. The Python service is run in the foreground
# so Ctrl-C tears it down; Next is started in the background and killed on
# exit via trap.

cleanup() {
  if [[ -n "${NEXT_PID:-}" ]] && kill -0 "$NEXT_PID" 2>/dev/null; then
    log_info "Stopping Next dev (pid $NEXT_PID)…"
    kill "$NEXT_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

log_info "Starting Next dev (background)…"
( cd "$REPO_ROOT/web" && npm run dev ) &
NEXT_PID=$!

log_info "Starting FastAPI on 127.0.0.1:8321 (foreground)…"
cd "$REPO_ROOT"
exec uv run uvicorn xenon.api.server:app --host 127.0.0.1 --port 8321 --reload
