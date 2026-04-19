# Phase 2 PR 1 → PR 2 Handover

**Last updated:** 2026-04-19. PR 0 merged to master; PR 1 ready on a feature branch awaiting merge. Paste the "Resume Prompt" at the bottom into a fresh Claude Code session.

---

## Current State

- **PR 0 merged to master.** 6 commits up through `83a76ce7`. Adds `scripts/fetchers/fetch_apex_data.py` (apex bucketing), `scripts/infra/dev/smoke_phase1_shims.sh` (+ symlink at `scripts/smoke_phase1_shims.sh`), `scripts/tests/test_phase1_shim_compat.py` (24 cases). Also amends `phase2-{plan,design,handover}.md` to retire the `ta/` bucket (see "Discoveries" below).
- **PR 1 ready on branch `phase2/pr1-pyproject-uv`.** 2 commits ahead of master:
  - `001767bf feat(scripts-reorg): adopt uv + declare xenon as installable package`
  - `c02e8d86 chore(scripts-reorg): clean up stale ruff isort entries + deprecate requirements-api.txt`
- **PR 1 verified locally:** `uv sync --frozen` exit 0; `import xenon` resolves to `src/xenon/__init__.py`; smoke 26/26 OK; compat pytest 24/24 PASSED **under system `python3.13`** (the uv venv deliberately ships only API deps until PR 3/4 migrate the rest of `requirements*.txt`; running compat under `.venv/bin/python` currently fails with `ModuleNotFoundError: pandas` on 13 cases — expected, not a regression); system-python `scripts.api.subprocess` still imports.
- **PR 1 NOT yet merged to master.** Awaiting user review/merge call before starting PR 2.

## Discoveries Made (already baked into the spec docs on master)

1. **TA bucket retired.** Commit `33b96e77` (Apr 17, two days before plan-write) deleted `ta_cli.py`, `ta_premarket_prep.py`, `ta_seed_yahoo.py`, and the FastAPI `_premarket_data_prep_loop` 6 AM ET driver. Every `ta/` reference in `phase2-plan.md`, `phase2-design.md`, and `phase2-handover.md` has been removed (commits `8d549951` + `83a76ce7`, on master). PR 3 has no `ta/` work; PR 3 + PR 4 soak gates lost the 6 AM ET TA prep checkbox; design `[project.scripts]` lost the 3 `xenon-ta-*` entries; PR 4 SHIM*FILES + rollback list lost the 3 `ta*\*.py` entries. **`ta_lib/`is unchanged** — it has 16 active consumers (including`fetchers/fetch_apex_data.py`and`scanners/trend/cli.py`) and stays put.
2. **`scripts/ta/` is now an empty dir** with only `__init__.py` from Phase 1 scaffolding. Cost is zero; PR 5 cleanup can remove it if anyone cares.
3. **Apex shim shape lesson** (already fixed in `8eb48119`): runpy-at-import-time breaks pytest collection. The shim must gate `runpy.run_module(...)` behind `if __name__ == "__main__":`. Tests that need to reach the real module must import via `scripts.fetchers.fetch_apex_data` (per `MEMORY/feedback_shim_vs_real_patching.md`), not via the shim.
4. **`requirements-api.txt` now carries a deprecation banner** at the top (commit `c02e8d86` on PR 1 branch). It still has the same 4 dep lines for legacy callers; deletion is in PR 4.
5. **Phase 1 shim conventions:** existing shims use `from <bucket>.<name> import *` + `from <bucket>.<name> import main` + `__main__` guard. The apex shim uses runpy gated under `__main__` per the plan's PR 0 spec — both shapes work. PR 2 plan says runpy is the target shape across all shims.

## Decisions Locked (do not re-litigate — also in `phase2-handover.md` on master)

| Area                                | Choice                                                                                                                              |
| ----------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| Scope                               | Phase 2 + 1 Phase 1 loose end (`apex_refresh.py` bucketing — DONE)                                                                  |
| PR cadence                          | 5 grouped PRs, soak only on production-path PRs                                                                                     |
| `apex_refresh.py` placement         | `scripts/fetchers/fetch_apex_data.py` → `xenon-fetch-apex-data` (entry point lands in PR 3)                                         |
| Smoke mechanism                     | `scripts/infra/dev/smoke_phase1_shims.sh` (DONE)                                                                                    |
| `src/xenon/` vs keep `scripts/`     | `src/xenon/` (scaffolded in PR 1 as empty package; populated in PR 2-4)                                                             |
| `exit_order_service`                | Retire — replaced by `monitor-daemon` (per `setup_monitor_daemon.sh:22-37`); no `xenon-exit-order-service` entry point              |
| `scripts/lib/` and `scripts/infra/` | Stay at `scripts/` (JS + Bash — not Python package content)                                                                         |
| `ta/` bucket                        | **Retired.** No work in any PR. (Was previously locked as "PR 3 — 6 AM ET production-path"; superseded by Apr 17 deletion finding.) |

## Workflow Pattern Established

- One feature branch per PR (`phase2/pr0-prerequisites` → merged; `phase2/pr1-pyproject-uv` → pending).
- Subagent-driven dispatch per task: implementer → spec reviewer → code quality reviewer → re-review on findings.
- Decisions are baked into the plan up front; no re-brainstorming inside subagents.
- VPS smoke and GHA workflow trigger are skipped per user preference; laptop verification is the gate.
- Commits do NOT carry `Co-Authored-By: Claude…` trailers (per `~/.claude/CLAUDE.md`).
- After each PR is approved by code review, controller asks user: merge to master / stack next PR / stop for review.

## Immediate Next Step

User's call:

