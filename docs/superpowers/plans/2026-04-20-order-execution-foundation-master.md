# Order Execution Foundation — Master Plan

> **For agentic workers:** This is an **orchestration plan**, not an implementation plan. It defines the delivery contract, sub-plan boundaries, dependencies, and go/no-go checkpoints between the two specs.
> Implementation happens via the **sub-plans** listed below. Each sub-plan is a standalone implementation plan following the writing-plans skill (TDD steps, exact file paths, commit points).
> Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to execute each sub-plan individually.

**Goal:** Deliver the Single-Leg Order Hardening spec AND the Leg-by-Leg Order Wizard spec as a single coordinated program, with shared foundation modules built once and reused.

**Architecture:** Two specs, one shared foundation. Phases F0–F7 ship the single-leg hardening and build five modules the wizard depends on (`universe.py`, `contract_normalize.py`, `preflight.py`, `quote_guard.py`, `orders.duckdb` + reconciler). Phases W1–W6 build the wizard on top. A mandatory ≥1-week live burn-in window separates the two.

**Tech Stack:** Python 3.13 (FastAPI, ib_async, DuckDB, pydantic), TypeScript (Next.js App Router, Vitest, Playwright), IB Gateway.

**Source specs:**

- `docs/superpowers/specs/2026-04-20-single-leg-hardening-design.md` (v0.2, post-tribunal)
- `docs/superpowers/specs/2026-04-20-leg-wizard-design.md` (v0.2, post-tribunal)

**Version:** v0.2 (post-tribunal). Changelog v0.1 → v0.2:
dependency graph split into "may start" vs "may release" (F3∥F4
allowed after F2); burn-in criterion rewritten from "zero naked-short
violations logged" to "zero escaped naked-short exposures reaching IB"
so the gate measures defects not prevention; §11.1 wizard cross-ref
edit changed from "replace" to "prepend pointer" so requirements are
not lost before F1 sub-plan captures them; added §"Verification plan
(explicit)" with three layers (per-phase, observability landing
sequence, program-level release verification); added master-plan
self-verification notes.

---

## Program shape

```
              ┌──────── FOUNDATION (single-leg hardening) ────────┐
              │                                                    │
  F0 ── F1 ──┬── F2 ──┬── F3 ──┬── F5 ──┬── F6                   │
   │    │    │   │    │   │    │   │    │                         │
   │    │    │   │    │   │    │   │    └──► user-facing polish   │
   │    │    │   │    │   │    │   │                              │
   │    │    │   │    │   │    │   └──────► cancel/modify trust   │
   │    │    │   │    │   │    │                                  │
   │    │    │   │    │   │    └──────────► quote truth           │
   │    │    │   │    │   │                                       │
   │    │    │   │    │   └─── F4 ────────► atomic idempotency    │
   │    │    │   │    │                                           │
   │    │    │   │    └────────────────── F7 ──► restart safety   │
   │    │    │   │                                                │
   │    │    │   └──────── preflight gate ──                      │
   │    │    │                                                    │
   │    │    └──────────── audit parity ─────                     │
   │    │                                                         │
   │    └─── contract normalize ─────                             │
   │                                                              │
   └──── universe registry ──────                                 │
                                                                   │
  ┌─────────────────── BURN-IN CHECKPOINT (≥1 week live) ────────┤
  │         No wizard work until single-leg is clean              │
  └───────────────────────────────────────────────────────────────┘
                                                                   │
              ┌──────── WIZARD (multi-leg) ────────┐              │
              │                                    │              │
       W1 ── W2 ── W3 ── W4 ── W5 ── W6                           ┘
```

## Sub-plans

Each phase has (or will have) a dedicated sub-plan at
`docs/superpowers/plans/2026-04-20-order-execution-<phase>-<name>.md`.
Sub-plans are **written at phase kickoff**, not upfront, so they reflect
the actual state of the codebase when work begins.

