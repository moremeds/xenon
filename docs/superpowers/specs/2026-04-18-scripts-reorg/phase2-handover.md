# Phase 2 Handover — Resume Prompt

**Last updated:** 2026-04-19. Paste the "Prompt for Fresh Session" at the bottom into a new Claude Code session and continue.

---

## Current State

- **Phase 1 shipped.** 58 top-level `scripts/` files moved into 8 verb-first buckets; old paths preserved via Python shims and shell symlinks. Commits through `e2a292ca`.
- **Phase 2 design written** at `docs/superpowers/specs/2026-04-18-scripts-reorg/phase2-design.md` (authoritative spec — `src/xenon/` package, `uv`, `xenon-*` entry binaries, shim retirement).
- **Phase 2 execution plan written** at `docs/superpowers/specs/2026-04-18-scripts-reorg/phase2-plan.md` — 5 grouped PRs with verification, soak, rollback. Corrected after a full 3-way tribunal review (Codex + Gemini + Claude) that surfaced 8 CRITICAL and 10 IMPORTANT issues, all resolved in-line.
- **Not committed yet.** Both files are on working tree; git status shows them as untracked or modified. Verify with `git status` before picking up.

## Decisions Locked (do not re-litigate)

| Decision                            | Choice                                                                                                                                                                              |
| ----------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Scope                               | Phase 2 + 1 Phase 1 loose end (`apex_refresh.py` bucketing)                                                                                                                         |
| PR cadence                          | 5 grouped PRs (PR 0–PR 5), not 10+ per-bucket PRs                                                                                                                                   |
| `apex_refresh.py` placement         | `scripts/fetchers/fetch_apex_data.py` → `xenon-fetch-apex-data`                                                                                                                     |
| Smoke mechanism                     | Bash script at `scripts/infra/dev/smoke_phase1_shims.sh`                                                                                                                            |
| `src/xenon/` vs keep `scripts/`     | `src/xenon/` (design spec)                                                                                                                                                          |
| `exit_order_service`                | **Retire** — replaced by `monitor-daemon` (per `setup_monitor_daemon.sh:22-37`)                                                                                                     |
| `scripts/lib/` and `scripts/infra/` | Stay at `scripts/` (JS + Bash — not Python package content)                                                                                                                         |
| `ta/` bucket                        | Retired — `ta_cli.py` / `ta_premarket_prep.py` / `ta_reseed_massive.py` deleted in commit 33b96e77 (Apr 17, 2026) along with the FastAPI 6 AM ET scheduler loop. No work in any PR. |

## Tribunal Findings Already Resolved

The plan file has a "Tribunal Review Findings — Resolved In-Line" table mapping each finding to its fix location. Do not re-run the tribunal unless the plan materially changes. Key fixes baked in:

- `xenon-api` needs `def main()` extraction (PR 4 Commit 0) — `scripts/api/server.py:1811` currently has only inline `if __name__ == "__main__"`.
- `_run_ib_script_with_recovery(script: str, args)` wrapper at `server.py:1697` takes script-name string; 7 call sites need entry-name conversion.
- `config/*.plist` files hardcode `.py`/`.sh` paths — setup installers `cp` verbatim; must rewrite plist sources.
- Inline `python -c "… from utils.market_calendar …"` exists in shell wrappers (PR 4 Commit 5b).
- Internal `subprocess.run([…, "scripts/X.py"])` callers in `risk_reversal.py:43,63`, `portfolio_report.py:905`, `cta_sync_service.py:230` (PR 4 Commit 7).
- FastAPI real routes are `POST /trend-scan` and `POST /regime/scan` — **not** `/trend-scan/run` / `/cri-scan/run`.
- `pyproject.toml` already exists with partial `[project]` + `target-version = "py39"` — PR 1 **expands**, bumps to `py313`.
- TA bucket retired post-plan-write (commit 33b96e77, Apr 17). PR 3 ta/ work + 6 AM ET soak gate amended out — see plan + design.

## Immediate Next Step

Invoke `writing-plans` to convert the execution plan into a step-by-step implementation plan with TDD-shaped tasks, per-step verification, and commit boundaries. Reference:

- `phase2-design.md` — authoritative design (what to build).
- `phase2-plan.md` — execution plan (how to sequence it, what to verify, what can go wrong).
- `phase1-design.md` — context for Phase 1 shim shape and the ~25-CLI smoke baseline.

The writing-plans output should land at `docs/superpowers/plans/2026-04-19-scripts-reorg-phase2.md`.

## Before You Resume

1. `git status` — confirm `phase2-plan.md` and this handover file are on working tree, nothing staged.
2. `git log --oneline -5` — confirm last commit is `e2a292ca` or later (Phase 1 v2 module-path patches).
3. Read `phase2-plan.md` end-to-end (≤25 min) before doing anything else. Every edit since the first draft is in the Tribunal Findings table.

## Out of Scope for This Handover

- Don't start executing PR 0 yet — writing-plans first, then execute step-by-step under that plan.
- Don't re-review the plan with codex-review unless material new content is added.
- Don't touch CHANGELOG references to old paths (accurate for their date).

## Prompt for Fresh Session

> I'm continuing Phase 2 of the `scripts/` → `src/xenon/` reorganization for the xenon project (trading system: FastAPI + Next.js + IB Gateway).
>
> Status: Phase 1 shipped (8 verb-first buckets under `scripts/`, old paths preserved via shims). Phase 2 design is written and tribunal-reviewed. The execution plan lives at `docs/superpowers/specs/2026-04-18-scripts-reorg/phase2-plan.md` — read the whole file first, especially the "Tribunal Review Findings — Resolved In-Line" section.
>
> Next step: invoke the `superpowers:writing-plans` skill to convert `phase2-plan.md` into a step-by-step implementation plan. The plan output should land at `docs/superpowers/plans/2026-04-19-scripts-reorg-phase2.md`.
>
> Context files to read before starting:
>
> 1. `docs/superpowers/specs/2026-04-18-scripts-reorg/phase2-handover.md` — this handover (locked decisions, tribunal fixes already baked in)
> 2. `docs/superpowers/specs/2026-04-18-scripts-reorg/phase2-plan.md` — the execution plan
> 3. `docs/superpowers/specs/2026-04-18-scripts-reorg/phase2-design.md` — authoritative design spec
> 4. `docs/superpowers/specs/2026-04-18-scripts-reorg/phase1-design.md` — Phase 1 shim shape
> 5. `CLAUDE.md` + `scripts/CLAUDE.md` — project rules (TDD, no naked shorts, four gates)
>
> Do NOT re-brainstorm, re-design, or re-review — go straight to writing-plans. All decisions are locked per the handover's "Decisions Locked" table. If you think a decision needs revisiting, flag it as a blocker and stop; do not silently diverge.
>
> Also: these plan files have not been committed yet. Check with `git status` before doing anything. Commit them as a first step (single commit with message "plan(scripts-reorg): phase 2 execution plan + handover") before invoking writing-plans.
