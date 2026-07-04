#!/usr/bin/env bash
# Order-path edit-time guardrail (Layer 1 of the regression-prevention plan).
#
# Triggered by .claude/settings.json PreToolUse on Edit/Write/MultiEdit.
# Reads the tool call JSON from stdin, extracts tool_input.file_path, and
# prints a reminder checklist if the target sits on the order path.
#
# Exits 0 always — this is advisory, not blocking. Layer 2 (pre-commit) is
# the deterministic gate. See:
#   docs/superpowers/plans/_archive/2026-04-28-order-path-regression-prevention.md

set -euo pipefail

# Read the full hook payload from stdin (Claude Code passes a JSON envelope
# with tool_name, tool_input, etc.).
payload="$(cat)"

# Pull file_path. PreToolUse on Edit/Write/MultiEdit always sets it; for
# other tools (e.g. Bash) it's absent and we exit silently.
# Use jq when available, fall back to a tolerant regex grep otherwise.
# Every step must be exit-safe — this hook is advisory and never blocks.
file_path=""
if command -v jq >/dev/null 2>&1; then
  file_path="$(printf '%s' "$payload" | jq -r '.tool_input.file_path // empty' 2>/dev/null)" || file_path=""
else
  file_path="$(printf '%s' "$payload" | grep -oE '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' 2>/dev/null | head -1 | sed -E 's/.*"file_path"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/' 2>/dev/null)" || file_path=""
fi

[[ -z "$file_path" ]] && exit 0

# Match against order-path globs. Match either absolute or repo-relative.
matched=0
case "$file_path" in
  */src/xenon/execution/*) matched=1 ;;
  */src/xenon/api/server.py) matched=1 ;;
  */src/xenon/api/routes/orders*) matched=1 ;;
  */web/app/api/orders/*) matched=1 ;;
  */web/lib/order/*) matched=1 ;;
  */web/lib/nakedShortGuard.ts) matched=1 ;;
  */web/lib/placeOrderBodySchema.ts) matched=1 ;;
  */web/lib/orderReasonCodes.ts) matched=1 ;;
esac

[[ $matched -eq 0 ]] && exit 0

cat <<'EOF'
⛔ ORDER-PATH FILE — checklist before editing

This file is on the order-placement path. Past PRs (#34, #47, #61) have
shipped silent regressions here. Keep these invariants:

1. Never read data/*.json on a runtime/order path. Postgres is the source
   of truth for portfolio, orders, NAV, vcg. Use scoped queries via
   AccountScope; legacy JSON is backfill input only.
2. Every order entrypoint must route through _run_preflight() in
   src/xenon/api/server.py. Do not call ib_place_order directly from
   anywhere except server.py, the module itself, and tests (the CI
   caller allowlist) — in-process bypass has caused regressions twice.
3. client_attempt_id is required, not optional, on every order body.
   Missing it must return INVALID_ORDER_BODY (400) — never accept and
   forward.
4. ReasonCode enum values must each correspond to one specific failure
   mode. Do not overload (STALE_QUOTE used to mean four different things
   — see src/xenon/execution/preflight.py for the split).
5. Naked-short Gate-4 (Gate 4 in CLAUDE.md) is server-side. The web
   route does not enforce it. Do not add a frontend-only guard that
   in-process callers can bypass.

Reference: docs/superpowers/plans/_archive/2026-04-28-order-path-regression-prevention.md
Reference: docs/reference/order-path-incident-history.md
EOF

exit 0
