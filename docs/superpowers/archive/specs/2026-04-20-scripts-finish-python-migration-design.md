# Scripts Finish Python Migration — Design

**Date:** 2026-04-20
**Status:** Draft, awaiting user review
**Predecessors:** Phase 1 (PR #19), Phase 2 (PR #20)
**Goal:** Leave `scripts/` with zero importable Python. `scripts/` becomes shell + JS + dev tools + tests only — matching the written rule in `scripts/CLAUDE.md`.

---

## Motivation

After Phase 2 (PR #20), the `scripts/` → `src/xenon/` migration is ~95% done. What remains:

1. **One Phase 1 shim was not deleted** — `scripts/run_pytest_affected.py` still exists as a self-declared compatibility shim. Its own docstring says "Removed in Phase 2."
2. **One Python package is still in `scripts/`** — `scripts/ta_lib/` (7 modules, live consumers). Phase 2 PR 1 intentionally kept it for later because it had no `xenon-*` entry point to migrate. Now that everything around it has moved, it's the last outlier.
3. **One empty directory** — `scripts/tests/test_ta/` contains only a stale `__pycache__/`. The real tests were deleted when the `ta/` bucket was retired on Apr 17 (commit `33b96e77`), but the empty shell and its pycache survived.

Shipping these three together is cheaper than shipping them separately because verification is identical (pytest + smoke + scanner `--help`) and they share a rollback boundary.

## Scope

In scope:

- Delete `scripts/run_pytest_affected.py` (shim).
- Delete `scripts/tests/test_ta/` (empty dir + stale pycache).
- Migrate `scripts/ta_lib/` → `src/xenon/ta_lib/` via phased shim approach.
- Update the 2 live consumers in `src/xenon/` (`fetchers/fetch_apex_data.py`, `scanners/trend/cli.py`).
- Migrate the 9 test files that import `scripts.ta_lib.*` to the new module path.
- **Refresh all CLAUDE.md files** to reflect Phase 1 + Phase 2 + this PR. Specific edits listed in "CLAUDE.md updates" below.

Out of scope (explicit):

- Tier C items (`batched_relay.py`, `site_seo_audit.py`, `context_constructor.py`, `autoresearch*.sh`) — intent unclear, separate follow-up.
- Moving `scripts/tests/` → top-level `tests/` — separate decision.
- Touching `scripts/infra/`, `scripts/lib/`, `scripts/services/` — locked to stay put per Phase 2 decision.

## Approach — phased shim migration (matches user's "zero-break" preference)

Instead of atomic rewrite, the ta_lib migration follows the same pattern Phase 1 used:

1. `git mv scripts/ta_lib/ src/xenon/ta_lib/`.
2. Rewrite internal `from scripts.ta_lib.X import …` → `from xenon.ta_lib.X import …` inside the moved files.
3. Leave a **re-export shim** at `scripts/ta_lib/__init__.py` that does `from xenon.ta_lib import *` + re-exports the submodules — keeps any missed caller working.
4. Update the 2 known `src/xenon/` consumers to the new import path. Drop the `sys.path.insert` workaround at `scanners/trend/cli.py:16`.
5. Migrate the 9 in-tree test files (per `MEMORY/feedback_shim_vs_real_patching.md` — tests patch the real bucket path, never the shim).
6. Update root `CLAUDE.md` Tests block: `scripts/run_pytest_affected.py` → `scripts/infra/dev/run_pytest_affected.py`.
7. **Hold the shim for one soak cycle** before deleting it in a follow-up PR. Soak cycle = one nightly R2 refresh + one 8:30 AM ET trend scan run green.

### Why phased, not atomic

- Live trading system. The 8:30 AM ET trend scan and nightly R2 refresh both depend on `ta_lib`. Atomic rewrite + missed caller = silent cron failure.
- Shim is 5 lines of code; the insurance is nearly free.
- Every prior scripts-reorg PR used this pattern successfully. No reason to deviate.

## Components

### Re-export shim — `scripts/ta_lib/__init__.py`

```python
"""Compatibility shim. Real home: src/xenon/ta_lib/.

Removed in a follow-up PR after one soak cycle (nightly R2 refresh + 8:30 AM ET trend scan)."""
from xenon.ta_lib import *  # noqa: F401,F403
from xenon.ta_lib import service, indicators, bars, apex_sync, r2_store, parquet_store, dry_run_store  # noqa: F401
from xenon.ta_lib.service import TAService  # noqa: F401
```

This preserves both `from scripts.ta_lib import TAService` and `from scripts.ta_lib.r2_store import R2Store` styles.

### Consumers updated in-place (no shim behind them)

| File                                    | Line(s)                                                   | Change                                |
| --------------------------------------- | --------------------------------------------------------- | ------------------------------------- |
| `src/xenon/fetchers/fetch_apex_data.py` | 83, 84, 91, 125, 265, 430, 435                            | `scripts.ta_lib.*` → `xenon.ta_lib.*` |
| `src/xenon/scanners/trend/cli.py`       | 16 (drop sys.path comment + any sys.path.insert), 22, 821 | `scripts.ta_lib.*` → `xenon.ta_lib.*` |

### Tests migrated (9 files)

`test_r2_store.py`, `test_apex_sync.py`, `test_apex_refresh.py`, `test_parquet_store.py`, and all 5 files in `test_ta_lib/`. Find-and-replace `scripts.ta_lib` → `xenon.ta_lib`.

### Deletions

- `scripts/run_pytest_affected.py` (shim file).
- `scripts/tests/test_ta/` (directory + `__pycache__/`).
- `__pycache__/` sweep via `git clean -fdX` — trivial housekeeping.

### CLAUDE.md updates

Stale references accumulated across Phase 1 + Phase 2 + this PR. Fix in a single commit alongside the shim deletion so the docs and code land together.

| File                      | Line | Current                                                                         | Replacement                                                                                 |
| ------------------------- | ---- | ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `CLAUDE.md` (root)        | 9    | `scripts/api/CLAUDE.md`                                                         | `src/xenon/api/CLAUDE.md`                                                                   |
| `CLAUDE.md` (root)        | 91   | `scripts/api/services/uw_analyze_cache.py` + `scripts/api/routes/uw_analyze.py` | `src/xenon/api/services/uw_analyze_cache.py` + `src/xenon/api/routes/uw_analyze.py`         |
| `CLAUDE.md` (root)        | 116  | `python3.13 scripts/run_pytest_affected.py`                                     | `python3.13 scripts/infra/dev/run_pytest_affected.py`                                       |
| `scripts/CLAUDE.md`       | 3    | `scripts/api/CLAUDE.md covers FastAPI/IB Gateway infra`                         | `src/xenon/api/CLAUDE.md covers FastAPI/IB Gateway infra`                                   |
| `scripts/CLAUDE.md`       | 26   | "`scripts/ta_lib/` — Cloudflare R2 parquet-mirror reader. …"                    | "`src/xenon/ta_lib/` — Cloudflare R2 parquet-mirror reader. …" (mirror path text unchanged) |
| `src/xenon/api/CLAUDE.md` | 28   | `trend_scan.py --top 25`                                                        | `xenon-trend-scan --top 25`                                                                 |
| `web/CLAUDE.md`           | —    | clean — no edits                                                                | —                                                                                           |
| `brand/CLAUDE.md`         | —    | clean — no edits                                                                | —                                                                                           |

**Open question flagged, not decided in this PR:** `scripts/CLAUDE.md` describes Python pipelines that now live in `src/xenon/`. Its current path is a historical artifact. Moving it to `src/xenon/CLAUDE.md` is a separate decision — deferred to a follow-up if the user wants it.

## Data flow

No runtime data flow changes. Only import paths move. Producer/consumer of parquet snapshots, R2 bucket layout, DuckDB schema — all unchanged.

## Error handling

No new error paths. Shim preserves existing exception types by pure re-export.

Rollback: `git revert <sha>` restores the Phase 2 terminal state. The shim absorbs any forgotten caller until it is removed in the follow-up PR.

## Testing

Verification per commit:

```bash
uv sync --frozen
.venv/bin/pytest scripts/tests/ -x              # all green
.venv/bin/xenon-trend-scan --help                # exit 0
.venv/bin/xenon-fetch-apex-data --help           # exit 0
bash scripts/infra/dev/smoke_phase1_shims.sh     # 26/26 OK
rg "from scripts.ta_lib|import scripts.ta_lib" src/ scripts/tests/  # zero hits after commit 4
```

End-of-PR additional check:

```bash
rg "^(from|import) scripts\." src/                # zero hits — no Python in scripts/ is imported from src/
find scripts/ -name '*.py' -not -path '*/tests/*' -not -path '*/infra/*' -not -path '*/__pycache__/*'
# must print nothing — scripts/ contains no importable Python outside tests + dev tools
```

## Commit shape (single PR, 5 commits, 1 follow-up PR)

This PR:

1. `chore(scripts-reorg): delete run_pytest_affected.py shim + empty test_ta/ dir + refresh all CLAUDE.md files for Phase 1/2/current state`
2. `refactor(ta-lib): git mv scripts/ta_lib → src/xenon/ta_lib + internal import rewrite + shim`
3. `refactor(ta-lib): point src/xenon/ consumers at xenon.ta_lib`
4. `test(ta-lib): migrate 9 test files to xenon.ta_lib import path`
5. `chore: git clean pycache + verify terminal state`

Follow-up PR (after one soak cycle):

- `chore(ta-lib): remove scripts/ta_lib/ shim`

## Open questions

None after this review round. Ready for writing-plans if user approves.

## Spec self-review

- Placeholder scan: none.
- Internal consistency: yes — shim file contents match the test migration list (both the package-level and submodule forms work).
- Scope check: single-PR scope, one follow-up for shim removal. Appropriate.
- Ambiguity check: the "soak cycle" is defined concretely (one nightly R2 refresh + one 8:30 AM ET trend scan green).
