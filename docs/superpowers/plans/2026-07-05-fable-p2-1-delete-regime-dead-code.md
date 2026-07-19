# Plan: P2.1 — Delete regime-gate dead code + vestigial `regime_overrides` write plumbing

**Date:** 2026-07-05
**Branch:** `chore/p2-1-delete-regime-dead-code`
**Finding IDs:** CX-3 (Dead code, web + Python) — `docs/fable/03-findings-table.md:61`, `docs/fable/10-roadmap.md:61` (P2.1)
**Severity:** Medium
**Goal (one line):** Delete the client-side regime-gate retry logic and its Python `override_audit` / `regime_overrides` write plumbing — all provably unreachable since the regime gate was removed server-side in PR #104 (commit `cc568c36`) — with **zero behavior change**.

This is a **deletion-only PR.** No new logic, no refactors, no renames. Every deletion below is justified by a grep proving zero remaining live references (encoded in the Verification Matrix).

---

## Context — what exists today (verified at HEAD)

The regime gate (VCG-CRI signal layer) was deleted server-side in the pure-portfolio pivot. `src/xenon/api/server.py:2162-2166` confirms it in a comment and hardcodes `override_audit = None`:

```python
# RegimeGate (VCG-CRI signal layer) was removed in the pure-portfolio
# pivot (#104). Order entry no longer gates on regime tiers; cover_ratio
# reverts to its NORMAL/no-gate default and there is no override audit.
cover_ratio_for_preflight = 1.0
override_audit = None
```

The backend therefore **never** returns `409 REGIME_BLOCK` or `422 REGIME_RESIZE_REQUIRED`, and **never** inserts a `regime_overrides` row. Everything downstream that handles those responses or writes that table is dead.

**Two distinct dead surfaces this PR removes:**

**A. Web regime-gate client code** (all confined to 3 files — grep-verified, see Key Facts):

- `web/lib/order/regimeGate.ts` — response parser + override/resize helpers. Imported only by `OrderTab.tsx`.
- `web/components/RegimeBlockModal.tsx` — a **null-rendering stub** (its own docstring says "the modal is unreachable in practice"). Imported only by `OrderTab.tsx`.
- `web/components/ticker-detail/OrderTab.tsx` — two near-identical form components (`NewOrderForm`/single-leg at the top, `ComboOrderForm` at `:807`) each carry a full copy of: `regimePrompt` state, the `parseRegimeGateResponse` branch inside `handlePlace`, a `retryRegimeOrder` callback, `handleRegimeOverride`/`handleRegimeResize` callbacks, and two `<RegimeBlockModal>` JSX blocks.

**B. Python `regime_overrides` write plumbing** (the TABLE stays — see Non-goals):

- `src/xenon/execution/orders_store.py` — `reserve_attempt(..., override_audit=None)` has a dead `if override_audit is not None:` insert block; `mark_submitted` has a dead `update(regime_overrides)` block that targets rows which are never created.
- `src/xenon/db/queries/regime_overrides.py` — an **entirely unimported** async query module (`insert_override`, `mark_broker_ids`, `list_overrides`, `get_override_for_submission`). No production code and no test imports it.
- `src/xenon/api/server.py` — the `override_audit = None` local and the `override_audit=override_audit` kwarg on the `reserve_attempt` call.

**What the executor does NOT need to understand:** the naked-short guard, the preflight/quote-validation pipeline, the IB subprocess place path, the account-scope machinery, or the live "market regime" _display_ feature (`lib/regimeHistory.ts`, `lib/regimeRelationships.ts`, `e2e/regime-*.spec.ts`, etc.). **That display feature is ALIVE and unrelated — do not touch anything named "regime" outside the exact files/lines listed in Steps.**

---

## Drift from review

- **CX-3 also mentions "combo net-price math ×3 → single combo-quote util".** That is a _refactor_, not a deletion, and is explicitly **out of scope** here (see Non-goals). The roadmap P2.1 line scopes this item to "regime dead code (web + override_audit plumbing)" only.
- **CX-3 says "unified-order-system hooks/components dead (`useOrderPrices`, `useOrderValidation`, 4 components)."** Verified at HEAD (grep in Key Facts): the genuinely-dead set is `useOrderPrices`, `useOrderValidation` (2 hooks), `OrderActionToggle`, `OrderQuantityInput`, `OrderPriceInput`, `OrderPriceButtons` (4 components), and the `buildPlaceOrderBody` alias. The **other** unified-order components named similarly (`OrderPriceStrip`, `OrderLegPills`, `OrderConfirmSummary`, `OrderTifSelector`) are **ALIVE** (used by `OrderTab.tsx`, `ModifyOrderModal.tsx`, `OptionsChainTab.tsx`, `PositionOrderModal.tsx`, `InstrumentDetailModal.tsx`, `BookTab.tsx`) and **must be kept**. This deletion is included as **Part B** (secondary), separated from the regime deletion (Part A) so it can be skipped without affecting Part A if its grep gate fails.
- **Line numbers in the finding (`OrderTab.tsx:455-620,933-1110`) have drifted.** At HEAD the two `handlePlace` bodies are at `:455` and `:933`, `retryRegimeOrder` at `:546` and `:1036`, modal JSX at `:745` and `:1559`. Steps anchor on **function names + unique snippets**, not line numbers.

