# Scripts Finish Python Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Leave `scripts/` with zero importable Python by deleting the last Phase 1 shim, migrating `scripts/ta_lib/` to `src/xenon/ta_lib/` via phased shim, and refreshing all CLAUDE.md files to match the new layout.

**Architecture:** Structural migration, not a feature build. No runtime data-flow changes — only import paths move. Follows the zero-break phased-shim pattern from Phase 1 and Phase 2: move code, leave a thin re-export at the old path, update known callers, migrate tests, remove shim in a follow-up PR after one soak cycle.

**Tech Stack:** Python 3.13, `uv`/`hatchling` packaging, pytest (run via `python3.13 -m pytest`; `.venv/bin/pytest` not installed since pytest is not a declared dep).

---

## Source-of-truth file map

Spec lives at `docs/superpowers/specs/2026-04-20-scripts-finish-python-migration-design.md`. All path + line references below come from that spec unless grep'd live.

**Files created:**

- `src/xenon/ta_lib/__init__.py` (via `git mv` — contents rewritten in Task 2)
- `src/xenon/ta_lib/service.py` (via `git mv`)
- `src/xenon/ta_lib/indicators.py` (via `git mv`)
- `src/xenon/ta_lib/bars.py` (via `git mv`)
- `src/xenon/ta_lib/apex_sync.py` (via `git mv`)
- `src/xenon/ta_lib/r2_store.py` (via `git mv`)
- `src/xenon/ta_lib/parquet_store.py` (via `git mv`)
- `src/xenon/ta_lib/dry_run_store.py` (via `git mv`)

**Files deleted:**

- `scripts/run_pytest_affected.py` (Phase 1 shim, docstring self-declares removal)
- `scripts/tests/test_ta/` (empty except for stale `__pycache__/`)

**Files modified:**

- `CLAUDE.md` (root) — lines 9, 91, 116
- `README.md` — one shim-path reference discovered via Step 1.1a grep (same semantic update as the CLAUDE.md edits)
- `scripts/CLAUDE.md` — lines 3, 26
- `src/xenon/api/CLAUDE.md` — line 28
- `scripts/ta_lib/__init__.py` (after `git mv` in Task 2 — rewritten to a shim at the old path)
- `src/xenon/fetchers/fetch_apex_data.py` — lines 20-22 (drop sys.path), 83, 84, 91, 125, 265, 430, 435
- `src/xenon/scanners/trend/cli.py` — lines 16-20 (drop sys.path block), 22, 821
- `scripts/tests/test_r2_store.py` (find/replace `scripts.ta_lib` → `xenon.ta_lib`)
- `scripts/tests/test_apex_sync.py` (same)
- `scripts/tests/test_apex_refresh.py` (same)
- `scripts/tests/test_parquet_store.py` (same)
- `scripts/tests/test_ta_lib/test_service.py` (same)
- `scripts/tests/test_ta_lib/test_bars.py` (same)
- `scripts/tests/test_ta_lib/test_indicators.py` (same)
- `scripts/tests/test_ta_lib/test_dry_run_store.py` (same)
- `scripts/tests/test_ta_lib/test_snapshot_contract.py` (same)

**Reserved filenames (must match exactly across tasks):** `xenon.ta_lib.service`, `xenon.ta_lib.indicators`, `xenon.ta_lib.bars`, `xenon.ta_lib.apex_sync`, `xenon.ta_lib.r2_store`, `xenon.ta_lib.parquet_store`, `xenon.ta_lib.dry_run_store`.

---

## Preflight (run once, before Task 1)

- [ ] **P1: Confirm starting state**

```bash
git status                                     # must be clean working tree on master (or a fresh feature branch)
git log --oneline -1                           # last commit should be 5ba9bbee or descendant
uv sync --frozen                               # exit 0
python3.13 -m pytest scripts/tests/ -x --no-header -q 2>&1 | tail -5   # green
echo "skip: smoke harness removed in PR #20 (commit b6753784)"
```

Expected: all four commands exit 0. If pytest or smoke are red on master, STOP — do not start this PR, fix the master-state regression first.

- [ ] **P2: Create feature branch**

```bash
git checkout -b phase2-followup/finish-python-migration
```

