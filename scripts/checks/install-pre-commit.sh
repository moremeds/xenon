#!/usr/bin/env bash
# Install order-path guards as a local pre-commit hook.
#
# Optional — CI runs the same guards on every PR via
# .github/workflows/ci.yml::order-path-guards. This script is for
# developers who want the failure to surface at `git commit` time.
#
# Idempotent. Skips if a pre-commit hook with the marker is already
# installed. Refuses to overwrite an unmarked existing hook (safety).

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOK_PATH="$REPO_ROOT/.git/hooks/pre-commit"
MARKER="# managed-by: scripts/checks/install-pre-commit.sh"

if [[ -f "$HOOK_PATH" ]] && ! grep -q "$MARKER" "$HOOK_PATH"; then
  echo "Refusing to overwrite existing $HOOK_PATH (no managed marker)." >&2
  echo "Move it aside or merge the body of this installer into it." >&2
  exit 1
fi

mkdir -p "$(dirname "$HOOK_PATH")"
cat > "$HOOK_PATH" <<EOF
#!/usr/bin/env bash
$MARKER
# Order-path regression-prevention guards.
# See docs/plans/2026-04-28-order-path-regression-prevention.md
set -euo pipefail
REPO_ROOT="\$(git rev-parse --show-toplevel)"
cd "\$REPO_ROOT"
python3 scripts/checks/no_json_fallback_on_order_path.py
python3 scripts/checks/order_path_caller_allowlist.py
EOF
chmod +x "$HOOK_PATH"
echo "Installed pre-commit guard at $HOOK_PATH"
echo "Bypass with --no-verify if you really need to (and document why)."