| Phase            | Sub-plan file                                                 | Status                                        | Spec ref          | Blocks             |
| ---------------- | ------------------------------------------------------------- | --------------------------------------------- | ----------------- | ------------------ |
| **F0**           | `f0-universe-normalize.md`                                    | **written** (see companion file)              | SL §2, §9         | F1, F2, everything |
| **F1**           | `2026-04-20-order-execution-pr-a-audit-preflight.md`          | **bundled into PR-A** (written; see sub-plan) | SL §13, Wiz §11.1 | F2, W1             |
| **F2**           | `2026-04-20-order-execution-pr-a-audit-preflight.md`          | **bundled into PR-A** (written; see sub-plan) | SL §5             | F3, F5, W1         |
| **F3**           | `2026-04-21-order-execution-pr-b-quote-tokens-idempotency.md` | **bundled into PR-B** (written; see sub-plan) | SL §7             | F5, W2             |
| **F4**           | `2026-04-21-order-execution-pr-b-quote-tokens-idempotency.md` | **bundled into PR-B** (written; see sub-plan) | SL §5.3, §12      | F5, W5             |
| **F5**           | `f5-cancel-modify-failure.md`                                 | TBD — write at F4 complete                    | SL §8             | W2                 |
| **F6**           | `f6-error-propagation-ui.md`                                  | TBD — write at F5 complete                    | SL §6, §10        | user-facing done   |
| **F7**           | `f7-rehydrate.md`                                             | TBD — write at F6 complete                    | SL §11            | W5                 |
| **BURN-IN GATE** | —                                                             | ≥1 week live with no critical regressions     | —                 | W1                 |
| **W1**           | `w1-wizard-planner.md`                                        | TBD — write at burn-in clear                  | Wiz P1            | W2                 |
| **W2**           | `w2-wizard-api.md`                                            | TBD                                           | Wiz P2            | W3                 |
| **W3**           | `w3-wizard-modal.md`                                          | TBD                                           | Wiz P3            | W4                 |
| **W4**           | `w4-wizard-protection.md`                                     | TBD                                           | Wiz P4            | W5                 |
| **W5**           | `w5-wizard-close-residual.md`                                 | TBD                                           | Wiz P5            | W6                 |
| **W6**           | `w6-wizard-mode-b.md`                                         | TBD                                           | Wiz P6            | —                  |

**Rationale for deferred sub-plans:** writing-plans requires
placeholder-free, fully-stepped TDD plans. A plan written today for W5
would be stale by the time W5 runs (code will have moved). Writing at
kickoff keeps plans accurate and avoids pre-committing to decisions
that are better made with fresh context.

**PR-A bundling (post-kickoff decision, 2026-04-21):** F1 and F2 ship as a
single coordinated PR. Both target Gate 4 parity (F1 in the post-sync audit,
F2 as a pre-submit gate) so they share fixture infrastructure and a
semantic seam. F3–F7 remain separate phases.

**PR-B bundling (post-kickoff decision, 2026-04-21):** F3 (quote tokens +
limit band) and F4 (atomic idempotency + `orders_submissions`) ship together.
F4 lands first so the DuckDB schema and `orders_events` journal are in place
when F3 writes `PREFLIGHT_ACK_LIMIT` override rows. F3 and F4 touch disjoint
modules (§dep graph above) so merge conflicts are minimal. F5–F7 remain
separate phases.

## Phase dependency graph (v0.2 — split into "may start" vs "may release")

**May-start dependencies** (when each phase can begin work):

```
F0 ──► F1 ──► F2 ──┬─► F3 ──┐
                   │         ├─► F5 ──► F6
                   └─► F4 ──┘
                   │
                   └─► F7 (needs F4 schema only)

W1 may start its PLANNING (sub-plan authoring) once F1 + F2 are stable.
W1 may START CODE behind an off-by-default feature flag once F5 is
stable AND burn-in is in progress — but W1 cannot RELEASE until
burn-in exits cleanly.
```

**May-release dependencies** (when each phase can merge to master and
ship to production):

```
F0 ► F1 ► F2 ► {F3, F4} ► F5 ► F6 ► F7 ► BURN-IN (7 days) ► W1 ► W2 ► W3 ► W4 ► W5 ► W6
```

Phases `F3` and `F4` touch disjoint modules (`quote_tokens.py` /
`quote_guard.py` vs `orders.duckdb` idempotency). They may merge in
either order once F2 is released, but both must be on master before
F5 starts. Wizard work stays gated strictly on the burn-in window
completing clean.

## Go/no-go checkpoints

After every phase, a checkpoint runs before the next phase starts.

### Per-phase checkpoint (runs after each of F0–F7, W1–W6)

Mandatory:

1. All tasks in the phase sub-plan marked complete.
2. All tests green (unit + integration + E2E per phase requirement).
3. Coverage for touched files ≥95% (project policy).
4. Manual browser verification per CLAUDE.md for UI-touching phases
   (F6, W3, W4, W5).