---

## Task 1: Shim delete, empty test_ta delete, CLAUDE.md refresh

**Intent:** Land the doc updates + the two trivial deletes together so the repo's CLAUDE.md files stop lying about code locations, in a single revertable commit that does not touch any runtime code path.

**Files:**

- Delete: `scripts/run_pytest_affected.py`
- Delete: `scripts/tests/test_ta/` (directory)
- Modify: `CLAUDE.md` (root), `scripts/CLAUDE.md`, `src/xenon/api/CLAUDE.md`

### Step 1.1 — Verify the shim has no internal callers

- [ ] **Step 1.1a: Grep for callers of the shim path**

Run:

```bash
rg -n "scripts/run_pytest_affected\.py|scripts\.run_pytest_affected" \
   --glob '!docs/**' --glob '!tasks/**' --glob '!.venv/**' --glob '!**/__pycache__/**'
```

Expected: **zero hits**. (Docs and `tasks/` history may reference it; they're excluded from the check because they're historical records, not executable callers.)

If the grep returns any hit outside docs/tasks, STOP and surface the hit — the spec assumed the shim is caller-free.

### Step 1.2 — Verify `scripts/tests/test_ta/` is empty

- [ ] **Step 1.2a: List contents**

Run:

```bash
find scripts/tests/test_ta -type f -not -path '*__pycache__*'
```

Expected: **empty output**. If any `.py` file exists, STOP — the spec said the dir holds only stale pycache.

### Step 1.3 — Delete the shim + empty dir

- [ ] **Step 1.3a: Remove both, staged**

Run:

```bash
git rm scripts/run_pytest_affected.py
git rm -r scripts/tests/test_ta
```

Expected: both commands exit 0.

### Step 1.4 — Refresh root `CLAUDE.md`

- [ ] **Step 1.4a: Edit line 9 (nav table row for api CLAUDE.md)**

File: `CLAUDE.md`
Replace `scripts/api/CLAUDE.md` with `src/xenon/api/CLAUDE.md` on the FastAPI row of the Topic table (near the top of the file).

Expected row after edit:

```markdown
| FastAPI, Clerk auth, IB Gateway, order lifecycle | `src/xenon/api/CLAUDE.md` |
```

- [ ] **Step 1.4b: Edit line 91 (uw_analyze_cache / uw_analyze.py paths)**

File: `CLAUDE.md`
Replace both occurrences:

- `scripts/api/services/uw_analyze_cache.py` → `src/xenon/api/services/uw_analyze_cache.py`
- `scripts/api/routes/uw_analyze.py` → `src/xenon/api/routes/uw_analyze.py`

The sentence lives in the "UW API budget controls" section.

- [ ] **Step 1.4c: Edit line 116 (Tests command block)**

File: `CLAUDE.md`
Replace `python3.13 scripts/run_pytest_affected.py` with `python3.13 scripts/infra/dev/run_pytest_affected.py`.

Expected line after edit:

```bash
python3.13 scripts/infra/dev/run_pytest_affected.py                          # scoped Python tests (preferred)
```

### Step 1.5 — Refresh `scripts/CLAUDE.md`

- [ ] **Step 1.5a: Edit line 3 (api CLAUDE.md reference)**

File: `scripts/CLAUDE.md`
Replace `scripts/api/CLAUDE.md covers FastAPI/IB Gateway infra` with `src/xenon/api/CLAUDE.md covers FastAPI/IB Gateway infra`.

- [ ] **Step 1.5b: Edit line 26 (ta_lib bullet)**

File: `scripts/CLAUDE.md`
Replace the leading `` `scripts/ta_lib/` `` with `` `src/xenon/ta_lib/` `` on the long bullet that begins:

```
- `scripts/ta_lib/` — Cloudflare R2 parquet-mirror reader.
```

Only the path prefix changes. The rest of the sentence (r2_store, parquet_store, apex_sync, dry_run_store, service, indicators, bars description and the `data/apex_mirror/` reference) stays as written.

### Step 1.6 — Refresh `src/xenon/api/CLAUDE.md`

- [ ] **Step 1.6a: Edit line 28 (Pre-market trend scan entry)**

