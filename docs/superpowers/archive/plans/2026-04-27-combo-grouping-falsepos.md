# Stop Grouping Unrelated Singles into "Combo (N legs)" — Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax. There is one explicit human checkpoint before Phase 4 (parity refactor) — that part is not blocking the bugfix and may be deferred to a follow-up PR.

**Goal:** Two single-leg option positions sharing the same `(ticker, expiry)` but **not** forming a recognized structure (e.g. two SHORT puts at different strikes) are being collapsed into a fake "Combo (2 legs)" row labeled "Other". User cannot place orders on them individually. Fix: stop collapsing groups that fall through structure detection.

Plus a 5-minute housekeeping pass to commit shipped-but-untracked work.

**Concrete failing case (from user screenshot, 2026-04-27):**

- QQQ, expiry 2026-05-22
- SHORT 1× Put $585
- SHORT 1× Put $595
- Currently rendered as: one row "Combo (2 legs)" SHORT, $-2,127, in the "Other" category.
- Expected: two separate rows, each labeled "Short Put (Cash-Secured)" (or whatever the leg's `structure_type` already is), each with its own row-level order button.

**Architecture (verified):** `collapse_positions()` in `src/xenon/execution/ib_sync.py:385-409` groups all positions by `(symbol, expiry)`, then calls `detect_structure_type()` on the group. For the QQQ case: 2 puts, both `position < 0`, so it skips the vertical branch (which requires `1 long + 1 short`) and falls through to the all-long check (false — both are short), then to the default at line 247: `return f"Combo ({len(legs)} legs)", "complex"`. Frontend `getStructureCategory("Combo (2 legs)")` misses the catalog → returns `"other"` (`web/lib/structureCatalog.ts:108`).

**Fix strategy:** When a 2-leg group's `detect_structure_type` returns the fall-through label (`Combo (2 legs)`), emit each leg as its own single-leg position rather than collapsing. Recognized structures (vertical, straddle, synthetic, covered call, etc.) keep collapsing as today.

**Predicate scope — narrow on purpose:** Only split when `len(legs) == 2` AND the structure is fall-through. The 3+ leg fall-through labels (`Long Call Combo (3 legs)`, `Combo (4 legs)`, etc.) more likely represent intentional multi-leg orders we just don't classify (butterflies, condors, custom structures) — splitting those would be wrong. The user's reported bug is specifically about pairs (`"two single leg option … gets to grouped as other"`), so scope the fix to that.

**Known imperfection (call out in PR):** IB position objects don't carry "originating combo order id," so we genuinely cannot tell "two unrelated singles" from "two legs of one user-submitted combo order whose structure we don't recognize." The `len == 2 + fall-through` heuristic catches the user's case but will also split legitimate user-opened 2-leg unrecognized combos. A future hardening would join against `order_submissions` to use `order_id` lineage — out of scope for this fix.

**Recursion safety (verified):** `detect_structure_type` lines 151-157 explicitly handle `len(legs) == 1` and return concrete labels (`"Long Call"`, `"Short Put"`, `"Stock"`) — never a `Combo (1 legs)` fall-through. So `collapse_positions([single_leg])` cannot infinite-loop.

**IB/Futu parity (Phase 4, deferrable):** User asked that IB and Futu use the same code path. Today: IB uses Python `collapse_positions` server-side; Futu does its own pairing in `web/lib/futuPortfolioAdapter.ts` + `web/lib/portfolioByStructure.ts:fuseVirtualPair` (Futu-only flag). Resolving this is a refactor, not a bugfix — keep it as a separate Phase 4 task that runs after the bugfix lands and is verified.

**Tech Stack:** Python 3.13 (uv), pytest, TypeScript (Vitest), Playwright/chrome-cdp, git.

---

## Phase 1 — Housekeeping (5 min, no code change)

### Task 0: Create the feature branch

We're starting from `master`. Per `~/.claude/CLAUDE.md`, never push master directly — always go via PR.

- [ ] **Step 1: Confirm current branch**

  ```bash
  git branch --show-current
  ```

  Expected: `master` (or whatever non-feature branch you're on).

- [ ] **Step 2: Create and switch to the feature branch**

  ```bash
  git checkout -b fix/combo-grouping-singles
  ```

  All Phase 1–3 commits land on this branch. The PR in Phase 3 targets `master`.

### Task 1: Commit untracked work

**Files:**

- `CLAUDE.md` (modified — adds todo-capture rule §7)
- `docs/todo-backlog.md` (new — formalizes inbox)
- `docs/superpowers/plans/2026-04-26-broker-account-scope.md` (plan; work shipped in `c864cf7a`/`478874a5`/`9814a440`/`963bdc16`)
- `docs/superpowers/plans/2026-04-26-postgres-review-fixes.md` (plan; shipped via PR #52 follow-ups)
- `docs/runbooks/mac-mini.md` (new ops runbook)
- `scripts/deploy/` (empty dir — drop)
- `config/templates/` (empty dir — drop)

- [ ] **Step 1: Verify the two empty dirs really are empty**

  ```bash
  find scripts/deploy config/templates -type f
  ```

  Expected: no output.

- [ ] **Step 2: Drop the empty dirs**

  ```bash
  rmdir scripts/deploy config/templates 2>/dev/null || true
  ```

- [ ] **Step 3: Eyeball the new runbook**

  ```bash
  wc -l docs/runbooks/mac-mini.md
  head -40 docs/runbooks/mac-mini.md
  ```

- [ ] **Step 4: Stage and commit**

  ```bash
  git add CLAUDE.md docs/todo-backlog.md \
          docs/superpowers/plans/2026-04-26-broker-account-scope.md \
          docs/superpowers/plans/2026-04-26-postgres-review-fixes.md \
          docs/runbooks/mac-mini.md
  git commit -m "chore(docs): commit todo-backlog, mac-mini runbook, and shipped scope/postgres plans"
  ```

- [ ] **Step 5: Confirm clean tree**

  ```bash
  git status --short
  ```

  Expected: empty.

---

## Phase 2 — Bugfix: stop collapsing unrelated singles (TDD)

### Task 2: Write the failing test

**Files:**

- Test: `scripts/tests/test_collapse_positions.py` (new — sibling to `test_all_long_combo.py`)
- Source: `src/xenon/execution/ib_sync.py:385-409` (`collapse_positions`), `:146-247` (`detect_structure_type`)

**Why a new file:** `scripts/tests/test_all_long_combo.py` already exists and tests the 3-leg `Long Call Combo` case; it documents that "Structure name should be descriptive, not 'Combo (2 legs)'" — directly relevant to the pre-fix behavior. Keep the new regression in a sibling file (`test_collapse_positions.py`) to keep a clear bug-name in the file name. The 3-leg test in `test_all_long_combo.py` MUST stay green after our fix — it's the guardrail proving we didn't widen the predicate too far.

- [ ] **Step 1: Confirm sibling test file exists; create new file next to it**

  ```bash
  ls scripts/tests/test_all_long_combo.py
  ```

  Then `touch scripts/tests/test_collapse_positions.py`.

- [ ] **Step 2: Write the failing test**

  Use the exact QQQ failing case. The test should assert that two SHORT puts with the same expiry but no recognized structure produce **two separate output positions**, not one collapsed combo.

  ```python
  def test_two_short_puts_same_expiry_stay_separate():
      """Regression: QQQ SHORT 1x Put $585 + SHORT 1x Put $595 same expiry
      were being collapsed into 'Combo (2 legs)' (Other category) and the
      user lost per-leg order entry. They are NOT a recognized structure
      and must remain as two separate single-leg positions."""
      from xenon.execution.ib_sync import collapse_positions

      positions = [
          {
              "symbol": "QQQ", "expiry": "20260522", "secType": "OPT",
              "right": "P", "strike": 585.0,
              "position": -1, "entry_cost": -858.93,
              "marketValue": -164.0, "structure": "Short Put $585",
          },
          {
              "symbol": "QQQ", "expiry": "20260522", "secType": "OPT",
              "right": "P", "strike": 595.0,
              "position": -1, "entry_cost": -1268.28,
              "marketValue": -211.0, "structure": "Short Put $595",
          },
      ]

      out = collapse_positions(positions)

      assert len(out) == 2, f"expected 2 separate rows, got {len(out)}: {out}"
      structure_types = sorted(p["structure_type"] for p in out)
      # Each leg keeps its individual structure label — no fake combo wrapper.
      for p in out:
          assert "Combo" not in p["structure_type"], (
              f"position should not be wrapped in a Combo: {p}"
          )

  def test_recognized_combos_still_collapse():
      """Guardrail: the fix must not regress real combos. A vertical
      (1 long + 1 short same type, opposite directions) must STILL collapse."""
      from xenon.execution.ib_sync import collapse_positions

      positions = [
          {
              "symbol": "AAPL", "expiry": "20260620", "secType": "OPT",
              "right": "C", "strike": 200.0, "position": 1,
              "entry_cost": 470.0, "marketValue": 480.0,
              "structure": "Long Call $200",
          },
          {
              "symbol": "AAPL", "expiry": "20260620", "secType": "OPT",
              "right": "C", "strike": 210.0, "position": -1,
              "entry_cost": -220.0, "marketValue": -210.0,
              "structure": "Short Call $210",
          },
      ]

      out = collapse_positions(positions)

      assert len(out) == 1, "vertical must collapse to one combo row"
      assert "Bull Call Spread" in out[0]["structure_type"]
  ```

- [ ] **Step 3: Run the test — must fail RED on the first case**

  ```bash
  cd /Users/chenxi/projects/xenon
  uv run pytest scripts/tests/test_collapse_positions.py -xvs
  ```

  Expected: `test_two_short_puts_same_expiry_stay_separate` fails with `expected 2 separate rows, got 1` (or the structure_type assertion). `test_recognized_combos_still_collapse` should already pass — it documents the behavior we're preserving.

  If the first test fails for a _different_ reason (e.g. import error, missing field), fix the test scaffolding before proceeding. The expected leg dict shape must match what `collapse_positions` actually consumes — verify by reading lines 385-409 and the IB position dict shape.

### Task 3: Minimal fix

- [ ] **Step 1: Refactor `collapse_positions` to split unrecognized 2-leg groups**

  Strategy: after `detect_structure_type(legs)`, check if the group is exactly 2 legs AND the returned `structure_type` matches the fall-through pattern. If so, emit each leg as its own single-leg group. Otherwise behave as today (preserves recognized structures AND 3+ leg fall-throughs).

  Edit `src/xenon/execution/ib_sync.py:385-409`. The minimal change is inside the `for (symbol, expiry), legs in groups.items():` loop — before computing aggregates, branch on whether the group is a 2-leg fall-through:

  ```python
  for (symbol, expiry), legs in groups.items():
      structure_type, risk_profile = detect_structure_type(legs)

      # Bugfix 2026-04-27: 2 legs sharing (ticker, expiry) that don't form
      # a recognized structure (e.g. two SHORT puts at different strikes)
      # are usually two unrelated single-leg orders, not a real combo.
      # Collapsing them hides per-leg order entry from the UI and buckets
      # the row under "Other". Split them back into singles.
      # Scope is intentionally narrow (==2 legs) — 3+ leg fall-throughs
      # like "Long Call Combo (3 legs)" more often represent intentional
      # multi-leg orders we just don't classify.
      if len(legs) == 2 and _is_unrecognized_combo(structure_type):
          for leg in legs:
              for emitted in collapse_positions([leg]):
                  emitted["id"] = position_id
                  position_id += 1
                  collapsed.append(emitted)
          continue

      structure_desc = format_structure_description(structure_type, legs)
      # ... existing aggregate logic
  ```

  Add the predicate as a module-level helper above `collapse_positions`:

  ```python
  def _is_unrecognized_combo(structure_type: str) -> bool:
      """True when detect_structure_type returned a fall-through "Combo (...)"
      label. Verified labels (ib_sync.py:241-247):
        - "Combo (N legs)"
        - "Long Combo (N legs)"
        - "Long Call Combo (N legs)"
        - "Long Put Combo (N legs)"
      All four end with " legs)" and contain " Combo (" or start with "Combo (".
      """
      return " Combo (" in structure_type or structure_type.startswith("Combo (")
  ```

  **Why recursion over a hand-rolled `_emit_single_leg`:** The collapsed-position dict shape (lines 410-545) carries many derived fields — `entry_cost`, `market_value`, `is_market_price_calculated`, `daily_pnl`, etc. — and downstream consumers in FastAPI + frontend rely on the exact shape. Recursing via `collapse_positions([leg])` reuses the existing code path, guaranteeing field parity. `detect_structure_type([leg])` for a single leg returns "Long Call" / "Short Put" / "Stock" (lines 151-157, verified) — no infinite-loop risk.

  After the `position_id` reassignment in the recursive branch, the inner call's own `position_id` numbering is discarded; only the outer counter persists. Verify by inspecting the inner call return: it ALWAYS sets `id = 1` since the inner counter starts at 1, and we overwrite it before appending. ✓

- [ ] **Step 2: Run the new test — must pass GREEN**

  ```bash
  uv run pytest scripts/tests/test_collapse_positions.py -xvs
  ```

  Both `test_two_short_puts_same_expiry_stay_separate` and `test_recognized_combos_still_collapse` should pass.

- [ ] **Step 3: Run the full Python suite — no regressions**

  ```bash
  uv run python scripts/infra/dev/run_pytest_affected.py
  ```

  Expected: all green. If anything fails in `test_ib_sync*`, `test_portfolio*`, `test_naked_short_audit`, or related — those are the load-bearing downstream consumers. Read the failure, decide if it's exposing a real regression vs. a test that was asserting the old (wrong) behavior.

- [ ] **Step 4: Verify Postgres write path doesn't reject the new shape**

  ```bash
  psql -h localhost -U xenon_app xenon_db -c "\d positions" | grep -E "PRIMARY|UNIQUE|CHECK"
  ```

  After the fix, `_save_portfolio_to_postgres(portfolio)` (`ib_sync.py:1168`) will write 2 rows for the QQQ case where it previously wrote 1. Confirm there is NO unique constraint on `(broker, account_env, broker_account, ticker, expiry)` that would conflict — only constraints on a per-leg key (e.g. including `strike`/`right`) are safe. If a conflicting unique key exists, the fix needs a schema migration before it can land. Document the constraint state in the PR.

- [ ] **Step 5: Verify naked-short audit still passes for the QQQ case**

  ```bash
  uv run pytest scripts/tests/test_naked_short_audit.py -xvs
  ```

  Per `src/xenon/CLAUDE.md` Gate-4 table, two cash-secured short puts are ALLOW. Splitting the wrapper shouldn't change that, but the audit was written against the collapsed shape. If `test_naked_short_audit.py` has a fixture matching "Combo (2 legs)", it was implicitly testing the wrapper — update it to test per-leg.

- [ ] **Step 6: Run the web tests + typecheck — frontend mock data may rely on the old shape**

  ```bash
  cd web && npm run typecheck && npm test
  ```

  Pay attention to `portfolio-by-structure.test.ts`, `position-table.test.ts`, and any test that uses fixture portfolios with same-expiry singles. The earlier audit (`grep "Combo (2 legs)"`) found ZERO matches in `web/tests`, `web/lib`, `web/components` — so no fixture currently hard-codes the old string. That said, run the full vitest to catch shape drift through other paths (e.g. fixtures using object literals that mimic the collapsed shape).

- [ ] **Step 7: Visual verify in chrome-cdp**

  Restart the dev stack so the Python sync re-runs:

  ```bash
  ./scripts/cloud.sh   # or local.sh per current setup
  ```

  Navigate to the IB portfolio tab → By Structure view. The QQQ row that previously read "Combo (2 legs)" under "Other" should now show as **two separate rows** under "Single", each with its own row-level order button (commit `a7cbbbc4` per `web/CLAUDE.md`).

  Screenshots: `/tmp/combo-other-before.png` (already taken from user) → `/tmp/combo-other-after.png` (capture now). Both go in the PR.

- [ ] **Step 8: Commit**

  ```bash
  git add src/xenon/execution/ib_sync.py scripts/tests/test_collapse_positions.py
  # plus any test fixture updates from Steps 5 or 6
  git commit -m "fix(portfolio): stop collapsing unrelated singles into 'Combo (N legs)'"
  ```

---

## Phase 3 — Ship the bugfix

### Task 4: PR

- [ ] **Step 1: Push branch + open PR**

  Branch should already be a feature branch (not master).

  ```bash
  git push -u origin <branch>
  gh pr create --title "fix(portfolio): stop collapsing unrelated singles into Combo (N legs)" --body "$(cat <<'EOF'
  ## Summary
  - `collapse_positions()` was grouping any two positions sharing `(ticker, expiry)` and labeling unrecognized combinations as "Combo (N legs)" → frontend bucketed them under "Other" + lost per-leg order entry
  - Fix: when a 2-leg group's `detect_structure_type` returns the generic fall-through label, emit each leg as its own single-leg position via recursion
  - Recognized structures (vertical, straddle, synthetic, covered call, …) still collapse as today
  - Scope is intentionally narrow (`len(legs) == 2`) — 3+ leg fall-throughs are left collapsed; they more often represent intentional unrecognized multi-leg orders

  ## Failing case
  - QQQ 2026-05-22: SHORT 1× Put $585 + SHORT 1× Put $595
  - Before: one "Combo (2 legs)" row under "Other"
  - After: two separate "Short Put" rows under "Single"

  ## Known limitation
  - IB position objects don't carry "originating combo order id," so we cannot perfectly distinguish "two unrelated singles" from "two legs of one user-submitted combo whose structure we don't recognize." The `len == 2 + fall-through` heuristic catches the user's case but will also split legitimate 2-leg unrecognized combos. Future hardening: join against `order_submissions` for `order_id` lineage.

  ## Evidence
  - Before: /tmp/combo-other-before.png (from user)
  - After:  /tmp/combo-other-after.png (chrome-cdp)
  - `\d positions` constraint inspection result: <paste from Step 4>

  ## Test plan
  - [x] New regression test: `scripts/tests/test_collapse_positions.py::test_two_short_puts_same_expiry_stay_separate`
  - [x] Guardrail test: `test_recognized_combos_still_collapse` (vertical still collapses)
  - [x] Existing `scripts/tests/test_all_long_combo.py` (3-leg case) still passes (proves predicate scope is correct)
  - [x] `test_naked_short_audit.py` passes (gate-4 still ALLOW for the QQQ case)
  - [x] Postgres `positions` table constraints don't conflict with new shape
  - [x] Web vitest + typecheck pass
  - [x] Visual verify in chrome-cdp on IB tab

  ## Out of scope
  - IB/Futu code-path unification (planned Phase 4 follow-up)
  - Backlog priority tagging (separate inbox item)
  - Full `order_id`-based lineage detection (see Known limitation)
  EOF
  )"
  ```

- [ ] **Step 2: Once PR merges, drop the inbox entry**

  Remove the `2026-04-27 — Bug: combo grouping false-positive on same-expiry singles` bullet from `docs/todo-backlog.md` (don't archive — the inbox is a queue).

---

## ⛑ Human Checkpoint — before Phase 4

Phase 4 is the IB/Futu parity refactor. It is **not** required to fix the user's bug — Phase 2 alone makes the QQQ case render correctly on the IB tab. Confirm with the user before starting Phase 4:

- Does the bugfix actually unblock you on the IB tab? (Visual check.)
- Is Futu showing the same bug? (If yes, Phase 4 becomes urgent. If no, it's a refactor priority call.)
- For the refactor, two real options:
  - **Option A — Frontend-only:** Move all combo-pairing logic out of `ib_sync.py` (remove `collapse_positions`) and let the frontend run pairing for both IB and Futu via `portfolioByStructure.ts`. Pro: single code path. Con: changes the schema sent over the wire; FastAPI/frontend contract change; touches every downstream Python consumer of the collapsed shape.
  - **Option B — Backend-only:** Pull the Futu adapter through the same Python pipeline. The Python service consumes Futu positions and runs the same `collapse_positions`. Pro: backend is already the structure source-of-truth. Con: requires plumbing Futu position data through the Python path, currently the frontend handles it directly.

The picker depends on user priority. Don't pre-commit to either.

---

## Phase 4 — IB/Futu parity (deferred, conditional on checkpoint)

> Tasks 5+ to be written after the user picks Option A or B. Not pre-written here per the no-placeholder rule.

---

## Out of scope (explicit non-goals)

- **Phase 2 portfolio Postgres read-path migration** (8 readers still on `data/portfolio.json`) — separate plan; recommended next.
- **Futu→PG migration** — deferred to ~2026-05-03.
- **Backlog priority tagging** — separate inbox item from 2026-04-27.
- Refactoring `detect_structure_type` beyond the predicate addition.
- Adding new structure recognizers (e.g. detecting the QQQ case as a "Multi-Leg Short Put Strategy") — would re-introduce the bug under a different name.