---

## Goal / Non-goals

**Goal:** Remove all dead regime-gate client code (Part A) and the dead `regime_overrides` write plumbing (Part B-Python), plus the dead unified-order-system hooks/components (Part B-web). Net effect: smaller bundle, fewer LOC, identical runtime behavior.

**Non-goals (explicitly NOT done here — one change, one PR):**

- **Do NOT drop the `regime_overrides` TABLE, its FK, or any migration.** Schema changes are P2.2 / schema territory. `schema.py:788` (`regime_overrides` Table), `schema.py:619` (FK-target comment on `order_submissions`), both migrations (`48343156f9b7`, `c4d5e6f70123`), and the `_test_db.py:74` truncate entry (`"xenon.regime_overrides"`) **stay** — they reference a table that still exists.
- Do NOT unify the combo net-price math (CX-3's third bullet). Separate refactor.
- Do NOT touch the market-regime _display_ feature. Do NOT touch S1 (auth), S2 (uncertain orderRef), S3 (poller), S4 (post-ack persist), S5-S7.
- Do NOT delete alive unified-order components (`OrderPriceStrip`, `OrderLegPills`, `OrderConfirmSummary`, `OrderTifSelector`) or the `buildFastApiPlaceOrderPayload` function (used by `app/api/orders/place/route.ts` — the live order path).

---

## Cross-plan coordination (READ BEFORE STARTING)

Three sibling plans edit `OrderTab.tsx`. This plan is **subtractive** and should merge **LAST**.

1. **Recommended merge order: S1 → S2 → S4 (and any other order-path fixes) FIRST, then P2.1.**
   - **S4** (`2026-07-05-fable-s4-protect-post-ack-persist.md`) instruments **four** success branches, two of which are the `retryRegimeOrder` success branches (`:582`, `:1072`) — which this plan **deletes entirely**. Rationale for S4-first: S4 is a higher-severity correctness fix (prevents a live-order broker-ack being dropped as an HTTP 500) and should not be blocked on a Medium cleanup. When S4 lands first, it adds a `persist_warning` suffix to all four branches; P2.1 then deletes the two `retryRegimeOrder` functions **carrying S4's now-dead instrumentation with them** (acceptable — those branches were unreachable), and leaves S4's suffix intact on the two surviving `handlePlace` success branches. P2.1 only removes the single `setRegimePrompt(null);` line from those surviving branches (Step A3), never the `setSuccess(...)` anchor S4 depends on.
   - If P2.1 were merged first instead, S4 would have to rebase its "four success branches" down to two — a rebase burden on the higher-priority PR. Avoid that.

2. **S2** (`2026-07-05-fable-s2-uncertain-orderref.md`) touches `OrderTab.tsx` **~line 199** (reading order status for display) — a region this plan does **not** touch. **P2.1's OrderTab edits are independent of S2's.** No coordination needed beyond re-running the grep anchors after rebase.

3. **`setRegimePrompt(null)` / `regimePrompt` appear inside the success branches that S2/S4 quote as anchors.** Deleting `regimePrompt` state changes those anchors. This is expected. **If S4 has already landed when you run this plan**, the single-leg and combo `handlePlace` success branches will contain an extra S4 `persist_warning` suffix between `setSuccess(...)` and `attemptId.markTerminal()`. **Leave that suffix untouched** — only remove the `setRegimePrompt(null);` line (Step A3). The `old_string` you match must be adapted to whatever is actually at HEAD of your branch; if the surrounding lines differ from the snippets below, re-read the file and match the real text. **Tripwire:** if `setRegimePrompt(null);` is no longer a standalone line in a success branch, STOP and re-read.

---

## Key facts (verified against the working tree at HEAD)

- **`web/lib/order/regimeGate.ts` is imported by exactly one file:** `web/components/ticker-detail/OrderTab.tsx:29-35`. Grep: `grep -rn "regimeGate" web/ --include="*.ts*" | grep -v node_modules` → only `OrderTab.tsx:35` + the file itself. Safe to delete the whole module.
- **`web/components/RegimeBlockModal.tsx` is imported by exactly one file:** `OrderTab.tsx:13`. It is a null-returning stub. Safe to delete.
- **No test imports any `regimeGate` export or `RegimeBlockModal`.** Grep of `web/tests/` + `web/e2e/` for `regimeGate|parseRegimeGateResponse|buildRegimeOverrideFields|suggestResizeQuantity|RegimeBlockModal|REGIME_BLOCK|REGIME_RESIZE` → **empty**.
- **`RegimePrompt` type, `regimePrompt`/`setRegimePrompt`, `retryRegimeOrder`, `handleRegimeOverride`, `handleRegimeResize` exist only in `OrderTab.tsx`.** No external references.
- **Python: `override_audit` is passed a value in exactly one place** — `server.py:2231`, where the value (`override_audit`, set at `:2166`) is always `None` (never reassigned between `:2166` and `:2227`). Grep: `grep -rn "override_audit=" src/ scripts/` → only `server.py:2231`.
- **`src/xenon/db/queries/regime_overrides.py` is imported by nobody.** Grep: `grep -rn "queries.regime_overrides\|from xenon.db.queries.regime_overrides" src/ scripts/` → **empty**. Its functions `insert_override`/`mark_broker_ids`/`list_overrides`/`get_override_for_submission` have zero call sites.
- **No test references `override_audit`, `insert_override`, or the `regime_overrides` query module.** Grep of `scripts/tests/`, `src/xenon/db/tests/`, `src/xenon/api/tests/` → **empty** (the only `regime_overrides` symbol referenced in tests is via the ORM `Table` object during truncate, unaffected).
- **`reserve_attempt` callers do not pass `override_audit`** except `server.py:2231`. Removing the keyword-only param with default `None` therefore breaks no test caller (verified: `test_orders_submissions_store.py`, `test_orders_store_modify_sequence.py`, `test_single_leg_rehydrate.py`, `test_preflight_route.py`, `test_schema_scope.py` all omit it).
- **Unified-order-system dead set (Part B-web), grep of app usage excluding `lib/order/`, `tests/`, `e2e/`:**
  - `useOrderPrices` → 0 app uses · `useOrderValidation` → 0 · `OrderActionToggle` → 0 · `OrderQuantityInput` → 0 · `OrderPriceInput` → 0 (only referenced by `OrderPriceButtons`, itself dead) · `OrderPriceButtons` → 0 (only referenced by `OrderPriceInput`) · `buildPlaceOrderBody` → 0 (alias `= buildFastApiPlaceOrderPayload` at `placeOrderContract.ts:48`, imported only by the barrel).
  - **ALIVE, keep:** `OrderPriceStrip`, `OrderLegPills`, `OrderConfirmSummary`, `OrderTifSelector`, `buildFastApiPlaceOrderPayload`, everything in `lib/order/types.ts` and `lib/order/placeOrderContract.ts` except the `buildPlaceOrderBody` alias.
- **`fmtSignedPrice` (OrderTab import) stays used** by the two combo success messages (`:1009`, `:1073`) — do not remove its import.
- **Test infra:** Python tests run via `uv run pytest`; web via `cd web && npm test` (Vitest, `ASSISTANT_MOCK=1 NODE_ENV=test`). `order-migration.test.ts` and `order-unified-components.test.ts` are **self-contained** (they import only `vitest` and re-implement logic locally — they reference dead symbols **only in `describe()` strings**), so deleting source does not break them; the describe blocks are trimmed purely for grep-cleanliness.

---

## Steps

Strictly ordered. **Part A** (regime) is self-contained and low-risk — do it first and get it green before Part B. **Part B** (unified dead code) is independent; its Step B0 grep gate can abort Part B alone.

### Part A — Regime-gate deletion

#### Step A1 — Delete the two regime-only web files

```bash
git rm web/lib/order/regimeGate.ts
git rm web/components/RegimeBlockModal.tsx
```

(Justification: Key Facts — each imported only by `OrderTab.tsx`, which Step A2/A3 stops importing them. No tests reference them.)

#### Step A2 — Remove regime imports + `RegimePrompt` type from `OrderTab.tsx`

File: `web/components/ticker-detail/OrderTab.tsx`.

**Edit A2a** — delete the `RegimeBlockModal` import (line 13):

```diff
-import { RegimeBlockModal } from "@/components/RegimeBlockModal";
```

**Edit A2b** — delete the `regimeGate` import block (lines 29-35):

```diff
-import {
-  buildRegimeOverrideFields,
-  parseRegimeGateResponse,
-  suggestResizeQuantity,
-  type RegimeBlockResponse,
-  type RegimeResizeResponse,
-} from "@/lib/order/regimeGate";
```

**Edit A2c** — delete the `RegimePrompt` type alias (lines 65-76):

```diff
-type RegimePrompt =
-  | {
-      kind: "block";
-      payload: RegimeBlockResponse;
-      requestBody: Record<string, unknown>;
-    }
-  | {
-      kind: "resize";
-      payload: RegimeResizeResponse;
-      requestBody: Record<string, unknown>;
-      currentQuantity: number;
-    };
```

#### Step A3 — Strip regime plumbing from BOTH form components

`OrderTab.tsx` has two components with **identical** regime plumbing. Apply the same five deletions to each. Component 1 = the top-level single-leg form (state at `:392`, `handlePlace` at `:455`). Component 2 = `ComboOrderForm` (state at `:829`, `handlePlace` at `:933`). Match by unique surrounding text (line numbers drift, especially if S4 landed).

**A3a — delete the `regimePrompt` state** (once per component):

```diff
-  const [regimePrompt, setRegimePrompt] = useState<RegimePrompt | null>(null);
```

(Appears twice — once each at `:392` and `:829`. Use `replace_all` or match with enough context to disambiguate; the line text is identical, so `replace_all: true` is correct here.)

**A3b — collapse the regime branch inside each `handlePlace`.** In the single-leg `handlePlace`, replace:

```typescript
      const regime = await parseRegimeGateResponse(res);
      const json = await res.json().catch(() => null);
      if (!res.ok) {
        if (regime.kind === "block") {
          setRegimePrompt({
            kind: "block",
            payload: regime.payload,
            requestBody,
          });
          return;
        }
        if (regime.kind === "resize") {
          setRegimePrompt({
            kind: "resize",
            payload: regime.payload,
            requestBody,
            currentQuantity: parsedQty,
          });
          return;
        }
        setError(errorFromResponseBody(json, "Order placement failed"));
        attemptId.markTerminal();
      } else {
```

with:

```typescript
      const json = await res.json().catch(() => null);
      if (!res.ok) {
        setError(errorFromResponseBody(json, "Order placement failed"));
        attemptId.markTerminal();
      } else {
```

Then in the same `else` success branch remove the now-orphaned `setRegimePrompt(null);` line:

```diff
         setSuccess(
           `Order placed: ${action} ${parsedQty} ${ticker} @ ${fmtPrice(parsedPrice)}`,
         );
         setConfirmStep(false);
-        setRegimePrompt(null);
         attemptId.markTerminal();
         onOrderPlaced?.();
```

For the **combo** `handlePlace`, the branch is identical except `currentQuantity: parsedQty` and the success message text (`Combo order placed: ... ${fmtSignedPrice(parsedPrice)}`). Apply the same collapse and remove its `setRegimePrompt(null);` at `:1012`.

> **If S4 landed first:** the success branch will have a `persist_warning` suffix between `setConfirmStep(false);`/`attemptId.markTerminal();` and `onOrderPlaced?.();`. Match `old_string` on the real HEAD text and remove **only** the `setRegimePrompt(null);` line, preserving S4's additions.

**A3c — delete each `retryRegimeOrder` `useCallback` in full.** Single-leg version (`:546-597`):

```diff
-  const retryRegimeOrder = useCallback(
-    async (requestBody: Record<string, unknown>) => {
-      setLoading(true);
-      ...
-    },
-    [attemptId, onOrderPlaced, parsedQty, ticker],
-  );
```

Delete the entire block from `const retryRegimeOrder = useCallback(` through its closing `);` (dependency array `[attemptId, onOrderPlaced, parsedQty, ticker]` for single-leg; `[attemptId, onOrderPlaced, parsedQty, position.structure]` for combo). Do the same for the combo copy (`:1036-1087`).

**A3d — delete each `handleRegimeOverride` + `handleRegimeResize` `useCallback` in full** (single-leg `:599-620`, combo `:1089-1110`):

```diff
-  const handleRegimeOverride = useCallback(
-    (overrideReason: string) => {
-      if (regimePrompt?.kind !== "block") return;
-      void retryRegimeOrder({
-        ...regimePrompt.requestBody,
-        ...buildRegimeOverrideFields(overrideReason),
-      });
-    },
-    [regimePrompt, retryRegimeOrder],
-  );
-
-  const handleRegimeResize = useCallback(
-    (newQuantity: number) => {
-      if (regimePrompt?.kind !== "resize") return;
-      _setQuantity(String(newQuantity));   // combo copy uses setQuantity(...)
-      void retryRegimeOrder({
-        ...regimePrompt.requestBody,
-        quantity: newQuantity,
-      });
-    },
-    [regimePrompt, retryRegimeOrder],
-  );
```

(Single-leg `handleRegimeResize` calls `_setQuantity(...)`; the combo copy calls `setQuantity(...)`. Match the real text.)

**A3e — delete the two `<RegimeBlockModal>` JSX blocks per component** (single-leg `:745-765`, combo `:1559-1579`):

```diff
       <OrderErrorBanner error={error} />
       {success && <div className="order-success">{success}</div>}
-      {regimePrompt?.kind === "block" && (
-        <RegimeBlockModal
-          kind="block"
-          payload={regimePrompt.payload}
-          onConfirm={handleRegimeOverride}
-          onCancel={() => setRegimePrompt(null)}
-        />
-      )}
-      {regimePrompt?.kind === "resize" && (
-        <RegimeBlockModal
-          kind="resize"
-          payload={regimePrompt.payload}
-          currentQuantity={regimePrompt.currentQuantity}
-          suggestedQuantity={suggestResizeQuantity(
-            regimePrompt.payload,
-            regimePrompt.currentQuantity,
-          )}
-          onResize={handleRegimeResize}
-          onCancel={() => setRegimePrompt(null)}
-        />
-      )}

       {/* Order Summary (shown in confirm step) */}
```

**Gate for Part A (web):** `cd web && npx tsc --noEmit` → **0 errors** (proves no orphaned identifier). Then the grep-clean check in the Verification Matrix (Web-Grep-1) must return empty.

#### Step A4 — Remove the dead `override_audit` param + write block from `orders_store.py`

File: `src/xenon/execution/orders_store.py`.

**A4a** — drop the `regime_overrides` import (line 25):

```diff
-from xenon.db.schema import order_events, order_fills, order_submissions, regime_overrides
+from xenon.db.schema import order_events, order_fills, order_submissions
```

**A4b** — remove the `override_audit` param from `reserve_attempt` (line 94) and its docstring paragraph (lines 99-104):

```diff
     broker: str = "IB",
     account_env: str = "legacy_unknown",
     broker_account: str = "legacy_unknown",
-    override_audit: dict | None = None,
 ) -> ReservationOutcome:
     """Atomically reserve a submission slot keyed by
     (broker, account_env, broker_account, user_id, client_attempt_id).
-
-    `override_audit` (when provided) writes a regime_overrides row in
-    the same transaction as the submission reservation. The deferred
-    composite FK on regime_overrides means both rows commit atomically
-    or both roll back. Schema: dict with keys
-    `route, vcg_tier, cri_tier, binding_side, block_reason, user_reason,
-    order_payload`.
     """
```

**A4c** — remove the dead insert block inside `reserve_attempt` (lines 136-153). The block is:

```python
        if inserted is not None:
            if override_audit is not None:
                conn.execute(
                    insert(regime_overrides).values(
                        user_id=user_id,
                        ... (all the override_audit[...] fields) ...
                    )
                )
            return ReservationOutcome(
```

becomes:

```python
        if inserted is not None:
            return ReservationOutcome(
```

**A4d** — remove the dead `update(regime_overrides)` block from `mark_submitted` (lines 519-526). After the `update(order_submissions)...` execute inside `mark_submitted`, delete:

```diff
                 state="WORKING",
                 updated_at=now,
             )
         )
-        conn.execute(
-            update(regime_overrides)
-            .where(regime_overrides.c.submission_id == submission_id)
-            .values(
-                perm_id=perm_id_int,
-                ib_order_id=ib_order_id_int,
-            )
-        )
```

After this, `perm_id_int` / `ib_order_id_int` (computed at the top of `mark_submitted`) become unused. Remove their two assignments too:

```diff
     now = datetime.now(timezone.utc)
-    perm_id_int = _int_or_none(perm_id)
-    ib_order_id_int = _int_or_none(ib_order_id)
     engine = get_sync_engine()
```

…and if `_int_or_none` (the inner helper defined at the top of `mark_submitted`) now has zero callers, remove its `def _int_or_none(...)` block as well. **Verify with a grep of the function body first** — if `_int_or_none` is used elsewhere in `mark_submitted`, keep it. (At HEAD its only two uses are the two lines just deleted, so it becomes dead; remove it.)

> **Order-path caution:** `mark_submitted` is on the live place path (called after IB ack). The `regime_overrides` UPDATE it performed was a **no-op** — it targets rows that are never inserted (Key Facts). Removing it cannot change any observable behavior; existing `mark_submitted` tests (`test_orders.py`, `test_orders_submissions_store.py`, `test_orders_store_modify_sequence.py`) assert on `order_submissions` only and must stay green.

#### Step A5 — Remove the dead `override_audit` local + kwarg in `server.py`

File: `src/xenon/api/server.py`. At `:2166` and `:2227-2233`:

```diff
     cover_ratio_for_preflight = 1.0
-    override_audit = None
```

```diff
     outcome = orders_store.reserve_attempt(
         user_id,
         cid,
         req_row,
-        override_audit=override_audit,
         **_resolve_scope_kwargs(),
     )
```

Keep the explanatory comment at `:2162-2164` (it documents _why_ the gate is gone — useful context), or trim its last clause "and there is no override audit" for accuracy. Minimal edit: leave the comment as-is. **Do not** touch `cover_ratio_for_preflight` — it feeds `_run_preflight`.

#### Step A6 — Delete the unimported `regime_overrides` query module

```bash
git rm src/xenon/db/queries/regime_overrides.py
```

(Justification: Key Facts — zero importers, zero test references. The `regime_overrides` **Table** in `schema.py` is unaffected and stays.)

**Gate for Part A (Python):** `uv run pytest scripts/tests/test_orders_submissions_store.py scripts/tests/test_orders_store_modify_sequence.py src/xenon/db/tests/test_orders.py scripts/tests/test_preflight_route.py -x` → all pass. Python-Grep-1 (Verification Matrix) returns empty for the removed symbols.

---

### Part B — Unified-order-system dead code (secondary; skippable)

#### Step B0 — Re-verify the dead set (GATE — abort Part B if any fail)

Run, expecting **each count to be 0** (matches excluding the barrel `index.ts`, the two self-contained tests, `e2e/`, and the symbol's own file):

```bash
cd web
for sym in useOrderPrices useOrderValidation OrderActionToggle OrderQuantityInput OrderPriceInput OrderPriceButtons buildPlaceOrderBody; do
  n=$(grep -rln "\b$sym\b" --include="*.ts" --include="*.tsx" . \
      | grep -v node_modules | grep -v "\.next/" \
      | grep -v "^\./lib/order/" | grep -v "^\./tests/" | grep -v "^\./e2e/" | wc -l | tr -d ' ')
  echo "$sym app-uses=$n"
done
```

**Tripwire:** if ANY symbol reports `app-uses` > 0, that symbol is NOT dead — **remove it from the Part B deletion list and note it in the PR description**. Do not delete a referenced symbol. (`OrderPriceButtons`/`OrderPriceInput` reference each other inside `lib/order/`, which the `^\./lib/order/` exclusion already discounts — that mutual reference is expected and does not make either alive.)

#### Step B1 — Delete the dead hook + component source files

```bash
git rm web/lib/order/hooks/useOrderPrices.ts
git rm web/lib/order/hooks/useOrderValidation.ts
git rm web/lib/order/components/OrderActionToggle.tsx
git rm web/lib/order/components/OrderQuantityInput.tsx
git rm web/lib/order/components/OrderPriceInput.tsx
git rm web/lib/order/components/OrderPriceButtons.tsx
```

#### Step B2 — Prune the barrel `web/lib/order/index.ts`

Remove the dead exports (keep everything else):

```diff
-// Hooks
-export { useOrderPrices } from "./hooks/useOrderPrices";
-export { useOrderValidation } from "./hooks/useOrderValidation";
 export {
   buildFastApiPlaceOrderPayload,
-  buildPlaceOrderBody,
 } from "./placeOrderContract";

 // Components
 export { OrderPriceStrip } from "./components/OrderPriceStrip";
 export { OrderLegPills } from "./components/OrderLegPills";
-export { OrderPriceButtons } from "./components/OrderPriceButtons";
-export { OrderActionToggle } from "./components/OrderActionToggle";
 export { OrderTifSelector } from "./components/OrderTifSelector";
-export { OrderQuantityInput } from "./components/OrderQuantityInput";
-export { OrderPriceInput } from "./components/OrderPriceInput";
 export { OrderConfirmSummary } from "./components/OrderConfirmSummary";
```

Update the header docstring example if it names a deleted symbol: line 8 `import { OrderPriceStrip, useOrderPrices, OrderAction } from "@/lib/order";` → replace `useOrderPrices` with an alive export, e.g. `OrderConfirmSummary`.

#### Step B3 — Remove the `buildPlaceOrderBody` alias from `placeOrderContract.ts`

File: `web/lib/order/placeOrderContract.ts`, line 48:

```diff
-export const buildPlaceOrderBody = buildFastApiPlaceOrderPayload;
```

(Verify no other reference first — Key Facts: only the barrel imported it, removed in B2.)

#### Step B4 — Trim dead-symbol `describe()` blocks from the two self-contained tests (grep-cleanliness only)

These tests import nothing from `lib/order`; they name dead symbols only in `describe()` strings. Delete only the dead-symbol blocks; **keep** the alive-symbol blocks.

- `web/tests/order-unified-components.test.ts`: delete `describe("useOrderPrices hook", ...)` (the block spanning the current lines 12-122) and `describe("useOrderValidation hook", ...)` (124-192). **Keep** the `OrderPriceStrip`/`OrderLegPills`/`OrderConfirmSummary` formatting blocks.
- `web/tests/order-migration.test.ts`: delete `describe("OrderActionToggle", ...)` (165-183), `describe("OrderQuantityInput", ...)` (193-203), and `describe("OrderPriceInput", ...)` (205-210, which also names `OrderPriceButtons` in its `it(...)` string). **Keep** the `OrderPriceStrip`/`OrderLegPills`/`OrderConfirmSummary`/`OrderTifSelector` blocks.

**Gate for Part B:** `cd web && npx tsc --noEmit` → 0 errors; `npm test` → green; Web-Grep-2 returns empty for the Part B symbols.

---

## Verification matrix

Run from repo root unless noted. Every command must produce the stated outcome.

### Grep-clean (the core P2.1 acceptance)

| ID                         | Command                                                                                                                                                                                                                                                                                                                                      | Expected                                                    |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| Web-Grep-1 (regime)        | `grep -rn "regimeGate\|RegimeBlockModal\|RegimePrompt\|regimePrompt\|retryRegimeOrder\|handleRegimeOverride\|handleRegimeResize\|parseRegimeGateResponse\|buildRegimeOverrideFields\|suggestResizeQuantity\|RegimeBlockResponse\|RegimeResizeResponse" web/ --include="*.ts" --include="*.tsx" \| grep -v node_modules \| grep -v "\.next/"` | **empty output** (exit 1)                                   |
| Web-Grep-2 (unified)       | `grep -rn "useOrderPrices\|useOrderValidation\|OrderActionToggle\|OrderQuantityInput\|OrderPriceInput\|OrderPriceButtons\|buildPlaceOrderBody" web/ --include="*.ts" --include="*.tsx" \| grep -v node_modules \| grep -v "\.next/"`                                                                                                         | **empty output** (exit 1)                                   |
| Py-Grep-1                  | `grep -rn "override_audit\|queries.regime_overrides\|from xenon.db.queries.regime_overrides\|insert_override" src/ scripts/ --include="*.py"`                                                                                                                                                                                                | **empty output** (exit 1)                                   |
| Py-Grep-2 (table survives) | `grep -rn "regime_overrides" src/xenon/db/schema.py src/xenon/_test_db.py`                                                                                                                                                                                                                                                                   | **non-empty** — the Table + truncate entry must STILL exist |

### Typecheck / lint (web touched)

| ID   | Command                      | Expected                                                                                        |
| ---- | ---------------------------- | ----------------------------------------------------------------------------------------------- |
| TS   | `cd web && npx tsc --noEmit` | exit 0, **no errors**                                                                           |
| Lint | `cd web && npm run lint`     | exit 0 (no new warnings on touched files; unused-import errors here mean a deletion was missed) |

### Unit — web

| ID           | Command                       | Expected                                                                                                                                                                              |
| ------------ | ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Vitest-Order | `cd web && npm test -- order` | all pass (covers `order-migration`, `order-unified-components`, `order-reliability`, `order-payload`, `order-tab-combo-sign`, `OrderTab.prefill`, `order-entrypoint-submit-contract`) |
| Vitest-Full  | `cd web && npm test`          | all pass                                                                                                                                                                              |

### Unit — Python

| ID             | Command                                                                                                                                                | Expected                                                           |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------ |
| Py-OrdersStore | `uv run pytest scripts/tests/test_orders_submissions_store.py scripts/tests/test_orders_store_modify_sequence.py src/xenon/db/tests/test_orders.py -x` | all pass (`reserve_attempt` + `mark_submitted` behavior unchanged) |
| Py-Preflight   | `uv run pytest scripts/tests/test_preflight_route.py -x`                                                                                               | all pass (exercises the `reserve_attempt` call site)               |
| Py-Affected    | `uv run python scripts/infra/dev/run_pytest_affected.py`                                                                                               | all pass                                                           |

### CI guards (order path — MANDATORY, this PR edits `orders_store.py` + `server.py`)

| ID      | Command                                                          | Expected |
| ------- | ---------------------------------------------------------------- | -------- |
| Guard-1 | `uv run python scripts/checks/no_json_fallback_on_order_path.py` | exit 0   |
| Guard-2 | `uv run python scripts/checks/no_json_write_on_order_path.py`    | exit 0   |
| Guard-3 | `uv run python scripts/checks/order_path_caller_allowlist.py`    | exit 0   |

### Bundle size (acceptance: "bundle size drop" — directional)

Reproducible measure via a clean production build, before and after, from the same shell:

1. On the base commit (before your branch's changes), from `web/`: `rm -rf .next && npx next build 2>&1 | tee /tmp/next-build-before.txt && du -sk .next/static | tee /tmp/next-static-before.txt`.
2. On your branch after all edits: `rm -rf .next && npx next build 2>&1 | tee /tmp/next-build-after.txt && du -sk .next/static | tee /tmp/next-static-after.txt`.
3. Compare: the `/[ticker]` route's **First Load JS** in the build output route table (after ≤ before) and `du -sk .next/static` (after ≤ before). Expected: a small drop (OrderTab shrinks; two web modules + six unified files removed). **Acceptance = non-increase; a measurable drop is the goal.** (Chunk hashing makes `du` only directional — the route-table First Load JS line is the authoritative number; paste both into the PR.)

### E2E browser (MANDATORY — place order still works end-to-end)

Order-path is UI-visible; a place-order flow must be re-proven. **PAPER only** — never live money.

1. Start the paper stack: `scripts/infra/dev.sh paper` (Next :3200, FastAPI :8421, IB paper on `127.0.0.1:4002`). Confirm `curl -s http://localhost:8421/health | jq .ib_gateway.port_listening` → `true`.
2. Run the existing place-order specs against the running stack:
   `cd web && npx playwright test e2e/order-combo.spec.ts e2e/position-order-button.spec.ts e2e/iwm-close-order-summary.spec.ts` → all pass.
3. Manual chrome-cdp confirmation (single-leg + combo), captured as a screenshot:
   - Navigate to a ticker with a position, open the **Order** tab.
   - Single-leg: set Action=BUY, a valid qty + limit, click **Place Order** → **Place Order (confirm)** → assert the on-screen success text `Order placed:` appears and no console error. Screenshot → `output/playwright/p2-1-single-place-2026-07-05.png`.
   - Combo: on a multi-leg position, submit a defined-risk combo → assert `Combo order placed:` success text. Screenshot → `output/playwright/p2-1-combo-place-2026-07-05.png`.
   - Assert the error path still renders: force a rejection (e.g. invalid price) → `OrderErrorBanner` shows the mapped reason. (Proves the collapsed `if (!res.ok)` branch still surfaces errors.)

### Negative / regression checks

| ID    | Check                                                                                                                            | Expected                                                        |
| ----- | -------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| Neg-1 | Error branch unaffected — a `!res.ok` response still calls `setError(errorFromResponseBody(...))` and `attemptId.markTerminal()` | verified by E2E step 3 error path + `order-reliability.test.ts` |
| Neg-2 | `mark_submitted` still transitions `order_submissions` → WORKING                                                                 | `test_orders_submissions_store.py` green                        |
| Neg-3 | `regime_overrides` Table + FK + migrations still present                                                                         | Py-Grep-2 non-empty; `uv run alembic heads` unchanged           |

**No migration is created or run in this PR** — the table is untouched, so the Migration-checks row of the brief is N/A. State this explicitly in the PR description.

---

## Tripwires / abort criteria

- **If any Web-Grep or Py-Grep in the matrix is non-empty after your edits**, you missed a deletion or a symbol is actually live — STOP, locate the reference, and either delete it (if in a listed file) or, if it's an unexpected live caller, STOP and report (the finding's premise that it's dead would be wrong).
- **If `npx tsc --noEmit` reports an "unused" or "cannot find name" error naming a regime/unified symbol**, a partial deletion happened — finish removing that symbol's references; do not silence with `eslint-disable`.
- **If a place-order Vitest or the E2E place flow FAILS**, the deletion changed behavior — this PR must be zero-behavior-change. Revert the offending edit and re-diagnose. Do not "fix" a test to make it pass.
- **If Step B0's grep shows any Part B symbol with `app-uses > 0`**, drop that symbol from Part B (it is alive). If ALL Part B symbols turn out live, skip Part B entirely and ship Part A alone.
- **If more than the files listed here need edits** (Part A: `regimeGate.ts`, `RegimeBlockModal.tsx`, `OrderTab.tsx`, `orders_store.py`, `server.py`, `queries/regime_overrides.py`; Part B: the 6 unified files + `index.ts` + `placeOrderContract.ts` + the 2 test files), STOP and report — an unexpected coupling exists.
- **If any step requires live IB**, use PAPER (`scripts/infra/dev.sh paper`, port 4002) — never live.
- **If S4 has not yet merged and you cannot match a `handlePlace` success-branch `old_string`**, re-read the file at HEAD and match the real text; the snippets above are HEAD-at-authoring and may have drifted.

---

## Rollback

- Pure branch work: `git checkout master && git branch -D chore/p2-1-delete-regime-dead-code` discards everything.
- No schema/migration change → no down-revision needed.
- If a regression surfaces post-merge, `git revert` the merge commit; the deletion is self-contained (no data or schema dependency), so revert is clean.

---

## Incident-history row

This is a dead-code deletion with **no order-path behavior change**, so no new bug is introduced. Per `docs/reference/order-path-incident-history.md` convention (rows document _fixes to bugs_), **no row is required.** If the project prefers to log the plumbing removal for traceability, append:

| N | 2026-07-05 chore/p2-1-delete-regime-dead-code | N/A (no bug) — dead-code removal | Regime gate removed server-side in #104 left ~500 ln of unreachable client retry logic (`regimeGate.ts`, `RegimeBlockModal` stub, `retryRegimeOrder`/`regimePrompt` ×2 in `OrderTab.tsx`) and a dead `regime_overrides` write path (`orders_store.reserve_attempt` override block + `mark_submitted` update + unimported `queries/regime_overrides.py`) — the backend never emits `REGIME_BLOCK`/`REGIME_RESIZE_REQUIRED` nor inserts an override row. | Deleted the client regime files + both `OrderTab` copies; removed the `override_audit` param/write block and the no-op `mark_submitted` `regime_overrides` UPDATE; deleted the unimported query module. **Table/FK/migrations retained** (schema territory, P2.2). Zero behavior change. | Grep-clean matrix + place-order Vitest + Playwright `order-combo`/`position-order-button` + `test_orders_submissions_store.py`. **Watch pattern:** when a server-side capability is removed, sweep the client retry/override plumbing and any DB write path it fed — a null-returning stub modal is a smell that a whole branch is dead. |
