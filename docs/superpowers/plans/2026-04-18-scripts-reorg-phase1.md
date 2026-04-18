# scripts/ Reorganization — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move 58 loose top-level `scripts/*.py|*.sh|*.js` files into 8 verb-first buckets and consolidate 4 paired `*_lib/` directories with their CLIs, without breaking any working invocation path (cron/launchd, FastAPI `run_script()`, web `subprocess.run`, sibling imports).

**Architecture:** 10 atomic PRs landed in sequence with ≥1 trading-day soak between. Every moved `.py` with an external caller gets a Python re-export shim at its old path; every moved `.sh` with an external caller gets a symlink at its old path. Sibling imports across the codebase get rewritten in the same commit as the move. Internal-only consolidations (e.g., `scanner_lib/` → `scanners/_shared/`) skip the shim and rely on synchronous import rewrite. `scripts/` name is preserved in Phase 1; rename to `src/` is Phase 2.

**Tech Stack:** Python 3.13 packages (regular, with `__init__.py`), ruff isort (`known-first-party`), pytest (`norecursedirs`), bash symlinks (`ln -s`), `git mv` for move tracking.

---

## Source of Truth

Full spec: `docs/superpowers/specs/2026-04-18-scripts-reorg/phase1-design.md`. Read it before starting any PR — this plan enumerates the execution steps; the spec holds the rationale, review findings, and full per-file rules.

## Common Patterns (referenced by every PR)

### Python shim template

File: `scripts/<old_name>.py` (replaces the moved file at its old path)

```python
#!/usr/bin/env python3.13
"""Compatibility shim. Real home: scripts/<bucket>/<new_name>.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from <bucket>.<new_name> import *  # noqa: F401,F403 — re-export for sibling imports
from <bucket>.<new_name> import main  # explicit CLI entry

if __name__ == "__main__":
    main()
```

Pre-conditions for a shim to work:

1. Target module exposes a module-level `def main(): ...` — if not, a prep commit extracts it.
2. No `from <old_name> import _private` callers — if any, add explicit re-exports or `__all__` in the target.
3. `scripts/` is on `sys.path[0]` — true by default for `python3.13 scripts/foo.py` invocation.

### Shell symlink template

```bash
# Run from repo root. Example: run_cri_scan.sh moved to services/
git mv scripts/run_cri_scan.sh scripts/services/run_cri_scan.sh
ln -s services/run_cri_scan.sh scripts/run_cri_scan.sh
git add scripts/run_cri_scan.sh
```

Verify with `ls -la scripts/run_cri_scan.sh` → shows `-> services/run_cri_scan.sh`.

### Import rewrite sweep

For each moved module `foo` going into `<bucket>/`:

```bash
rg -l "from foo import\b|^import foo\b" scripts/ web/     # find callers
# rewrite each: from foo import X  →  from <bucket>.foo import X
rg "from foo import\b|^import foo\b" scripts/ web/        # verify zero hits
```

### Smoke list (run after every bucket PR)

```
python3.13 scripts/fetch_flow.py --help
python3.13 scripts/fetch_ticker.py          # exit 2 expected
python3.13 scripts/fetch_analyst_ratings.py --help
python3.13 scripts/fetch_menthorq_dashboard.py --help
python3.13 scripts/discover.py --help
python3.13 scripts/scanner.py --help
python3.13 scripts/kelly.py --help
python3.13 scripts/evaluate.py --help
python3.13 scripts/ib_order_manage.py --help
python3.13 scripts/ib_sync.py --help
python3.13 scripts/ib_option_chain.py --help
python3.13 scripts/leap_scanner_uw.py --help
python3.13 scripts/trend_scan.py --help
python3.13 scripts/uw_scan.py --help
python3.13 scripts/uw_analyze.py --help
python3.13 scripts/ta_cli.py --help
python3.13 scripts/ta_premarket_prep.py --help
python3.13 scripts/cri_scan.py --help
python3.13 scripts/vcg_scan.py --help
python3.13 scripts/gex_scan.py --help
python3.13 scripts/generate_gex_share.py --help
python3.13 scripts/generate_regime_share.py --help
python3.13 scripts/generate_cta_share.py --help
python3.13 scripts/generate_vcg_share.py --help
python3.13 scripts/test_ib_realtime.py --help
bash scripts/run_cri_scan.sh --help 2>&1 | head -3
bash scripts/run_cta_sync.sh --help 2>&1 | head -3
```

All expected to print `--help` text or exit cleanly — NOT `ModuleNotFoundError`, `ImportError`, or `No such file`.

### Per-bucket commit gate (checklist after each PR)

- [ ] `python3.13 scripts/run_pytest_affected.py` — green
- [ ] `python3.13 -m pytest scripts/tests/ -x --tb=short` — green
- [ ] Full smoke list above — green for everything moved so far
- [ ] `rg "from <old_module_name>\b"` returns 0 hits in `scripts/` and `web/` for every module moved in this bucket
- [ ] `bash scripts/infra/local.sh` (or `scripts/local.sh` via symlink) starts — `curl localhost:8321/health` returns `ib_gateway.port_listening: true`
- [ ] `curl localhost:8321/trend-scan/run` completes successfully
- [ ] `cd web && npx playwright test tests/integration.test.ts` — green

---

## Pre-Flight (before PR 0)

### Task A: Baseline green

**Files:** none — verification only.

- [ ] **Step 1: Confirm clean working tree**

```bash
git status
```

Expected: only untracked `.serena/` and `docs/superpowers/specs/2026-04-18-scripts-reorg/` — same as session start.

- [ ] **Step 2: Run full test baseline**

```bash
python3.13 scripts/run_pytest_affected.py
python3.13 -m pytest scripts/tests/ -x --tb=short
cd web && npm test && npx playwright test
```

Expected: all green. Save output to `/tmp/phase1-baseline.log` for regression comparison.

- [ ] **Step 3: Capture pre-move smoke baseline**

Run the full smoke list from Common Patterns above. All 27 commands expected to print help text or exit cleanly today. Save output to `/tmp/phase1-smoke-baseline.log`.

### Task B: Launchd / cron audit

**Files:** none — external-system audit only.

- [ ] **Step 1: Local machine audit**

```bash
ls ~/Library/LaunchAgents/ | grep -iE "xenon|cri|cta|exit_order|trend|ta_prem|monitor|data_refresh"
launchctl list | grep -iE "xenon|cri|cta|exit_order|data_refresh"
```

Expected at spec time: empty. Record any hits to `docs/superpowers/specs/2026-04-18-scripts-reorg/launchd-audit.md`.

- [ ] **Step 2: VPS audit**

```bash
ssh vps 'launchctl list | grep -iE "xenon|cri|cta|exit_order|data_refresh"'
ssh vps 'systemctl list-units --type=service --all | grep -iE "xenon|cri|cta"'
ssh vps 'crontab -l'
```

- [ ] **Step 3: Inspect any plists found**

For every plist hit:

```bash
cat ~/Library/LaunchAgents/<name>.plist | grep -E "scripts/(run|setup)_"
```

Record to `launchd-audit.md`. This is the blocker list for PR 5 and PR 6.

### Task C: main() extraction audit

**Files:** none — read-only scan.

- [ ] **Step 1: Find files needing `main()` extraction**

```bash
rg -l '^if __name__ == .__main__.:' scripts/*.py | while read f; do
  if ! rg -q '^def main\(' "$f"; then echo "NEEDS EXTRACTION: $f"; fi
done
```

Record the list to `docs/superpowers/specs/2026-04-18-scripts-reorg/main-extraction-list.md`. Every file on this list gets a separate prep commit inside its bucket PR before its move commit.

### Task D: Private-name sweep

**Files:** none — read-only scan.

- [ ] **Step 1: Find `_private` re-export hazards**

For every bucket, grep for `from <module> import _` across the codebase. Spec section "Private-name sweep (ISSUE-11)":

```bash
for m in fetch_flow fetch_ticker fetch_analyst_ratings fetch_menthorq_dashboard \
         discover scanner kelly evaluate ib_order_manage ib_sync ib_option_chain \
         leap_scanner_uw trend_scan uw_scan uw_analyze ta_cli ta_premarket_prep \
         cri_scan vcg_scan gex_scan generate_gex_share generate_regime_share \
         generate_cta_share generate_vcg_share blotter risk_reversal \
         portfolio_report portfolio_performance; do
  hits=$(rg "from $m import _" scripts/ web/ 2>/dev/null)
  [ -n "$hits" ] && echo "--- $m ---" && echo "$hits"
done
```

Record any hits to `private-export-list.md`. Each module with hits needs an explicit re-export in its shim or `__all__` in the target.

---

## PR 0 — Pre-Phase-1 Scaffolding

**Goal:** Create empty bucket directories with `__init__.py`, update `pyproject.toml` `known-first-party` and `norecursedirs`. Zero functional change. Lands standalone.

### Task 0.1: Create bucket directories with `__init__.py`

**Files:**

- Create: `scripts/fetchers/__init__.py`
- Create: `scripts/scanners/__init__.py`
- Create: `scripts/scanners/_shared/__init__.py`
- Create: `scripts/scanners/trend/__init__.py`
- Create: `scripts/scanners/uw/__init__.py`
- Create: `scripts/execution/__init__.py`
- Create: `scripts/reports/__init__.py`
- Create: `scripts/shares/__init__.py`
- Create: `scripts/services/__init__.py`
- Create: `scripts/ta/__init__.py`
- Create: `scripts/infra/__init__.py`
- Create: `scripts/infra/ib_realtime/__init__.py`
- Create: `scripts/infra/dev/__init__.py`

- [ ] **Step 1: Create directories and empty init files**

```bash
for d in fetchers scanners scanners/_shared scanners/trend scanners/uw \
         execution reports shares services ta infra infra/ib_realtime infra/dev; do
  mkdir -p "scripts/$d"
  touch "scripts/$d/__init__.py"
done
```

- [ ] **Step 2: Verify**

```bash
find scripts -name __init__.py -path 'scripts/*' -newer pyproject.toml | sort
```

Expected: 13 lines, matching the list above.

### Task 0.2: Update pyproject.toml

**Files:**

- Modify: `pyproject.toml` (entire file)

- [ ] **Step 1: Rewrite pyproject.toml**

