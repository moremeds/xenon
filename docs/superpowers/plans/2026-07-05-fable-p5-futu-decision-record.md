# P5 — Decision record: Futu remains read-only (FU-1)

- **Date:** 2026-07-05
- **Type:** DECISION RECORD, not an implementation plan. The only "execution" is filing this
  decision where future sessions will find it.
- **Finding IDs:** FU-1 (Info) — `docs/fable/03-findings-table.md` §3.4; roadmap Phase 5
  (`docs/fable/10-roadmap.md`).
- **Decision:** **Futu order placement is NOT built. Futu stays read-only.** Revisit only when
  ALL six conditions below hold.

---

## 1. Grounds (verified at review time, commit `4d864294`)

1. **Execution code does not exist.** `futu_client.py` performs read-only
   `OpenSecTradeContext` query calls only; there is no `futu_place_order` CLI, no unlock-trade
   flow, no Futu write path anywhere (verified by the fable review's Futu capability audit).
2. **The execution ledger is schema-locked to IB by design.**
   `CheckConstraint("broker = 'IB'")` on `order_submissions` (and the trades/wizard tables)
   deliberately blocks non-IB execution rows. This is a safety feature until a deliberate
   schema decision is made, not an oversight.
3. **Every order-path safety rail is IB-shaped:** the idempotency reservation, naked-short
   guard (3 layers), incident-history tooling, caller-allowlist CI guard, cancel/modify
   clientId semantics. None of it transfers to Futu without deliberate design.
4. **The one-operator workflow uses Futu as a custody/reporting account.** There is no
   trading need pulling execution to Futu; the read mirror (positions snapshot, nightly deal
   sync) already serves the actual use.

## 2. The six preconditions for revisiting (ALL must hold)

| #   | Condition                                                                                                                                          | How to check it holds                                                                                      |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| 1   | Option A hardening complete and soaked: S1–S6 merged + P2.2 (state chokepoint + CHECK) merged, no order-path incident for ≥1 month after           | `git log --oneline` shows the S1–S6 + P2.2 PRs MERGED (not merely review-cycled in the index); ≥1 month elapsed since the last of those merge dates; `docs/reference/order-path-incident-history.md` has no new rows dated after that merge |
| 2   | Broker capability table exists and drives the 403s (`docs/fable/11-code-sketches.md` §2 / 04 §4.3-1)                                               | the `/orders/place` route resolves its broker 403 THROUGH the capability table (read the route body — at authoring HEAD it is still an inline `broker != "IB"` hard-403, which does NOT satisfy this); a route test proves a non-IB broker is rejected via the table                                                                 |
| 3   | Schema decision executed deliberately: either relax the `broker='IB'` CHECKs with migration + tests, or a parallel `futu_order_submissions` ledger | migration exists + tests reference it                                                                      |
| 4   | `unlock_trade` flow designed with the same secrets discipline as IB creds; Futu paper (`trd_env=SIMULATE`) test loop established                   | a design doc + a passing simulate-env test exist                                                           |
| 5   | Futu-aware naked-short/coverage guard implemented from the P2.5 shared parity fixture table                                                        | fixture file has Futu cases + a Futu guard test lane                                                       |
| 6   | Caller-allowlist guard + incident-history discipline extended to the Futu CLI                                                                      | `order_path_caller_allowlist.py` covers the new CLI                                                        |

## 3. What this decision protects

- The `broker='IB'` CHECK constraints must **not** be relaxed by any other plan "in passing"
  (e.g. P2.2's state CHECK migration touches the same table — it must leave the broker CHECK
  alone).
- No plan may add Futu write calls (`place_order`, `unlock_trade`, `modify_order`,
  `cancel_order` on `OpenSecTradeContext`) to `futu_client.py` without a plan that satisfies
  §2 first.
- The Futu positions path keeps its deliberate JSON-cache exception (fable 04 §4.2) — it is
  not "legacy debt to migrate"; it is the accepted design for a read-only secondary broker.

## 4. Standing tripwire for future sessions

If a future task asks for "place an order via Futu" or "make Futu tradable": STOP, read this
record and `docs/fable/10-roadmap.md` Phase 5, and confirm with the operator that all six
§2 conditions hold before writing any code. Risk class: high — treat as a project, not a PR.

## 5. Verification (for the executor filing this record)

This plan's execution = ensuring discoverability:

1. This file exists (it does — you are reading it).
2. Add one line to `docs/todo-backlog.md` **Inbox**: `2026-07-05 — Futu execution decision
record: remains read-only; six preconditions in docs/superpowers/plans/2026-07-05-fable-p5-futu-decision-record.md`.
3. `docs/todo-backlog.md` already contains an older deferred "Futu order integration" item
   (search for it). Do NOT delete it; add a `**Notes:**` sub-bullet under it pointing here:
   `superseded by the Futu read-only decision record (this plan) — six preconditions gate
   any revisit`. Without this, a future planning session is more likely to find the old
   backlog item than this record.
4. No code changes. The only modified/added files are this plan file and
   `docs/todo-backlog.md` (`git status --short` may show other untracked fable docs — that
   is fine; the tripwire is any diff under `src/` or `web/` → STOP).
