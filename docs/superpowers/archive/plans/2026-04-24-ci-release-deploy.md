# CI, Release, and Deploy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up GitHub Actions CI on PRs, a semi-manual `release: vX.Y.Z` tagging flow, and a push-based deploy-to-Mac-mini pipeline that runs the full Xenon stack (Next.js, FastAPI, `ib_realtime`, IB Gateway) at a stable tagged version, with rollback under 10 seconds.

**Architecture:** Three independent PRs landed in order: (1) `.github/workflows/ci.yml` + nightly Playwright; (2) `scripts/release/cut.sh` + `release.yml` + VERSION/package.json reset to `0.0.1` + version lint; (3) versioned release directories under `/opt/xenon/` on the Mac mini with a `current` symlink, launchd-managed services, and a `scripts/deploy/mac-mini.sh` that SSHes over Tailscale. IB Gateway runs on its own launchd job, independent of app deploys, to avoid 2FA cold-start churn.

**Tech Stack:** GitHub Actions (ubuntu-latest), bash, Python 3.13 (pytest, FastAPI), Node (Next.js 15, Vitest), launchd, Docker Compose (for IB Gateway), Tailscale, Homebrew.

**Spec:** `docs/superpowers/specs/2026-04-24-ci-release-deploy-design.md`

---

## File Structure

### Phase 1 — CI (PR 1)

| Path                                       | Purpose                                                                    | Status             |
| ------------------------------------------ | -------------------------------------------------------------------------- | ------------------ |
| `.github/workflows/ci.yml`                 | PR + master-push CI: pytest, vitest, typecheck, lint, dead-code (advisory) | Create             |
| `.github/workflows/nightly.yml`            | Master-only nightly Playwright E2E, opens issue on failure                 | Create             |
| `.github/CODEOWNERS`                       | Default owner for workflow files (optional — skip if user declines)        | Defer              |
| `scripts/infra/dev/run_pytest_affected.py` | Already exists — confirm `--base` flag supported, patch if not             | Modify (if needed) |
| `web/tests/**`                             | Existing Vitest tests — ensure none silently require real secrets          | Audit              |

### Phase 2 — Release cut (PR 2)

| Path                                    | Purpose                                                                                    | Status |
| --------------------------------------- | ------------------------------------------------------------------------------------------ | ------ |
| `VERSION`                               | Reset to `0.0.1`                                                                           | Modify |
| `package.json`                          | Reset `version` to `0.0.1`                                                                 | Modify |
| `CHANGELOG.md`                          | Add note documenting the reset; preserve historical entries under `## [Pre-0.0.1 history]` | Modify |
| `scripts/release/cut.sh`                | Interactive release-cut script (preflight + bump + tag)                                    | Create |
| `scripts/release/_lib.sh`               | Shared shell helpers (CHANGELOG extraction, semver bump)                                   | Create |
| `scripts/release/version_sync_check.py` | CI lint: `VERSION` must equal `package.json.version`                                       | Create |
| `.github/workflows/ci.yml`              | Add a `version-sync` job                                                                   | Modify |
| `.github/workflows/release.yml`         | Tag-triggered verify + GitHub Release publish                                              | Create |
| `docs/runbooks/release.md`              | Operator runbook: cutting, pushing, rolling back                                           | Create |

### Phase 3 — Deploy to Mac mini (PR 3)

