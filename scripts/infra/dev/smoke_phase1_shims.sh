#!/usr/bin/env bash
# Smoke test: exercise --help on every Phase 1 shim to catch import/path
# regressions before they reach a scheduled job. Re-runnable on laptop + VPS.
# Source of truth for the shim list lives inline (CLIS array below) — adding
# or retiring a shim is a one-line edit. Removed when Phase 2 PR 4 deletes shims.
#
# Usage:
#   bash scripts/infra/dev/smoke_phase1_shims.sh
#   bash scripts/smoke_phase1_shims.sh        # via symlink
#
# Per docs/superpowers/specs/2026-04-18-scripts-reorg/phase2-plan.md PR 0 step 2.

set -u

# Resolve symlinks so the symlink at scripts/smoke_phase1_shims.sh works the
# same as direct invocation at scripts/infra/dev/smoke_phase1_shims.sh.
script_path="$0"
while [ -L "$script_path" ]; do
  link_target="$(readlink "$script_path")"
  case "$link_target" in
    /*) script_path="$link_target" ;;
    *)  script_path="$(dirname "$script_path")/$link_target" ;;
  esac
done
cd "$(dirname "$script_path")/../../.."   # repo root, regardless of invocation cwd

# Make the `xenon` package importable under bare `python3.13` without requiring
# a system-wide `pip install -e .`. As Phase 2 bucket moves land, runpy-gated
# shims resolve `xenon.<bucket>.<name>` via sys.path. Exported (not prefixed)
# so `bash` wrappers in CLIS inherit it too.
export PYTHONPATH="$(pwd)/src${PYTHONPATH:+:$PYTHONPATH}"

CLIS=(
  # --- Python shims (Phase 1 baseline from phase1-design.md §Baseline) ---
  # Note: ta_cli.py and ta_premarket_prep.py were in the original baseline
  # but were dropped from the codebase in commit 33b96e77 (Apr 17, 2026,
  # "chore: drop ta_premarket_prep, ta_reseed_massive, api_status, ta_cli...").
  # They are intentionally absent here.
  "python3.13 scripts/fetch_flow.py --help"
  "python3.13 scripts/fetch_ticker.py --help"
  "python3.13 scripts/fetch_analyst_ratings.py --help"
  "python3.13 scripts/fetch_menthorq_dashboard.py --help"
  "python3.13 scripts/discover.py --help"
  "python3.13 scripts/scanner.py --help"
  "python3.13 scripts/kelly.py --help"
  "python3.13 scripts/evaluate.py --help"
  "python3.13 scripts/ib_order_manage.py --help"
  "python3.13 scripts/ib_sync.py --help"
  "python3.13 scripts/ib_option_chain.py --help"
  "python3.13 scripts/leap_scanner_uw.py --help"
  "python3.13 scripts/trend_scan.py --help"
  "python3.13 scripts/uw_scan.py --help"
  "python3.13 scripts/uw_analyze.py --help"
  "python3.13 scripts/cri_scan.py --help"
  "python3.13 scripts/vcg_scan.py --help"
  "python3.13 scripts/gex_scan.py --help"
  "python3.13 scripts/generate_gex_share.py --help"
  "python3.13 scripts/generate_regime_share.py --help"
  "python3.13 scripts/generate_cta_share.py --help"
  "python3.13 scripts/generate_vcg_share.py --help"
  "python3.13 scripts/test_ib_realtime.py --help"
  # --- New in PR 0 (added by this branch) ---
  "python3.13 scripts/apex_refresh.py --help"
  # --- Shell wrappers ---
  "bash scripts/run_cri_scan.sh --help"
  "bash scripts/run_cta_sync.sh --help"
)

failures=0
for cmd in "${CLIS[@]}"; do
  if eval "$cmd" >/dev/null 2>&1; then
    printf 'OK   %s\n' "$cmd"
  else
    printf 'FAIL %s\n' "$cmd"
    failures=$((failures + 1))
  fi
done

echo
if (( failures > 0 )); then
  echo "$failures shim(s) failed — investigate before any move PR."
  exit 1
fi
echo "All shims green."
