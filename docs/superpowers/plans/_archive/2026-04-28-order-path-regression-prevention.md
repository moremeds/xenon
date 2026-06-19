# Order Path Regression Prevention — Design Doc

**Date:** 2026-04-28
**Status:** Design / proposal — not scheduled. Promote individual layers to
`docs/plans/YYYY-MM-DD-<slug>-IMPL.md` when picking one up.
**Trigger:** PR #61 (`fix/order-placement-reliability`, merged 2026-04-28
12:57 UTC, commit `15be00e9`) shipped five distinct order-path bug fixes
in one branch. The PR description explicitly notes _"No live or paper order
smoke test was run after the final fixes."_ The goal of this doc is to
prevent the same five regression classes from re-emerging.

Related plan: `docs/plans/2026-04-27-order-placement-reliability.md`
(implementation that PR #61 executed).

---

## Why this matters

Order placement is the single most consequential code path in xenon — a
silent regression here can submit a real order against a real account.
Past pattern shows the same shapes recur:

- `[Postgres read-side gap]` memory — silent JSON file fallbacks survived
  three migrations (vcg, portfolio, orders).
- `[In-process route bypass]` memory — FastAPI `Depends` only fires on
  HTTP entry; in-process handler calls (`_orders_X_from_body`,
  `submit_combo`) skip every guard. Has caused regressions twice.
- `[Live E2E surfaces contract bugs]` memory — mocked-boundary unit tests
  - AI tribunal review both passed for a subprocess stdout/exit-code
    contract break that live `curl` caught immediately.
- `[PR #34 reverted]` memory — quote_token integration shipped + reverted;
  the F3 regression worked-around-by-removal, not fixed.
- `[Universal auth gating]` memory — sensitive-page gating must be applied
  uniformly; per-flow gates have caused 2 regressions (#34, #47).

These are structural patterns, not isolated bugs. Each requires a
structural prevention.

## The five regression classes

| #   | Class                                            | Example in PR #61                                                                                                 | Cheapest detection                              |
| --- | ------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------- | ----------------------------------------------- |
| A   | Silent file fallback after a Postgres migration  | `data/portfolio.json`, `data/orders.json` reads in order route + `_load_portfolio_view`                           | Pre-commit AST grep                             |
| B   | In-process bypass of a route-level guard         | Combo Gate-4 was only in web route; in-process callers skipped it                                                 | Allowlist test on importers of `ib_place_order` |
| C   | Reason-code overloading                          | `STALE_QUOTE` was used for 4 distinct conditions (closed market, contract mismatch, crossed quote, expired token) | Uniqueness test on `ReasonCode` enum            |
| D   | Required wire-protocol field treated as optional | `client_attempt_id` was `?:`, caused idempotency drift on retries                                                 | Contract test per order entrypoint              |
| E   | Live broker contract drift never tested          | "No paper order smoke test was run" — same pattern as past breaks                                                 | Real-socket paper smoke                         |

Class E is the only one that catches **unknown** regression shapes. The
other four catch known shapes. Investment in E pays the most per hour.

## Layered automation strategy

Move each guard one layer left of where the bug currently escapes.
Cheapest layer first.

### Layer 1 — Edit time (Claude Code hook)

**Surface:** `.claude/settings.json` PreToolUse hook on `Edit`/`Write`.

**Match paths:** `src/xenon/execution/`, `src/xenon/api/server.py`,
`web/app/api/orders/`, `web/lib/order/`.

**Action:** Inject reminder before tool runs:

> This file is on the order path. Required: (a) no new `data/*.json` reads,
> (b) any new entrypoint must route through `_run_preflight`,
> (c) `client_attempt_id` is required, (d) reason codes must be unique.

Catches ~80% of model-amnesia accidents. Does not catch deliberate or
non-Claude edits. Effort: ~30 min.

### Layer 2 — Commit time (pre-commit hooks)

**Surface:** `.pre-commit-config.yaml` or `.git/hooks/pre-commit`.

**Checks:**

1. No silent JSON fallback on order-path or migrated-surface routes:

   ```
   grep -rn "readDataFile\|readFile.*data/.*\.json\|json\.load.*data/" \
     web/app/api/ src/xenon/api/routes/
   ```

   Fail if any match outside an allowlist (legacy/backfill paths only).

2. No direct `ib_place_order` import outside allowlist:
   ```
   grep -rn "from xenon.execution.ib_place_order import\|from xenon.execution import ib_place_order" \
     --include="*.py"
   ```
   Allowlist today: `src/xenon/api/server.py`,
   `src/xenon/execution/ib_order_manage.py`.

Catches A and B. Cheap, deterministic, runs on every `git commit`.
Effort: ~1 hour.

### Layer 3 — PR time (CI path-filtered job)

**Surface:** `.github/workflows/ci.yml`. New job, runs only when paths
under `src/xenon/execution/`, `src/xenon/api/`, `web/app/api/orders/`,
`web/lib/order/` change.

**Tests:**

1. **Reason-code uniqueness.** Each `ReasonCode` enum value appears in
   exactly one Verdict construction site, or is on a documented exception
   list. Catches C.
2. **Required-field contract.** Every order entrypoint rejects missing
   `client_attempt_id` with `INVALID_ORDER_BODY` 400. Catches D.
3. **Allowlist drift.** Re-runs the Layer 2 grep checks in CI in case a
   pre-commit hook was bypassed with `--no-verify`.

Path-filtered so unrelated PRs don't pay the cost. Effort: ~half day.

### Layer 4 — Pre-merge live smoke (most important layer)

**Surface:** `.github/workflows/live-order-smoke.yml`. Runs a real
`place → cancel` round-trip on a paper account through the full UI →
FastAPI → IB Gateway stack.

**Two implementation options:**

- **A. Self-hosted runner + Tailscale to VPS IB Gateway (paper).**
  Auto-trigger on PRs touching order-path files. Requires runner uptime
  ~15 min/PR. Higher cost, lower friction.
- **B. PR-label trigger.** Reviewer adds `live-smoke` label;
  `gh workflow run` fires the same job. Lower runner cost, requires
  discipline. Recommended starting point.

This is the only layer that catches **unknown regression shapes**. Per
the `[Live E2E surfaces contract bugs]` memory: mocked tests + AI review
have failed at this exact gate before. Effort: ~1 day for B, ~2 days for A.

### Layer 5 — Nightly safety net

**Surface:** `.github/workflows/nightly.yml` (already exists).

**Action:** Extend with one additional Playwright test — place + cancel
a 1-share paper TSLA order through the full stack. Failure auto-comments
on the existing tracking issue (workflow already does this).

Catches drift between PRs and any flake-prone failure that Layer 4
batches missed. Effort: ~2 hours.

### Layer 6 — Auto LLM review on order-path PRs

**Surface:** GitHub Action that auto-comments `/codex-review` on any PR
whose diff touches order-path files. Uses existing `codex-review` skill.

Free, async, catches the "looks-correct-but-mocks-wrong" class that
human reviewers also miss when the diff is large. Effort: ~1 hour.

## Recommended build order

| Step | Layer                                  | Effort    | Catches                            | Why this order                                                              |
| ---- | -------------------------------------- | --------- | ---------------------------------- | --------------------------------------------------------------------------- |
| 1    | **L4 (Option B — labeled live smoke)** | ~1 day    | unknown regressions                | Highest leverage per hour. Only layer catching what mocks miss.             |
| 2    | **L2 (pre-commit AST guards)**         | ~1 hour   | A + B (known patterns from memory) | Lowest effort, locks in two patterns that have already burned twice.        |
| 3    | **L1 (edit-time reminder)**            | ~30 min   | model amnesia                      | Free, just config.                                                          |
| 4    | **L3 (CI structural tests)**           | ~half day | C + D                              | Less urgent — PR #61 already required `client_attempt_id` on the main path. |
| 5    | **L5 (extend nightly)**                | ~2 hours  | inter-PR drift                     | Safety net under the safety net.                                            |
| 6    | **L6 (auto codex review)**             | ~1 hour   | review-bypass                      | Cheap incremental.                                                          |

**Total effort:** ~3 person-days for full coverage. Layers 1–2 alone
(~2 hours) lock in both regression patterns the memory has burned on.

## Tradeoffs

- More layers = more friction in the inner loop = more flake surface.
- Live brokers go down. Self-hosted runners hiccup. Tailscale routes flake.
- Bias: for an order path, false-positives that cost 2 minutes to retry
  are _cheaper_ than a single naked-short order reaching IB.
- Provide fast bypass mechanisms for genuine emergencies:
  `--no-verify` on commit hooks, `skip-smoke` PR label.

## Out of scope (for now)

- Replacing `data/portfolio.json` and `data/orders.json` with Postgres
  reads on the _write_ path — already done by PR #52 + #61.
- Removing the legacy backfill JSON files entirely — covered by the
  separate Postgres-migration completion plan.
- Order-router rate limiting / circuit breakers — different failure mode.

## Open questions

- Self-hosted runner: who owns uptime? VPS or workstation? Cost vs.
  reliability.
- Should Layer 4 run pre-merge (blocking) or post-merge (auto-revert on
  failure)? Post-merge is faster but dirtier.
- Layer 6: should codex review _block_ merge or just comment? Probably
  comment-only to avoid LLM-flake noise.