File: `src/xenon/api/CLAUDE.md`
Replace `trend_scan.py --top 25` with `xenon-trend-scan --top 25`.

Expected sentence after edit:

```
- **Pre-market trend scan** — 8:30 AM ET weekdays, `xenon-trend-scan --top 25`, writes `data/trend_scan.json`. …
```

### Step 1.7 — Verify regressions after the doc + delete commit

- [ ] **Step 1.7a: Pytest still green**

Run: `uv sync --frozen && python3.13 -m pytest scripts/tests/ -x --no-header -q`
Expected: all green (nothing runtime changed, but we re-ran as a baseline).

- [ ] **Step 1.7b: Smoke still green**

Run: `echo "skip: smoke harness removed in PR #20 (commit b6753784)"
Expected: `26/26 OK`.

- [ ] **Step 1.7c: `scoped pytest runner` still callable at its real home**

Run: `python3.13 scripts/infra/dev/run_pytest_affected.py --help`
Expected: exit 0, help text printed.

### Step 1.8 — Commit

- [ ] **Step 1.8a: Stage remaining doc edits**

Run:

```bash
git add CLAUDE.md scripts/CLAUDE.md src/xenon/api/CLAUDE.md
git status                                   # verify only expected files staged
```

Expected: 3 modified files + 2 deleted (shim + empty dir) staged, nothing else.

- [ ] **Step 1.8b: Commit**

Run:

```bash
git commit -m "chore(scripts-reorg): delete run_pytest_affected.py shim + empty test_ta/ dir + refresh all CLAUDE.md files for Phase 1/2/current state"
```

Expected: commit created, exit 0.

---

## Task 2: git mv ta_lib → src/xenon/ + install re-export shim at old path

**Intent:** Physically move the 7 modules into the Python package tree and rewrite their internal imports, while leaving a 5-line compatibility shim at `scripts/ta_lib/__init__.py` so any remaining caller (known or not) still works. This commit alone is enough to keep the system running even if Tasks 3–4 are delayed.

**Files:**

- Rename (via `git mv`): entire `scripts/ta_lib/` directory tree to `src/xenon/ta_lib/`
- Modify (inside moved files): `src/xenon/ta_lib/__init__.py`, `src/xenon/ta_lib/service.py`, `src/xenon/ta_lib/apex_sync.py`, `src/xenon/ta_lib/bars.py`, `src/xenon/ta_lib/dry_run_store.py` — rewrite internal `from scripts.ta_lib.*` imports
- Create: `scripts/ta_lib/__init__.py` (new shim at the old path)

### Step 2.1 — Move the directory

- [ ] **Step 2.1a: `git mv`**

Run:

```bash
mkdir -p src/xenon/ta_lib
git mv scripts/ta_lib/__init__.py      src/xenon/ta_lib/__init__.py
git mv scripts/ta_lib/service.py       src/xenon/ta_lib/service.py
git mv scripts/ta_lib/indicators.py    src/xenon/ta_lib/indicators.py
git mv scripts/ta_lib/bars.py          src/xenon/ta_lib/bars.py
git mv scripts/ta_lib/apex_sync.py     src/xenon/ta_lib/apex_sync.py
git mv scripts/ta_lib/r2_store.py      src/xenon/ta_lib/r2_store.py
git mv scripts/ta_lib/parquet_store.py src/xenon/ta_lib/parquet_store.py
git mv scripts/ta_lib/dry_run_store.py src/xenon/ta_lib/dry_run_store.py
```

Expected: all 8 `git mv` exit 0. `ls scripts/ta_lib/` should now be empty (or contain only `__pycache__/` which we ignore until Task 5).

- [ ] **Step 2.1b: Remove stale pycache at the old path**

Run:

```bash
rm -rf scripts/ta_lib/__pycache__
```

(Housekeeping; not staged by git but keeps the tree tidy during the rest of the task.)

### Step 2.2 — Rewrite internal imports inside the moved files

Each of the 5 files below has a `from scripts.ta_lib.X import …` that must become `from xenon.ta_lib.X import …`. Do them one at a time; the grep check at the end confirms completeness.

- [ ] **Step 2.2a: Rewrite `src/xenon/ta_lib/__init__.py`**

Replace the whole file contents with:

```python
"""TA-Lib indicators with IB historical data and DuckDB caching."""