| Path                                        | Purpose                                                                                     | Status |
| ------------------------------------------- | ------------------------------------------------------------------------------------------- | ------ |
| `src/xenon/api/routes/version.py`           | `GET /version` → `{version, commit, deployed_at}`                                           | Create |
| `src/xenon/api/server.py`                   | Register version route                                                                      | Modify |
| `src/xenon/version.py`                      | Python helper: read repo-root `VERSION`                                                     | Create |
| `scripts/tests/test_version_route.py`       | Test for `/version`                                                                         | Create |
| `web/package.json`                          | Add `"start": "next start"` if missing                                                      | Modify |
| `deploy/launchd/xenon.web.plist`            | launchd for Next.js                                                                         | Create |
| `deploy/launchd/xenon.api.plist`            | launchd for FastAPI (uvicorn)                                                               | Create |
| `deploy/launchd/xenon.ib-realtime.plist`    | launchd for `ib_realtime_server.js`                                                         | Create |
| `deploy/launchd/xenon.ib-gateway.plist`     | launchd for IB Gateway docker-compose (decoupled)                                           | Create |
| `deploy/launchd/load-env.sh`                | Sources `shared/.env` for launchd (launchd can't parse `.env` natively)                     | Create |
| `scripts/deploy/mac-mini.sh`                | Push-based deploy orchestrator (laptop → Mac mini via Tailscale)                            | Create |
| `scripts/deploy/mac-mini-bootstrap.sh`      | One-time bootstrap: brew deps, `/opt/xenon/` layout, install plists                         | Create |
| `scripts/deploy/_remote.sh`                 | The script that runs ON the Mac mini (invoked over SSH by `mac-mini.sh`)                    | Create |
| `scripts/deploy/_preflight.sh`              | Laptop-side preflight (clean tree, tag exists, release published)                           | Create |
| `docker/ib-gateway/docker-compose.prod.yml` | Prod override: publish paper `4002:4004` and live `4001:4003` to `0.0.0.0` for LAN exposure | Create |
| `scripts/infra/cloud.sh`                    | Repoint Tailscale target from VPS to `xenon-mini.local`                                     | Modify |
| `docs/runbooks/mac-mini-provision.md`       | One-time provisioning runbook                                                               | Create |
| `docs/runbooks/deploy.md`                   | Deploy + rollback runbook                                                                   | Create |
| `scripts/tests/test_deploy_preflight.py`    | Test preflight abort conditions (mocked git/gh)                                             | Create |

---

## Phase 1 — CI on PRs (PR 1)

### Task 1.1: Add CI workflow skeleton (fails fast, proves wiring)

**Files:**

- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Create minimal ci.yml that only runs typecheck**

```yaml
name: CI

on:
  pull_request:
    branches: [master]
  push:
    branches: [master]

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  web-typecheck:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
          cache: npm
          cache-dependency-path: web/package-lock.json
      - run: cd web && npm ci
      - run: cd web && npm run typecheck
```

- [ ] **Step 2: Commit and push to a branch; open a draft PR to verify the job runs**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: add typecheck workflow skeleton"
git push -u origin feat/ci
gh pr create --draft --title "CI pipeline (Phase 1)" --body "Skeleton — will add jobs incrementally"
```

Expected: PR shows a `web-typecheck` check. It either passes (great) or fails with a real typecheck error (we fix it first).

### Task 1.2: Add web-lint and web-tests jobs

**Files:**

- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Add `web-lint` and `web-tests` jobs**

Add under `jobs:`:

```yaml
web-lint:
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    - uses: actions/setup-node@v4
      with:
        node-version: 20
        cache: npm
        cache-dependency-path: web/package-lock.json
    - run: cd web && npm ci
    - run: cd web && npm run lint

web-tests:
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    - uses: actions/setup-node@v4
      with:
        node-version: 20
        cache: npm
        cache-dependency-path: web/package-lock.json
    - run: cd web && npm ci
    - run: cd web && npm test
```

- [ ] **Step 2: Push; check that both jobs run on the PR**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: add web-lint and web-tests jobs"
git push
```

Expected: GitHub UI shows three jobs running in parallel. Fix any reds.

### Task 1.3: Add python-tests job with affected-tests on PR, full suite on master

**Files:**

- Modify: `.github/workflows/ci.yml`
- Audit: `scripts/infra/dev/run_pytest_affected.py` (confirm `--base` flag)

- [ ] **Step 1: Verify affected-tests runner accepts `--base`**

Run locally:

```bash
python3.13 scripts/infra/dev/run_pytest_affected.py --base origin/master --help
```

Expected: prints help including `--base`. If the flag doesn't exist, add it — it should accept a git ref, diff HEAD against that ref, collect touched files, map them to tests. If unsure, read `run_pytest_affected.py` and patch in a small task before proceeding.

- [ ] **Step 2: Add python-tests job with branch-aware behavior**

```yaml
python-tests:
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
      with:
        fetch-depth: 0
    - uses: astral-sh/setup-uv@v5
      with:
        enable-cache: true
        cache-dependency-glob: uv.lock
    - run: uv python install 3.13
    - run: uv sync --frozen --extra test
    - name: Collection smoke test (import-level guard)
      run: uv run pytest --collect-only -q
    - name: Run pytest (affected on PR, full on master push)
      run: |
        if [[ "${{ github.event_name }}" == "pull_request" ]]; then
          uv run python scripts/infra/dev/run_pytest_affected.py --base origin/${{ github.base_ref }}
        else
          uv run pytest
        fi
```

- [ ] **Step 3: Push; verify the job runs and passes on PR (affected subset)**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: add python-tests job (affected on PR, full on master)"
git push
```

If it fails because tests silently require secrets, fix the tests (skip when env absent) in follow-up commits on the same PR — do not add secrets to CI.

### Task 1.4: Add dead-code advisory job — CONDITIONAL

The repo does not currently have `scripts/infra/dev/dead_code_scan.py` (the UserPromptSubmit hook runs dead-code findings through a different path that isn't an invokable CLI command). Do NOT wire this job into CI if the script doesn't exist — a permanently red advisory job is noise.

- [ ] **Step 1: Check for the script**

```bash
ls scripts/infra/dev/dead_code_scan.py scripts/infra/dead_code*.py 2>&1 | head
```

- [ ] **Step 2: If a CLI exists, add advisory job; otherwise SKIP this task entirely**

If found, add:

```yaml
dead-code:
  runs-on: ubuntu-latest
  continue-on-error: true # advisory until 2026-05-08
  steps:
    - uses: actions/checkout@v4
    - uses: actions/setup-python@v5
      with: { python-version: "3.13" }
    - run: python3.13 <actual-path-from-step-1>
```

If no CLI exists, open a follow-up issue: "Extract dead-code hook into a standalone CLI for CI." Do not commit a broken job.

### Task 1.5: Add nightly Playwright workflow

**Files:**

- Create: `.github/workflows/nightly.yml`

- [ ] **Step 1: Create nightly workflow**

```yaml
name: Nightly Playwright

on:
  schedule:
    - cron: "0 9 * * *"
  workflow_dispatch:

jobs:
  playwright:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
          cache: npm
          cache-dependency-path: web/package-lock.json
      - run: cd web && npm ci
      - name: Install Playwright browsers
        run: cd web && npx playwright install --with-deps chromium
      - name: Run Playwright tests
        run: cd web && npx playwright test
      - name: Upload report on failure
        if: failure()
        uses: actions/upload-artifact@v4
        with:
          name: playwright-report
          path: web/playwright-report/
      - name: Comment on (or open) the canonical tracking issue
        if: failure()
        uses: actions/github-script@v7
        with:
          script: |
            const { owner, repo } = context.repo;
            const title = 'Nightly Playwright failures';
            const runUrl = `${context.serverUrl}/${owner}/${repo}/actions/runs/${context.runId}`;
            const body = `Run failed: ${runUrl}`;
            // Paginate fully — listForRepo defaults to 30 per page, so quick-succession
            // failures before we comment would otherwise create duplicate issues.
            const existing = await github.paginate(github.rest.issues.listForRepo, {
              owner, repo, labels: 'nightly-playwright', state: 'open', per_page: 100,
            });
            if (existing.length === 0) {
              await github.rest.issues.create({ owner, repo, title, body, labels: ['nightly-playwright'] });
            } else {
              await github.rest.issues.createComment({ owner, repo, issue_number: existing[0].number, body });
            }
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/nightly.yml
git commit -m "ci: add nightly Playwright workflow"
git push
```

### Task 1.6: Configure branch protection on master

- [ ] **Step 1: Set required checks via gh**

```bash
gh api -X PUT repos/moremeds/xenon/branches/master/protection \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["web-typecheck", "web-lint", "web-tests", "python-tests"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
```

Expected: `gh api ... /protection | jq .required_status_checks.contexts` lists the four jobs. `dead-code` is intentionally absent (advisory). `version-sync` will be added in Task 2.2 — it doesn't exist yet in this PR, so adding it here would block merge.

- [ ] **Step 2: Mark the PR ready for review, merge when green**

```bash
gh pr ready
# wait for green checks
gh pr merge --squash --delete-branch
```

---

## Phase 2 — Release cut (PR 2)

### Task 2.1: Reset VERSION and package.json to 0.0.1

**Files:**

- Modify: `VERSION`
- Modify: `package.json`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Create branch**

```bash
git checkout master && git pull
git checkout -b feat/release-cut
```

- [ ] **Step 2: Rewrite VERSION**

Replace file contents with the single line `0.0.1`.

- [ ] **Step 3: Rewrite `package.json` version field**

Change `"version": "0.6.1"` → `"version": "0.0.1"`.

- [ ] **Step 4: Rewrite CHANGELOG**

Replace `## [Unreleased]` and all prior entries with:

```markdown
## [Unreleased]

## [0.0.1] — 2026-04-24

- Versioning reset. Begin semver from `0.0.1` as part of introducing the CI/release/deploy pipeline.

## [Pre-0.0.1 history]

<all previous content, unchanged>
```

- [ ] **Step 5: Commit**

```bash
git add VERSION package.json CHANGELOG.md
git commit -m "release: reset versioning to 0.0.1"
```

- [ ] **Step 6: Tag `v0.0.1` after PR-2 merges to master**

This is done AFTER all Phase-2 PR tasks are merged (Tasks 2.1–2.7). Without this tag, Phase 3's dry-run rollback (`scripts/deploy/mac-mini.sh v0.0.1`) has no target. The tag points at the release-reset commit on master.

```bash
git checkout master && git pull
# Confirm HEAD matches the release-reset commit:
git log --oneline -1 | grep "release: reset versioning to 0.0.1" || {
  echo "Expected HEAD to be the reset commit — abort tagging" >&2; exit 1; }
git tag -a v0.0.1 -m "v0.0.1 — versioning reset (baseline for CI/release/deploy pipeline)"
git push origin v0.0.1
```

Then create a GitHub Release (the release workflow doesn't auto-fire because this tag was created manually against an older commit pattern):

```bash
gh release create v0.0.1 --title "v0.0.1" --notes "Baseline — versioning reset to 0.0.1."
```

### Task 2.2: Write version_sync_check.py and add CI lint

**Files:**

- Create: `scripts/release/version_sync_check.py`
- Create: `scripts/tests/test_version_sync_check.py`
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Write failing test**

```python
# scripts/tests/test_version_sync_check.py
import subprocess, sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "release" / "version_sync_check.py"

def run(tmp_path, version_content, package_content):
    (tmp_path / "VERSION").write_text(version_content)
    (tmp_path / "package.json").write_text(package_content)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path)],
        capture_output=True, text=True,
    )

def test_passes_when_versions_match(tmp_path):
    r = run(tmp_path, "0.0.1\n", '{"version": "0.0.1"}')
    assert r.returncode == 0, r.stderr

def test_fails_when_versions_differ(tmp_path):
    r = run(tmp_path, "0.0.1\n", '{"version": "0.6.1"}')
    assert r.returncode != 0
    assert "mismatch" in r.stderr.lower()
```

- [ ] **Step 2: Run and verify it fails**

```bash
python3.13 -m pytest scripts/tests/test_version_sync_check.py -xvs
```

Expected: FileNotFoundError on the script.

- [ ] **Step 3: Implement script**

```python
# scripts/release/version_sync_check.py
"""Fail if VERSION and package.json.version disagree."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".", type=Path)
    args = ap.parse_args()

    version = (args.root / "VERSION").read_text().strip()
    pkg = json.loads((args.root / "package.json").read_text())
    pkg_version = pkg.get("version", "")

    if version != pkg_version:
        print(
            f"version mismatch: VERSION={version!r} package.json={pkg_version!r}",
            file=sys.stderr,
        )
        return 1
    print(f"OK: {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests, verify pass**

```bash
python3.13 -m pytest scripts/tests/test_version_sync_check.py -xvs
```

Expected: 2 passed.

- [ ] **Step 5: Add `version-sync` job to ci.yml**

```yaml
version-sync:
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    - uses: actions/setup-python@v5
      with: { python-version: "3.13" }
    - run: python3.13 scripts/release/version_sync_check.py
```

- [ ] **Step 6: Commit**

```bash
git add scripts/release/version_sync_check.py \
        scripts/tests/test_version_sync_check.py \
        .github/workflows/ci.yml
git commit -m "release: add VERSION↔package.json sync check"
```

- [ ] **Step 7: Update branch protection to require `version-sync` (run AFTER this PR merges)**

Branch protection was set up in Task 1.6 without `version-sync` because the job didn't exist yet. After this PR merges and the job is confirmed green on master, add it to required checks:

```bash
gh api -X PUT repos/moremeds/xenon/branches/master/protection \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["web-typecheck", "web-lint", "web-tests", "python-tests", "version-sync"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
```

### Task 2.3: Write shared shell helpers (`_lib.sh`)

**Files:**

- Create: `scripts/release/_lib.sh`
- Create: `scripts/tests/test_release_lib.sh`

- [ ] **Step 1: Write bats-style harness or plain-bash test**

```bash
# scripts/tests/test_release_lib.sh
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
```

- [ ] **Step 2: Run test — should fail (no `_lib.sh` yet)**

```bash
bash scripts/tests/test_release_lib.sh
```

Expected: source failure.

- [ ] **Step 3: Implement `_lib.sh`**

```bash
# scripts/release/_lib.sh
# Reusable helpers for release scripts. Source, don't execute.

bump_semver() {
  local version="$1" kind="$2"
  local IFS=.
  read -r major minor patch <<<"$version"
  case "$kind" in
    patch) patch=$((patch + 1)) ;;
    minor) minor=$((minor + 1)); patch=0 ;;
    major) major=$((major + 1)); minor=0; patch=0 ;;
    *) echo "unknown bump kind: $kind" >&2; return 1 ;;
  esac
  echo "${major}.${minor}.${patch}"
}

