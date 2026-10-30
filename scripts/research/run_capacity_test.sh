#!/usr/bin/env bash
# Live capacity test for the option-chain snapshotter (this branch, as-is).
#
# Runs the daemon against the macmini LIVE IB gateway, writing ONLY to an
# isolated local DB (optionchain_captest) — never core_dev/core_test. The daemon
# self-gates on RTH, so it will idle until the session opens, then snapshot every
# ~10 min. Shares the prod 100-line market-data budget (BATCH_SIZE=50) — watch the
# log for IB error 10187.
#
# Analyze afterwards:
#   DATABASE_URL=postgresql://localhost/optionchain_captest \
#     uv run python scripts/research/analyze_capacity.py
set -euo pipefail
cd "$(dirname "$0")/../.."

export DATABASE_URL="postgresql+psycopg://localhost/optionchain_captest"
export IB_GATEWAY_HOST="${IB_GATEWAY_HOST:-100.66.147.98}"
export IB_GATEWAY_PORT="${IB_GATEWAY_PORT:-4001}"
export IB_CLIENT_ID_SNAPSHOTTER="${IB_CLIENT_ID_SNAPSHOTTER:-980}"  # 901/902 reserved for prod stack

ts="$(date +%Y%m%d-%H%M%S)"
LOG="/tmp/oc-capacity-${ts}.log"
echo "snapshotter -> IB ${IB_GATEWAY_HOST}:${IB_GATEWAY_PORT} clientId=${IB_CLIENT_ID_SNAPSHOTTER}  DB=optionchain_captest"
echo "log: ${LOG}"
# Redirect directly — no tee pipe, so SIGPIPE from a parent consumer never kills the daemon.
exec uv run python -m xenon.option_chain_snapshotter >> "${LOG}" 2>&1