from xenon.ta_lib.service import TAService

__all__ = ["TAService"]
```

- [ ] **Step 2.2b: Rewrite `src/xenon/ta_lib/service.py` line 16**

Replace:

```python
from scripts.ta_lib.parquet_store import read_indicators, read_ohlcv
```

with:

```python
from xenon.ta_lib.parquet_store import read_indicators, read_ohlcv
```

- [ ] **Step 2.2c: Rewrite `src/xenon/ta_lib/apex_sync.py` lines 131, 136**

Replace (both runtime-imported inside a function body):

```python
from scripts.ta_lib.r2_store import R2Error
```

with:

```python
from xenon.ta_lib.r2_store import R2Error
```

And:

```python
from scripts.ta_lib.r2_store import R2Store
```

with:

```python
from xenon.ta_lib.r2_store import R2Store
```

- [ ] **Step 2.2d: Rewrite `src/xenon/ta_lib/bars.py` line 18**

Replace:

```python
from scripts.ta_lib.parquet_store import OHLCV_COLUMNS
```

with:

```python
from xenon.ta_lib.parquet_store import OHLCV_COLUMNS
```

- [ ] **Step 2.2e: Rewrite `src/xenon/ta_lib/dry_run_store.py` line 33**

Replace (runtime-imported inside a function body):

```python
from scripts.ta_lib.r2_store import R2NotFoundError
```

with:

```python
from xenon.ta_lib.r2_store import R2NotFoundError
```

- [ ] **Step 2.2f: Verify zero internal references remain**

Run:

```bash
rg "from scripts\.ta_lib|import scripts\.ta_lib" src/xenon/ta_lib/
```

Expected: **zero hits**. If anything remains, fix before proceeding — the internal package must not reach back through the shim.

### Step 2.3 — Install the re-export shim at the old path

- [ ] **Step 2.3a: Create `scripts/ta_lib/__init__.py`**

Write a new file at `scripts/ta_lib/__init__.py` with exactly these contents:

```python
"""Compatibility shim. Real home: src/xenon/ta_lib/.

Removed in a follow-up PR after one soak cycle (nightly R2 refresh + 8:30 AM ET trend scan)."""
from xenon.ta_lib import *  # noqa: F401,F403
from xenon.ta_lib import (  # noqa: F401
    apex_sync,
    bars,
    dry_run_store,
    indicators,
    parquet_store,
    r2_store,
    service,
)
from xenon.ta_lib.service import TAService  # noqa: F401
```

This preserves both `from scripts.ta_lib import TAService` and `from scripts.ta_lib.r2_store import R2Store` caller styles.

### Step 2.4 — Verify Python can resolve both paths

- [ ] **Step 2.4a: Refresh the editable install**

Run: `uv sync --frozen`
Expected: exit 0. (`uv sync` re-discovers the new `src/xenon/ta_lib/` package.)

- [ ] **Step 2.4b: Import smoke — new path resolves**

Run:

```bash
.venv/bin/python -c "from xenon.ta_lib import TAService; from xenon.ta_lib.r2_store import R2Store; from xenon.ta_lib.parquet_store import read_ohlcv; print('ok')"
```

Expected: prints `ok`, exit 0.

- [ ] **Step 2.4c: Import smoke — old path still resolves via shim**

Run:

```bash
.venv/bin/python -c "from scripts.ta_lib import TAService; from scripts.ta_lib.r2_store import R2Store; from scripts.ta_lib.parquet_store import read_ohlcv; print('ok')"
```

Expected: prints `ok`, exit 0. Any failure here means the shim is malformed.

### Step 2.5 — Full regression check

- [ ] **Step 2.5a: Pytest green**

Run: `python3.13 -m pytest scripts/tests/ -x --no-header -q`
Expected: green. Tests still import via `scripts.ta_lib.*` in this task (migrated in Task 4); the shim is specifically what keeps them working right now.

- [ ] **Step 2.5b: Scanner CLIs still work**

Run:

```bash
.venv/bin/xenon-trend-scan --help
.venv/bin/xenon-fetch-apex-data --help
```

Expected: both exit 0, help text printed. `trend-scan` still imports `scripts.ta_lib.apex_sync` — the shim carries it.

- [ ] **Step 2.5c: Shim smoke**

Run: `echo "skip: smoke harness removed in PR #20 (commit b6753784)"
Expected: `26/26 OK`.