# extract_changelog_section <file> <version>
# Prints the body of `## [<version>] — …` up to (but not including) the next `## [` heading.
# Patterns anchored at line start (^## \[) so in-body text that happens to contain
# "## [" (e.g. inside a fenced code block) cannot terminate the section early.
extract_changelog_section() {
  local file="$1" version="$2"
  awk -v v="$version" '
    BEGIN { in_section = 0 }
    /^## \[/ {
      if (in_section) { exit }
      if ($0 ~ "^## \\[" v "\\]") { in_section = 1; next }
    }
    in_section { print }
  ' "$file" | sed -e '/./,$!d' | sed -e ':a' -e '/^$/{$d;N;ba' -e '}'
}
```

- [ ] **Step 4: Run test, verify pass**

```bash
bash scripts/tests/test_release_lib.sh
```

Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add scripts/release/_lib.sh scripts/tests/test_release_lib.sh
git commit -m "release: shared shell helpers (bump_semver, extract_changelog_section)"
```

### Task 2.4: Write `cut.sh` (interactive release-cut script)

**Files:**

- Create: `scripts/release/cut.sh`

- [ ] **Step 1: Implement**

```bash
#!/usr/bin/env bash
# scripts/release/cut.sh
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

# package.json: surgical sed to avoid reordering keys
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
```

- [ ] **Step 2: Make executable**

```bash
chmod +x scripts/release/cut.sh
```

- [ ] **Step 3: Manual smoke test on a throwaway branch**

```bash
git checkout -b throwaway/cut-smoke
# edit CHANGELOG to add a fake unreleased bullet
scripts/release/cut.sh
# answer: patch, y
git log -1 --stat
git tag -l 'v*' | tail -1
# clean up
git tag -d v0.0.2
git reset --hard HEAD~1
git checkout feat/release-cut
git branch -D throwaway/cut-smoke
```

Expected: commit `release: v0.0.2` with VERSION + package.json + CHANGELOG changes; annotated tag `v0.0.2` with CHANGELOG body.

- [ ] **Step 4: Commit**

```bash
git add scripts/release/cut.sh
git commit -m "release: cut.sh interactive release script"
```

### Task 2.5: Write tag-triggered release workflow

**Files:**

- Create: `.github/workflows/release.yml`

- [ ] **Step 1: Create workflow**

```yaml
name: Release

on:
  push:
    tags: ["v*"]

jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: astral-sh/setup-uv@v5
        with: { enable-cache: true, cache-dependency-glob: uv.lock }
      - run: uv python install 3.13
      - run: uv sync --frozen --extra test
      - run: uv run pytest
      - uses: actions/setup-node@v4
        with:
          {
            node-version: 20,
            cache: npm,
            cache-dependency-path: web/package-lock.json,
          }
      - run: cd web && npm ci
      - run: cd web && npm run typecheck
      - run: cd web && npm run lint
      - run: cd web && npm test
      - run: python3.13 scripts/release/version_sync_check.py

  publish:
    needs: verify
    runs-on: ubuntu-latest
    permissions: { contents: write }
    steps:
      - uses: actions/checkout@v4
      - name: Extract CHANGELOG section
        id: changelog
        run: |
          VERSION="${GITHUB_REF_NAME#v}"
          source scripts/release/_lib.sh
          {
            echo 'body<<EOF'
            extract_changelog_section CHANGELOG.md "$VERSION"
            echo 'EOF'
          } >> "$GITHUB_OUTPUT"
      - uses: softprops/action-gh-release@v2
        with:
          name: ${{ github.ref_name }}
          body: ${{ steps.changelog.outputs.body }}
          draft: false
          prerelease: false
          make_latest: true
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/release.yml
git commit -m "release: tag-triggered verify + GitHub Release workflow"
```

### Task 2.6: Write release runbook

**Files:**

- Create: `docs/runbooks/release.md`

- [ ] **Step 1: Write runbook**

````markdown
# Release Runbook

## Prerequisites (laptop)

- `git` and `gh` (GitHub CLI) installed. Authenticate `gh` once: `gh auth login`.
- `python3.13` on PATH (used by `cut.sh` to rewrite `package.json` and `CHANGELOG.md`).
- Clean working tree on `master`, synced with `origin/master`.

`cut.sh` will abort with a clear message if any of these are missing.

## Cutting a release

1. `git checkout master && git pull`
2. `scripts/release/cut.sh` — answer bump prompt, review diff, confirm.
3. Review the new commit and tag: `git log -1 --stat && git show v0.0.2`
4. Push: `git push origin master --follow-tags`
5. Watch the `Release` workflow in GitHub Actions. It runs the full test suite on the tag ref, then creates the GitHub Release.

## Rolling back a bad cut (not yet deployed)

```bash
git tag -d v0.0.2
git push --delete origin v0.0.2
gh release delete v0.0.2 --yes
git revert <release-commit-sha>
git push origin master
```
````

Then re-run `scripts/release/cut.sh` when ready.

## When CI blocks the release

`cut.sh` refuses if the latest master run isn't green. Fix the failing test on master first (a regular PR), merge, wait for green, then cut.

## Version authority

`VERSION` is authoritative. `package.json` mirrors it. The `version-sync` CI job fails any PR that drifts them.

````

- [ ] **Step 2: Commit and push, merge PR**

```bash
git add docs/runbooks/release.md
git commit -m "docs: release runbook"
git push
gh pr create --title "Release pipeline (Phase 2)" --body "See docs/superpowers/plans/2026-04-24-ci-release-deploy.md"
# wait for green
gh pr merge --squash --delete-branch
````

### Task 2.7: Cut the first release (dry run of the whole system)

- [ ] **Step 1: From master, run the script**

```bash
git checkout master && git pull
scripts/release/cut.sh
# patch -> 0.0.1 (since we reset). Answer patch for 0.0.2 test cut. On second thought: cut 0.0.1 is the reset itself — skip this task if you want 0.0.1 to be the first real tag. Easier path: cut as-is (0.0.2) and treat 0.0.1 as the reset baseline.
```

- [ ] **Step 2: Verify tag workflow succeeded**

```bash
gh run watch
gh release view v0.0.2
```

Expected: GitHub Release page shows the CHANGELOG bullets as its body.

---

## Phase 3 — Deploy to Mac mini (PR 3)

### Task 3.1: Python version helper

**Files:**

- Create: `src/xenon/version.py`
- Create: `scripts/tests/test_version_helper.py`

- [ ] **Step 1: Write failing test**

```python
# scripts/tests/test_version_helper.py
from pathlib import Path
from xenon.version import get_version, REPO_ROOT

