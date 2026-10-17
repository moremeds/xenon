#!/usr/bin/env bash
# Install all six order-path/CI guards as a local pre-commit hook.
#
# Optional — CI runs the same six guards on every PR via
# .github/workflows/ci.yml::order-path-guards. This script is for
# developers who want the failure to surface at `git commit` time.
#
# Idempotent. Skips if a pre-commit hook with the marker is already
# installed. Refuses to overwrite an unmarked existing hook (safety).

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
# --git-path resolves hooks/pre-commit correctly in linked worktrees too
# (.git is a file there, not a dir).
HOOK_PATH="$(git rev-parse --git-path hooks/pre-commit)"
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
# All six scripts/checks guards — the same set ci.yml::order-path-guards runs.
# Background: docs/reference/order-path-incident-history.md
set -euo pipefail
REPO_ROOT="\$(git rev-parse --show-toplevel)"
cd "\$REPO_ROOT"
for check in \\
  no_json_fallback_on_order_path \\
  no_json_write_on_order_path \\
  order_path_caller_allowlist \\
  no_ib_insync \\
  no_pg_insert_nav_history \\
  no_yahoo_finance
do
  uv run --no-sync python "scripts/checks/\$check.py"
done
EOF
chmod +x "$HOOK_PATH"
echo "Installed pre-commit guard at $HOOK_PATH"
echo "Bypass with --no-verify if you really need to (and document why)."
