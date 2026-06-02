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

# Mode dictates BOTH port (4001 live / 4002 paper) AND the gateway host.
# Paper runs locally on this machine; live runs on a remote server reachable
# via IB_GATEWAY_HOST in .env. Without overriding host here, paper would try
# to dial the remote live gateway and fail.
case "$MODE" in
  paper)
    IB_PORT=4002
    IB_HOST="127.0.0.1"
    ;;
  live)
    IB_PORT=4001
    # Use .env's IB_GATEWAY_HOST (typically a remote server). Default to
    # 127.0.0.1 if unset.
    IB_HOST="$(grep -E '^IB_GATEWAY_HOST=' "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d '"' | tr -d "'" | xargs || true)"
    IB_HOST="${IB_HOST:-127.0.0.1}"
    ;;
  *)
    log_err "Invalid mode '$MODE' — must be 'paper' or 'live'."
    exit 2
    ;;
esac

log_info "Trading mode: $MODE  →  IB Gateway $IB_HOST:$IB_PORT"

# Next.js dev port stays on the Next default (3000) for both paper and live.
# Override via the PORT env var if 3000 is taken.
# Internal IB Gateway port lives in IB_PORT — `PORT` is reserved for Next.js.

# 2. Apply pending Postgres migrations. Postgres is the primary persistence
# layer — running new code against a stale schema causes obscure runtime
# errors. `alembic upgrade head` is a no-op when the DB is already at head.
#
# Mode also dictates which Postgres to talk to. Live trades against the LAN
# `core_dev` (DATABASE_URL); paper prefers DATABASE_URL_PAPER so off-LAN dev
# doesn't hang trying to reach the remote box. Mirrors the IB host override
# above — both sides of the broker/storage pair stay local in paper mode.
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source <(grep -E '^(DATABASE_URL|DATABASE_URL_TEST|DATABASE_URL_PAPER|DATABASE_URL_TEST_PAPER)=' "$ENV_FILE")
  set +a
fi
if [[ "$MODE" == "paper" ]]; then
  if [[ -n "${DATABASE_URL_PAPER:-}" ]]; then
    DATABASE_URL="$DATABASE_URL_PAPER"
    log_info "Using DATABASE_URL_PAPER (local) for paper mode."
  else
    log_warn "DATABASE_URL_PAPER not set — paper mode will use DATABASE_URL ($DATABASE_URL)."
    log_warn "Off-LAN dev: add DATABASE_URL_PAPER=postgresql+asyncpg://xenon_dev:<dev-pw>@127.0.0.1:5432/core_test to .env"
  fi
  if [[ -n "${DATABASE_URL_TEST_PAPER:-}" ]]; then
    DATABASE_URL_TEST="$DATABASE_URL_TEST_PAPER"
  fi
fi
export DATABASE_URL DATABASE_URL_TEST

# dev.sh never targets prod. core_dev is written exclusively by the
# Docker stack on the macmini (see docs/runbooks/dev-prod-db-cutover.md
# and docs/architecture/production-database-strategy.md §dev/prod split).
# Strip query-string and trailing `/` to extract the bare db name.
_db_name="${DATABASE_URL##*/}"
_db_name="${_db_name%%\?*}"
if [[ "$_db_name" == "core_dev" ]]; then
  log_err "FATAL: dev.sh refuses to start against core_dev."
  log_err "  core_dev is the prod DB — written only by the macmini Docker stack."
  log_err "  Point DATABASE_URL at core_test (or a local dev DB) and retry."
  log_err "  See docs/runbooks/dev-prod-db-cutover.md for the split policy."
  exit 2
fi

if [[ -n "${DATABASE_URL:-}" ]]; then
  log_info "Applying alembic migrations…"
  (cd "$REPO_ROOT" && uv run alembic upgrade head)
else
  log_warn "DATABASE_URL not set — skipping alembic upgrade. Postgres-backed routes will fail."
fi

