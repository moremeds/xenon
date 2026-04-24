#!/usr/bin/env bash
# Interactive release cut: preflight → bump → CHANGELOG rewrite → commit → tag.
# Does NOT push. Operator reviews and pushes manually.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
# shellcheck source=scripts/release/_lib.sh
. "$ROOT/scripts/release/_lib.sh"

say()  { printf '\033[1;34m> %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mFAIL: %s\033[0m\n' "$*" >&2; exit 1; }

# --- Preflight ---
say "Preflight checks"

[[ "$(git symbolic-ref --short HEAD)" == "master" ]] || die "not on master"
git diff --quiet && git diff --cached --quiet || die "working tree dirty"
git fetch origin master >/dev/null
[[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/master)" ]] || die "local master not synced with origin"

say "Checking CI status for origin/master HEAD"
head_sha="$(git rev-parse origin/master)"
# Target the CI workflow specifically on the exact SHA we're about to release.
# Plain `--branch master --limit 1` can return unrelated workflow runs or stale results.
conclusion="$(gh run list --workflow CI --commit "$head_sha" --limit 1 --json conclusion --jq '.[0].conclusion // "missing"')"
[[ "$conclusion" == "success" ]] || die "CI run for $head_sha is '$conclusion' (need 'success')"

grep -q '^## \[Unreleased\]' CHANGELOG.md || die "CHANGELOG missing [Unreleased] section"
unreleased_body="$(awk '/^## \[Unreleased\]/{flag=1; next} /^## \[/{flag=0} flag' CHANGELOG.md | sed '/^$/d')"
[[ -n "$unreleased_body" ]] || die "CHANGELOG [Unreleased] is empty — nothing to release"

current="$(cat VERSION)"
say "Current version: $current"

# --- Interactive bump ---
printf 'Bump type? [patch/minor/major/custom]: '
read -r bump_kind
case "$bump_kind" in
  patch|minor|major) next="$(bump_semver "$current" "$bump_kind")" ;;
  custom)
    printf 'Enter new version (no v prefix): '
    read -r next
    [[ "$next" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "invalid semver: $next"
    ;;
  *) die "unknown bump kind" ;;
esac

say "New version: $next"
git rev-parse "v$next" >/dev/null 2>&1 && die "tag v$next already exists"

# --- Preview ---
today="$(date +%Y-%m-%d)"
cat <<EOF
Will:
  - rewrite VERSION: $current -> $next
  - rewrite package.json version: $current -> $next
  - CHANGELOG: insert '## [$next] — $today' below Unreleased, move current Unreleased bullets under it
  - commit: 'release: v$next'
  - annotated tag: v$next (message = CHANGELOG section)
EOF
printf 'Proceed? [y/N]: '
read -r confirm
[[ "$confirm" == "y" || "$confirm" == "Y" ]] || die "aborted"

# --- Mutate ---
echo "$next" > VERSION

# package.json: surgical python rewrite to avoid reordering keys
tmp="$(mktemp)"
python3.13 - "$current" "$next" <<'PY' > "$tmp"
import json, sys
current, nxt = sys.argv[1], sys.argv[2]
with open("package.json") as f:
    data = json.load(f)
assert data["version"] == current, f"expected {current}, got {data['version']}"
data["version"] = nxt
print(json.dumps(data, indent=2))
PY
mv "$tmp" package.json

# CHANGELOG rewrite — single deterministic pass.
# Move the body currently under ## [Unreleased] to a new ## [X.Y.Z] — DATE heading,
# leave [Unreleased] empty. Preserves the rest of the file verbatim.
python3.13 - "$next" "$today" <<'PY'
import re, sys
nxt, today = sys.argv[1], sys.argv[2]
path = "CHANGELOG.md"
with open(path) as f:
    text = f.read()

# Match [Unreleased] heading, capture body up to the next ^## [  heading (or EOF).
m = re.search(
    r"^(## \[Unreleased\]\s*?\n)(.*?)(?=^## \[|\Z)",
    text, flags=re.MULTILINE | re.DOTALL,
)
assert m, "CHANGELOG missing [Unreleased] section"
body = m.group(2).rstrip() + "\n" if m.group(2).strip() else ""
new_section = f"## [Unreleased]\n\n## [{nxt}] — {today}\n\n{body}"
updated = text[:m.start()] + new_section + text[m.end():]
assert updated != text, "CHANGELOG rewrite produced no change"
# Sanity: the rest of the file (post-new-section) must be unchanged.
assert text[m.end():] in updated, "CHANGELOG tail was altered"
with open(path, "w") as f:
    f.write(updated)
PY

git add VERSION package.json CHANGELOG.md
git commit -m "release: v$next"

section="$(extract_changelog_section CHANGELOG.md "$next")"
git tag -a "v$next" -m "v$next

$section"

say "Tagged v$next. To publish:"
echo "  git push origin master --follow-tags"
say "Or to undo:"
echo "  git tag -d v$next && git reset --hard HEAD~1"