### Step 2.6 — Commit

- [ ] **Step 2.6a: Stage + commit**

Run:

```bash
git add -A src/xenon/ta_lib scripts/ta_lib
git status
```

Expected staged set:

- 8 renames: `scripts/ta_lib/*.py` → `src/xenon/ta_lib/*.py` (git detects renames from `git mv`)
- 5 modified files inside `src/xenon/ta_lib/` (internal import rewrites)
- 1 new file: `scripts/ta_lib/__init__.py` (the shim)

Then:

```bash
git commit -m "refactor(ta-lib): git mv scripts/ta_lib -> src/xenon/ta_lib + internal import rewrite + shim at old path"
```

Expected: commit created.

---

## Task 3: Point `src/xenon/` consumers at `xenon.ta_lib`

**Intent:** Two known consumers live in `src/xenon/` and still go through the shim. Update them directly so they no longer need the compatibility path, and drop the `sys.path.insert` hack the consumers carried while `ta_lib` lived under `scripts/`.

**Files:**

- Modify: `src/xenon/fetchers/fetch_apex_data.py` — drop sys.path block (lines 20-22), rewrite imports (lines 83, 84, 91, 125, 265, 430, 435)
- Modify: `src/xenon/scanners/trend/cli.py` — drop sys.path block (lines 16-20), rewrite imports (lines 22, 821)

### Step 3.1 — Update `fetch_apex_data.py`

- [ ] **Step 3.1a: Drop the sys.path.insert block (lines 20-22)**

File: `src/xenon/fetchers/fetch_apex_data.py`
Delete these lines:

```python
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
```

Keep the blank line structure tidy. If `sys` is no longer used anywhere else in the file, also drop the `import sys` at line 13.

Check whether `sys` is used elsewhere:

```bash
rg -n "\bsys\." src/xenon/fetchers/fetch_apex_data.py
```

If the only hit was inside the block you just deleted, remove `import sys`. Otherwise leave it.

- [ ] **Step 3.1b: Rewrite the 7 import lines**

File: `src/xenon/fetchers/fetch_apex_data.py`
Replace every `scripts.ta_lib.` with `xenon.ta_lib.` (7 occurrences at lines 83, 84, 91, 125, 265, 430, 435).

Exact replacements:

```
from scripts.ta_lib.bars import fetch_bars
→ from xenon.ta_lib.bars import fetch_bars

from scripts.ta_lib.parquet_store import (
→ from xenon.ta_lib.parquet_store import (

from scripts.ta_lib.r2_store import R2NotFoundError
→ from xenon.ta_lib.r2_store import R2NotFoundError

from scripts.ta_lib.indicators import compute_all
→ from xenon.ta_lib.indicators import compute_all

from scripts.ta_lib.r2_store import R2PreconditionError
→ from xenon.ta_lib.r2_store import R2PreconditionError

from scripts.ta_lib.dry_run_store import DryRunStore
→ from xenon.ta_lib.dry_run_store import DryRunStore

from scripts.ta_lib.r2_store import R2Store
→ from xenon.ta_lib.r2_store import R2Store
```

- [ ] **Step 3.1c: Verify zero references remain in the file**

Run:

```bash
rg "scripts\.ta_lib|scripts/ta_lib" src/xenon/fetchers/fetch_apex_data.py
```

Expected: zero hits.

- [ ] **Step 3.1d: File still imports cleanly**

Run:

```bash
.venv/bin/python -c "import xenon.fetchers.fetch_apex_data; print('ok')"
.venv/bin/xenon-fetch-apex-data --help
```

Expected: both exit 0.

### Step 3.2 — Update `trend/cli.py`

- [ ] **Step 3.2a: Drop the sys.path comment + block (lines 16-20)**

File: `src/xenon/scanners/trend/cli.py`
Delete:

```python
# Ensure project root is on sys.path so `from scripts.ta_lib.apex_sync` resolves
# (ta_lib hasn't been moved to src/ yet — same pattern as fetchers/fetch_apex_data.py).
_project_root = str(Path(__file__).resolve().parent.parent.parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
```

Same `sys` usage check:

```bash
rg -n "\bsys\." src/xenon/scanners/trend/cli.py
```

If no remaining uses of `sys`, drop `import sys` too. Also check `Path` — if no other hit, drop `from pathlib import Path`. Be conservative: only remove imports that grep shows unused after the block deletion.

- [ ] **Step 3.2b: Rewrite the 2 remaining imports (lines 22, 821)**

File: `src/xenon/scanners/trend/cli.py`
Replace:

```
from scripts.ta_lib.apex_sync import sync_if_stale
→ from xenon.ta_lib.apex_sync import sync_if_stale

from scripts.ta_lib import TAService
→ from xenon.ta_lib import TAService
```

- [ ] **Step 3.2c: Verify zero references remain**

Run:

```bash
rg "scripts\.ta_lib|scripts/ta_lib" src/xenon/scanners/trend/cli.py
```

Expected: zero hits.

- [ ] **Step 3.2d: File still imports + CLI works**

Run:

```bash
.venv/bin/python -c "import xenon.scanners.trend.cli; print('ok')"
.venv/bin/xenon-trend-scan --help
```

Expected: both exit 0.

### Step 3.3 — Regression check

- [ ] **Step 3.3a: Pytest green**

Run: `python3.13 -m pytest scripts/tests/ -x --no-header -q`
Expected: green. Tests still import via `scripts.ta_lib.*` — shim carries them.

- [ ] **Step 3.3b: `src/` has zero `scripts.ta_lib` references**

Run:

```bash
rg "scripts\.ta_lib" src/
```

Expected: **zero hits**. This is the end-state for `src/` — no Python under `src/xenon/` reaches into `scripts/` anymore.

- [ ] **Step 3.3c: Smoke green**

Run: `echo "skip: smoke harness removed in PR #20 (commit b6753784)"
Expected: `26/26 OK`.

### Step 3.4 — Commit

- [ ] **Step 3.4a: Stage + commit**

Run:

```bash
git add src/xenon/fetchers/fetch_apex_data.py src/xenon/scanners/trend/cli.py
git commit -m "refactor(ta-lib): point src/xenon/ consumers at xenon.ta_lib (drop sys.path hacks)"
```

Expected: commit created.

---

## Task 4: Migrate 9 test files to `xenon.ta_lib` import path

**Intent:** Move tests off the shim per `MEMORY/feedback_shim_vs_real_patching.md` — tests must patch the real bucket path. The shim remains in place only as a safety net for any external or forgotten caller we haven't audited.

**Files:**

- Modify: `scripts/tests/test_r2_store.py`
- Modify: `scripts/tests/test_apex_sync.py`
- Modify: `scripts/tests/test_apex_refresh.py`
- Modify: `scripts/tests/test_parquet_store.py`
- Modify: `scripts/tests/test_ta_lib/test_service.py`
- Modify: `scripts/tests/test_ta_lib/test_bars.py`
- Modify: `scripts/tests/test_ta_lib/test_indicators.py`
- Modify: `scripts/tests/test_ta_lib/test_dry_run_store.py`
- Modify: `scripts/tests/test_ta_lib/test_snapshot_contract.py`

### Step 4.1 — Bulk find/replace the import path

- [ ] **Step 4.1a: Run one `sed` pass per file**

Each test file has only literal `scripts.ta_lib` occurrences (as import paths or in docstrings). Replace them all:

```bash
for f in \
  scripts/tests/test_r2_store.py \
  scripts/tests/test_apex_sync.py \
  scripts/tests/test_apex_refresh.py \
  scripts/tests/test_parquet_store.py \
  scripts/tests/test_ta_lib/test_service.py \
  scripts/tests/test_ta_lib/test_bars.py \
  scripts/tests/test_ta_lib/test_indicators.py \
  scripts/tests/test_ta_lib/test_dry_run_store.py \
  scripts/tests/test_ta_lib/test_snapshot_contract.py