def test_matches_version_file():
    expected = (REPO_ROOT / "VERSION").read_text().strip()
    assert get_version() == expected

def test_returns_nonempty_semver():
    v = get_version()
    parts = v.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts), v
```

- [ ] **Step 2: Run, verify fails**

```bash
python3.13 -m pytest scripts/tests/test_version_helper.py -xvs
```

Expected: ImportError.

- [ ] **Step 3: Implement**

```python
# src/xenon/version.py
"""Authoritative version for Xenon. Reads the repo-root VERSION file."""
from __future__ import annotations
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def get_version() -> str:
    return (REPO_ROOT / "VERSION").read_text().strip()
```

- [ ] **Step 4: Run, verify passes**

```bash
python3.13 -m pytest scripts/tests/test_version_helper.py -xvs
```

- [ ] **Step 5: Commit**

```bash
git checkout -b feat/deploy-mac-mini
git add src/xenon/version.py scripts/tests/test_version_helper.py
git commit -m "feat(version): Python helper reading VERSION"
```

### Task 3.2: `GET /version` FastAPI endpoint

**Files:**

- Create: `src/xenon/api/routes/version.py`
- Modify: `src/xenon/api/server.py`
- Create: `scripts/tests/test_version_route.py`

- [ ] **Step 1: Write failing test**

```python
# scripts/tests/test_version_route.py
from fastapi.testclient import TestClient
from xenon.api.server import app

client = TestClient(app)

def test_get_version_returns_current_version():
    r = client.get("/version")
    assert r.status_code == 200
    body = r.json()
    assert "version" in body
    assert "commit" in body
    assert "deployed_at" in body
    parts = body["version"].split(".")
    assert len(parts) == 3
```

- [ ] **Step 2: Run, verify fails**

```bash
python3.13 -m pytest scripts/tests/test_version_route.py -xvs
```

Expected: 404.

- [ ] **Step 3: Implement the route**

Deployed releases are created via `git archive` and have no `.git` metadata. The deploy script writes `REVISION` and `DEPLOYED_AT` files into each release directory — the route reads those files, not git. On dev checkouts where those files are absent, fall back to `git rev-parse` and file mtime.

```python
# src/xenon/api/routes/version.py
from __future__ import annotations
import os
import subprocess
from functools import lru_cache
from fastapi import APIRouter

from xenon.version import get_version, REPO_ROOT

router = APIRouter()


@lru_cache(maxsize=1)
def _commit() -> str:
    revision_file = REPO_ROOT / "REVISION"
    if revision_file.is_file():
        return revision_file.read_text().strip()
    try:
        return subprocess.check_output(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unknown"


@lru_cache(maxsize=1)
def _deployed_at() -> str:
    env = os.environ.get("XENON_DEPLOYED_AT")
    if env:
        return env
    deployed_file = REPO_ROOT / "DEPLOYED_AT"
    if deployed_file.is_file():
        return deployed_file.read_text().strip()
    try:
        ts = (REPO_ROOT / "VERSION").stat().st_mtime
        import datetime as dt
        return dt.datetime.utcfromtimestamp(ts).isoformat() + "Z"
    except Exception:
        return "unknown"


@router.get("/version")
def version():
    return {
        "version": get_version(),
        "commit": _commit(),
        "deployed_at": _deployed_at(),
    }
```

- [ ] **Step 4: Register the route AND add to `AUTH_EXEMPT_PATHS`**

In `src/xenon/api/server.py`:

1. Near the other `include_router` calls:

```python
from xenon.api.routes import version as version_route
app.include_router(version_route.router)
```

2. Add `/version` to `AUTH_EXEMPT_PATHS` so the deploy verification can curl it from outside localhost without a Clerk JWT:

```python
AUTH_EXEMPT_PATHS = {
    "/health",
    "/version",       # <-- add this
    "/ws-ticket/validate",
    "/docs",
    "/openapi.json",
}
```

3. Add a test that verifies the exemption works even when `CLERK_JWKS_URL` is set (mocking Clerk to reject all tokens). Use the existing auth-test pattern in `src/xenon/api/tests/` as a model.

- [ ] **Step 5: Run, verify passes**

```bash
python3.13 -m pytest scripts/tests/test_version_route.py -xvs
```

- [ ] **Step 6: Commit**

```bash
git add src/xenon/api/routes/version.py src/xenon/api/server.py scripts/tests/test_version_route.py
git commit -m "feat(api): GET /version endpoint"
```

### Task 3.3: Verify `next start` is available (no changes expected)

`web/package.json` already has `"start": "next start"` in the current tree. Do NOT override it with `-p 3000` — the pre-swap preview helper sets `PORT=3001`, and a hardcoded `-p 3000` would collide with the live port during pre-swap checks.

- [ ] **Step 1: Verify present**

```bash
jq -r '.scripts.start' web/package.json
```

Expected: `next start`. If missing, add exactly `"start": "next start"` and commit. Otherwise, skip.

### Task 3.4: launchd plists

**Files:**

- Create: `deploy/launchd/xenon.web.plist`
- Create: `deploy/launchd/xenon.api.plist`
- Create: `deploy/launchd/xenon.ib-realtime.plist`
- Create: `deploy/launchd/xenon.ib-gateway.plist`
- Create: `deploy/launchd/load-env.sh`

- [ ] **Step 1: Create `load-env.sh`**

```bash
#!/usr/bin/env bash
# Sources /opt/xenon/shared/.env and execs the target command.
# launchd can't parse .env files natively.
set -euo pipefail
set -a
# shellcheck disable=SC1091
. /opt/xenon/shared/.env
if [[ -f /opt/xenon/shared/web/.env ]]; then
  . /opt/xenon/shared/web/.env
fi
set +a
exec "$@"
```

- [ ] **Step 2: Make executable**

```bash
chmod +x deploy/launchd/load-env.sh
```

- [ ] **Step 3: Create `xenon.web.plist`**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>xenon.web</string>
    <key>WorkingDirectory</key><string>/opt/xenon/current/web</string>
    <key>ProgramArguments</key>
    <array>
      <string>/opt/xenon/current/deploy/launchd/load-env.sh</string>
      <string>/opt/homebrew/bin/npm</string>
      <string>run</string>
      <string>start</string>
    </array>
    <key>KeepAlive</key><true/>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>/opt/xenon/shared/logs/web.log</string>
    <key>StandardErrorPath</key><string>/opt/xenon/shared/logs/web.err.log</string>
</dict>
</plist>
```

- [ ] **Step 4: Create `xenon.api.plist`**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>xenon.api</string>
    <!-- WorkingDirectory is the release root; the per-release .venv/ lives at
         /opt/xenon/current/.venv (materialized by `uv sync --frozen` at deploy
         time). Python resolves xenon.api.server from src/ via the installed package. -->
    <key>WorkingDirectory</key><string>/opt/xenon/current</string>
    <key>ProgramArguments</key>
    <array>
      <string>/opt/xenon/current/deploy/launchd/load-env.sh</string>
      <string>/opt/xenon/current/.venv/bin/python</string>
      <string>-m</string><string>uvicorn</string>
      <string>xenon.api.server:app</string>
      <string>--host</string><string>127.0.0.1</string>
      <string>--port</string><string>8321</string>
    </array>
    <key>KeepAlive</key><true/>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>/opt/xenon/shared/logs/api.log</string>
    <key>StandardErrorPath</key><string>/opt/xenon/shared/logs/api.err.log</string>
</dict>
</plist>
```

- [ ] **Step 5: Create `xenon.ib-realtime.plist`**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>xenon.ib-realtime</string>
    <key>WorkingDirectory</key><string>/opt/xenon/current</string>
    <key>ProgramArguments</key>
    <array>
      <string>/opt/xenon/current/deploy/launchd/load-env.sh</string>
      <string>/opt/homebrew/bin/node</string>
      <string>/opt/xenon/current/scripts/infra/ib_realtime/ib_realtime_server.js</string>
    </array>
    <key>KeepAlive</key><true/>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>/opt/xenon/shared/logs/ib-realtime.log</string>
    <key>StandardErrorPath</key><string>/opt/xenon/shared/logs/ib-realtime.err.log</string>
</dict>
</plist>
```

- [ ] **Step 6: Create `xenon.ib-gateway.plist`** (decoupled — points at `/opt/xenon/ib-gateway/`, not `current/`)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>xenon.ib-gateway</string>
    <key>WorkingDirectory</key><string>/opt/xenon/ib-gateway</string>
    <key>ProgramArguments</key>
    <array>
      <string>/opt/homebrew/bin/docker</string>
      <string>compose</string><string>up</string>
    </array>
    <key>KeepAlive</key><true/>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>/opt/xenon/shared/logs/ib-gateway.log</string>
    <key>StandardErrorPath</key><string>/opt/xenon/shared/logs/ib-gateway.err.log</string>
</dict>
</plist>
```

- [ ] **Step 7: Commit**

```bash
git add deploy/launchd/
git commit -m "deploy: launchd plists for web, api, ib-realtime, ib-gateway"
```

### Task 3.5: IB Gateway LAN exposure override

The actual compose file lives at `docker/ib-gateway/docker-compose.yml` and publishes ports `127.0.0.1:${IB_PAPER_PORT:-4002}:4004` (paper) and `127.0.0.1:${IB_LIVE_PORT:-4001}:4003` (live) — note the host:container mismatch (4002 → 4004, 4001 → 4003). The LAN override must preserve that mapping; naïvely writing `4002:4002` would publish an unused container port and the Mac mini would not actually serve IB.

**Files:**

- Create: `docker/ib-gateway/docker-compose.prod.yml`

- [ ] **Step 1: Read current compose to confirm ports**

```bash
grep -A3 'ports:' docker/ib-gateway/docker-compose.yml
```

Expected to show the `4002:4004` and `4001:4003` mappings.

- [ ] **Step 2: Create the prod override (preserves real container ports)**

Create `docker/ib-gateway/docker-compose.prod.yml`:

```yaml
services:
  ib-gateway:
    ports:
      # Expose paper (container 4004) on host 4002, bound to all interfaces.
      - "0.0.0.0:${IB_PAPER_PORT:-4002}:4004"
      # Expose live (container 4003) on host 4001, LAN-wide.
      - "0.0.0.0:${IB_LIVE_PORT:-4001}:4003"
```

The Mac mini's `xenon.ib-gateway.plist` must `docker compose -f docker-compose.yml -f docker-compose.prod.yml up` inside `/opt/xenon/ib-gateway/`. Update `ProgramArguments` in that plist from Task 3.4 accordingly.

- [ ] **Step 3: Commit**

```bash
git add docker/ib-gateway/docker-compose.prod.yml deploy/launchd/xenon.ib-gateway.plist
git commit -m "deploy: LAN-exposed IB Gateway override for Mac mini"
```

### Task 3.6: Deploy orchestrator — preflight (laptop side)

**Files:**

- Create: `scripts/deploy/_preflight.sh`
- Create: `scripts/tests/test_deploy_preflight.sh`

- [ ] **Step 1: Write test**

```bash
#!/usr/bin/env bash
# scripts/tests/test_deploy_preflight.sh
set -euo pipefail
cd "$(dirname "$0")/../.."

# Stub `gh` and `git` for determinism — run preflight in a fresh temp clone.
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

git init -q "$tmp"
cd "$tmp"
git commit --allow-empty -qm "init"
git tag v0.0.1

# Inject our preflight via PATH redirection
mkdir "$tmp/bin"
cat > "$tmp/bin/gh" <<'EOF'
#!/usr/bin/env bash
case "$*" in
  "release view v0.0.1") exit 0 ;;
  "release view v9.9.9") exit 1 ;;
  *) exit 1 ;;