1. **Merge PR 1 to master** (`git checkout master && git merge --ff-only phase2/pr1-pyproject-uv && git branch -d phase2/pr1-pyproject-uv`), then start PR 2 from a fresh branch `phase2/pr2-leaf-buckets`.
2. **Stack PR 2 on the PR 1 branch** without merging.
3. **Stop and review PR 1** before continuing.

Default if no answer at session start: option 3.

## PR 2 Readiness Notes (when starting)

PR 2 spec lives in `docs/superpowers/specs/2026-04-18-scripts-reorg/phase2-plan.md` PR 2 section (already amended for the `ta/` retirement — `ta/` is in the carve-out list, NOT in the buckets-moved list).

**Buckets to move (Python-only, no scheduler dependency, no soak required):**

- `shares/` (4 files)
- `clients/`, `utils/`, `analysis/`, `config/`, `monitor_daemon/`, `trade_blotter/` (foundational)

**Buckets carved out (do NOT move into `src/xenon/`):**

- `scripts/lib/` — JS only; stays put.
- `scripts/infra/` — bash + Node + dev tools; stays put. `scripts/infra/dev/*.py` is invoked by path, not import — no entry point binaries needed.
- `scripts/ta/` — RETIRED (now empty).

**Per-bucket move pattern (per phase2-plan.md PR 2 step 1-5):**

1. `git mv scripts/<bucket> src/xenon/<bucket>`.
2. Rewrite bare-name imports (`from <bucket>.X import …` → `from xenon.<bucket>.X import …`) using `rg -l` + sed.
3. Remove `sys.path.insert` blocks from per-file Phase 1 fixes.
4. Update Phase 1 shim at `scripts/<old>.py` to forward via `runpy.run_module("xenon.<bucket>.<name>", run_name="__main__")` — gated under `__main__` per the apex lesson.
5. Add `[project.scripts]` entry per CLI file in `pyproject.toml`. Run `uv sync` so binaries appear.

**Verification per bucket move:**

```bash
uv sync --frozen
.venv/bin/pytest tests/ -x
bash scripts/infra/dev/smoke_phase1_shims.sh
rg "^(from|import) <bucket_just_moved>\b" src/ tests/   # must be zero
```

**End-of-PR verification additions:** sibling-import regression checks (`from xenon.utils import ib_connection`, `from xenon.clients import ib_client`, `from xenon.trade_blotter import flex_query`); web test suite still green; `web/tests/lru-cache.test.ts` still resolves `scripts/lib/lru-cache.js`.

**Gate before PR 3:**

- All `[project.scripts]` entries for moved buckets resolve to working `.venv/bin/xenon-*` binaries.
- `rg '^(from|import) (shares|clients|utils|analysis|config|monitor_daemon|trade_blotter)\b' src/` returns zero.
- Phase 1 smoke + compat pytest still green.
- Web + Playwright green.
- No `sys.path.insert` lines in any moved file.

**Updated shim shape note:** PR 0's apex shim uses runpy gated under `__main__`. The plan PR 2 step 4 also says runpy. So all PR 2 shim updates should use:

```python
"""Compatibility shim. Real home: src/xenon/<bucket>/<name>.py.

Phase 1 preserves old invocation paths. Removed in Phase 2 PR 4."""
import runpy

if __name__ == "__main__":
    runpy.run_module("xenon.<bucket>.<name>", run_name="__main__")
```

NOT the old `from X import *` shape — that's brittle for pytest collection (the apex C1 finding proved it). All in-tree tests should already point at the real module per Phase 1 v2 (commit `e2a292ca`); if any still go through shims, migrate them in the same PR 2 commit (per MEMORY rule).

## Active TaskList Snapshot

Most tasks completed in this session:

- #1-5: PR 0 tasks (apex bucketing, smoke harness, compat test, gate verification, plan amendment) — ALL DONE
- #6: PR 1 (pyproject + uv sync + xenon stub) — DONE

When resuming, start a fresh TaskList for PR 2 (one task per bucket move + a final PR-level verification task).

## Resume Prompt for Fresh Session

> I'm continuing Phase 2 of the `scripts/` → `src/xenon/` reorganization for the xenon project (trading system: FastAPI + Next.js + IB Gateway).
>
> **Status:** PR 0 merged to master (6 commits up through `83a76ce7`). PR 1 is ready on branch `phase2/pr1-pyproject-uv` (2 commits) but NOT yet merged. Verified locally: `uv sync --frozen` works, `import xenon` works, Phase 1 shims still green.
>
> **First step:** read `docs/superpowers/specs/2026-04-18-scripts-reorg/phase2-pr1-handover.md` (this file) for the full state, locked decisions (including the now-retired `ta/` bucket), and PR 2 readiness notes. Also read `phase2-plan.md` PR 2 section (already amended for `ta/` retirement on master) and the resume prompt at the bottom of `phase2-handover.md` if you need broader context.
>
> **Branching policy:** one feature branch per PR. Decide with me at session start: merge PR 1 to master then branch fresh for PR 2, OR stack PR 2 on PR 1, OR stop and review PR 1 first. Default: stop and review.
>
> **Workflow:** subagent-driven (using `superpowers:subagent-driven-development`). One implementer subagent per task, spec reviewer + code quality reviewer after each. VPS smoke and GHA workflow trigger are skipped — laptop verification is the gate. Commits do NOT carry `Co-Authored-By` trailers per `~/.claude/CLAUDE.md`.
>
> **Decisions locked** per the handover's "Decisions Locked" table — do not re-brainstorm. If a decision needs revisiting, flag as a blocker and stop.
>
> **PR 2 shim shape gotcha** (learned in PR 0): use runpy gated under `if __name__ == "__main__":` — NOT `from X import *` at module level. The latter breaks pytest collection of any test that still imports through the shim. Tests should target the real bucket module path (per `MEMORY/feedback_shim_vs_real_patching.md`).