# 3. Probe the Gateway port. Warn if it's not up, but don't block — the user
# may want to start the dev stack for frontend work without IB Gateway running.
# FD 3 is opened only inside the subshell, so it's already closed in the
# parent when the subshell exits. Don't `exec 3<&-` here — `exec` without a
# command applies its redirections (including the trailing `2>/dev/null`) to
# the *parent shell, permanently*, silently routing all subsequent stderr to
# /dev/null and hiding errors from the broker-account guard below.
if (exec 3<>/dev/tcp/"$IB_HOST"/"$IB_PORT") 2>/dev/null; then
  log_info "IB Gateway port $IB_PORT is listening at $IB_HOST."
else
  log_warn "IB Gateway is NOT listening on $IB_HOST:$IB_PORT."
  log_warn "Continuing anyway — start IB Gateway in '$MODE' mode when you need broker calls."
fi

# 4. Export the resolved mode + host/port so child processes (uvicorn, Next,
# the Node realtime relay) all see the same value — without this, a
# per-invocation arg override would silently desync from .env. The host/port
# exports take precedence over the .env-loaded defaults inside Python.
export XENON_TRADING_MODE="$MODE"
export IB_GATEWAY_HOST="$IB_HOST"
export IB_GATEWAY_PORT="$IB_PORT"

# Live mode from dev.sh is for *debugging* against live IB — the data
# flows in but nothing persists. Real live trading goes through the
# Docker stack on the macmini, which writes core_dev. Forcing read-only
# here keeps dev experiments from polluting core_test with live fills.
if [[ "$MODE" == "live" ]]; then
  export XENON_READ_ONLY=1
  log_warn "dev.sh live: XENON_READ_ONLY=1 — order placement and ib_sync writes are disabled."
  log_warn "  For real live trading, deploy the Docker stack on the macmini instead."
fi

# Pin Next.js → FastAPI proxy target to IPv4. xenonApi.ts defaults to
# `http://localhost:8321`, but Node ≥18's fetch (undici) resolves `localhost`
# to IPv6 `::1` on macOS while uvicorn binds only `127.0.0.1`. The dual-stack
# mismatch causes intermittent 502s after the 5s connect timeout. Forcing the
# IPv4 literal sidesteps the whole DNS-order issue.
export XENON_API_URL="${XENON_API_URL:-http://127.0.0.1:8321}"

# Per-mode broker account placeholder. AccountScope.resolve_from_env() raises
# if XENON_BROKER_ACCOUNT is unset, and many sync subprocesses (ib_sync,
# ib_reconcile, naked_short_audit) call it at startup before IB Gateway is
# even reachable. ib_sync overrides this with managedAccounts()[0] after the
# IB connection is established — so this export is only the initial value to
# unblock imports. Pulled from .env vars XENON_PAPER_ACCOUNT / XENON_LIVE_ACCOUNT
# (paper requires DU* prefix; live requires U* and not DU*).
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source <(grep -E '^(XENON_PAPER_ACCOUNT|XENON_LIVE_ACCOUNT)=' "$ENV_FILE")
  set +a
fi
case "$MODE" in
  paper) BROKER_ACCOUNT="${XENON_PAPER_ACCOUNT:-}" ;;
  live)  BROKER_ACCOUNT="${XENON_LIVE_ACCOUNT:-}"  ;;
esac
if [[ -z "$BROKER_ACCOUNT" ]]; then
  if [[ "$MODE" == "paper" ]]; then
    log_err "XENON_PAPER_ACCOUNT must be set in .env for paper mode (e.g. DU1234567)."
  else
    log_err "XENON_LIVE_ACCOUNT must be set in .env for live mode (e.g. U1234567)."
  fi
  log_err "  Required: clean-slate PG cutoff — fake DU0000000 default would let unscoped rows leak into PG."
  exit 2
fi
export XENON_BROKER_ACCOUNT="$BROKER_ACCOUNT"
log_info "Broker account: $XENON_BROKER_ACCOUNT (overridden by ib_sync after IB connect)"

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