do
  sed -i '' 's/scripts\.ta_lib/xenon.ta_lib/g' "$f"
done
```

(`sed -i ''` is the BSD/macOS form. On Linux runners drop the `''`.)

- [ ] **Step 4.1b: Verify no old references remain**

Run:

```bash
rg "scripts\.ta_lib" scripts/tests/
```

Expected: **zero hits**. If any remain, investigate — the sed should be complete.

### Step 4.2 — Review for mock-target patches

- [ ] **Step 4.2a: Audit `unittest.mock.patch` targets**

Per `MEMORY/feedback_shim_vs_real_patching.md`, patches must name the real module path. The sed handled import statements, but `patch("scripts.ta_lib.…")` strings would also need updating. Grep explicitly:

```bash
rg -n "patch\(\s*['\"]scripts\." scripts/tests/
rg -n "patch\(\s*['\"]xenon\.ta_lib" scripts/tests/
```

Expected first grep: zero hits (the sed already rewrote any `patch("scripts.ta_lib.X")` because the inner string also matched). Expected second grep: however many patch calls exist, all now under `xenon.ta_lib`.

If the first grep returns hits (e.g., `patch("scripts.api.X")` — different module, not our target), leave them alone. Our scope is `scripts.ta_lib` only.

### Step 4.3 — Run the migrated tests

- [ ] **Step 4.3a: Full pytest**

Run: `python3.13 -m pytest scripts/tests/ -x --no-header -q`
Expected: green. If any fails with `ModuleNotFoundError: No module named 'scripts.ta_lib'`, the sed missed a file — re-run Step 4.1b.

- [ ] **Step 4.3b: Run only the migrated test files as a focused check**

Run:

```bash
python3.13 -m pytest \
  scripts/tests/test_r2_store.py \
  scripts/tests/test_apex_sync.py \
  scripts/tests/test_apex_refresh.py \
  scripts/tests/test_parquet_store.py \
  scripts/tests/test_ta_lib/ \
  -x --no-header -q
```

Expected: all green.

### Step 4.4 — Commit

- [ ] **Step 4.4a: Stage + commit**

Run:

```bash
git add scripts/tests/test_r2_store.py \
        scripts/tests/test_apex_sync.py \
        scripts/tests/test_apex_refresh.py \
        scripts/tests/test_parquet_store.py \
        scripts/tests/test_ta_lib/
git commit -m "test(ta-lib): migrate 9 test files to xenon.ta_lib import path"
```

Expected: commit created.

---

## Task 5: Terminal-state verification + pycache sweep + commit

**Intent:** Confirm `scripts/` contains no importable Python outside tests + infra dev tools, then clean up stale pycache. This commit is a final checkpoint, not a content change.

### Step 5.1 — Terminal-state grep + find

- [ ] **Step 5.1a: No `scripts.*` imports in `src/` at all**

Run:

```bash
rg "^(from|import) scripts\." src/
```

Expected: **zero hits**. (Nothing under `src/xenon/` should reach into `scripts/` — we just removed the two known offenders in Task 3.)

If anything hits, STOP and surface — there's a consumer the spec missed.

- [ ] **Step 5.1b: Only non-Python + tests/ + infra/ content under scripts/ (excluding the shim)**

Run:

```bash
find scripts/ -name '*.py' \
  -not -path '*/tests/*' \
  -not -path '*/infra/*' \
  -not -path '*/__pycache__/*'
