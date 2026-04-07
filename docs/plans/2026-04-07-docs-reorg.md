# Docs Reorganization Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reorganize `docs/` into stable functional subdirectories without breaking repo references.

**Architecture:** Move documents into category folders that reflect how the repo uses them, keep `docs/README.md` and `docs/status.md` stable as entry points, then update every hard-coded path reference in code, docs, tests, and metadata. Verification is a repo-wide stale-path scan plus diff inspection.

**Tech Stack:** Markdown, shell utilities (`mv`, `rg`, `perl`), git

---

### Task 1: Move files into functional directories

**Files:**
- Create: `docs/architecture/`
- Create: `docs/reference/`
- Create: `docs/runbooks/`
- Create: `docs/trading/`
- Create: `docs/workflows/`
- Modify: `docs/*`

**Step 1: Create the target directories**

Run: `mkdir -p docs/architecture docs/reference docs/runbooks docs/trading docs/workflows`
Expected: directories created with no output

**Step 2: Move the architecture, reference, runbook, trading, and workflow docs**

Run: `mv ...`
Expected: files relocated into their new folders

**Step 3: Check the new tree**

Run: `find docs -maxdepth 3 -type f | sort`
Expected: all moved files appear under their new directories

### Task 2: Refresh docs navigation

**Files:**
- Modify: `docs/README.md`

**Step 1: Rewrite the index around the new folder layout**

Update the root docs index so each section points at its new subdirectory.

**Step 2: Re-read the file**

Run: `sed -n '1,220p' docs/README.md`
Expected: root index matches the new structure

### Task 3: Update inbound references

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `web/README.md`
- Modify: `web/CLAUDE.md`
- Modify: `brand/CLAUDE.md`
- Modify: `scripts/**/*.py`
- Modify: `scripts/**/CLAUDE.md`
- Modify: `scripts/tests/*.py`
- Modify: `site/lib/landing-content.ts`
- Modify: `tasks/**/*.md`

**Step 1: Replace stale doc paths**

Run repo-wide replacements for the moved files so all references point at the new locations.

**Step 2: Preserve stable root files**

Leave references to `docs/README.md` and `docs/status.md` unchanged.

**Step 3: Check for missed paths**

Run: `rg -n "docs/[A-Za-z0-9_./-]+" -g '!docs/**' .`
Expected: only intentional references remain, using the new subdirectory paths

### Task 4: Verify and review

**Files:**
- Modify: `git index`

**Step 1: Inspect git status**

Run: `git status --short`
Expected: moved docs and updated reference files are listed

**Step 2: Inspect the diff**

Run: `git diff -- docs README.md CLAUDE.md web/README.md web/CLAUDE.md scripts site/lib/landing-content.ts tasks`
Expected: moves and path rewrites only

**Step 3: Final stale-path scan**

Run: `rg -n "docs/(api-infrastructure|architecture|brand-identity|chart-system|data-files|ib-connection-troubleshooting|ib-gateway-docker|ib_tws_api|ibc-remote-access|implement|intraday-interpolation|menthorq-prompts|oauth-subscription-auth|ops|options-flow-verification|options-structures|performance-reconstruction|plans|prompt|signal-thresholds|strategies|strategy-garch-convergence|strategy-vcg|unusual_whales_api|web-ui-reference)\\.(md|json|yaml)" -g '!docs/**' .`
Expected: no matches
