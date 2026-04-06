#!/usr/bin/env bash
# Remove dead code identified by the audit hook.
#
# Usage:
#   ./scripts/cleanup-dead-code.sh [--dry-run] [FILE_OR_DIR ...]
#
# Modes:
#   No args     — reads .claude/hooks/dead-code.manifest (written by audit hook)
#   With args   — removes only the specified paths
#   --dry-run   — preview what would be removed without deleting anything

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

MANIFEST="$PROJECT_ROOT/.claude/hooks/dead-code.manifest"
DRY_RUN=false
targets=()

# ── Parse args ────────────────────────────────────────────
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    *)         targets+=("$arg") ;;
  esac
done

# ── Load targets ──────────────────────────────────────────
# If no explicit targets, read from the manifest written by the audit hook.
if [[ ${#targets[@]} -eq 0 ]]; then
  if [[ ! -f "$MANIFEST" ]]; then
    echo "No manifest found at .claude/hooks/dead-code.manifest"
    echo "Run the audit first:  .claude/hooks/audit-dead-code.sh"
    echo "Or pass paths directly: ./scripts/cleanup-dead-code.sh path1 path2 ..."
    exit 1
  fi

  # Manifest format: "relative_path<TAB>reason" per line
  while IFS=$'\t' read -r rel reason; do
    [[ -z "$rel" ]] && continue
    targets+=("$rel")
  done < "$MANIFEST"

  if [[ ${#targets[@]} -eq 0 ]]; then
    echo "✅ Manifest is empty — nothing to remove."
    exit 0
  fi
fi

# ── Execute removal ───────────────────────────────────────
if $DRY_RUN; then
  echo "=== DRY RUN — no files will be deleted ==="
  echo ""
fi

removed=0
skipped=0

for rel in "${targets[@]}"; do
  path="$PROJECT_ROOT/$rel"

  if [[ ! -e "$path" ]]; then
    skipped=$((skipped + 1))
    continue
  fi

  if $DRY_RUN; then
    if [[ -d "$path" ]]; then
      echo "  [would remove] $rel/"
    else
      echo "  [would remove] $rel"
    fi
  else
    if [[ -d "$path" ]]; then
      rm -rf "$path"
      echo "  ✗ $rel/"
    else
      rm "$path"
      echo "  ✗ $rel"
    fi
  fi
  removed=$((removed + 1))
done

# ── Summary ───────────────────────────────────────────────
echo ""
if [[ $removed -eq 0 && $skipped -gt 0 ]]; then
  echo "✅ All $skipped item(s) already removed — codebase is clean."
elif [[ $removed -eq 0 ]]; then
  echo "✅ Nothing to remove."
elif $DRY_RUN; then
  echo "Would remove $removed item(s). Run without --dry-run to execute."
else
  echo "Removed $removed item(s)."
  # Clear the manifest since we've acted on it
  > "$MANIFEST"
fi