```toml
[tool.ruff]
target-version = "py39"
line-length = 120

[tool.ruff.lint]
select = ["E", "F", "W", "B", "I"]
ignore = ["E501"]  # line length handled separately

[tool.ruff.lint.isort]
known-first-party = [
    "utils",
    "trade_blotter",
    "fetchers",
    "scanners",
    "execution",
    "reports",
    "shares",
    "services",
    "ta",
    "infra",
    "clients",
    "analysis",
    "scanner_lib",
    "trend_scan_lib",
    "uw_scan_lib",
    "ta_lib",
]

[project]
name = "xenon"
version = "0.1.0"
requires-python = ">=3.13"

[project.optional-dependencies]
test = [
    "pytest",
    "responses",
]

[tool.pytest.ini_options]
asyncio_mode = "strict"
norecursedirs = [
    "scripts/infra/ib_realtime",
    ".venv",
    "node_modules",
    "data",
    "logs",
    "tmp",
]
markers = [
    "integration: live tests hitting real MenthorQ (requires credentials)",
    "e2e: live tests hitting real Massive API (requires MASSIVE_API_KEY)",
]
```

- [ ] **Step 2: Verify ruff + pytest still parse**

```bash
python3.13 -m ruff check scripts/ --no-fix 2>&1 | head -20
python3.13 -m pytest scripts/tests/ --collect-only 2>&1 | tail -5
```