5. `codex-review` tribunal pass on the PR — no unresolved CRITICAL or
   IMPORTANT consensus items.
6. Phase-specific smoke test executed against paper IB (see sub-plan).

### Burn-in checkpoint (between F7 and W1)

**Entry:** F7 complete, deployed to production, user actively trading
the nine-ticker universe.

Exit criteria (ALL must hold for ≥ 7 consecutive calendar days):

- **Zero escaped naked-short exposures** reaching IB / open positions.
  Detection events in the audit log are EXPECTED (prevention working);
  what must be zero is any position that _reached_ a naked-short state
  (post-fill audit finds a violation, or a short leg fills before its
  cover). Track separately: blocked-attempt count is a **health signal**
  (nonzero is fine), not a failure.
- Zero silent cancel/modify failures (every failure visible in UI).
- Zero idempotency double-submits in `orders_events`.
- Zero rehydrate state disagreements unresolved (source-mismatch events
  investigated; each must have a recorded resolution).
- Zero IB `Error 201` from limit-price out of bounds.
- User subjective sign-off on UX of block-reason toasts.

If any criterion fails during the 7-day window, the clock resets.
Wizard work (W1) does not begin until the window completes cleanly.

**Why this window exists:** the wizard amplifies every bug in the
foundation. A flaky Gate 4 against one single-leg order becomes a
wrong-leg wizard submission; a stale quote on one order becomes a
mispriced combo. Shipping the wizard on a foundation with unknown
defects is asymmetric risk.

## Repo mechanics per phase

Every phase follows this rhythm:

1. **Kickoff**: write the sub-plan by invoking `writing-plans` with the
   relevant spec section quoted.
2. **Worktree**: `git worktree add ../xenon-<phase> -b phase/<phase>`
   for isolation. Use `superpowers:using-git-worktrees`.
3. **Execute**: `superpowers:subagent-driven-development` with the
   sub-plan (preferred) OR `superpowers:executing-plans` inline.
4. **Review gate**: `codex-review` tribunal before merge.
5. **Merge**: squash-merge the branch into `master` with the phase
   code as commit body.
6. **Checkpoint**: run the per-phase checklist above.
7. **Next phase**: repeat.

Burn-in between F7 and W1 pauses this rhythm; W1 kickoff is blocked
until exit criteria hold for 7 days.

## Scope boundary — not in this program

- Multi-broker execution (Futu stays read-only).
- Roll flow (wizard spec §15 defers).
- True auto-SL / Mode B auto-chase — wizard W6, explicitly feature-flag
  off at launch.
- Apex / R2 data pipeline changes.
- Scanner changes.
- Web UI redesigns beyond the specific widgets for new gates.

If any phase sub-plan expands scope into the above, stop and push back
against the spec instead of silently widening the plan.

## Delivery targets (rough)

No dates attached — each phase ships when complete. Historical velocity
in this repo for similar-size efforts (scripts reorg Phase 1,
trend-scanner overhaul) suggests:

- F0–F1: small (1–2 days each)
- F2, F3, F4, F7: medium (3–5 days each)
- F5, F6: small-medium (2–3 days each)
- Burn-in: 7 days minimum
- W1, W2, W4, W5: medium-large (4–7 days each)
- W3: medium (3–5 days)
- W6: small behind flags (1–2 days)

Total foundation ≈ 3 weeks engineering + 1 week burn-in.
Total wizard ≈ 4–5 weeks engineering.
Total program ≈ 8–9 weeks to W5 (W6 is an extra flagged-off phase).

## Cross-reference edits to both specs

These three edits close the loop between the two specs once this
master plan is approved:

1. `docs/superpowers/specs/2026-04-20-single-leg-hardening-design.md`:
   add a line under the status: `Part of the Order Execution Foundation
program. Sub-plans tracked in
docs/superpowers/plans/2026-04-20-order-execution-foundation-master.md`.
2. `docs/superpowers/specs/2026-04-20-leg-wizard-design.md`:
   under the status, add: `Depends on completion of the Single-Leg
Order Hardening spec (F0–F7) plus a ≥7-day live burn-in.`
3. `docs/superpowers/specs/2026-04-20-leg-wizard-design.md` §11.1:
   **prepend** (do not replace) the bullets with a pointer line:
   `Owned by phase F1 of the Order Execution Foundation master plan.
Requirements below are authoritative; F1 sub-plan will implement
them verbatim.` The existing §11.1 bullets (wizard-tag skip +
   long-option coverage check + regression) stay as the requirement
   source of truth until F1 sub-plan is written, at which point they
   migrate verbatim into the F1 sub-plan and §11.1 collapses to the
   pointer.