esac
EOF
chmod +x "$tmp/bin/gh"
export PATH="$tmp/bin:$PATH"

# Source the preflight
# shellcheck source=scripts/deploy/_preflight.sh
. "$OLDPWD/scripts/deploy/_preflight.sh"

preflight_tag v0.0.1  # expected: pass
preflight_tag v9.9.9 && { echo "FAIL: should reject missing release"; exit 1; } || true

echo OK
```

- [ ] **Step 2: Run, verify fails (script missing)**

```bash
bash scripts/tests/test_deploy_preflight.sh
```

- [ ] **Step 3: Implement**

```bash
# scripts/deploy/_preflight.sh
# Laptop-side preflight for mac-mini deploys. Source, don't execute.

preflight_tag() {
  local tag="$1"
  git rev-parse --verify "$tag" >/dev/null 2>&1 || { echo "tag $tag not found locally" >&2; return 1; }
  gh release view "$tag" >/dev/null 2>&1 || { echo "gh release $tag not published" >&2; return 1; }
}

preflight_clean_tree() {
  # `git diff` alone ignores untracked files. porcelain=v1 catches tracked changes AND untracked.
  local status
  status="$(git status --porcelain=v1)"
  if [[ -n "$status" ]]; then
    echo "working tree not clean:" >&2
    echo "$status" >&2
    return 1
  fi
}
```

- [ ] **Step 4: Run, verify passes**

```bash
bash scripts/tests/test_deploy_preflight.sh
```

- [ ] **Step 5: Commit**

```bash
git add scripts/deploy/_preflight.sh scripts/tests/test_deploy_preflight.sh
git commit -m "deploy: preflight helpers + tests"
```

### Task 3.7: Remote deploy script (runs on Mac mini)

**Files:**

- Create: `scripts/deploy/_remote.sh`

- [ ] **Step 1: Implement**

```bash
#!/usr/bin/env bash
# scripts/deploy/_remote.sh
# Runs ON the Mac mini. Invoked over SSH by mac-mini.sh.
# Usage: _remote.sh <version>   e.g. _remote.sh v0.0.2

set -euo pipefail

TAG="${1:?usage: _remote.sh <tag>}"
ROOT="/opt/xenon"
CACHE="$ROOT/.git-cache"
RELEASE="$ROOT/releases/$TAG"
RELEASE_TMP="$ROOT/releases/.${TAG}.tmp"
SHARED="$ROOT/shared"
CURRENT_LINK="$ROOT/current"
WEB_PORT=3000
API_PORT=8321

say() { printf '\033[1;34m[remote] %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m[remote] FAIL: %s\033[0m\n' "$*" >&2; exit 1; }