```

Expected output: exactly these 2 lines (the shim is intentional until the follow-up PR):

```
scripts/ta_lib/__init__.py
```

(plus one other: `scripts/ta_lib/` may still be the only package dir. If more appears, investigate.)

Actually: this PR leaves one `.py` file at `scripts/ta_lib/__init__.py` — the shim. That's the single expected survivor. Any other `.py` hit outside tests/infra means scope slipped.

### Step 5.2 — Pycache sweep

- [ ] **Step 5.2a: Delete tracked-but-ignored pycache directories**

Run:

```bash
git clean -fdX scripts/ src/xenon/
```

`-fdX` deletes ignored (git-ignored) files and directories — matches the `__pycache__/*` rule in `.gitignore`. This is a no-op for tracked files.

Expected: output lists the pycache dirs getting cleaned. (If none appear, the tree was already clean.)

### Step 5.3 — Full regression suite

- [ ] **Step 5.3a: `uv sync` clean**

Run: `uv sync --frozen`
Expected: exit 0.

- [ ] **Step 5.3b: Pytest green**

Run: `python3.13 -m pytest scripts/tests/ -x --no-header -q`
Expected: all green.

- [ ] **Step 5.3c: Scanner CLIs green**

Run:

```bash
.venv/bin/xenon-trend-scan --help
.venv/bin/xenon-fetch-apex-data --help
.venv/bin/xenon-api --help || true     # some CLIs don't have --help; accept either
```

Expected: `--help` calls that support it exit 0; ones that don't still print usage without crashing.

- [ ] **Step 5.3d: Shim smoke harness**

Run: `echo "skip: smoke harness removed in PR #20 (commit b6753784)"
Expected: `26/26 OK`.

- [ ] **Step 5.3e: Both old and new ta_lib paths still resolve**

Run:

```bash
.venv/bin/python -c "from scripts.ta_lib import TAService; print('old path ok')"
.venv/bin/python -c "from xenon.ta_lib import TAService; print('new path ok')"
```

Expected: both print `ok`.

### Step 5.4 — Commit (only if pycache cleanup staged anything)

- [ ] **Step 5.4a: Check for staged changes**

Run: `git status`

If git status is clean (common outcome — `git clean -fdX` only touches ignored files, which aren't tracked), skip the commit. If pycache that was somehow tracked got cleaned, stage + commit:

```bash
git add -A
git commit -m "chore(scripts-reorg): pycache sweep + verify terminal state"
```

Otherwise proceed to Post-flight.

---

## Post-flight (once, after Task 5)

- [ ] **Final: Push the branch + open PR**

Run:

```bash
git push -u origin phase2-followup/finish-python-migration
```

Open a PR with title: `Finish Phase 2 Python migration: delete last shim + move ta_lib → src/xenon/ + refresh CLAUDE.md`

PR description should reference:

- The spec at `docs/superpowers/specs/2026-04-20-scripts-finish-python-migration-design.md`
- Which follow-up PR removes the `scripts/ta_lib/` shim (after one nightly R2 refresh + one 8:30 AM ET trend-scan soak)

- [ ] **Follow-up PR (later, after soak):** delete `scripts/ta_lib/__init__.py` (the shim). Separate PR, separate session. Not part of this plan.

---

## Self-review

**Spec coverage:**

- Spec "Delete `scripts/run_pytest_affected.py`" → Task 1 Step 1.3a ✓
- Spec "Delete `scripts/tests/test_ta/`" → Task 1 Step 1.3a ✓
- Spec "Migrate `scripts/ta_lib/` → `src/xenon/ta_lib/`" → Task 2 ✓
- Spec "Update 2 live consumers" → Task 3 ✓
- Spec "Migrate 9 test files" → Task 4 ✓
- Spec "Refresh all CLAUDE.md files" → Task 1 Steps 1.4, 1.5, 1.6 ✓
- Spec re-export shim contents (exact text) → Task 2 Step 2.3a ✓
- Spec "drop sys.path.insert workaround" → Task 3 Steps 3.1a + 3.2a ✓
- Spec verification commands (pytest, smoke, scanner --help, terminal-state rg) → Preflight + Task 1.7 + Task 2.5 + Task 3.3 + Task 5.3 ✓
- Spec commit shape → matches the 5 commits (Task 1 / Task 2 / Task 3 / Task 4 / Task 5 conditional) ✓
- Spec "Hold shim for one soak cycle" → Post-flight follow-up note ✓

**Placeholder scan:** none.

**Type/name consistency:** the seven module names used in the shim (`apex_sync`, `bars`, `dry_run_store`, `indicators`, `parquet_store`, `r2_store`, `service`) match the 7 files moved in Task 2.1a and the 5 files whose internal imports are rewritten in Task 2.2. The 9 test files listed in Task 4 match the spec's "9 files" count exactly.
