# P2.8 — Doc Repair: order-stack-end-to-end.md staleness, 403→400 claims, README CLI table

- **Date:** 2026-07-05
- **Branch:** `docs/fable-p2-8-doc-repair`
- **Finding IDs:** CX-4 (Medium, Confirmed) — `docs/fable/03-findings-table.md` row CX-4
- **Severity:** Medium
- **Goal (one line):** Bring three stale docs into agreement with HEAD — mark the removed regime gate as removed, mark TWS-cancel mirroring as shipped, correct the naked-short HTTP status from 403 to 400 in the three docs that claim it, and regenerate the README CLI table (deleting the non-existent `xenon-position-rules` CLI) — **docs-only, zero code changes.**

---

## 1. Context (what exists today, verified against HEAD)

HEAD at planning time: `fb5b6d8133425d066148cde125c8da3c753227df` (the fable review was done at `4d864294`; several relevant fixes landed after it).

This is a **documentation-only** repair. The executor does **not** need to understand the order stack, the regime gate, IB internals, or the naked-short algorithm. Every edit below is a text substitution in a Markdown file (plus one HTML banner and one README table). You will not touch any `.py`, `.ts`, `.tsx`, `.sql`, or config file. The final `git diff` must be limited to `docs/`, `README.md`, root `CLAUDE.md`, and `src/xenon/CLAUDE.md` (the two CLAUDE.md files carry 403-claim corrections — see V8) — this is a hard tripwire (see §7).

**Verified ground truth (each fact re-checked against the working tree — do not re-derive, just trust these):**

1. **The server-side regime gate is GONE.** These files do **not** exist at HEAD:
   - `src/xenon/api/services/regime_gate.py` — absent
   - `src/xenon/api/services/regime_state.py` — absent
   - `src/xenon/execution/regime_gate.py` — absent
     `src/xenon/api/server.py` has **no** `_run_regime_gate` call site. It carries this comment at the place-order path (verified `server.py:2162-2166`):

   ```
   # RegimeGate (VCG-CRI signal layer) was removed in the pure-portfolio
   # pivot (#104). Order entry no longer gates on regime tiers; cover_ratio
   # reverts to its NORMAL/no-gate default and there is no override audit.
   ```

   Removal chronology: **#104** (`cc568c3`, pure-portfolio pivot) deleted the modules; the dead call sites were ripped out on **2026-06-15** (`fix/order-path-regime-gate-deadcode`) — this is **incident-history row 22** in `docs/reference/order-path-incident-history.md`.

2. **Frontend regime artifacts still physically exist but are dead code.** Verified:
   - `web/components/RegimeBlockModal.tsx` (490 bytes) — its own docstring says _"Stub — the regime gate was removed in the pure-portfolio pivot… Kept as a null-rendering shim."_ It returns `null`.
   - `web/lib/order/regimeGate.ts` (3742 bytes) — parser still present, still imported by `web/components/ticker-detail/OrderTab.tsx`, but the backend never returns `409 REGIME_BLOCK` / `422 REGIME_RESIZE_REQUIRED` anymore, so the parse branches are unreachable in production.
   - `xenon.regime_overrides` table still exists in `src/xenon/db/schema.py:788` — a **dormant orphan table**; nothing writes it at HEAD.