These three edits can be applied as a single small commit before F0
kickoff.

## Verification plan (explicit)

Three layers, each with concrete artifacts.

### Layer 1 — Per-phase verification (gate between phases)

Every phase (F0–F7, W1–W6) is verified by the per-phase checkpoint
above. In addition, each sub-plan MUST include, before it is considered
"writable":

- **Success criteria section** — 3–5 concrete assertions specific to
  the phase (e.g., F0: "`web/lib/universe.ts` regenerates from
  `universe.py` on every `npm run typecheck`; drift test passes").
- **Smoke-test recipe** — exact commands against paper IB (port 4002) that a reviewer can re-run. Examples: F2 "POST a short SPX
  call with no long cover → expect HTTP 400 with
  `INDEX_CALL_UNCOVERED`"; F5 "trigger `/orders/cancel` with IB
  Gateway stopped → expect HTTP 503 with reason `IB_CONNECTION`".
- **Rollback recipe** — the git revert command + sanity steps that
  restore the system to the prior phase's state, in case a
  post-merge regression is discovered.

Without those three, the sub-plan is incomplete and the phase cannot
start.

### Layer 2 — Observability landing sequence

Burn-in criteria depend on observability surfaces. Those surfaces land
in the foundation itself, so verification of earlier phases relies on
lighter instrumentation and burn-in verification uses the full stack.

| Observability surface                  | Available from                   |
| -------------------------------------- | -------------------------------- |
| `orders_submissions` + `orders_events` | F4 (schema) / F5 (cancel events) |
| `REHYDRATE_RECONCILED` / `_UNCERTAIN`  | F7                               |
| Reason-code toast telemetry (UI log)   | F6                               |
| Pool clientId contention metrics       | F5 (part of cancel path)         |
| Audit parity logs (Python vs TS guard) | F1                               |

Before burn-in starts (after F7 merge), an **observability
readiness check** runs:

1. Insert a synthetic `PREFLIGHT_BLOCKED` event via a test endpoint →
   confirm `orders_events` row appears with correct `detail` payload.
2. Trigger a synthetic rehydrate disagreement in a paper environment →
   confirm `REHYDRATE_UNCERTAIN` event fires and dashboard surfaces it.
3. Drop IB Gateway socket mid-cancel → confirm UI FAILED toast fires
   AND `orders_events` records the failure classification.

Only after all three pass does the 7-day burn-in clock start.

### Layer 3 — Program-level release verification

When all phase checkpoints pass AND burn-in exits clean, program
release verification runs once:

1. **Real-trade acceptance (W5)**:
   - ≥1 real multi-leg **open** using the wizard on a real IB live
     account. Required structures: at least one **long vertical**
     (simplest fixed-risk) AND one **iron condor** (two short legs,
     tests Gate 4 server-side enforcement end-to-end).
   - ≥1 real multi-leg **close** using the wizard (either full close
     or residual-BAG path exercised).
   - Each real trade's session_id is recorded with entry net, realized
     slippage, and the `SessionEvent` trail attached.
2. **Post-flight report** written to `docs/superpowers/archive/` by
   the user, covering: what went well, what broke, what should
   change before W6.
3. **Both specs archived** to `docs/superpowers/archive/specs/` with
   a note referencing the archive post-flight report.
4. **User sign-off** — explicit "program complete" message in the
   chat or a signed off note in the post-flight report.

### Master-plan self-verification

This master plan is verified by:

- **Tribunal review** on every v0.x → v0.(x+1) revision (no code,
  just the Markdown; bilateral Codex + Claude is acceptable).
- **Cross-check** during each phase kickoff: does the phase's sub-plan
  align with what the master plan promised? If not, either update the
  master plan explicitly (new version) or adjust the sub-plan — never
  let them drift silently.
- **No long-lived TBDs** in the master plan. The sub-plan status
  column is expected to show `TBD — write at phase X complete` but
  nothing else should say TBD.

## Exit condition for the whole program

Program is **complete** when:

- All 14 phase checkpoints pass.
- Burn-in checkpoint passed.
- Layer-3 program-level release verification passed (real-trade
  acceptance + post-flight report + spec archives + user sign-off).

W6 is deliberately outside the "complete" definition — it's bonus
automation behind feature flags.