Expected: ruff runs (may report existing lint issues — that's fine), pytest collects tests.

### Task 0.3: Commit PR 0

- [ ] **Step 1: Commit**

```bash
git add scripts/*/__init__.py scripts/scanners/*/__init__.py \
        scripts/infra/*/__init__.py pyproject.toml
git commit -m "chore(scripts): Phase 1 scaffolding — empty bucket dirs + pyproject updates"
```

- [ ] **Step 2: Push + PR**

```bash
git push -u origin HEAD
gh pr create --title "chore(scripts): Phase 1 scaffolding — empty bucket dirs + pyproject updates" \
  --body "$(cat <<'EOF'
## Summary
- Empty `__init__.py` in 13 new bucket dirs (fetchers/, scanners/{,_shared,trend,uw}/, execution/, reports/, shares/, services/, ta/, infra/{,ib_realtime,dev}/)
- pyproject.toml: expand ruff isort known-first-party, add pytest norecursedirs for infra/ib_realtime/
- Zero functional change. Lands standalone before any file moves.

## Test plan
- [ ] python3.13 -m ruff check scripts/ runs
- [ ] python3.13 -m pytest scripts/tests/ --collect-only runs
- [ ] ls scripts/ shows new empty dirs
EOF
)"
```

- [ ] **Step 3: Merge after CI green**

```bash
gh pr merge --squash --delete-branch
git checkout master && git pull
```

---

## PR 1 — `shares/` (Warm-Up Bucket)

**Goal:** Move 4 share-card generators into `scripts/shares/`. Lowest blast radius — all 4 are invoked by FastAPI `run_script()` and `web` subprocess but have minimal sibling imports.

**Files moved (4):**

| Old                                | New                                       |
| ---------------------------------- | ----------------------------------------- |
| `scripts/generate_cta_share.py`    | `scripts/shares/generate_cta_share.py`    |
| `scripts/generate_regime_share.py` | `scripts/shares/generate_regime_share.py` |
| `scripts/generate_vcg_share.py`    | `scripts/shares/generate_vcg_share.py`    |
| `scripts/generate_gex_share.py`    | `scripts/shares/generate_gex_share.py`    |

### Task 1.1: Prep — main() extraction (if Task C flagged any)

- [ ] **Step 1: Check extraction list from Task C**

If any of `generate_{cta,regime,vcg,gex}_share.py` appears in `main-extraction-list.md`, do the extraction:

```python
# BEFORE (at bottom of file):
if __name__ == "__main__":
    # 30 lines of inline logic
    ...

# AFTER:
def main():
    # same 30 lines
    ...

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify each still runs**

```bash
for f in generate_cta_share generate_regime_share generate_vcg_share generate_gex_share; do
  python3.13 scripts/$f.py --help 2>&1 | head -3
done
```

Expected: help text for each.

- [ ] **Step 3: Commit prep (only if extractions were needed)**

```bash
git add scripts/generate_*_share.py
git commit -m "refactor(shares): extract main() for upcoming shares/ move"
```

### Task 1.2: Move files + create shims

- [ ] **Step 1: git mv files**

```bash
git mv scripts/generate_cta_share.py    scripts/shares/generate_cta_share.py
git mv scripts/generate_regime_share.py scripts/shares/generate_regime_share.py
git mv scripts/generate_vcg_share.py    scripts/shares/generate_vcg_share.py
git mv scripts/generate_gex_share.py    scripts/shares/generate_gex_share.py
```

- [ ] **Step 2: Rewrite sibling imports**

```bash
rg -l "from generate_cta_share\b|import generate_cta_share\b" scripts/ web/
rg -l "from generate_regime_share\b|import generate_regime_share\b" scripts/ web/
rg -l "from generate_vcg_share\b|import generate_vcg_share\b" scripts/ web/
rg -l "from generate_gex_share\b|import generate_gex_share\b" scripts/ web/
```

For every hit, rewrite `from generate_X import Y` → `from shares.generate_X import Y` and bare `import generate_X` → `import shares.generate_X as generate_X`. Use editor or sed; verify with:

```bash
rg "from generate_(cta|regime|vcg|gex)_share\b|^import generate_(cta|regime|vcg|gex)_share\b" scripts/ web/
```

Expected: 0 hits.

- [ ] **Step 3: Create 4 shims**

Each shim identical pattern — write `scripts/generate_cta_share.py`:

```python
#!/usr/bin/env python3.13
"""Compatibility shim. Real home: scripts/shares/generate_cta_share.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from shares.generate_cta_share import *  # noqa: F401,F403
from shares.generate_cta_share import main

if __name__ == "__main__":
    main()
```

Repeat for `generate_regime_share.py`, `generate_vcg_share.py`, `generate_gex_share.py` — substitute the module name.

- [ ] **Step 4: chmod +x shims** (originals were executable)

```bash
chmod +x scripts/generate_cta_share.py scripts/generate_regime_share.py \
         scripts/generate_vcg_share.py scripts/generate_gex_share.py
```

### Task 1.3: Verify

- [ ] **Step 1: Smoke test each path (shim + real)**

```bash
python3.13 scripts/generate_cta_share.py --help       # via shim
python3.13 scripts/generate_regime_share.py --help    # via shim
python3.13 scripts/generate_vcg_share.py --help       # via shim
python3.13 scripts/generate_gex_share.py --help       # via shim
python3.13 -c "from shares.generate_cta_share import main; print('ok')"
python3.13 -c "from shares.generate_gex_share import main; print('ok')"
```

Expected: help text + `ok` for the import checks.

- [ ] **Step 2: Tests**

```bash
python3.13 -m pytest scripts/tests/ -x --tb=short
```

Expected: green.

- [ ] **Step 3: Per-bucket commit gate** (from Common Patterns)

Run all 7 gate checks. All green.

### Task 1.4: Commit + PR + merge

- [ ] **Step 1: Commit**

```bash
git add scripts/shares/ scripts/generate_*_share.py
git commit -m "refactor(scripts): move generate_*_share.py into scripts/shares/"
```

- [ ] **Step 2: Open PR**

```bash
gh pr create --title "refactor(scripts): move generate_*_share.py into scripts/shares/" \
  --body "$(cat <<'EOF'
## Summary
- Move 4 share-card generators into scripts/shares/
- Python re-export shims at old paths preserve FastAPI run_script() and web subprocess callers

## Test plan
- [ ] python3.13 scripts/generate_cta_share.py --help via shim
- [ ] python3.13 scripts/generate_regime_share.py --help via shim
- [ ] python3.13 scripts/generate_vcg_share.py --help via shim
- [ ] python3.13 scripts/generate_gex_share.py --help via shim
- [ ] pytest scripts/tests/ green
- [ ] FastAPI /trend-scan/run succeeds
EOF
)"
```

- [ ] **Step 3: Merge, wait ≥1 trading day before PR 2**

---

## PR 2 — `reports/`

**Goal:** Move 12 report/analysis files into `scripts/reports/`. Includes the `blotter.py` hardcoded-sibling fix and `portfolio_report.py` / `risk_reversal.py` `sys.path` depth fixes.

**Files moved (12):**

| Old                                       | New                                               |
| ----------------------------------------- | ------------------------------------------------- |
| `scripts/portfolio_attribution.py`        | `scripts/reports/portfolio_attribution.py`        |
| `scripts/portfolio_performance.py`        | `scripts/reports/portfolio_performance.py`        |
| `scripts/portfolio_report.py`             | `scripts/reports/portfolio_report.py`             |
| `scripts/performance_explainer_report.py` | `scripts/reports/performance_explainer_report.py` |
| `scripts/scenario_analysis.py`            | `scripts/reports/scenario_analysis.py`            |
| `scripts/scenario_report.py`              | `scripts/reports/scenario_report.py`              |
| `scripts/evaluate.py`                     | `scripts/reports/evaluate.py`                     |
| `scripts/kelly.py`                        | `scripts/reports/kelly.py`                        |
| `scripts/risk_reversal.py`                | `scripts/reports/risk_reversal.py`                |
| `scripts/blotter.py`                      | `scripts/reports/blotter.py`                      |
| `scripts/free_trade_analyzer.py`          | `scripts/reports/free_trade_analyzer.py`          |
| `scripts/verify_options_oi.py`            | `scripts/reports/verify_options_oi.py`            |

### Task 2.1: Prep commit — sys.path depth fixes

**Files:**

- Modify: `scripts/risk_reversal.py:29`
- Modify: `scripts/portfolio_report.py:46,62,778`
- Modify: `scripts/portfolio_performance.py:36`
- Modify: `scripts/evaluate.py:43`
- Modify: `scripts/verify_options_oi.py:25`

- [ ] **Step 1: `risk_reversal.py:29`**

```python
# BEFORE:
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

# AFTER (compensates for upcoming move to reports/ — both current path AND new path point to scripts/):
SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))
```

- [ ] **Step 2: `portfolio_report.py` lines 46, 62, 778**

All three sites: `SCRIPT_DIR = Path(__file__).parent` → `SCRIPT_DIR = Path(__file__).resolve().parent.parent`. Replace the variable definition once; if all three lines redefine it, fix all three.

- [ ] **Step 3: `portfolio_performance.py:36`**

```python
# BEFORE:
sys.path.insert(0, str(Path(__file__).resolve().parent))
# AFTER:
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

- [ ] **Step 4: `evaluate.py:43`**

```python
# BEFORE:
_SCRIPT_DIR = Path(__file__).resolve().parent
# AFTER:
_SCRIPT_DIR = Path(__file__).resolve().parent.parent
```

- [ ] **Step 5: `verify_options_oi.py:25`**

```python
# BEFORE:
sys.path.insert(0, str(Path(__file__).parent))
# AFTER:
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

- [ ] **Step 6: `blotter.py:19` hardcoded sibling path (ISSUE-2)**

```python
# BEFORE:
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trade_blotter'))
# AFTER:
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'trade_blotter'))
```

- [ ] **Step 7: main() extraction for any files flagged in Task C**

Perform extraction per Task 1.1 pattern.

- [ ] **Step 8: Verify everything still runs pre-move**

```bash
for f in portfolio_attribution portfolio_performance portfolio_report \
         performance_explainer_report scenario_analysis scenario_report \
         evaluate kelly risk_reversal blotter free_trade_analyzer verify_options_oi; do
  python3.13 scripts/$f.py --help 2>&1 | head -2
done
```

Expected: no ImportError from any. The `parent.parent` changes resolve to `/project/` today (pre-move), which still has the expected modules importable, so behavior is unchanged pre-move.

- [ ] **Step 9: Commit prep**

```bash
git add scripts/risk_reversal.py scripts/portfolio_report.py \
        scripts/portfolio_performance.py scripts/evaluate.py \
        scripts/verify_options_oi.py scripts/blotter.py
git commit -m "refactor(reports): prep sys.path depth for upcoming reports/ move"
```

### Task 2.2: Move files + import rewrite + shims

- [ ] **Step 1: git mv all 12 files**

```bash
for f in portfolio_attribution portfolio_performance portfolio_report \
         performance_explainer_report scenario_analysis scenario_report \
         evaluate kelly risk_reversal blotter free_trade_analyzer verify_options_oi; do
  git mv scripts/$f.py scripts/reports/$f.py
done
```

- [ ] **Step 2: Rewrite sibling imports**

For each module, find and rewrite callers:

```bash
for m in portfolio_attribution portfolio_performance portfolio_report \
         performance_explainer_report scenario_analysis scenario_report \
         evaluate kelly risk_reversal blotter free_trade_analyzer verify_options_oi; do
  echo "--- $m ---"
  rg -l "from $m\b|^import $m\b" scripts/ web/ 2>/dev/null
done
```

Rewrite every hit. Pattern: `from kelly import X` → `from reports.kelly import X`. Verify zero hits remain.

- [ ] **Step 3: Create 12 shims at old paths**

For each moved module, write `scripts/<name>.py`:

```python
#!/usr/bin/env python3.13
"""Compatibility shim. Real home: scripts/reports/<name>.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from reports.<name> import *  # noqa: F401,F403
from reports.<name> import main

if __name__ == "__main__":
    main()
```

Substitute `<name>` for each of the 12 modules.

- [ ] **Step 4: chmod +x any shims that were executable originals**

```bash
for f in portfolio_attribution portfolio_performance portfolio_report \
         performance_explainer_report scenario_analysis scenario_report \
         evaluate kelly risk_reversal blotter free_trade_analyzer verify_options_oi; do
  [ -x scripts/reports/$f.py ] && chmod +x scripts/$f.py
done
```

### Task 2.3: Verify

- [ ] **Step 1: Smoke each path**

```bash
for f in portfolio_attribution portfolio_performance portfolio_report \
         performance_explainer_report scenario_analysis scenario_report \
         evaluate kelly risk_reversal blotter free_trade_analyzer verify_options_oi; do
  echo "--- $f shim ---"
  python3.13 scripts/$f.py --help 2>&1 | head -3
done
```

Expected: help text for each; `blotter.py` and `evaluate.py` especially show the trade_blotter path fix landed.

- [ ] **Step 2: Import works at new path**

```bash
python3.13 -c "from reports.blotter import main; print('ok')"
python3.13 -c "from reports.evaluate import main; print('ok')"
python3.13 -c "from reports.kelly import main; print('ok')"
```

- [ ] **Step 3: Zero old-name hits**

```bash
rg "from (portfolio_attribution|portfolio_performance|portfolio_report|performance_explainer_report|scenario_analysis|scenario_report|evaluate|kelly|risk_reversal|blotter|free_trade_analyzer|verify_options_oi)\b" scripts/ web/ | grep -v 'from reports\.' | grep -v '^scripts/reports/'
```

Expected: 0 hits (shims themselves use `from reports.X`, which matches — they're covered by `grep -v`).

- [ ] **Step 4: Per-bucket commit gate**

Run all 7 gate checks.

### Task 2.4: Commit + PR + merge

- [ ] **Step 1: Commit**

```bash
git add scripts/reports/ scripts/*.py  # shims are at top level
git commit -m "refactor(scripts): move report/analysis CLIs into scripts/reports/"
```

- [ ] **Step 2: PR**

```bash
gh pr create --title "refactor(scripts): move report/analysis CLIs into scripts/reports/" \
  --body "$(cat <<'EOF'
## Summary
- Move 12 report/analysis files into scripts/reports/
- Fix blotter.py hardcoded 'trade_blotter' sibling path (now '..', 'trade_blotter')
- Fix sys.path depth in risk_reversal.py, portfolio_report.py, portfolio_performance.py, evaluate.py, verify_options_oi.py
- Shims preserve FastAPI run_script() and web subprocess callers

## Test plan
- [ ] All 12 --help smokes green via shim
- [ ] trade_blotter import still resolves from blotter.py
- [ ] pytest scripts/tests/ green
- [ ] web playwright integration.test.ts green
EOF
)"
```

- [ ] **Step 3: Merge, soak ≥1 trading day**

---

## PR 3 — `fetchers/`

**Goal:** Move 10 `fetch_*.py` data-acquisition files into `scripts/fetchers/`.

**Files moved (10):**

| Old                                   | New                                            |
| ------------------------------------- | ---------------------------------------------- |
| `scripts/fetch_ticker.py`             | `scripts/fetchers/fetch_ticker.py`             |
| `scripts/fetch_flow.py`               | `scripts/fetchers/fetch_flow.py`               |
| `scripts/fetch_options.py`            | `scripts/fetchers/fetch_options.py`            |
| `scripts/fetch_oi_changes.py`         | `scripts/fetchers/fetch_oi_changes.py`         |
| `scripts/fetch_analyst_ratings.py`    | `scripts/fetchers/fetch_analyst_ratings.py`    |
| `scripts/fetch_news.py`               | `scripts/fetchers/fetch_news.py`               |
| `scripts/fetch_menthorq_cta.py`       | `scripts/fetchers/fetch_menthorq_cta.py`       |
| `scripts/fetch_menthorq_dashboard.py` | `scripts/fetchers/fetch_menthorq_dashboard.py` |
| `scripts/fetch_x_watchlist.py`        | `scripts/fetchers/fetch_x_watchlist.py`        |
| `scripts/fetch_x_xai.py`              | `scripts/fetchers/fetch_x_xai.py`              |

### Task 3.1: Prep — sys.path fix

- [ ] **Step 1: `fetch_news.py:22` depth fix**

```python
# BEFORE:
sys.path.insert(0, str(_SCRIPT_DIR))   # _SCRIPT_DIR = Path(__file__).resolve().parent
# AFTER:
_SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPT_DIR))
```

- [ ] **Step 2: main() extraction for any files flagged in Task C**

Perform extraction per Task 1.1 pattern for any `fetch_*.py` on the list.

- [ ] **Step 3: Verify pre-move**

```bash
for f in fetch_ticker fetch_flow fetch_options fetch_oi_changes fetch_analyst_ratings \
         fetch_news fetch_menthorq_cta fetch_menthorq_dashboard fetch_x_watchlist fetch_x_xai; do
  python3.13 scripts/$f.py --help 2>&1 | head -2
done
```

- [ ] **Step 4: Commit prep**

```bash
git add scripts/fetch_news.py  # + any extraction sites
git commit -m "refactor(fetchers): prep sys.path depth for upcoming fetchers/ move"
```

### Task 3.2: Move + rewrite + shim

- [ ] **Step 1: git mv**

```bash
for f in fetch_ticker fetch_flow fetch_options fetch_oi_changes fetch_analyst_ratings \
         fetch_news fetch_menthorq_cta fetch_menthorq_dashboard fetch_x_watchlist fetch_x_xai; do
  git mv scripts/$f.py scripts/fetchers/$f.py
done
```

- [ ] **Step 2: Rewrite sibling imports**

```bash
for m in fetch_ticker fetch_flow fetch_options fetch_oi_changes fetch_analyst_ratings \
         fetch_news fetch_menthorq_cta fetch_menthorq_dashboard fetch_x_watchlist fetch_x_xai; do
  echo "--- $m ---"
  rg -l "from $m\b|^import $m\b" scripts/ web/ 2>/dev/null
done
```

Rewrite every hit. Pattern: `from fetch_flow import X` → `from fetchers.fetch_flow import X`.

- [ ] **Step 3: Create 10 shims**

For each module, `scripts/<name>.py`:

```python
#!/usr/bin/env python3.13
"""Compatibility shim. Real home: scripts/fetchers/<name>.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from fetchers.<name> import *  # noqa: F401,F403
from fetchers.<name> import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: chmod +x**

```bash
for f in fetch_ticker fetch_flow fetch_options fetch_oi_changes fetch_analyst_ratings \
         fetch_news fetch_menthorq_cta fetch_menthorq_dashboard fetch_x_watchlist fetch_x_xai; do
  [ -x scripts/fetchers/$f.py ] && chmod +x scripts/$f.py
done
```

### Task 3.3: Verify + Commit + PR + merge

- [ ] **Step 1: Smoke list + per-bucket gate** (as in PR 1/2)

- [ ] **Step 2: Commit**

```bash
git add scripts/fetchers/ scripts/fetch_*.py
git commit -m "refactor(scripts): move fetch_*.py into scripts/fetchers/"
```

- [ ] **Step 3: PR + merge + soak ≥1 trading day**

---

## PR 4 — `execution/` (Broker-Critical)

**Goal:** Move 9 IB/Futu execution files. High risk — broker-touching. Run full integration smoke before and after.

**Files moved (9):**

| Old                            | New                                      |
| ------------------------------ | ---------------------------------------- |
| `scripts/ib_execute.py`        | `scripts/execution/ib_execute.py`        |
| `scripts/ib_place_order.py`    | `scripts/execution/ib_place_order.py`    |
| `scripts/ib_order_manage.py`   | `scripts/execution/ib_order_manage.py`   |
| `scripts/ib_orders.py`         | `scripts/execution/ib_orders.py`         |
| `scripts/ib_option_chain.py`   | `scripts/execution/ib_option_chain.py`   |
| `scripts/ib_reconcile.py`      | `scripts/execution/ib_reconcile.py`      |
| `scripts/ib_sync.py`           | `scripts/execution/ib_sync.py`           |
| `scripts/naked_short_audit.py` | `scripts/execution/naked_short_audit.py` |
| `scripts/futu_sync.py`         | `scripts/execution/futu_sync.py`         |

### Task 4.1: Prep — sys.path + main() extraction

- [ ] **Step 1: Fix sys.path depth (compensate for upcoming move)**

| File                              | Change                                                                                                                    |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `scripts/ib_order_manage.py:20`   | `sys.path.insert(0, str(Path(__file__).parent))` → `str(Path(__file__).resolve().parent.parent)`                          |
| `scripts/ib_reconcile.py:26`      | same                                                                                                                      |
| `scripts/naked_short_audit.py:20` | same                                                                                                                      |
| `scripts/ib_place_order.py:25,26` | two inserts: fix `Path(__file__).parent` → `.parent.parent`; leave `PROJECT_ROOT` line alone unless it needs the same fix |

- [ ] **Step 2: main() extraction for any Task-C hits**

- [ ] **Step 3: Pre-move smoke**

```bash
for f in ib_execute ib_place_order ib_order_manage ib_orders ib_option_chain \
         ib_reconcile ib_sync naked_short_audit futu_sync; do
  python3.13 scripts/$f.py --help 2>&1 | head -2
done
```

- [ ] **Step 4: Integration smoke** (before move — capture baseline)

```bash
bash scripts/local.sh &
sleep 8
curl -s localhost:8321/health | python3.13 -m json.tool | grep ib_gateway
curl -s localhost:8321/portfolio | python3.13 -m json.tool | head -20
kill %1
```

Expected: `ib_gateway.port_listening: true`, portfolio JSON returns.

- [ ] **Step 5: Commit prep**

```bash
git add scripts/ib_order_manage.py scripts/ib_reconcile.py \
        scripts/naked_short_audit.py scripts/ib_place_order.py
git commit -m "refactor(execution): prep sys.path depth for upcoming execution/ move"
```

### Task 4.2: Move + rewrite + shim

- [ ] **Step 1: git mv all 9**

```bash
for f in ib_execute ib_place_order ib_order_manage ib_orders ib_option_chain \
         ib_reconcile ib_sync naked_short_audit futu_sync; do
  git mv scripts/$f.py scripts/execution/$f.py
done
```

- [ ] **Step 2: Rewrite sibling imports**

```bash
for m in ib_execute ib_place_order ib_order_manage ib_orders ib_option_chain \
         ib_reconcile ib_sync naked_short_audit futu_sync; do
  echo "--- $m ---"
  rg -l "from $m\b|^import $m\b" scripts/ web/ 2>/dev/null
done
```

Rewrite: `from ib_execute import X` → `from execution.ib_execute import X`.

- [ ] **Step 3: Create 9 shims**

Same template — substitute `<bucket>=execution` and `<name>` for each.

- [ ] **Step 4: chmod +x**

### Task 4.3: Verify — broker-integration smoke

- [ ] **Step 1: Module smokes**

```bash
for f in ib_execute ib_place_order ib_order_manage ib_orders ib_option_chain \
         ib_reconcile ib_sync naked_short_audit futu_sync; do
  python3.13 scripts/$f.py --help 2>&1 | head -3
done
```

- [ ] **Step 2: FastAPI + portfolio + order-route integration**

```bash
bash scripts/local.sh &
sleep 8
curl -s localhost:8321/health | python3.13 -m json.tool | grep ib_gateway
curl -s localhost:8321/portfolio | python3.13 -m json.tool | head -20
curl -s localhost:8321/reconcile | python3.13 -m json.tool | head -10
kill %1
```

Expected: identical output to Task 4.1 Step 4 baseline.

- [ ] **Step 3: Order route integration tests**

```bash
cd web && XENON_API_TEST_MODE=1 npx playwright test tests/orders.integration.test.ts
```

- [ ] **Step 4: Per-bucket gate**

### Task 4.4: Commit + PR + merge + soak

- [ ] **Step 1: Commit**

```bash
git add scripts/execution/ scripts/ib_*.py scripts/futu_sync.py scripts/naked_short_audit.py
git commit -m "refactor(scripts): move IB/Futu execution CLIs into scripts/execution/"
```

- [ ] **Step 2: PR + merge**

- [ ] **Step 3: Soak ≥1 trading day. Monitor `data/reconciliation.json` next-morning timestamp.**

---

## PR 5 — `infra/` (requires launchd pre-audit)

**Goal:** Move 14 infra files across 3 tiers. **Blocker:** Task B launchd audit must be complete and `launchd-audit.md` populated before starting.

**Files moved (14):**

Top-level (6):
| Old | New |
| ------------------------------- | ------------------------------------- |
| `scripts/cloud.sh` | `scripts/infra/cloud.sh` |
| `scripts/local.sh` | `scripts/infra/local.sh` |
| `scripts/docker_ib_gateway.sh` | `scripts/infra/docker_ib_gateway.sh` |
| `scripts/ibc_remote_control.sh` | `scripts/infra/ibc_remote_control.sh` |
| `scripts/setup_ibc.sh` | `scripts/infra/setup_ibc.sh` |
| `scripts/cleanup-dead-code.sh` | `scripts/infra/cleanup-dead-code.sh` |

`infra/ib_realtime/` (4):
| Old | New |
| --------------------------------- | --------------------------------------------------- |
| `scripts/ib_realtime_server.js` | `scripts/infra/ib_realtime/ib_realtime_server.js` |
| `scripts/ib_connection_status.js` | `scripts/infra/ib_realtime/ib_connection_status.js` |
| `scripts/ib_tick_handler.js` | `scripts/infra/ib_realtime/ib_tick_handler.js` |
| `scripts/test_ib_realtime.py` | `scripts/infra/ib_realtime/test_ib_realtime.py` |

`infra/dev/` (4):
| Old | New |
| -------------------------------- | ------------------------------------------ |
| `scripts/run_pytest_affected.py` | `scripts/infra/dev/run_pytest_affected.py` |
| `scripts/site_seo_audit.py` | `scripts/infra/dev/site_seo_audit.py` |
| `scripts/context_constructor.py` | `scripts/infra/dev/context_constructor.py` |
| `scripts/batched_relay.py` | `scripts/infra/dev/batched_relay.py` |

### Task 5.1: Launchd blocker check

- [ ] **Step 1: Verify `launchd-audit.md` populated**

```bash
cat docs/superpowers/specs/2026-04-18-scripts-reorg/launchd-audit.md
```

Expected: contains local + VPS audit results. Any plist referencing `scripts/cloud.sh`, `scripts/local.sh`, `scripts/docker_ib_gateway.sh`, `scripts/setup_ibc.sh`, `scripts/ibc_remote_control.sh`, or `scripts/cleanup-dead-code.sh` is noted with host + path.

- [ ] **Step 2: If any plist hardcodes a moving `.sh` path**

Coordinate with user: `setup_ibc.sh install` (or equivalent `install` command for the affected service) will need to run post-merge to rewrite the plist at the new path. Do not proceed with move until rollback runbook (step 3 of Rollback section in spec) is acknowledged.

- [ ] **Step 3: Record decision to proceed in PR description**

### Task 5.2: Move + symlinks + (Node-file care)

- [ ] **Step 1: Move top-level `.sh` (6 files)**

```bash
for f in cloud local docker_ib_gateway ibc_remote_control setup_ibc cleanup-dead-code; do
  git mv scripts/$f.sh scripts/infra/$f.sh
done
```

- [ ] **Step 2: Create symlinks at old paths**

```bash
for f in cloud local docker_ib_gateway ibc_remote_control setup_ibc cleanup-dead-code; do
  ln -s infra/$f.sh scripts/$f.sh
done
git add scripts/*.sh
```

- [ ] **Step 3: Test symlink transparency**

```bash
for f in cloud local docker_ib_gateway ibc_remote_control setup_ibc cleanup-dead-code; do
  echo "--- $f ---"
  ls -la scripts/$f.sh
  bash scripts/$f.sh --help 2>&1 | head -3
done
```

Expected: each `ls -la` shows `-> infra/<f>.sh`; each `--help` prints usage (or expected output — `setup_ibc.sh` may need a specific invocation).

- [ ] **Step 4: Move `ib_realtime/` JS files**

```bash
git mv scripts/ib_realtime_server.js   scripts/infra/ib_realtime/ib_realtime_server.js
git mv scripts/ib_connection_status.js scripts/infra/ib_realtime/ib_connection_status.js
git mv scripts/ib_tick_handler.js      scripts/infra/ib_realtime/ib_tick_handler.js
git mv scripts/test_ib_realtime.py     scripts/infra/ib_realtime/test_ib_realtime.py
```

- [ ] **Step 5: Fix JS require paths**

```bash
rg -l "require\(.*ib_(connection_status|tick_handler|realtime_server)" \
    scripts/infra/ib_realtime/ web/ 2>/dev/null
```

Within `scripts/infra/ib_realtime/*.js`, relative requires between siblings remain valid (all moved as a unit). External `require()` references from `web/` must be updated to the new path if any exist — grep and fix.

- [ ] **Step 6: `web/package.json` + `web/README.md` references**

Spec notes `web/package.json:20` and `web/README.md:291-293` reference `scripts/test_ib_realtime.py`. Those references must be updated to `scripts/infra/ib_realtime/test_ib_realtime.py` — **unless the plan is to symlink `scripts/test_ib_realtime.py` for compat**. This plan takes the **update-the-reference** path (Node files have no subprocess shim equivalent, and updating the package.json is cleaner than a symlink).

```bash
# In web/package.json line 20 and web/README.md 291-293:
# Replace scripts/test_ib_realtime.py with scripts/infra/ib_realtime/test_ib_realtime.py
```

- [ ] **Step 7: Move `infra/dev/` Python files**

```bash
git mv scripts/run_pytest_affected.py scripts/infra/dev/run_pytest_affected.py
git mv scripts/site_seo_audit.py      scripts/infra/dev/site_seo_audit.py
git mv scripts/context_constructor.py scripts/infra/dev/context_constructor.py
git mv scripts/batched_relay.py       scripts/infra/dev/batched_relay.py
```

- [ ] **Step 8: Update internal callers of `run_pytest_affected.py`**

```bash
rg "scripts/run_pytest_affected\.py" . --glob '!scripts/infra/' --glob '!docs/' 2>/dev/null
```

`CLAUDE.md` (root + `scripts/CLAUDE.md`) referenced as `python3.13 scripts/run_pytest_affected.py`. Update to `python3.13 scripts/infra/dev/run_pytest_affected.py`. Same for any `package.json` scripts, `Makefile`, or CI config.

- [ ] **Step 9: No Python shim for `infra/dev/` files**

These 4 Python files are dev tools with few/no external callers. If Task D sweep revealed any sibling `import run_pytest_affected`, rewrite to `from infra.dev.run_pytest_affected import X`. Otherwise, skip the shim.

### Task 5.3: Verify

- [ ] **Step 1: Symlink transparency full run**

```bash
bash scripts/cloud.sh            # starts local dev services via symlink (if that's the default)
# OR
bash scripts/local.sh            # fully local
# Verify FastAPI comes up the same way
curl -s localhost:8321/health | python3.13 -m json.tool
```

- [ ] **Step 2: Updated test-runner path**

```bash
python3.13 scripts/infra/dev/run_pytest_affected.py
```

Expected: same behavior as before from old path.

- [ ] **Step 3: JS suite (if applicable)**

```bash
node scripts/infra/ib_realtime/ib_realtime_server.js --help 2>&1 | head -5
```

- [ ] **Step 4: Per-bucket gate**

### Task 5.4: Commit + PR + merge + launchd follow-up

- [ ] **Step 1: Commit**

```bash
git add scripts/infra/ scripts/*.sh web/package.json web/README.md \
        CLAUDE.md scripts/CLAUDE.md
git commit -m "refactor(scripts): move infra .sh + JS + dev tools into scripts/infra/"
```

- [ ] **Step 2: PR, call out launchd audit status in PR description**

- [ ] **Step 3: Post-merge — re-install any affected launchd plists per Task 5.1 Step 2 finding**

- [ ] **Step 4: Soak ≥1 trading day. Verify launchctl list still shows xenon services running.**

---

## PR 6 — `services/` (requires launchd pre-audit)

**Goal:** Move 10 daemon + installer + cron files into `scripts/services/`. Same launchd-plist blocker pattern as PR 5.

**Files moved (10):**

| Old                                     | New                                              |
| --------------------------------------- | ------------------------------------------------ |
| `scripts/exit_order_service.py`         | `scripts/services/exit_order_service.py`         |
| `scripts/cta_sync_service.py`           | `scripts/services/cta_sync_service.py`           |
| `scripts/setup_exit_order_service.sh`   | `scripts/services/setup_exit_order_service.sh`   |
| `scripts/setup_cta_sync_service.sh`     | `scripts/services/setup_cta_sync_service.sh`     |
| `scripts/setup_monitor_daemon.sh`       | `scripts/services/setup_monitor_daemon.sh`       |
| `scripts/setup_cri_service.sh`          | `scripts/services/setup_cri_service.sh`          |
| `scripts/setup_data_refresh_service.sh` | `scripts/services/setup_data_refresh_service.sh` |
| `scripts/run_cta_sync.sh`               | `scripts/services/run_cta_sync.sh`               |
| `scripts/run_cri_scan.sh`               | `scripts/services/run_cri_scan.sh`               |
| `scripts/run_data_refresh.sh`           | `scripts/services/run_data_refresh.sh`           |

### Task 6.1: Launchd re-audit + cross-bucket note

- [ ] **Step 1: Re-check launchd-audit.md** — plists touched at PR 5 post-merge are noted; any newly-failing at current state?

- [ ] **Step 2: Note cross-bucket dependency**

`run_data_refresh.sh:185` calls `scripts/repair_cri_rvol_cache.py` — which remains at old path until PR 8a. The Phase 1 shim strategy ensures `scripts/repair_cri_rvol_cache.py` is still present (as the real file until PR 8a, later as a shim). This PR can proceed without waiting for PR 8a.

### Task 6.2: Prep — main() extraction for service daemons

- [ ] **Step 1: `exit_order_service.py` + `cta_sync_service.py` main() check**

If either is on Task C's extraction list, extract and commit as a prep commit.

```bash
git add scripts/exit_order_service.py scripts/cta_sync_service.py
git commit -m "refactor(services): extract main() for upcoming services/ move"
```

### Task 6.3: Move + rewrite + shims + symlinks

- [ ] **Step 1: git mv .py files**

```bash
git mv scripts/exit_order_service.py scripts/services/exit_order_service.py
git mv scripts/cta_sync_service.py   scripts/services/cta_sync_service.py
```

- [ ] **Step 2: Rewrite Python sibling imports**

```bash
for m in exit_order_service cta_sync_service; do
  echo "--- $m ---"
  rg -l "from $m\b|^import $m\b" scripts/ web/ 2>/dev/null
done
```

Rewrite: `from exit_order_service import X` → `from services.exit_order_service import X`.

- [ ] **Step 3: Create 2 Python shims**

`scripts/exit_order_service.py` + `scripts/cta_sync_service.py` — standard shim template with `<bucket>=services`.

- [ ] **Step 4: Move + symlink 8 shell scripts**

```bash
for f in setup_exit_order_service setup_cta_sync_service setup_monitor_daemon \
         setup_cri_service setup_data_refresh_service \
         run_cta_sync run_cri_scan run_data_refresh; do
  git mv scripts/$f.sh scripts/services/$f.sh
  ln -s services/$f.sh scripts/$f.sh
done
git add scripts/*.sh
```

- [ ] **Step 5: Verify internal path references inside the moved shell scripts**

Each `run_*.sh` and `setup_*.sh` may have logic like `SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"` then `python3.13 "$SCRIPT_DIR/../cri_scan.py"`. After move:

- If invoked via symlink from `scripts/run_X.sh`, `$0` points to the symlink; `$(dirname "$0")` = `scripts/`. Old relative paths like `"$SCRIPT_DIR/cri_scan.py"` still resolve to `scripts/cri_scan.py` (which exists as shim).
- If invoked directly from `scripts/services/run_X.sh`, `$(dirname "$0")` = `scripts/services/`. Old relative paths break.

**Action:** inspect each moved `.sh` for `$SCRIPT_DIR/<foo>.py` references and make them resolve to `scripts/` regardless of invocation path. Either:

```bash
# Option A: use BASH_SOURCE with readlink to find the real script dir, then go up one:
REAL_SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
SCRIPTS_ROOT="$REAL_SCRIPT_DIR/.."  # from scripts/services/ → scripts/

# Option B: always assume called from repo root, compute from that:
SCRIPTS_ROOT="$(git rev-parse --show-toplevel)/scripts"
```

This plan uses Option A (macOS/Linux-portable — `readlink -f` available on both). Apply to every `.sh` in the services/ move that references siblings by path.

- [ ] **Step 6: chmod +x shims + verify symlinks**

```bash
chmod +x scripts/exit_order_service.py scripts/cta_sync_service.py
ls -la scripts/run_*.sh scripts/setup_*.sh | grep '^l'
```

### Task 6.4: Verify — service lifecycle smoke

- [ ] **Step 1: Shell smoke**

```bash
for f in run_cta_sync run_cri_scan run_data_refresh; do
  bash scripts/$f.sh --help 2>&1 | head -3
done
```

- [ ] **Step 2: Setup scripts — dry-run mode if available, else --help**

```bash
for f in setup_exit_order_service setup_cta_sync_service setup_cri_service \
         setup_monitor_daemon setup_data_refresh_service; do
  bash scripts/$f.sh --help 2>&1 | head -3 || echo "no --help; inspect manually"
done
```

- [ ] **Step 3: Service daemon CLI entry**

```bash
python3.13 scripts/exit_order_service.py --help
python3.13 scripts/cta_sync_service.py --help
```

- [ ] **Step 4: Per-bucket gate**

### Task 6.5: Commit + PR + merge + launchd follow-up

- [ ] **Step 1: Commit**

```bash
git add scripts/services/ scripts/*.py scripts/*.sh
git commit -m "refactor(scripts): move service daemons + installers + cron wrappers into scripts/services/"
```

- [ ] **Step 2: PR**

- [ ] **Step 3: Post-merge — re-run affected `setup_*_service.sh install` commands to rewrite plists at new paths. Run on every audited host.**

```bash
# Example (adjust per launchd-audit.md findings):
bash scripts/services/setup_cri_service.sh install
launchctl unload ~/Library/LaunchAgents/xenon-cri.plist 2>/dev/null
launchctl load   ~/Library/LaunchAgents/xenon-cri.plist
launchctl list | grep xenon
```

- [ ] **Step 4: Soak ≥1 trading day — monitor CRI 30-min interval runs, exit-order service heartbeat, next morning's data refresh.**

---

## PR 7 — `ta/` (Paired Consolidation; 6 AM ET Scheduler)

**Goal:** Consolidate `ta_cli.py` + `ta_premarket_prep.py` + `ta_reseed_massive.py` + `ta_lib/` into `scripts/ta/`. `ta_premarket_prep.py` runs at 6 AM ET via scheduler — shim must be bulletproof.

**Files moved:**

CLIs:
| Old | New |
| ------------------------------- | --------------------------- |
| `scripts/ta_cli.py` | `scripts/ta/cli.py` |
| `scripts/ta_premarket_prep.py` | `scripts/ta/premarket_prep.py` |
| `scripts/ta_reseed_massive.py` | `scripts/ta/reseed_massive.py` |

Library contents (rename + move):
| Old | New |
| --------------------------- | ----------------------- |
| `scripts/ta_lib/bars.py` | `scripts/ta/bars.py` |
| `scripts/ta_lib/indicators.py` | `scripts/ta/indicators.py` |
| `scripts/ta_lib/store.py` | `scripts/ta/store.py` |
| `scripts/ta_lib/service.py` | `scripts/ta/service.py` |
| `scripts/ta_lib/__init__.py` | (merge into `scripts/ta/__init__.py`) |

Tests rename:
| Old | New |
| ---------------------------- | ------------------------- |
| `scripts/tests/test_ta_lib/` | `scripts/tests/test_ta/` |

### Task 7.1: Prep — project-root depth fix (ISSUE-3)

- [ ] **Step 1: Fix `ta_cli.py` + `ta_premarket_prep.py` + verify `ta_reseed_massive.py`**

```python
# BEFORE (all three files):
_project_root = Path(__file__).resolve().parent.parent
# AFTER:
_project_root = Path(__file__).resolve().parent.parent.parent
```

Grep verify:

```bash
rg -n "_project_root = Path" scripts/ta_cli.py scripts/ta_premarket_prep.py scripts/ta_reseed_massive.py
```

Each should show the 3-level `.parent.parent.parent` after fix.

- [ ] **Step 2: main() extraction if flagged**

- [ ] **Step 3: Pre-move smoke**

```bash
python3.13 scripts/ta_cli.py --help
python3.13 scripts/ta_premarket_prep.py --help
python3.13 scripts/ta_reseed_massive.py --help
```

- [ ] **Step 4: Commit prep**

```bash
git add scripts/ta_cli.py scripts/ta_premarket_prep.py scripts/ta_reseed_massive.py
git commit -m "refactor(ta): prep _project_root depth for upcoming ta/ move"
```

### Task 7.2: Move library contents

- [ ] **Step 1: Move `ta_lib/*` → `ta/*`**

```bash
git mv scripts/ta_lib/bars.py       scripts/ta/bars.py
git mv scripts/ta_lib/indicators.py scripts/ta/indicators.py
git mv scripts/ta_lib/store.py      scripts/ta/store.py
git mv scripts/ta_lib/service.py    scripts/ta/service.py
```

- [ ] **Step 2: Merge `ta_lib/__init__.py` content into `ta/__init__.py`**

```bash
cat scripts/ta_lib/__init__.py
# Copy the re-exports into scripts/ta/__init__.py (preserving the empty __init__.py from PR 0)
```

Edit `scripts/ta/__init__.py` to include the same re-exports as `ta_lib/__init__.py` previously provided.

```bash
git rm scripts/ta_lib/__init__.py
rmdir scripts/ta_lib
```

- [ ] **Step 3: Rewrite library imports**

```bash
rg -l "from ta_lib\b|^import ta_lib\b" scripts/ web/ 2>/dev/null
```

Rewrite every `from ta_lib.X import Y` → `from ta.X import Y`. Same for `from ta_lib import X` → `from ta import X` (if the re-export is in `ta/__init__.py`).

Verify:

```bash
rg "from ta_lib\b|^import ta_lib\b" scripts/ web/
```

Expected: 0 hits.

### Task 7.3: Move CLIs + shims

- [ ] **Step 1: git mv CLI files with rename**

```bash
git mv scripts/ta_cli.py            scripts/ta/cli.py
git mv scripts/ta_premarket_prep.py scripts/ta/premarket_prep.py
git mv scripts/ta_reseed_massive.py scripts/ta/reseed_massive.py
```

- [ ] **Step 2: Adjust `_project_root` back to 3 levels now that file is at new depth**

Actually: the Step 7.1 prep set it to `.parent.parent.parent`. From `scripts/ta/cli.py`, `.parent.parent.parent` = repo root. ✓ Correct.

- [ ] **Step 3: Rewrite internal imports within the moved CLIs**

Inside `scripts/ta/cli.py`, `scripts/ta/premarket_prep.py`, `scripts/ta/reseed_massive.py` — any `from ta_lib.X` or `from ta_cli import` must now become `from ta.X` or `from ta.cli import` (relative imports not used in this codebase per observation).

- [ ] **Step 4: Create 3 shims at old paths**

`scripts/ta_cli.py`:

```python
#!/usr/bin/env python3.13
"""Compatibility shim. Real home: scripts/ta/cli.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from ta.cli import *  # noqa: F401,F403
from ta.cli import main

if __name__ == "__main__":
    main()
```

`scripts/ta_premarket_prep.py` — substitute `from ta.premarket_prep import *`.

`scripts/ta_reseed_massive.py` — substitute `from ta.reseed_massive import *`.

- [ ] **Step 5: chmod +x**

```bash
chmod +x scripts/ta_cli.py scripts/ta_premarket_prep.py scripts/ta_reseed_massive.py
```

### Task 7.4: Rename test directory

- [ ] **Step 1: git mv test dir**

```bash
git mv scripts/tests/test_ta_lib scripts/tests/test_ta
```

- [ ] **Step 2: Rewrite imports inside the test files**

```bash
rg -l "from ta_lib\b" scripts/tests/test_ta/ 2>/dev/null
```

Rewrite to `from ta.X import Y`.

### Task 7.5: Verify — 6 AM ET scheduler dry run

- [ ] **Step 1: Shim + direct smoke**

```bash
python3.13 scripts/ta_cli.py --help                       # shim
python3.13 scripts/ta_premarket_prep.py --help            # shim
python3.13 scripts/ta_reseed_massive.py --help            # shim
python3.13 -c "from ta.cli import main; print('ok')"
python3.13 -c "from ta.premarket_prep import main; print('ok')"
python3.13 -c "from ta.reseed_massive import main; print('ok')"
python3.13 -c "from ta.bars import *; from ta.indicators import *; print('ok')"
```

- [ ] **Step 2: FastAPI scheduler dry-trigger**

```bash
bash scripts/local.sh &
sleep 8
# Hit the scheduler's manual trigger endpoint (if exists) or simulate the call:
python3.13 -c "
from api.subprocess import run_script
r = run_script('ta_premarket_prep.py', args=['--dry-run'], timeout=60)
print(r)
"
kill %1
```

Expected: `run_script` resolves `ta_premarket_prep.py` to the shim at `SCRIPTS_DIR/ta_premarket_prep.py`, which dispatches into `ta/premarket_prep.py`. Output matches pre-move behavior.

- [ ] **Step 3: ta/ unit tests**

```bash
python3.13 -m pytest scripts/tests/test_ta/ -x --tb=short
```

- [ ] **Step 4: Per-bucket gate**

### Task 7.6: Commit + PR + merge + soak

- [ ] **Step 1: Commit**

```bash
git add scripts/ta/ scripts/ta_cli.py scripts/ta_premarket_prep.py \
        scripts/ta_reseed_massive.py scripts/tests/test_ta/
git rm -r scripts/ta_lib  # if rmdir didn't stage the removal
git commit -m "refactor(scripts): consolidate ta_cli + ta_lib into scripts/ta/"
```

- [ ] **Step 2: PR + merge**

- [ ] **Step 3: Soak ≥1 trading day. Verify next 6 AM ET scheduler run succeeds (check `data/ta/*` timestamps next morning).**

---

## PR 8a — `scanners/` Direct Moves + `_shared/`

**Goal:** Rename `scanner_lib/` → `scanners/_shared/`, move 10 scanner CLIs directly into `scanners/`. `trend/` + `uw/` paired consolidations come in PR 8b.

### Task 8a.1: Rename `scanner_lib/` → `scanners/_shared/`

- [ ] **Step 1: git mv the whole directory**

```bash
# scanner_lib/ has: cache.py, executor.py, models.py, scoring.py, universe.py, __init__.py (plus maybe more)
ls scripts/scanner_lib/
for f in scripts/scanner_lib/*.py; do
  bn=$(basename "$f")
  [ "$bn" = "__init__.py" ] && continue  # __init__.py already exists in scanners/_shared/ from PR 0
  git mv "$f" "scripts/scanners/_shared/$bn"
done
# For __init__.py: merge contents into scanners/_shared/__init__.py (overwrite the empty PR-0 file with scanner_lib/__init__.py content)
cp scripts/scanner_lib/__init__.py scripts/scanners/_shared/__init__.py
git add scripts/scanners/_shared/__init__.py
git rm scripts/scanner_lib/__init__.py
rmdir scripts/scanner_lib
```

- [ ] **Step 2: Rewrite `from scanner_lib.X import Y` → `from scanners._shared.X import Y`**

```bash
rg -l "from scanner_lib\b|^import scanner_lib\b" scripts/ web/ 2>/dev/null
```

Rewrite every hit. Verify 0 remaining:

```bash
rg "from scanner_lib\b|^import scanner_lib\b" scripts/ web/
```

- [ ] **Step 3: No shim** (internal-only library, never invoked by path)

### Task 8a.2: Direct moves into `scanners/`

**Files (10):**

| Old                                | New                                         |
| ---------------------------------- | ------------------------------------------- |
| `scripts/cri_scan.py`              | `scripts/scanners/cri.py`                   |
| `scripts/vcg_scan.py`              | `scripts/scanners/vcg.py`                   |
| `scripts/gex_scan.py`              | `scripts/scanners/gex.py`                   |
| `scripts/leap_iv_scanner.py`       | `scripts/scanners/leap_iv.py`               |
| `scripts/leap_scanner_uw.py`       | `scripts/scanners/leap_uw.py`               |
| `scripts/garch_convergence.py`     | `scripts/scanners/garch.py`                 |
| `scripts/scanner.py`               | `scripts/scanners/scanner.py`               |
| `scripts/discover.py`              | `scripts/scanners/discover.py`              |
| `scripts/discover_forex_dom.py`    | `scripts/scanners/discover_forex.py`        |
| `scripts/repair_cri_rvol_cache.py` | `scripts/scanners/repair_cri_rvol_cache.py` |

- [ ] **Step 1: Prep — sys.path depth fixes**

| File                              | Change                                                             |
| --------------------------------- | ------------------------------------------------------------------ |
| `scripts/gex_scan.py:35`          | `_SCRIPT_DIR = Path(__file__).resolve().parent` → `.parent.parent` |
| `scripts/garch_convergence.py:47` | same                                                               |

- [ ] **Step 2: main() extraction for any Task-C hits**

- [ ] **Step 3: Commit prep**

```bash
git add scripts/gex_scan.py scripts/garch_convergence.py
git commit -m "refactor(scanners): prep sys.path depth for upcoming scanners/ move"
```

- [ ] **Step 4: git mv all 10 (with rename where shown)**

```bash
git mv scripts/cri_scan.py              scripts/scanners/cri.py
git mv scripts/vcg_scan.py              scripts/scanners/vcg.py
git mv scripts/gex_scan.py              scripts/scanners/gex.py
git mv scripts/leap_iv_scanner.py       scripts/scanners/leap_iv.py
git mv scripts/leap_scanner_uw.py       scripts/scanners/leap_uw.py
git mv scripts/garch_convergence.py     scripts/scanners/garch.py
git mv scripts/scanner.py               scripts/scanners/scanner.py
git mv scripts/discover.py              scripts/scanners/discover.py
git mv scripts/discover_forex_dom.py    scripts/scanners/discover_forex.py
git mv scripts/repair_cri_rvol_cache.py scripts/scanners/repair_cri_rvol_cache.py
```

- [ ] **Step 5: Rewrite sibling imports**

```bash
for pair in "cri_scan:scanners.cri" "vcg_scan:scanners.vcg" "gex_scan:scanners.gex" \
            "leap_iv_scanner:scanners.leap_iv" "leap_scanner_uw:scanners.leap_uw" \
            "garch_convergence:scanners.garch" "scanner:scanners.scanner" \
            "discover:scanners.discover" "discover_forex_dom:scanners.discover_forex" \
            "repair_cri_rvol_cache:scanners.repair_cri_rvol_cache"; do
  old="${pair%:*}"; new="${pair#*:}"
  echo "--- $old → $new ---"
  rg -l "from $old\b|^import $old\b" scripts/ web/ 2>/dev/null
done
```

Rewrite each. Verify zero old-name hits.

- [ ] **Step 6: Create 10 shims at old paths**

For each old→new pair, write `scripts/<old>.py` with the shim template — but note the **rename**: e.g., `scripts/cri_scan.py` shim body is `from scanners.cri import *` not `from scanners.cri_scan import *`. Explicit list:

| Shim file                          | Re-export from                   |
| ---------------------------------- | -------------------------------- |
| `scripts/cri_scan.py`              | `scanners.cri`                   |
| `scripts/vcg_scan.py`              | `scanners.vcg`                   |
| `scripts/gex_scan.py`              | `scanners.gex`                   |
| `scripts/leap_iv_scanner.py`       | `scanners.leap_iv`               |
| `scripts/leap_scanner_uw.py`       | `scanners.leap_uw`               |
| `scripts/garch_convergence.py`     | `scanners.garch`                 |
| `scripts/scanner.py`               | `scanners.scanner`               |
| `scripts/discover.py`              | `scanners.discover`              |
| `scripts/discover_forex_dom.py`    | `scanners.discover_forex`        |
| `scripts/repair_cri_rvol_cache.py` | `scanners.repair_cri_rvol_cache` |

- [ ] **Step 7: chmod +x shims**

### Task 8a.3: Verify + Commit + merge

- [ ] **Step 1: Smoke**

```bash
python3.13 scripts/cri_scan.py --help
python3.13 scripts/vcg_scan.py --help
python3.13 scripts/gex_scan.py --help
python3.13 scripts/leap_iv_scanner.py --help
python3.13 scripts/leap_scanner_uw.py --help
python3.13 scripts/garch_convergence.py --help
python3.13 scripts/scanner.py --help
python3.13 scripts/discover.py --help
python3.13 scripts/discover_forex_dom.py --help
python3.13 scripts/repair_cri_rvol_cache.py --help
```

- [ ] **Step 2: `run_data_refresh.sh:185` still resolves**

```bash
grep repair_cri_rvol_cache scripts/services/run_data_refresh.sh
# The path scripts/repair_cri_rvol_cache.py is now the shim → dispatches to scanners.repair_cri_rvol_cache
bash scripts/run_data_refresh.sh --dry-run 2>&1 | head -10
```

- [ ] **Step 3: Per-bucket gate**

- [ ] **Step 4: Commit**

```bash
git add scripts/scanners/ scripts/cri_scan.py scripts/vcg_scan.py scripts/gex_scan.py \
        scripts/leap_iv_scanner.py scripts/leap_scanner_uw.py scripts/garch_convergence.py \
        scripts/scanner.py scripts/discover.py scripts/discover_forex_dom.py \
        scripts/repair_cri_rvol_cache.py
git commit -m "refactor(scripts): rename scanner_lib→scanners/_shared and move scanner CLIs into scanners/"
```

- [ ] **Step 5: PR + merge + soak ≥1 trading day**

---

## PR 8b — `scanners/trend/` + `scanners/uw/` (Paired Consolidations)

**Goal:** Collapse `trend_scan.py` + `trend_scan_lib/` into `scanners/trend/`, and `uw_scan.py` + `uw_analyze.py` + `uw_scan_lib/` into `scanners/uw/`. `trend_scan.py` runs at 8:30 AM ET — shim is critical.

**Files moved (trend):**

| Old                                       | New                                  |
| ----------------------------------------- | ------------------------------------ |
| `scripts/trend_scan.py`                   | `scripts/scanners/trend/cli.py`      |
| `scripts/trend_scan_lib/stages/*.py`      | `scripts/scanners/trend/stages/*.py` |
| `scripts/trend_scan_lib/*.py` (top-level) | `scripts/scanners/trend/*.py` (flat) |
| `scripts/tests/test_trend_scan_lib/`      | `scripts/tests/test_scanners_trend/` |

**Files moved (uw):**

| Old                                 | New                                 |
| ----------------------------------- | ----------------------------------- |
| `scripts/uw_scan.py`                | `scripts/scanners/uw/scan.py`       |
| `scripts/uw_analyze.py`             | `scripts/scanners/uw/analyze.py`    |
| `scripts/uw_scan_lib/signals/`      | `scripts/scanners/uw/signals/`      |
| `scripts/uw_scan_lib/context/`      | `scripts/scanners/uw/context/`      |
| `scripts/uw_scan_lib/confluence.py` | `scripts/scanners/uw/confluence.py` |
| `scripts/uw_scan_lib/ranking.py`    | `scripts/scanners/uw/ranking.py`    |
| `scripts/uw_scan_lib/universe.py`   | `scripts/scanners/uw/universe.py`   |

### Task 8b.1: Prep — main() extraction + CLI imports

- [ ] **Step 1: `trend_scan.py` main() extraction if flagged**

- [ ] **Step 2: `uw_scan.py` + `uw_analyze.py` main() extraction if flagged**

- [ ] **Step 3: Pre-move smoke**

```bash
python3.13 scripts/trend_scan.py --help
python3.13 scripts/uw_scan.py --help
python3.13 scripts/uw_analyze.py --help
```

- [ ] **Step 4: Commit prep if any extractions were done**

### Task 8b.2: `scanners/trend/` consolidation

- [ ] **Step 1: Move library dir contents**

```bash
ls scripts/trend_scan_lib/
# Example structure: scripts/trend_scan_lib/{stages/,cache.py,storage.py,__init__.py, ...}
# Move stages/ subdir:
mkdir -p scripts/scanners/trend/stages
git mv scripts/trend_scan_lib/stages/*.py scripts/scanners/trend/stages/
[ -f scripts/trend_scan_lib/stages/__init__.py ] && \
  git mv scripts/trend_scan_lib/stages/__init__.py scripts/scanners/trend/stages/__init__.py

# Move top-level files (anything except __init__.py, which merges into existing):
for f in scripts/trend_scan_lib/*.py; do
  bn=$(basename "$f")
  [ "$bn" = "__init__.py" ] && continue
  git mv "$f" "scripts/scanners/trend/$bn"
done

# Merge __init__.py content:
cp scripts/trend_scan_lib/__init__.py scripts/scanners/trend/__init__.py
git add scripts/scanners/trend/__init__.py
git rm scripts/trend_scan_lib/__init__.py
rmdir scripts/trend_scan_lib/stages scripts/trend_scan_lib
```

- [ ] **Step 2: Move CLI with rename**

```bash
git mv scripts/trend_scan.py scripts/scanners/trend/cli.py
```

- [ ] **Step 3: Rewrite imports**

```bash
rg -l "from trend_scan_lib\b|^import trend_scan_lib\b" scripts/ web/ 2>/dev/null
```

Rewrite `from trend_scan_lib.X import Y` → `from scanners.trend.X import Y`.

```bash
rg -l "from trend_scan\b|^import trend_scan\b" scripts/ web/ 2>/dev/null
```

Rewrite `from trend_scan import X` → `from scanners.trend.cli import X` (if anyone imports the CLI module directly, which is rare).

Verify zero old-name hits.

- [ ] **Step 4: Rename test directory**

```bash
git mv scripts/tests/test_trend_scan_lib scripts/tests/test_scanners_trend
```

Rewrite `from trend_scan_lib.X` inside the test files to `from scanners.trend.X`:

```bash
rg -l "from trend_scan_lib\b" scripts/tests/test_scanners_trend/ 2>/dev/null
```

- [ ] **Step 5: Create shim**

`scripts/trend_scan.py`:

```python
#!/usr/bin/env python3.13
"""Compatibility shim. Real home: scripts/scanners/trend/cli.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from scanners.trend.cli import *  # noqa: F401,F403
from scanners.trend.cli import main

if __name__ == "__main__":
    main()
```

```bash
chmod +x scripts/trend_scan.py
```

### Task 8b.3: `scanners/uw/` consolidation

- [ ] **Step 1: Move library dir contents**

```bash
# uw_scan_lib/ has signals/, context/, confluence.py, ranking.py, universe.py, __init__.py
mkdir -p scripts/scanners/uw/signals scripts/scanners/uw/context

# Move subdirs:
for f in scripts/uw_scan_lib/signals/*.py; do
  bn=$(basename "$f")
  git mv "$f" "scripts/scanners/uw/signals/$bn"
done
for f in scripts/uw_scan_lib/context/*.py; do
  bn=$(basename "$f")
  git mv "$f" "scripts/scanners/uw/context/$bn"
done

# Move top-level:
git mv scripts/uw_scan_lib/confluence.py scripts/scanners/uw/confluence.py
git mv scripts/uw_scan_lib/ranking.py    scripts/scanners/uw/ranking.py
git mv scripts/uw_scan_lib/universe.py   scripts/scanners/uw/universe.py

# __init__.py — merge content:
cp scripts/uw_scan_lib/__init__.py scripts/scanners/uw/__init__.py
git add scripts/scanners/uw/__init__.py
git rm scripts/uw_scan_lib/__init__.py
[ -f scripts/uw_scan_lib/signals/__init__.py ] && \
  git rm scripts/uw_scan_lib/signals/__init__.py
[ -f scripts/uw_scan_lib/context/__init__.py ] && \
  git rm scripts/uw_scan_lib/context/__init__.py
# touch missing __init__.py in new subdirs if not moved:
touch scripts/scanners/uw/signals/__init__.py scripts/scanners/uw/context/__init__.py
git add scripts/scanners/uw/signals/__init__.py scripts/scanners/uw/context/__init__.py
rmdir scripts/uw_scan_lib/signals scripts/uw_scan_lib/context scripts/uw_scan_lib
```

- [ ] **Step 2: Move CLIs with rename**

```bash
git mv scripts/uw_scan.py    scripts/scanners/uw/scan.py
git mv scripts/uw_analyze.py scripts/scanners/uw/analyze.py
```

- [ ] **Step 3: Rewrite imports**

```bash
rg -l "from uw_scan_lib\b|^import uw_scan_lib\b" scripts/ web/ 2>/dev/null
```

Rewrite `from uw_scan_lib.X import Y` → `from scanners.uw.X import Y`. Subdir references: `from uw_scan_lib.signals.X` → `from scanners.uw.signals.X`.

```bash
rg -l "from uw_scan\b|^import uw_scan\b|from uw_analyze\b|^import uw_analyze\b" scripts/ web/ 2>/dev/null
```

Rewrite to `from scanners.uw.scan import X` / `from scanners.uw.analyze import X`.

Verify zero hits remain.

- [ ] **Step 4: Create 2 shims**

`scripts/uw_scan.py`:

```python
#!/usr/bin/env python3.13
"""Compatibility shim. Real home: scripts/scanners/uw/scan.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from scanners.uw.scan import *  # noqa: F401,F403
from scanners.uw.scan import main

if __name__ == "__main__":
    main()
```

`scripts/uw_analyze.py` — substitute `from scanners.uw.analyze import *`.

```bash
chmod +x scripts/uw_scan.py scripts/uw_analyze.py
```

### Task 8b.4: Verify — 8:30 AM ET scheduler dry run

- [ ] **Step 1: Smoke**

```bash
python3.13 scripts/trend_scan.py --help    # shim
python3.13 scripts/uw_scan.py --help       # shim
python3.13 scripts/uw_analyze.py --help    # shim
python3.13 -c "from scanners.trend.cli import main; print('trend ok')"
python3.13 -c "from scanners.uw.scan import main; print('uw scan ok')"
python3.13 -c "from scanners.uw.analyze import main; print('uw analyze ok')"
python3.13 -c "from scanners.uw.signals import *; print('signals ok')"
```

- [ ] **Step 2: FastAPI scheduler dry-trigger for trend_scan**

```bash
bash scripts/local.sh &
sleep 8
python3.13 -c "
from api.subprocess import run_script
r = run_script('trend_scan.py', args=['--dry-run'], timeout=120)
print(r)
"
# Also hit the HTTP endpoint:
curl -s -X POST localhost:8321/trend-scan/run | python3.13 -m json.tool | head -20
kill %1
```

- [ ] **Step 3: Trend + UW unit tests**

```bash
python3.13 -m pytest scripts/tests/test_scanners_trend/ -x --tb=short
# Any uw-specific test dir:
python3.13 -m pytest scripts/tests/ -k "uw_scan or uw_analyze" -x --tb=short
```

- [ ] **Step 4: Per-bucket gate**

### Task 8b.5: Commit + PR + merge + soak

- [ ] **Step 1: Commit**

```bash
git add scripts/scanners/trend/ scripts/scanners/uw/ scripts/trend_scan.py \
        scripts/uw_scan.py scripts/uw_analyze.py scripts/tests/test_scanners_trend/
git rm -rf scripts/trend_scan_lib scripts/uw_scan_lib 2>/dev/null
git commit -m "refactor(scripts): consolidate trend_scan + uw_scan/analyze into scanners/{trend,uw}/"
```

- [ ] **Step 2: PR + merge**

- [ ] **Step 3: Soak ≥1 trading day. Verify 8:30 AM ET pre-market scheduler run completes. Check `data/trend_scan.json` timestamp.**

---

## Phase 1 Final Verification

After PR 8b soaks one trading day without incident, Phase 1 is complete. Run the Phase 1 success criteria (spec §"Success Criteria"):

- [ ] **Step 1: `ls scripts/` count**

```bash
ls scripts/ | wc -l
```

Expected: ~22 entries (down from ~75). Of the remaining entries, most are shims + symlinks + the new bucket dirs + `CLAUDE.md` + `requirements-api.txt` + untouched subdirs.

- [ ] **Step 2: All old-path invocations still green**

```bash
# Full smoke list from Common Patterns — all 27 commands green.
```

- [ ] **Step 3: Zero old-name bare imports**

```bash
for m in fetch_ticker fetch_flow fetch_options fetch_oi_changes fetch_analyst_ratings \
         fetch_news fetch_menthorq_cta fetch_menthorq_dashboard fetch_x_watchlist fetch_x_xai \
         portfolio_attribution portfolio_performance portfolio_report \
         performance_explainer_report scenario_analysis scenario_report evaluate kelly \
         risk_reversal blotter free_trade_analyzer verify_options_oi \
         generate_cta_share generate_regime_share generate_vcg_share generate_gex_share \
         ib_execute ib_place_order ib_order_manage ib_orders ib_option_chain \
         ib_reconcile ib_sync naked_short_audit futu_sync \
         exit_order_service cta_sync_service \
         ta_cli ta_premarket_prep ta_reseed_massive ta_lib \
         trend_scan trend_scan_lib uw_scan uw_analyze uw_scan_lib \
         cri_scan vcg_scan gex_scan leap_iv_scanner leap_scanner_uw \
         garch_convergence scanner discover discover_forex_dom \
         repair_cri_rvol_cache scanner_lib; do
  hits=$(rg "from $m\b|^import $m\b" scripts/ web/ --glob '!scripts/*.py' 2>/dev/null)
  [ -n "$hits" ] && echo "--- $m still has callers ---" && echo "$hits"
done
```

Expected: zero output (shims themselves use the new paths; any remaining old-path callers surface here).

- [ ] **Step 4: Full test suite green**

```bash
python3.13 -m pytest scripts/tests/ -x --tb=short
cd web && npm test && npx playwright test
```

- [ ] **Step 5: Two consecutive trading days of 8:30 AM ET trend scan + 6 AM ET TA prep + 30-min CRI scan successful**

Monitor `data/trend_scan.json`, `data/ta/*`, `logs/cri-scan.*.log`.

- [ ] **Step 6: Launchd plists audit — no errors in Console.app for one week**

- [ ] **Step 7: Phase 2 handoff**

Phase 2 spec (`docs/superpowers/specs/2026-04-18-scripts-reorg/phase2-design.md`) takes over: rename `scripts/` → `src/xenon/`, adopt `uv` + `pyproject.toml` entry points, switch production callers to `.venv/bin/xenon-X`, delete shims.

---

## Rollback Runbook (any bucket PR)

Per spec §"Rollback":

1. **Git revert the bucket commit:** `git revert <merge-commit-sha> -m 1`.
2. **Verify old files restored at old paths** with original content.
3. **Re-install any launchd plists** that were updated post-merge to point to new paths — re-run the OLD installer script to restore the plist's `<string>scripts/<old_path>.sh</string>` entry.
4. **Reload affected launchd jobs:** `launchctl unload ~/Library/LaunchAgents/<name>.plist && launchctl load ~/Library/LaunchAgents/<name>.plist`.
5. **Verify next scheduled run succeeds** — wait for the next 8:30 AM ET window, `data/trend_scan.json` timestamp, CRI log heartbeat.

---

## Self-Review Checklist (complete before marking plan ready)

- [ ] Every file listed in `phase1-design.md` §"Per-File Placement Manifest" has a task.
- [ ] Every pre-commit checklist item (ISSUE-1, 2, 3, 4, 10, 11) is addressed in its corresponding PR's prep task.
- [ ] Every shim in §"External Callsite Audit" is created in its PR.
- [ ] Every symlink in §"Files receiving symlinks" is created in its PR (PR 5 or PR 6).
- [ ] `pyproject.toml` update (PR 0) includes all bucket names + transition `*_lib` names.
- [ ] `norecursedirs` includes `scripts/infra/ib_realtime`.
- [ ] Test directory renames (`test_trend_scan_lib` → `test_scanners_trend`, `test_ta_lib` → `test_ta`) covered in PR 7 and PR 8b.
- [ ] Cross-bucket shim dependencies (spec §"Cross-bucket shim dependencies") are preserved through Phase 1 — the `scanner.py`, `discover.py`, `cri_scan.py`, `repair_cri_rvol_cache.py` shims remain alive through PR 6 and are only replaced by renames in PR 8a.
- [ ] Launchd audit (Task B) is a blocker for PRs 5 and 6.
- [ ] `web/package.json` + `web/README.md` references to `scripts/test_ib_realtime.py` updated in PR 5.
- [ ] Every PR ends with a ≥1 trading-day soak before the next starts.