3. **TWS-cancel mirroring SHIPPED** (was the doc's "longest-standing open gap"). `sweep_disappeared_orders` in `src/xenon/api/services/ib_activity_mirror.py` transitions vanished `WORKING`/`PARTIALLY_FILLED` rows to `FILLED` or `CANCELLED (reason_code=TWS_CANCEL_MIRROR)`. This is **incident-history row 20** (2026-06-13, `fix/order-system-repair`); tests `scripts/tests/test_tws_cancel_sweep.py` (5 cases). Documented live in `src/xenon/api/CLAUDE.md` § "TWS cancel mirroring".

4. **Naked-short violation returns HTTP 400, not 403.** Server-side Gate 4 (`_run_preflight`) failure is mapped in `src/xenon/api/server.py::_orders_place_from_body` (verified `server.py:2170-2182`):

   ```python
   verdict = await _run_preflight(body, cover_ratio=cover_ratio_for_preflight)
   if not verdict.accept:
       code = verdict.reason_code.value if verdict.reason_code else None
       return JSONResponse(
           status_code=400,          # ← 400, not 403
           content={"detail": ..., "reason_code": code, "reason_detail": ...},
       )
   ```

   The Next.js route `web/app/api/orders/place/route.ts` has **no independent naked-short guard** — it forwards FastAPI's status via `xenonFetch`. The only 403s on the place path are `READ_ONLY_BROKER` (non-IB broker) and `READ_ONLY_MODE` (`XENON_READ_ONLY=1`), which are **correct** and must be left alone.

5. **`xenon-position-rules` CLI does not exist.** `pyproject.toml [project.scripts]` (verified) has 26 entries; none is `xenon-position-rules`. Position-close rules are served by the FastAPI router `src/xenon/api/routes/position_rules.py` and the `xenon-monitor-daemon`, not a standalone CLI. The phantom CLI is documented only in `src/xenon/CLAUDE.md`'s Commands table.

6. **README CLI table is incomplete.** README (`README.md:122-130`) omits 6 real scripts: `xenon-ib-market-depth`, `xenon-ib-option-greeks`, `xenon-futu-history-sync`, `xenon-futu-statement-sync`, `xenon-nav-flex-refresh`, `xenon-nav-reconcile`.

7. **No markdown linter in the repo** (verified: no `.markdownlint*`, no `remark-lint` / `markdownlint` in `package.json` or CI). So there is no lint gate to run for these Markdown edits.

---

## 2. Drift from review

The fable finding is **accurate but understates two things**, both confirmed above and folded into this plan:

- **The regime removal is deeper than "documented as live."** The server modules are physically deleted (not just dormant), AND there is a _second_ removal event (row 22, dead-code rip-out 2026-06-15) after the #104 deletion. The frontend files (`regimeGate.ts`, `RegimeBlockModal.tsx`) still exist as **orphaned/stub dead code** — so the doc's frontend claims are half-true (files exist) but functionally dead (backend never triggers them). The plan says "removed server-side; frontend orphaned," not a blanket "deleted."
- **The `.html` companion (`order-stack-end-to-end.html`, tracked, 32.9KB) is equally stale** (31 regime/403 mentions, "verified 2026-05-02"). The finding lists only the `.md`. The plan adds a single stale banner to the `.html` and defers full HTML re-rendering (decision + rationale in §3, Edit H1).

No fact in the finding was contradicted. Nothing is already-fixed that the finding claims is broken.

---

## 3. Goal / Non-goals

### Goal

Make these files agree with HEAD:

- `docs/architecture/order-stack-end-to-end.md` — stale-banner + verified header + targeted section rewrites (regime removed, TWS-cancel shipped).
- `docs/architecture/order-stack-end-to-end.html` — single stale banner only.
- `README.md` — 403→400 fix + regenerated CLI table.
- `CLAUDE.md` (root) — 403→400 fix.
- `src/xenon/CLAUDE.md` — 403→400 fix + delete phantom `xenon-position-rules` row.

### Design decisions (each picked, one-line rationale — no options left open)

- **Do NOT chase all 79 regime mentions line-by-line in the `.md`.** Instead: one prominent **stale/verified banner** at the top (authoritative catch-all for threaded prose mentions) **+** targeted full-section rewrites of the highest-impact standalone falsehoods (§6, §5.3, §10.4, §12.2, §14, TL;DR). _Rationale:_ 79 surgical edits threaded through 1036 lines is high-risk for the executor and low-value; a banner + the 6 section rewrites removes every actively-misleading claim a reader would act on.
- **Do NOT renumber the 129 `:NNNN` line-number citations.** Add a one-line disclaimer in the verified header that bare line numbers are indicative and may have drifted; anchor future edits to function names. _Rationale:_ re-verifying and rewriting 129 citations by hand is infeasible and error-prone; the disclaimer neutralizes the harm without the risk.
- **`.html`: banner only, defer full re-render.** _Rationale:_ the `.html` is a hand-authored rendered companion with Mermaid; keeping it byte-synced to a heavily-edited `.md` by hand would double the diff and the risk. A banner pointing to the corrected `.md` is the honest, bounded fix.

### Non-goals (explicitly NOT done here — one change, one PR)

- **No code changes.** Not deleting the orphaned `regimeGate.ts` / `RegimeBlockModal.tsx` / `regime_overrides` table — those are code cleanups tracked elsewhere, out of scope for a docs PR.
- **Not** touching the `READ_ONLY_BROKER` / `READ_ONLY_MODE` 403 references (they are correct).
- **Not** re-rendering the `.html` body.
- **Not** editing `src/xenon/api/CLAUDE.md` (its `position_rules.py` / `position_rules_cancel.py` references are real module names, not the phantom CLI) or `production-database-strategy.md` (its 403 is `READ_ONLY_MODE`, correct).
- **Not** reflecting any not-yet-merged fable plan (see §6 tripwire).

---

## 4. Key facts (verified — the executor relies only on these)

| Fact                           | Value                                                                                                | Verified location                                         |
| ------------------------------ | ---------------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| HEAD                           | `fb5b6d81`                                                                                           | `git rev-parse HEAD`                                      |
| Regime server modules          | absent                                                                                               | `ls src/xenon/api/services/regime_gate.py` → No such file |
| Regime removal PRs             | #104 (`cc568c3`) + row 22 (2026-06-15)                                                               | `server.py:2162`, incident-history row 22                 |
| TWS-cancel sweep               | shipped, `sweep_disappeared_orders`                                                                  | incident-history row 20, `api/CLAUDE.md`                  |
| Naked-short HTTP status        | **400**                                                                                              | `server.py:2176` (`status_code=400`)                      |
| `xenon-position-rules`         | does NOT exist                                                                                       | `pyproject.toml [project.scripts]`                        |
| Missing README CLIs            | market-depth, option-greeks, futu-history-sync, futu-statement-sync, nav-flex-refresh, nav-reconcile | `pyproject.toml:48-73` vs `README.md:122-130`             |
| Full `[project.scripts]` count | 26                                                                                                   | `pyproject.toml:48-73`                                    |
| MD linter                      | none                                                                                                 | no config / CI step                                       |

Full verified `[project.scripts]` list (26): `xenon-api`, `xenon-blotter`, `xenon-blotter-history`, `xenon-market-hours`, `xenon-presets`, `xenon-monitor-daemon`, `xenon-preset-rebalance`, `xenon-ib-execute`, `xenon-ib-place-order`, `xenon-ib-order-manage`, `xenon-ib-orders`, `xenon-ib-option-chain`, `xenon-ib-market-depth`, `xenon-ib-option-greeks`, `xenon-ib-reconcile`, `xenon-ib-sync`, `xenon-naked-short-audit`, `xenon-futu-sync`, `xenon-futu-history-sync`, `xenon-futu-statement-sync`, `xenon-portfolio-report`, `xenon-portfolio-attrib`, `xenon-portfolio-perf`, `xenon-perf-explainer`, `xenon-nav-flex-refresh`, `xenon-nav-reconcile`.

---

## 5. Steps (strictly ordered; each is an exact text substitution)

> For every `Edit`, the `old_string` is copied verbatim from HEAD (unique in its file). If any `old_string` is **not found** or **not unique**, STOP and report — the file drifted (see §7).

### Step 0 — Branch

```bash
cd /Users/chenxi/projects/xenon
git checkout -b docs/fable-p2-8-doc-repair
git rev-parse HEAD   # must print fb5b6d81...; if different, see §6 tripwire
```

---

### Step 1 — `README.md` 403→400 (Edit R1)

**File:** `README.md`
**old_string:**

```
2. **API gate** — `POST /api/orders/place` returns 403 on violation
```

**new_string:**

```
2. **API gate** — `POST /api/orders/place` returns 400 on violation
```

### Step 2 — `CLAUDE.md` (root) 403→400 (Edit C1)

**File:** `CLAUDE.md`
**old_string:**

```
UI pre-submission (`web/lib/nakedShortGuard.ts`), API gate (`/api/orders/place` returns 403), and post-sync audit (`naked_short_audit.py` cancels violators after every `ib_sync`).
```

**new_string:**

```
UI pre-submission (`web/lib/nakedShortGuard.ts`), API gate (`/api/orders/place` returns 400), and post-sync audit (`naked_short_audit.py` cancels violators after every `ib_sync`).
```

### Step 3 — `src/xenon/CLAUDE.md` 403→400 (Edit X1)

**File:** `src/xenon/CLAUDE.md`
**old_string:**

```
2. **API gate** — `orders/place/route.ts` returns 403 if guard fails
```

**new_string:**

```
2. **API gate** — server-side Gate 4 (`_orders_place_from_body` in `api/server.py`) returns **HTTP 400** with `reason_code` on violation; `orders/place/route.ts` forwards that status (it has no independent guard)
```

### Step 4 — `src/xenon/CLAUDE.md` delete phantom `xenon-position-rules` CLI row (Edit X2)

**File:** `src/xenon/CLAUDE.md`
**old_string:**

```
| `xenon-monitor-daemon`    | Position-rules + fill monitor + preset rebalance daemon               |
| `xenon-position-rules`    | Position-close rule CLI                                               |
| `xenon-preset-rebalance`  | Preset rebalance handler                                              |
```

**new_string:**

```
| `xenon-monitor-daemon`    | Position-rules + fill monitor + preset rebalance daemon               |
| `xenon-preset-rebalance`  | Preset rebalance handler                                              |
```

(Position-close rules are served by the FastAPI router `api/routes/position_rules.py` + the monitor daemon — there is no `xenon-position-rules` entry in `pyproject.toml [project.scripts]`.)

---

### Step 5 — `README.md` regenerate CLI table (Edit R2)

**File:** `README.md`
**old_string** (the whole current table, verbatim — `README.md:122-130`):

```
| **Server**           | `xenon-api` (FastAPI bridge)                                                                                        |
| **Orders**           | `xenon-ib-place-order` · `xenon-ib-order-manage` · `xenon-ib-orders` · `xenon-ib-execute` · `xenon-ib-option-chain` |
| **Sync / reconcile** | `xenon-ib-sync` · `xenon-ib-reconcile` · `xenon-futu-sync`                                                          |
| **Risk / audit**     | `xenon-naked-short-audit`                                                                                           |
| **Reports**          | `xenon-portfolio-report` · `xenon-portfolio-perf` · `xenon-portfolio-attrib` · `xenon-perf-explainer`               |
| **Trade log**        | `xenon-blotter` · `xenon-blotter-history`                                                                           |
| **Daemons**          | `xenon-monitor-daemon` · `xenon-preset-rebalance`                                                                   |
| **Utilities**        | `xenon-market-hours` · `xenon-presets`                                                                              |
```

**new_string** (adds the 6 missing CLIs into the right rows; one-liners derived from each module's docstring, verified in §context):

```
| **Server**             | `xenon-api` (FastAPI bridge)                                                                                             |
| **Orders**             | `xenon-ib-place-order` · `xenon-ib-order-manage` · `xenon-ib-orders` · `xenon-ib-execute` · `xenon-ib-option-chain`      |
| **Market data (IB)**   | `xenon-ib-market-depth` (L2 snapshot) · `xenon-ib-option-greeks` (broker modelGreeks)                                   |
| **Sync / reconcile**   | `xenon-ib-sync` · `xenon-ib-reconcile` · `xenon-futu-sync` · `xenon-futu-history-sync` · `xenon-futu-statement-sync`     |
| **NAV jobs**           | `xenon-nav-flex-refresh` (daily Flex NAV) · `xenon-nav-reconcile` (intraday-vs-close NAV audit)                          |
| **Risk / audit**       | `xenon-naked-short-audit`                                                                                                |
| **Reports**            | `xenon-portfolio-report` · `xenon-portfolio-perf` · `xenon-portfolio-attrib` · `xenon-perf-explainer`                    |
| **Trade log**          | `xenon-blotter` · `xenon-blotter-history`                                                                                |
| **Daemons**            | `xenon-monitor-daemon` · `xenon-preset-rebalance`                                                                        |
| **Utilities**          | `xenon-market-hours` · `xenon-presets`                                                                                   |
```

> After this edit, the README table lists all 26 scripts. Cross-check with Step V4 below.

---

### Step 6 — `order-stack-end-to-end.md` verified/stale banner (Edit D1)

**File:** `docs/architecture/order-stack-end-to-end.md`
**old_string:**

```
**Companion file:** `order-stack-end-to-end.html` renders the same content with Mermaid sequence/state diagrams. Open via `open docs/architecture/order-stack-end-to-end.html`.
```

**new_string:**

```
**Companion file:** `order-stack-end-to-end.html` renders the same content with Mermaid sequence/state diagrams. Open via `open docs/architecture/order-stack-end-to-end.html`.

> **⚠️ Verified as of 2026-07-05 (commit `fb5b6d81`).** Two subsystems described in the original 2026-05-02 draft have since changed; this doc has been reconciled at the section level:
> - **The regime gate (VCG-CRI) was REMOVED server-side** (#104 `cc568c3`, dead-code rip-out 2026-06-15 — see `order-path-incident-history.md` row 22). All "Regime Gate" / "regime tier" / "RegimeState" / "override" narrative below is **historical, not live** unless a section is explicitly re-marked. The `regime_overrides` Postgres table and the frontend `regimeGate.ts` / `RegimeBlockModal.tsx` files still exist as **dormant/orphaned dead code**; the backend never triggers them.
> - **TWS-cancel mirroring SHIPPED** (`sweep_disappeared_orders`, `order-path-incident-history.md` row 20) — it is no longer an open gap.
>
> **Line-number citations (`file.py:NNNN`) are indicative and may have drifted** from this commit; anchor any edit to the named function/symbol, not the bare line number.
```

### Step 7 — `order-stack-end-to-end.md` TL;DR: drop the shipped TWS-cancel gap (Edit D2)

**File:** `docs/architecture/order-stack-end-to-end.md`
**old_string:**

```
, (6) the late-arriving CommissionReport coupling, and (7) the TWS-cancel-not-mirrored gap in the activity poller.
```

**new_string:**

```
, and (6) the late-arriving CommissionReport coupling. *(The TWS-cancel-not-mirrored gap noted in the original draft has since shipped — see §10.4.)*
```

### Step 8 — `order-stack-end-to-end.md` §5.3 mark frontend regime dead (Edit D3)

**File:** `docs/architecture/order-stack-end-to-end.md`
Replace the whole `### 5.3` section. **old_string** = lines from the `### 5.3 Regime block / resize UX` heading through the `---` that closes it (verbatim, unique):

````
### 5.3 Regime block / resize UX

When the place fetch returns 409/422, `parseRegimeGateResponse` (`web/lib/order/regimeGate.ts:56`) clones the response and reads body:

```typescript
if (res.status === 409 && reasonCode === "REGIME_BLOCK") {
  return { kind: "block", payload: obj as RegimeBlockResponse };
}
if (res.status === 422 && reasonCode === "REGIME_RESIZE_REQUIRED") {
  return { kind: "resize", payload: obj as RegimeResizeResponse };
}
````

`OrderTab` opens `RegimeBlockModal` with the parsed payload. When the user submits an override reason ≥ 10 chars (validated by `isRegimeOverrideReasonValid` at `regime_gate.ts:89`), `handleRegimeOverride` calls `retryRegimeOrder` with `{...originalBody, ...buildRegimeOverrideFields(reason)}`. **The `client_attempt_id` is reused** — the override path proves intent on the _same_ idempotency slot, so a second-attempt regime block doesn't create a parallel WORKING row.

For resize: `suggestResizeQuantity(payload, currentQuantity)` (`regime_gate.ts:105`) computes the largest quantity that stays under the cap by linear scaling (`floor(currentQty × cap / max_loss)`). The user accepts/edits this in the modal, and `handleRegimeResize` re-POSTs with the trimmed quantity (also reusing `client_attempt_id`).

**Implication:** the same `client_attempt_id` may run through Stage 4 multiple times before reaching Stage 7. This is intentional — Stage 4 is a pure decision; Stage 7 is the atomic commit. But this also means `_run_regime_gate` must be idempotent, which it is (no side effects until `reserve_attempt`).

---

```
**new_string:**
```

### 5.3 Regime block / resize UX — REMOVED (historical)

> **Removed server-side (#104 + 2026-06-15 dead-code rip-out).** The backend no longer returns `409 REGIME_BLOCK` / `422 REGIME_RESIZE_REQUIRED`, so this UX is unreachable. `web/lib/order/regimeGate.ts` (`parseRegimeGateResponse`, `suggestResizeQuantity`, `buildRegimeOverrideFields`) still exists and is still imported by `OrderTab.tsx`, but its parse branches never fire. `web/components/RegimeBlockModal.tsx` is now a **null-rendering stub** (see its own docstring). Both are orphaned dead code retained so `OrderTab.tsx` compiles without surgery.

---

```

### Step 9 — `order-stack-end-to-end.md` §6 collapse the Regime Gate Detail (Edit D4)

**File:** `docs/architecture/order-stack-end-to-end.md`
Replace the **entire** `## 6. Regime Gate Detail` section (from its `## 6.` heading up to — but NOT including — the `## 7. Quote Gate Detail` heading). This is a large block (≈ heading `## 6. Regime Gate Detail` through the `---` immediately before `## 7.`). **Anchor:** delete everything between and including the line `## 6. Regime Gate Detail` and the closing `---` that precedes `## 7. Quote Gate Detail (and the STALE_QUOTE Mystery)`. Because the block contains long code fences, use the following procedure instead of one giant `old_string` (dumbproof, avoids a 75-line verbatim match):

1. Open the file. Find the line `## 6. Regime Gate Detail` (currently ~line 466).
2. Find the next top-level heading `## 7. Quote Gate Detail (and the STALE_QUOTE Mystery)` (currently ~line 541).
3. Replace all lines from `## 6. Regime Gate Detail` up to (exclusive of) `## 7. Quote Gate Detail...`, including the blank line and `---` that sit between them, with exactly:
```

## 6. Regime Gate Detail — REMOVED (historical)

> **This entire subsystem was removed.** #104 (`cc568c3`, pure-portfolio pivot) deleted `api/services/regime_gate.py` + `regime_state.py`; the orphaned call sites (`_run_regime_gate`, `_run_modify_regime_gate`, tier ladder, hedge classification, risk-reducing-exit bypass, override completion) were ripped out on 2026-06-15 (`fix/order-path-regime-gate-deadcode` — `order-path-incident-history.md` row 22). Order entry no longer gates on regime tiers: `cover_ratio` reverts to its `1.0` default and there is no override audit. **Naked-short / preflight Gate 4 is untouched** and remains the live risk gate (see §12.1). The `regime_overrides` table still exists in `schema.py` but is a dormant orphan — nothing writes it.
>
> The original §6.1–§6.5 (tier ladder, hedge classification, risk-reducing-exit bypass, modify gate, override completion) described the deleted logic and has been removed to prevent an agent from coding against it. If you need the historical design, read the diff of `fix/order-path-regime-gate-deadcode`.

---

```
> **Verification for this step:** after the edit, `grep -n "^## " docs/architecture/order-stack-end-to-end.md` must still show `## 6. Regime Gate Detail — REMOVED (historical)` immediately followed by `## 7. Quote Gate Detail (and the STALE_QUOTE Mystery)`. The old sub-headings `### 6.1 Tier ladder`, `### 6.2 Hedge classification`, `### 6.3`, `### 6.4`, `### 6.5` must be **gone** (grep returns nothing for `### 6.1`).

### Step 10 — `order-stack-end-to-end.md` §10.4 TWS-cancel gap → shipped (Edit D5)

**File:** `docs/architecture/order-stack-end-to-end.md`
Replace the whole `### 10.4` section. **old_string** = heading through the last bold sentence (verbatim, unique):
```

### 10.4 The TWS-cancel mirroring gap

If the user cancels an order in **TWS** (not the Xenon UI), the backend is blind. The activity poller (`ib_activity_mirror`) calls `sync_open_orders_to_postgres` which inserts new orders found via `reqAllOpenOrders` and updates drifted rows (`register_from_snapshot`) — but it does **not** transition `WORKING` → `CANCELLED` for orders that disappeared from `get_open_orders()`. Reason (per `src/xenon/api/CLAUDE.md` line 44): naive disappearance-detection misclassifies _fills_ as cancels (an order that fills mid-tick also disappears). The right fix combines disappeared-set ∩ no-fills-in-`order_fills` ∩ idle grace window, but that's not yet implemented.

Operational impact: a TWS-cancelled order stays `WORKING` in `order_submissions` until the next FastAPI restart triggers `single_leg_rehydrate.rehydrate_on_boot`, which drops it from the working set. The UI ends up showing a phantom "open" order until refresh. **This is the longest-standing open gap in the order surface.**

```
**new_string:**
```

### 10.4 TWS-cancel mirroring — SHIPPED (2026-06-13, `order-path-incident-history.md` row 20)

If the user cancels an order in **TWS** (not the Xenon UI), the activity poller now mirrors it. `sweep_disappeared_orders` (`src/xenon/api/services/ib_activity_mirror.py`) runs each poll tick, **after** the open-order sync and fills tick both succeed, and sweeps `WORKING`/`PARTIALLY_FILLED` rows that vanished from `get_open_orders()`:

- disappeared **and** `order_fills` for the same `(perm_id, scope)` cover the quantity → `FILLED`;
- disappeared for **two consecutive sweeps** without full fills → `CANCELLED` (`reason_code=TWS_CANCEL_MIRROR`), one-tick grace held in a module-level `_SWEEP_GRACE` set.

Safety guards (all covered by `scripts/tests/test_tws_cancel_sweep.py`, 5 cases): an **empty** snapshot while WORKING rows exist skips the whole sweep (a stale post-reconnect read must never mass-cancel); presence is matched on `perm_id` **OR** `ib_order_id` (survives the permId=0 race); a BAG with leg fills but no envelope row stays `WORKING`; a `fill_qty>0` guard stops a quantity-0 row being marked `FILLED`. `mark_terminal` uses an `expected_states` optimistic guard so the sweep's terminal write is a no-op if a concurrent fill/cancel already transitioned the row.

Known residual (deliberate): a TWS cancel of your _only_ open order yields an empty snapshot, which the guard skips — that cancel mirrors on the next non-empty sweep or boot rehydrate, not instantly. Full behavior: `src/xenon/api/CLAUDE.md` § "TWS cancel mirroring".

```

### Step 11 — `order-stack-end-to-end.md` §12.2 regime block → removed (Edit D6)

**File:** `docs/architecture/order-stack-end-to-end.md`
Replace the whole `### 12.2` section. **old_string** = heading through the last bullet (verbatim, unique):
```

### 12.2 Regime block + override

Single-leg place in PANIC tier:

- Server returns 409 `REGIME_BLOCK`, `override_required: true`, `override_min_reason_chars: 10`.
- `OrderTab` opens `RegimeBlockModal`.
- User types reason ≥ 10 chars → `handleRegimeOverride` → `retryRegimeOrder({...body, override: true, override_reason})`.
- Same `client_attempt_id` → server gate sees `override_requested && len(reason) >= 10` → proceeds with `override_audit` set → `reserve_attempt` writes both rows in one txn → subprocess → on success, `mark_submitted` back-fills `regime_overrides.{perm_id, ib_order_id}`.
- Audit row in `regime_overrides` is the durable record of "user knowingly bypassed the gate at PANIC".

```
**new_string:**
```

### 12.2 Regime block + override — REMOVED (historical)

> Removed with the regime gate (§6). The backend no longer returns `409 REGIME_BLOCK`, and nothing writes the (now-dormant) `regime_overrides` table. The live risk gate on the place path is naked-short/preflight Gate 4 (§12.1), which returns **HTTP 400** on violation.

```

### Step 12 — `order-stack-end-to-end.md` §14 Known Issues: TWS-cancel done, regime items historical (Edit D7)

**File:** `docs/architecture/order-stack-end-to-end.md`
**old_string** (item 3, verbatim):
```

3. **TWS-cancel not mirrored** — `WORKING` rows for orders cancelled outside Xenon become phantoms until restart. Disambiguate with `order_fills` for the same `(perm_id, scope)` + idle-grace window.

```
**new_string:**
```

3. ~~**TWS-cancel not mirrored**~~ — **SHIPPED 2026-06-13** (`sweep_disappeared_orders`, incident row 20). No longer an open item; see §10.4.

```

**old_string** (item 6, verbatim):
```

6. **C-2.3 modify override audit** — `_run_modify_regime_gate` override path does not write `regime_overrides`. `override_supported: false` is the current honest signal; the structural fix is plumbing override fields through `apply_modify` into the audit table.

```
**new_string:**
```

6. ~~**C-2.3 modify override audit**~~ — **OBSOLETE.** `_run_modify_regime_gate` and the regime override path were removed (§6). `regime_overrides` is a dormant orphan table.

```

**old_string** (item 7, verbatim):
```

7. **`acknowledge_limit_override` audit fragmentation (§13.14)** — limit-band overrides land in `order_events`, not `regime_overrides`. Extend `regime_overrides` with `kind ∈ {regime, limit_band}` and unify the writer surface.

```
**new_string:**
```

7. **`acknowledge_limit_override` audit fragmentation (§13.14)** — limit-band overrides land in `order_events`. (The former "unify with `regime_overrides`" fix is moot: `regime_overrides` is now a dormant orphan — see §6. If audit consolidation is still wanted, target `order_events` alone.)

```

---

### Step 13 — `order-stack-end-to-end.html` stale banner (Edit H1)

**File:** `docs/architecture/order-stack-end-to-end.html`
**old_string:**
```

    <h1>Order Stack — End-to-End Architecture</h1>

```
**new_string:**
```

    <h1>Order Stack — End-to-End Architecture</h1>
    <p style="border:1px solid #b58900;padding:8px 12px;border-radius:4px;background:rgba(181,137,0,0.08)">
      ⚠️ <strong>Stale companion (verified 2026-05-02).</strong> The regime gate was removed server-side (#104) and TWS-cancel mirroring shipped (2026-06-13). This HTML has <em>not</em> been re-rendered — read
      <code>docs/architecture/order-stack-end-to-end.md</code> for the reconciled 2026-07-05 version.
    </p>

````

---

## 6. Tripwire — re-check merged fable plans before executing

Several sibling fable plans in `docs/superpowers/plans/2026-07-05-fable-*.md` (e.g. **S2** adds an `UNCERTAIN` preflight verdict; **S4** adds `persist_warning`) will change some behaviors this doc describes. This plan reflects **HEAD at planning time (`fb5b6d81`)**. Before writing the edits:

```bash
cd /Users/chenxi/projects/xenon
git rev-parse HEAD
git log --oneline -20 | grep -iE "fable|regime|uncertain|persist_warning|preflight" || echo "no fable merges since planning"
````

- If HEAD == `fb5b6d81` → proceed exactly as written.
- If any fable S1–S7 / P1 / P2 plan has merged (e.g. a commit like `feat/…uncertain…`), **STOP and report** which one merged. The naked-short/preflight description in Step 8/9/11 or the "400" status may need to reflect the new `UNCERTAIN`/`persist_warning` reality. Do not guess — surface it. The doc must describe _merged_ reality only.

---

## 7. Verification matrix (every corrected claim gets a grep/code citation check)

Run all of these after the edits. Each has a literal expected result.

**V1 — 403→400 fixed in exactly the three naked-short docs, READ_ONLY 403s untouched:**

```bash
grep -rn "returns 403 on violation\|returns 403 if guard\|/api/orders/place` returns 403" README.md CLAUDE.md src/xenon/CLAUDE.md
```

Expected: **no output** (exit 1). Then confirm the 400s exist:

```bash
grep -rn "returns 400 on violation\|returns \*\*HTTP 400\*\*\|/api/orders/place` returns 400" README.md CLAUDE.md src/xenon/CLAUDE.md
```

Expected: **3 matches** (one per file).

**V1b — READ_ONLY / broker 403s preserved (negative check):**

```bash
grep -c "READ_ONLY" CLAUDE.md docs/architecture/production-database-strategy.md src/xenon/api/CLAUDE.md
```

Expected: each file still ≥1 (these 403s were NOT touched). Also confirm the code truth is unchanged:

```bash
grep -n "status_code=400" src/xenon/api/server.py | head -1
```

Expected: a hit (the naked-short mapping — proves the doc now matches code).

**V2 — phantom CLI gone, real CLIs all present:**

```bash
grep -rn "xenon-position-rules" README.md CLAUDE.md src/xenon/CLAUDE.md src/xenon/api/CLAUDE.md
```

Expected: **no output** (exit 1).

```bash
grep -oE "xenon-[a-z-]+" pyproject.toml | sort -u > /tmp/scripts_real.txt
grep -oE "xenon-[a-z-]+" README.md | sort -u > /tmp/scripts_readme.txt
comm -23 /tmp/scripts_real.txt /tmp/scripts_readme.txt
```

Expected: **no output** — every `[project.scripts]` entry now appears in the README. (`comm -13` may still show README-only doc-example tokens; that direction is not asserted.)

**V3 — regime marked removed, not live, in the .md:**

```bash
grep -n "### 6.1 Tier ladder\|### 6.2 Hedge classification\|### 12.2 Regime block + override$" docs/architecture/order-stack-end-to-end.md
```

Expected: **no output** (old sub-sections gone).

```bash
grep -n "REMOVED (historical)\|Verified as of 2026-07-05" docs/architecture/order-stack-end-to-end.md
```

Expected: **≥4 matches** (banner + §5.3 + §6 + §12.2).

**V4 — section skeleton intact (no accidental section deletion):**

```bash
grep -n "^## " docs/architecture/order-stack-end-to-end.md
```

Expected: `## 6. Regime Gate Detail — REMOVED (historical)` is immediately followed by `## 7. Quote Gate Detail (and the STALE_QUOTE Mystery)`, and headings `## 1.` … `## 14.` are all still present and in order.

**V5 — TWS-cancel gap marked shipped:**

```bash
grep -n "longest-standing open gap" docs/architecture/order-stack-end-to-end.md
```

Expected: **no output**.

```bash
grep -n "SHIPPED (2026-06-13\|sweep_disappeared_orders" docs/architecture/order-stack-end-to-end.md
```

Expected: **≥1 match**.

**V6 — HTML banner present:**

```bash
grep -n "Stale companion (verified 2026-05-02)" docs/architecture/order-stack-end-to-end.html
```

Expected: **1 match**.

**V7 — Markdown well-formed (no linter in repo, so a JSON-style structural sanity instead):** confirm no unbalanced code fences were introduced in the edited .md:

````bash
grep -c '^```' docs/architecture/order-stack-end-to-end.md
````

Expected: an **even** number (every fence closed). If odd → a code-fence block was left open → STOP and fix.

**V8 — diff scope guard (HARD TRIPWIRE):**

```bash
git status --porcelain
```

Expected: modified paths are **only** among:
`README.md`, `CLAUDE.md`, `src/xenon/CLAUDE.md`, `docs/architecture/order-stack-end-to-end.md`, `docs/architecture/order-stack-end-to-end.html`, and this plan file. **If any `.py` / `.ts` / `.tsx` / `.sql` / `pyproject.toml` shows as modified → STOP, revert it, report.**

**V9 — no test/build gates apply** (docs-only). Do NOT run `pytest`, `npm test`, `tsc`, or the order-path CI guards — nothing they cover was touched, and there is no MD linter. (If you feel the urge to run them "to be safe," don't — it wastes time and none of them read these files.)

---

## 8. Tripwires / abort criteria

- **Any `old_string` in §5 not found or not unique → STOP and report.** The doc drifted since planning; do not fuzzy-match or guess a replacement.
- **HEAD ≠ `fb5b6d81` AND a fable plan has merged (§6) → STOP and report** which plan merged before editing regime/preflight/status claims.
- **V8 shows a non-doc file modified → STOP,** `git checkout -- <file>` it, report. This PR is docs-only.
- **V7 shows an odd fence count → STOP,** you broke a code block in §6/§8 — re-open and balance it.
- **You find a _fourth_ doc claiming naked-short → 403** (beyond README / root CLAUDE.md / src/xenon/CLAUDE.md) → fix it the same way and note it in the PR body; do not silently expand scope elsewhere.
- **No live IB, no dev stack, no browser** is required for this task. If any step seems to need them, you misread the step — re-read §3 Non-goals.

---

## 9. Rollback

Docs-only, no schema, no migration, no code. To abandon:

```bash
git checkout master
git branch -D docs/fable-p2-8-doc-repair
```

Nothing to un-migrate or redeploy. If already committed and merged and later found wrong, a plain revert commit of the doc diff is sufficient.

---

## 10. Incident-history row

Not applicable — this is a documentation repair, not an order-path code fix. Do **not** append a row to `docs/reference/order-path-incident-history.md` (that log is for non-trivial order-path _bugs_; a doc-sync change is not one). The banner in Step 6 already points readers at rows 20 and 22, which are the relevant existing entries.

---

## 11. Commit / PR (per repo policy)

- Commit message (no AI attribution trailer):
  `docs(order-stack): reconcile order-stack doc with HEAD — regime removed, TWS-cancel shipped, 403→400, README CLI table`
- Push branch, open PR with `gh pr create`; **never** push to master directly. Wait for CI green (CI will pass trivially — docs-only), then merge.
