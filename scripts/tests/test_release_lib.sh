#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck source=scripts/release/_lib.sh
. release/_lib.sh

assert_eq() {
  if [[ "$1" != "$2" ]]; then
    echo "FAIL: expected $2, got $1" >&2
    exit 1
  fi
}

assert_eq "$(bump_semver 0.0.1 patch)" "0.0.2"
assert_eq "$(bump_semver 0.1.9 minor)" "0.2.0"
assert_eq "$(bump_semver 1.2.3 major)" "2.0.0"

CHANGELOG_FIXTURE="$(mktemp)"
cat >"$CHANGELOG_FIXTURE" <<'EOF'
## [Unreleased]

## [0.0.1] — 2026-04-24

- First release.
EOF

assert_eq "$(extract_changelog_section "$CHANGELOG_FIXTURE" 0.0.1 | head -1)" "- First release."

echo "OK"
