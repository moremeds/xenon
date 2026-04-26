#!/usr/bin/env bash
# dev.sh — primary dev launcher. Reads XENON_TRADING_MODE, derives the IB
# Gateway port (4001 live, 4002 paper), probes that the native Gateway is
# logged in for that mode, then starts FastAPI + Next dev.
#
# Usage:
#   ./scripts/infra/dev.sh                       # mode from .env (default: paper)
#   ./scripts/infra/dev.sh paper                 # override per-invocation
#   ./scripts/infra/dev.sh live
#   ./scripts/infra/dev.sh paper --no-auth       # also bypass Clerk for the session
#
# Does NOT edit .env. Does NOT start IB Gateway — that is manual for v1.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

log_info() { printf '\033[32m[dev.sh]\033[0m %s\n' "$*"; }
log_warn() { printf '\033[33m[dev.sh]\033[0m %s\n' "$*" >&2; }
log_err()  { printf '\033[31m[dev.sh]\033[0m %s\n' "$*" >&2; }

# 1. Parse args (mode in any position; --no-auth optional)
MODE=""
NO_AUTH=0
for arg in "$@"; do
  case "$arg" in
    --no-auth) NO_AUTH=1 ;;
    paper|live|PAPER|LIVE) MODE="$arg" ;;
    *)
      log_err "Unknown argument '$arg' — expected paper|live|--no-auth"
      exit 2
      ;;
  esac
done

# Resolve mode: arg > .env > default paper
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

# 2. Apply pending Postgres migrations. Postgres is the primary persistence
# layer — running new code against a stale schema causes obscure runtime
# errors. `alembic upgrade head` is a no-op when the DB is already at head.
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source <(grep -E '^(DATABASE_URL|DATABASE_URL_TEST)=' "$ENV_FILE")
  set +a
fi
if [[ -n "${DATABASE_URL:-}" ]]; then
  log_info "Applying alembic migrations…"
  (cd "$REPO_ROOT" && uv run alembic upgrade head)
else
  log_warn "DATABASE_URL not set — skipping alembic upgrade. Postgres-backed routes will fail."
fi

# 3. Probe the Gateway port. Warn if it's not up, but don't block — the user
# may want to start the dev stack for frontend work without IB Gateway running.
if (exec 3<>/dev/tcp/127.0.0.1/"$PORT") 2>/dev/null; then
  exec 3<&- 2>/dev/null || true
  exec 3>&- 2>/dev/null || true
  log_info "IB Gateway port $PORT is listening."
else
  log_warn "IB Gateway is NOT listening on 127.0.0.1:$PORT."
  log_warn "Continuing anyway — start IB Gateway in '$MODE' mode when you need broker calls."
fi

# 4. Export the resolved mode so child processes (uvicorn, Next, the Node
# realtime relay) all see the same value — without this, a per-invocation
# arg override would silently desync from .env.
export XENON_TRADING_MODE="$MODE"

# Optionally bypass Clerk for the dev session. The flag is honored by
# web/middleware.ts (XENON_DISABLE_AUTH=1).
if [[ "$NO_AUTH" == "1" ]]; then
  export XENON_DISABLE_AUTH=1
  log_warn "Clerk auth bypassed for this session (XENON_DISABLE_AUTH=1)."
fi

# 5. Delegate to web/package.json's `dev` script, which already orchestrates
# `next dev`, the IB realtime relay, AND uvicorn via concurrently. Running a
# separate uvicorn here would race for 127.0.0.1:8321. The npm script inherits
# our exported XENON_TRADING_MODE.
log_info "Starting next dev + ib realtime + uvicorn (via npm run dev)…"
cd "$REPO_ROOT/web"
exec npm run dev