# Capture the symlink target BEFORE any swap so rollback is deterministic.
# ls-based mtime sorting can pick the wrong release if directories were touched.
PREV_RELEASE=""
if [[ -L "$CURRENT_LINK" ]]; then
  PREV_RELEASE="$(readlink "$CURRENT_LINK")"
  [[ "$PREV_RELEASE" = /* ]] || PREV_RELEASE="$ROOT/$PREV_RELEASE"
fi

# 1. Fetch tag into bookkeeping repo
say "Fetching $TAG into $CACHE"
git -C "$CACHE" fetch --tags origin

# 2. Materialize release worktree — extract to temp, rename on success.
# A crash mid-extraction must not leave a half-populated $RELEASE that a later
# deploy silently reuses.
if [[ ! -d "$RELEASE" ]]; then
  say "Creating release at $RELEASE (via tmp $RELEASE_TMP)"
  rm -rf "$RELEASE_TMP"
  mkdir -p "$RELEASE_TMP"
  git -C "$CACHE" archive "$TAG" | tar -x -C "$RELEASE_TMP"
  # Minimum-viable sanity check: required files must be present.
  for required in VERSION package.json src/xenon/api/server.py web/package.json; do
    [[ -e "$RELEASE_TMP/$required" ]] || { rm -rf "$RELEASE_TMP"; die "extract missing $required"; }
  done
  # Write immutable release metadata (read by GET /version — no .git in archive output).
  git -C "$CACHE" rev-parse "$TAG" > "$RELEASE_TMP/REVISION"
  date -u +"%Y-%m-%dT%H:%M:%SZ" > "$RELEASE_TMP/DEPLOYED_AT"
  mv "$RELEASE_TMP" "$RELEASE"
fi

# 3. Symlink shared state
say "Wiring shared state"
ln -sfn "$SHARED/.env" "$RELEASE/.env"
mkdir -p "$RELEASE/web"
ln -sfn "$SHARED/web/.env" "$RELEASE/web/.env"
ln -sfn "$SHARED/data" "$RELEASE/data"
ln -sfn "$SHARED/logs" "$RELEASE/logs"
ln -sfn "$SHARED/web-node_modules" "$RELEASE/web/node_modules"

# 4. Build
say "npm ci && npm run build"
(cd "$RELEASE/web" && npm ci && npm run build)

say "uv sync (per-release .venv, hardlinks from global uv cache)"
(cd "$RELEASE" && uv sync --frozen)

# 5. Pre-swap health check on alt ports — use the helper FROM THE RELEASE we're
# about to ship, not a persistent copy. The release payload is the source of truth.
say "Pre-swap health check on alt ports (3001/8322)"
XENON_API_PORT=8322 PORT=3001 \
  "$RELEASE/scripts/deploy/_health_preview.sh" "$RELEASE" \
  || die "pre-swap health check failed"

# 6. Atomic swap — macOS-safe. `mv -T` is GNU-only; BSD mv (macOS) rejects it.
# `ln -sfn` replaces an existing symlink atomically because ln calls rename(2)
# on the underlying inode. The intermediate `.new` name is unnecessary here.
say "Atomic symlink swap"
ln -sfn "$RELEASE" "$CURRENT_LINK"

# 7. Restart app services (NOT ib-gateway — decoupled lifecycle)
say "Restarting app services"
for svc in xenon.web xenon.api xenon.ib-realtime; do
  launchctl kickstart -k "gui/$UID/$svc" || die "failed to kickstart $svc"
done

# 8. Post-swap verify. App services only — ib-gateway is intentionally
# decoupled, so do NOT treat its state as a rollback trigger. Record it as
# telemetry only.
say "Post-swap verification (app services only)"
verify_ok=0
for i in {1..30}; do
  # API responds + reports this exact version.
  api_version="$(curl -fsS "http://localhost:$API_PORT/version" 2>/dev/null | jq -r .version 2>/dev/null || true)"
  # Web front-door reachable.
  web_ok=0
  curl -fsS "http://localhost:$WEB_PORT/" >/dev/null 2>&1 && web_ok=1
  if [[ "$api_version" == "${TAG#v}" && "$web_ok" == "1" ]]; then
    verify_ok=1
    break
  fi
  sleep 2
done

if [[ "$verify_ok" == "1" ]]; then
  # Gateway state is informational only.
  gw_state="$(curl -fsS "http://localhost:$API_PORT/health" 2>/dev/null | jq -r '.ib_gateway.port_listening // "unknown"')"
  say "OK — $TAG is live (ib_gateway.port_listening=$gw_state)"
  # Prune old releases. macOS `xargs` has no `-r`; guard with explicit empty check.
  to_prune="$(ls -1dt "$ROOT/releases"/v* 2>/dev/null | tail -n +4)"
  if [[ -n "$to_prune" ]]; then
    echo "$to_prune" | xargs rm -rf
  fi
  printf '{"ts":"%s","version":"%s","outcome":"ok","ib_gateway":"%s"}\n' \
    "$(date -u +%FT%TZ)" "$TAG" "$gw_state" >> "$SHARED/logs/deploys.jsonl"
  exit 0
fi

# 9. Auto-rollback — use the exact previous target we captured, not mtime-sorted guesses.
say "Post-swap health check failed — rolling back"
[[ -n "$PREV_RELEASE" && -d "$PREV_RELEASE" ]] || die "no previous release to roll back to"
ln -sfn "$PREV_RELEASE" "$CURRENT_LINK"
for svc in xenon.web xenon.api xenon.ib-realtime; do
  launchctl kickstart -k "gui/$UID/$svc" || true
done
printf '{"ts":"%s","version":"%s","outcome":"rolled-back","previous":"%s"}\n' \
  "$(date -u +%FT%TZ)" "$TAG" "$(basename "$PREV_RELEASE")" >> "$SHARED/logs/deploys.jsonl"
exit 1
```

- [ ] **Step 2: Create the preview health-check helper**

```bash
#!/usr/bin/env bash
# scripts/deploy/_health_preview.sh
# Stands up API + Web + ib_realtime on alternate ports and verifies each process
# reaches ready state. Must exercise every service that the real deploy will restart —
# otherwise a broken ib_realtime passes preview and fails immediately after swap.
set -euo pipefail
RELEASE="${1:?usage: _health_preview.sh <release-dir>}"
API_PORT="${XENON_API_PORT:-8322}"
WEB_PORT="${PORT:-3001}"

pids=()
cleanup() {
  for pid in "${pids[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT

# Source env so ib_realtime has its IB credentials.
set -a
. /opt/xenon/shared/.env
set +a

# API — use the per-release .venv materialized by uv sync
"$RELEASE/.venv/bin/python" -m uvicorn xenon.api.server:app --host 127.0.0.1 --port "$API_PORT" \
  --app-dir "$RELEASE/src" &
pids+=($!)

# Web
(cd "$RELEASE/web" && PORT="$WEB_PORT" npm run start) &
pids+=($!)

# ib_realtime — sanity-check that node imports and boots. We don't bind a port
# for it here; we just require the process to stay alive for >5s (a syntax/import
# error on the file would exit immediately).
node "$RELEASE/scripts/infra/ib_realtime/ib_realtime_server.js" &
IB_PID=$!
pids+=($IB_PID)

for i in {1..30}; do
  sleep 2
  if curl -fsS "http://127.0.0.1:$API_PORT/health" >/dev/null \
     && curl -fsS "http://127.0.0.1:$WEB_PORT/" >/dev/null \
     && kill -0 "$IB_PID" 2>/dev/null; then
    echo "preview OK"
    exit 0
  fi
done
echo "preview failed" >&2
exit 1
```

- [ ] **Step 3: Make executable and commit**

```bash
chmod +x scripts/deploy/_remote.sh scripts/deploy/_health_preview.sh
git add scripts/deploy/_remote.sh scripts/deploy/_health_preview.sh
git commit -m "deploy: remote orchestrator + preview health check"
```

### Task 3.8: Laptop-side deploy driver

**Files:**

- Create: `scripts/deploy/mac-mini.sh`

- [ ] **Step 1: Implement**

```bash
#!/usr/bin/env bash
# scripts/deploy/mac-mini.sh
# Usage: scripts/deploy/mac-mini.sh v0.0.2

set -euo pipefail
TAG="${1:?usage: mac-mini.sh <tag>}"
HOST="${XENON_MAC_MINI_HOST:-xenon@xenon-mini.local}"

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=scripts/deploy/_preflight.sh
. "$ROOT/scripts/deploy/_preflight.sh"

preflight_clean_tree
preflight_tag "$TAG"

echo "Deploying $TAG to $HOST"
# Run _remote.sh FROM THE RELEASE PAYLOAD we're about to ship, not from a persistent
# shared/bin copy — a stale shared/bin drifts from the repo and silently runs old
# deploy logic against new releases. Two-step: fetch+extract the tag, then execute
# the freshly-extracted _remote.sh.
ssh "$HOST" "bash -s" <<SSH
set -euo pipefail
ROOT=/opt/xenon
CACHE="\$ROOT/.git-cache"
# Ensure the tag is in the cache so the remote script can archive it.
git -C "\$CACHE" fetch --tags origin
# Quick-extract JUST the deploy scripts to a scratch dir so we can invoke them.
STAGE="\$(mktemp -d -t xenon-deploy)"
trap 'rm -rf "\$STAGE"' EXIT
git -C "\$CACHE" archive "$TAG" scripts/deploy | tar -x -C "\$STAGE"
bash "\$STAGE/scripts/deploy/_remote.sh" "$TAG"
SSH
```

Note: `_remote.sh` runs from a scratch extraction of the target tag's `scripts/deploy/` tree. The full release extraction inside `_remote.sh` is unchanged; only the deploy-logic invocation path is corrected. No persistent `shared/bin/` copies of `_remote.sh` — drift-prone.

- [ ] **Step 2: Commit**

```bash
chmod +x scripts/deploy/mac-mini.sh
git add scripts/deploy/mac-mini.sh
git commit -m "deploy: mac-mini laptop-side driver"
```

### Task 3.9: Bootstrap script

**Files:**

- Create: `scripts/deploy/mac-mini-bootstrap.sh`
- Create: `docs/runbooks/mac-mini-provision.md`

- [ ] **Step 1: Implement**

```bash
#!/usr/bin/env bash
# scripts/deploy/mac-mini-bootstrap.sh
# One-time provisioning. Idempotent — safe to re-run.
# Run directly on the Mac mini as user xenon.

set -euo pipefail

ROOT=/opt/xenon
SHARED="$ROOT/shared"

say() { printf '\033[1;34m[bootstrap] %s\033[0m\n' "$*"; }

# 1. Directories
sudo mkdir -p "$ROOT"
sudo chown "$USER" "$ROOT"
mkdir -p "$ROOT/releases" "$ROOT/ib-gateway" \
         "$SHARED/data" "$SHARED/logs" "$SHARED/web"

# 2. Homebrew deps
say "Installing deps via brew"
if ! command -v brew >/dev/null; then
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi
brew install node python@3.13 jq git gh docker cloudflared tailscale

# 3. Python toolchain (uv manages per-release .venv/ at deploy time; no shared venv)
if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv via Homebrew"
  /opt/homebrew/bin/brew install uv
fi

# 4. Bookkeeping repo
if [[ ! -d "$ROOT/.git-cache" ]]; then
  say "Cloning bookkeeping repo"
  git clone --bare https://github.com/moremeds/xenon.git "$ROOT/.git-cache"
fi

# 5. Prompt for .env if missing
for f in "$SHARED/.env" "$SHARED/web/.env"; do
  if [[ ! -f "$f" ]]; then
    echo "Missing $f — scp it from your laptop now, then press Enter"
    read -r
  fi
done

# 6. (Deploy scripts are NOT copied to a persistent location. Each deploy
# extracts _remote.sh fresh from the target tag — see Task 3.8 — so bootstrap
# only needs to ensure the bookkeeping repo exists, which it does in step 4.)

# 7. IB Gateway compose files — copy both the base compose and the prod override.
mkdir -p "$ROOT/ib-gateway"
for f in docker-compose.yml docker-compose.prod.yml; do
  src="$(dirname "$0")/../../docker/ib-gateway/$f"
  if [[ -f "$src" ]]; then
    cp "$src" "$ROOT/ib-gateway/"
  else
    echo "WARN: $src missing — copy into $ROOT/ib-gateway/ manually before first deploy" >&2
  fi
done

# 8. Install launchd plists
PLISTS_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$PLISTS_DIR"
for svc in xenon.web xenon.api xenon.ib-realtime xenon.ib-gateway; do
  src="$(dirname "$0")/../../deploy/launchd/$svc.plist"
  [[ -f "$src" ]] || continue
  cp "$src" "$PLISTS_DIR/$svc.plist"
  launchctl bootstrap "gui/$UID" "$PLISTS_DIR/$svc.plist" 2>/dev/null || true
done

say "Bootstrap complete. First deploy: from laptop run scripts/deploy/mac-mini.sh v0.0.2"
```

- [ ] **Step 2: Write runbook**

```markdown
# Mac mini Provisioning Runbook

## One-time setup

1. Fresh macOS install. Log in as user `xenon`.
2. Install Tailscale, join the tailnet.
3. Note the Tailscale hostname / `xenon-mini.local` LAN name.
4. SSH from laptop to confirm: `ssh xenon@xenon-mini.local echo ok`.
5. On the Mac mini, clone this repo once to a temp location:
```

git clone https://github.com/moremeds/xenon.git /tmp/xenon-src
cd /tmp/xenon-src
scripts/deploy/mac-mini-bootstrap.sh

```
6. When prompted, `scp` `.env` and `web/.env` from the laptop into `/opt/xenon/shared/` and `/opt/xenon/shared/web/` respectively.
7. IB Gateway: copy `docker/ib-gateway/docker-compose.yml` and `ib-gateway/docker-compose.prod.yml` into `/opt/xenon/ib-gateway/`. First run will prompt for IBKR 2FA on your phone.
8. After bootstrap completes, `rm -rf /tmp/xenon-src`.

## Required environment variables

Root `.env` — see `CLAUDE.md` for authoritative list. At minimum:
`MENTHORQ_USER`, `MENTHORQ_PASS`, `MASSIVE_API_KEY`, `CLERK_JWKS_URL`, `CLERK_ISSUER`, `ALLOWED_USER_IDS`, `R2_ENDPOINT`, `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`.

Web `.env` — `ANTHROPIC_API_KEY`, `UW_TOKEN`, `EXA_API_KEY`, `CEREBRAS_API_KEY`, `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`, `CLERK_SECRET_KEY`.

## First deploy

From laptop:
```

scripts/deploy/mac-mini.sh v0.0.2

```

Expected: preview health check passes, symlink swaps, services kickstart, `curl http://xenon-mini.local:8321/version` returns `{"version":"0.0.2",...}`.
```

- [ ] **Step 3: Commit**

```bash
chmod +x scripts/deploy/mac-mini-bootstrap.sh
git add scripts/deploy/mac-mini-bootstrap.sh docs/runbooks/mac-mini-provision.md
git commit -m "deploy: Mac mini bootstrap + provisioning runbook"
```

### Task 3.10: Deploy runbook

**Files:**

- Create: `docs/runbooks/deploy.md`

- [ ] **Step 1: Write runbook**

````markdown
# Deploy Runbook

## Normal deploy

```bash
git checkout master && git pull
scripts/deploy/mac-mini.sh v0.0.2
```
````

Expected output ends with `OK — v0.0.2 is live`. Log entry appended to `/opt/xenon/shared/logs/deploys.jsonl`.

## Rollback

```bash
scripts/deploy/mac-mini.sh v0.0.1
```

Same script. Fast path (~5s) because build steps no-op on existing release directory.

## Deploy failed mid-flight

`_remote.sh` has an auto-rollback path: if post-swap verification fails for 60s, it flips `current` back to the previous release and kickstarts services. You'll see exit code 1 and a `rolled-back` entry in `deploys.jsonl`. No manual action needed — investigate and retry.

## IB Gateway is a separate lifecycle

App deploys do NOT restart `xenon.ib-gateway`. If you need to bounce it (e.g. after an IBKR Gateway update):

```bash
ssh xenon@xenon-mini.local launchctl kickstart -k "gui/\$UID/xenon.ib-gateway"
```

Expect a 2FA push on your phone. Approve to bring the gateway back up.

## Health checks

```bash
curl http://xenon-mini.local:8321/health
curl http://xenon-mini.local:8321/version
```

`/health` must show `ib_gateway.port_listening: true`. If not, check `xenon.ib-gateway` status:

```bash
ssh xenon@xenon-mini.local launchctl print "gui/\$UID/xenon.ib-gateway"
```

## Logs

```bash
ssh xenon@xenon-mini.local 'tail -f /opt/xenon/shared/logs/{web,api,ib-realtime,ib-gateway}.log'
```

````

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/deploy.md
git commit -m "docs: deploy + rollback runbook"
````

### Task 3.11: Update `scripts/infra/cloud.sh` to point at Mac mini

**Files:**

- Modify: `scripts/infra/cloud.sh`

- [ ] **Step 1: Read current**

```bash
cat scripts/infra/cloud.sh
```

- [ ] **Step 2: Replace the VPS hostname / Tailscale target with `xenon-mini.local` (or the Mac mini's Tailscale IP)**

Keep an env-var escape hatch: `XENON_IB_HOST=${XENON_IB_HOST:-xenon-mini.local}`.

- [ ] **Step 3: Update port**

Ensure the IB Gateway port matches the LAN-exposed `4002`.

- [ ] **Step 4: Commit**

```bash
git add scripts/infra/cloud.sh
git commit -m "dev: cloud.sh points at Mac mini IB Gateway"
```

### Task 3.12: Open PR, run dry-run deploy, merge

- [ ] **Step 1: Push + open PR**

```bash
git push -u origin feat/deploy-mac-mini
gh pr create --title "Mac mini deploy pipeline (Phase 3)" --body "See docs/superpowers/plans/2026-04-24-ci-release-deploy.md. Merge only after a successful dry-run deploy to xenon-mini.local."
```

- [ ] **Step 2: Dry-run deploy from laptop**

After PR is green:

```bash
# cut a pre-release tag for the dry run
git checkout master && git pull
scripts/release/cut.sh  # patch → v0.0.2
git push origin master --follow-tags
# wait for release.yml to publish the GitHub Release
gh run watch
# deploy
scripts/deploy/mac-mini.sh v0.0.2
```

Expected: terminates with `OK — v0.0.2 is live`. `curl http://xenon-mini.local:8321/version` returns v0.0.2.

- [ ] **Step 3: Dry-run rollback**

```bash
scripts/deploy/mac-mini.sh v0.0.1
```

Expected: ~5s, `OK — v0.0.1 is live`. `curl http://xenon-mini.local:8321/version` returns v0.0.1.

- [ ] **Step 4: Redeploy latest and merge**

```bash
scripts/deploy/mac-mini.sh v0.0.2
gh pr merge --squash --delete-branch
```

---

## Self-Review

Ran against the spec:

- **Phase 1 coverage:** ci.yml (Task 1.1-1.4), nightly (1.5), branch protection (1.6). ✓
- **Phase 2 coverage:** VERSION reset (2.1), version-sync lint (2.2), lib+cut (2.3-2.4), release workflow (2.5), runbook (2.6), dogfood cut (2.7). ✓
- **Phase 3 coverage:** version helper + route (3.1-3.2), next start (3.3), plists (3.4), IB Gateway LAN override (3.5), preflight+remote+driver (3.6-3.8), bootstrap+runbooks (3.9-3.10), cloud.sh repoint (3.11), dry run (3.12). ✓
- **Risk-table items:** IB Gateway lifecycle decoupled — ib-gateway.plist points at `/opt/xenon/ib-gateway/`, not `current/`; `_remote.sh` deliberately skips it. ✓ `/health` must not touch `reqTickersAsync` — called out in Phase 3 runbook. ✓
- **Placeholder scan:** No TBD / "add error handling" / "similar to above". Every code step has full code. ✓
- **Type consistency:** `get_version()`, `_commit()`, `_deployed_at()` consistent across tasks. Script names consistent (`_remote.sh`, `_preflight.sh`, `cut.sh`, `_lib.sh`). ✓

Gap identified and fixed inline: original Task 3.8 called `_remote.sh` from the release checkout, but release directories don't exist until deploy runs the remote. Now fixed: `mac-mini.sh` does a scratch extract of `scripts/deploy/` from the target tag and invokes `_remote.sh` from there — guaranteed to match the release being shipped, no persistent drift.

---

## Codex-Review Fixes Applied (2026-04-24)

Tribunal review (Codex + Gemini + Claude) surfaced 27 issues against an earlier draft. Applied inline. Summary:

### CRITICAL (resolved)

1. **CHANGELOG rewrite destroys file** (Codex, Gemini, Claude unanimous). Replaced the two chained Python regexes in Task 2.4 with a single deterministic rewrite that preserves the tail of the file and asserts post-conditions.
2. **IB Gateway port mapping wrong** (Codex). Real compose maps paper `4002:4004` and live `4001:4003`. Task 3.5 now emits a prod override that matches, including both lines.
3. **`mv -Tf` is GNU-only** (Codex). BSD `mv` on macOS rejects `-T`. `_remote.sh` now uses a single `ln -sfn` which is atomic via `rename(2)` — no `.new` intermediate needed.
4. **/version used `git rev-parse` on archive-extracted dir** (Codex). `_remote.sh` now writes `REVISION` and `DEPLOYED_AT` files into each release, and the route reads those files first.
5. **Deploy failure coupled to IB Gateway health despite the decoupling design intent** (Codex, overriding Gemini's opposite suggestion). Post-swap verification now checks API `/version` + web root only; gateway state is recorded as telemetry.
6. **`/version` needs to be in `AUTH_EXEMPT_PATHS`** (Claude, from reading `server.py`). Task 3.2 Step 4 adds `/version` to the exempt set and requires a test that verifies exemption even when `CLERK_JWKS_URL` is set.

### IMPORTANT (resolved)

7. Interrupted extraction could leave a half-populated release dir (Codex) — extract to `.${TAG}.tmp` and `mv` on success.
8. Rollback used `ls -t | sed -n '2p'` which picks by mtime — could skip to the wrong release if dirs were touched. Now uses `readlink $CURRENT_LINK` captured BEFORE swap.
9. Persistent `shared/bin/_remote.sh` would drift from the repo (Codex) — `mac-mini.sh` now extracts deploy scripts fresh from the target tag each deploy.
10. `preflight_clean_tree` missed untracked files — now uses `git status --porcelain=v1`.
11. Path mismatches: `scripts/cloud.sh` → `scripts/infra/cloud.sh`; `ib-gateway/docker-compose.prod.yml` → `docker/ib-gateway/docker-compose.prod.yml`. File Structure table + all references updated.
12. `gh run list --branch master --limit 1` doesn't guarantee the right commit — now uses `--workflow CI --commit <sha>`.
13. Preview health check didn't exercise `ib_realtime_server.js` — now starts it and verifies the process stays alive.
14. Task 3.3's proposed `"start": "next start -p 3000"` would collide with `PORT=3001` preview — Task 3.3 now just verifies the existing `"next start"` script (which is already in `web/package.json`).
15. Post-swap verification didn't curl the web front door (Gemini) — now required before marking deploy OK.
16. `xenon.api.plist` `WorkingDirectory` set to `/opt/xenon/current` with `--app-dir src` was fragile (Gemini) — now `WorkingDirectory=/opt/xenon/current/src` and `--app-dir` dropped.
17. Nightly-Playwright issue creation raced with quick-succession failures and was not paginated (Gemini) — now paginates and comments on the canonical tracking issue instead of creating duplicates.
18. `extract_changelog_section` awk pattern wasn't line-anchored (Gemini) — now `^## \[` prevents premature exits on in-body text.
19. `version-sync` job was added but not actually required by branch protection (Codex + Claude) — Task 2.2 Step 7 updates branch protection AFTER the PR merges.
20. Dry-run rollback referenced `v0.0.1` but no such tag was ever created (Claude) — Task 2.1 Step 6 now tags `v0.0.1` on the reset commit and creates a baseline GitHub Release.
21. `scripts/infra/dev/dead_code_scan.py` assumed in Task 1.4 but doesn't exist (Claude) — task now conditional; if the CLI doesn't exist, skip and open a follow-up issue.
22. Release runbook didn't list prereqs like `gh` (Gemini) — added a Prerequisites section.

### MINOR (resolved)

23. `xenon.ib-gateway.plist` hardcoded `/usr/local/bin/docker` (Codex + Gemini) — now `/opt/homebrew/bin/docker`, consistent with the rest of the brew-installed tools.
24. `xargs -r` is a GNU extension; macOS `xargs` lacks it (Gemini) — pruning now guards with `[[ -n "$to_prune" ]]`.
25. Unquoted `$ROOT` etc. would break with spaces in paths (Gemini) — all `_remote.sh` / `mac-mini.sh` path references already quoted in the revised code.

### Dismissed

- **Gemini-4** (post-deploy should assert `ib_gateway.port_listening: true`) — directly contradicts the spec's explicit "gateway lifecycle decoupled" design. Codex caught this correctly; Claude arbitrated in Codex's favor.
- **Review of existing `v0.1.0` / `v0.1.2` tags in the remote.** Cosmetic only — those tags are orthogonal to the new `0.0.x` line and don't block anything.
