# VCG-R + CRI Rewiring — Phase 0 Audit

**Date:** 2026-04-29
**Scope:** Code-anchored audit to enumerate the pre-existing scheduler topology, every order-entry call site, the canonical hedge structure set, and remaining `data/cri.json` readers — input for Phase 0–3 of `docs/superpowers/plans/2026-04-29-vcg-cri-strategies-rewiring.md`.

This audit also surfaces three spec/plan inaccuracies that the implementation will need to handle. Per the prompt's "codebase wins" rule, those are flagged in § 5; the working code, not the planned code, is authoritative.

---

## 1. Pre-existing scheduler

**No `_vcg_cri_scan_loop` exists today.** No scheduled CRI scanner. No scheduled VCG scanner.

Scanner triggers today:

| Scanner | Entry point                            | Cooldown             | PG write target                                                                                                             | Notes                                                                     |
| ------- | -------------------------------------- | -------------------- | --------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| VCG     | `POST /vcg/scan` (`server.py:2676`)    | 60 s in-process lock | `vcg_series` (via `save_vcg_scan` at `server.py:621`) **and** `scan_results` (via `_write_scan_to_postgres("vcg.json", …)`) | Working as Phase-1-spec'd.                                                |
| CRI     | `POST /regime/scan` (`server.py:2596`) | None                 | `scan_results` only (via `_write_scan_to_postgres("cri.json", …)` at `server.py:2605`)                                      | **`cri_series` is never written.** This is the gap Phase 0.3 + 0.4 close. |

`GET /regime` (`server.py:2589`) reads `_load_latest_scan_payload("cri")`, which queries the generic `scan_results` table — not `cri_series`. After Phase 0, both tables will be populated by every CRI scan; the Phase 1 `regime_state` view reads `cri_series` for tier classification, and `GET /regime` continues reading `scan_results` for the legacy UI shape.

UW-daily worker (the pattern the spec § 4.1 told us to mirror): lives at `server.py:397` (`uw_daily_task = asyncio.create_task(uw_daily_run_loop(...))`). The implementation in `src/xenon/api/services/uw_analyze_daily_job.py` (285 lines) has **no advisory lock or any multi-worker guard** — see § 5 finding #1.

## 2. Order entry-point allowlist

Inputs to Phase 3 task "wire `RegimeGate.veto` into every order entry point". The first column is **whether the gate runs** under spec § 4.6.

| Gate? | Symbol / route             | File                                          | Line                   | Notes                                                                                                                                                                                           |
| ----- | -------------------------- | --------------------------------------------- | ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ✅    | `POST /orders/place`       | `src/xenon/api/server.py`                     | 2099                   | HTTP route; calls `_orders_place_from_body`.                                                                                                                                                    |
| ✅    | `_orders_place_from_body`  | `src/xenon/api/server.py`                     | 2123                   | In-process helper; called by `/orders/place` and `submit_combo`. **Gate must run inside this helper** so both HTTP and in-process callers are covered (per `feedback_in_process_route_bypass`). |
| ✅    | `submit_combo`             | `src/xenon/execution/combo_wizard/session.py` | 293 (call site at 327) | Wizard combo-submit; calls `_orders_place_from_body`. Gating inside `_orders_place_from_body` covers this transitively.                                                                         |
| ⚠️    | `POST /orders/modify`      | `src/xenon/api/server.py`                     | 2421                   | Conditional per spec § 4.6.1: pure-price + quantity-decrease bypass; quantity-increase/side-change/replacement run through gate (delta-order for quantity-increase).                            |
| ⚠️    | `_orders_modify_from_body` | `src/xenon/api/server.py`                     | 2439                   | Same conditional rule as the route.                                                                                                                                                             |
| ❌    | `POST /orders/cancel`      | `src/xenon/api/server.py`                     | 2354                   | Cancels never gated (way out of a regime).                                                                                                                                                      |
| ❌    | `POST /orders/refresh`     | `src/xenon/api/server.py`                     | 1606                   | Read-only.                                                                                                                                                                                      |
| ❌    | Wizard reprice             | `src/xenon/execution/combo_wizard/session.py` | 398                    | Calls `_orders_modify_from_body` with price-only changes by construction; modify-route conditional rules cover the bypass.                                                                      |

**`ib_place_order` direct callers** (per `scripts/checks/order_path_caller_allowlist.py:41`): only `src/xenon/execution/ib_place_order.py` itself. All other order entry runs through the FastAPI helpers above. The Phase 3 CI guard (`scripts/checks/order_path_regime_gate_called.py`) gets a small allowlist: `_orders_place_from_body` and `_orders_modify_from_body` are the canonical gate sites; routes delegating to them via `return await …` are covered transitively.

## 3. Canonical hedge structure set (input to `_is_hedge`)

Distilled from `docs/trading/strategy-vcg.md` (line 280) and `docs/trading/strategies.md` (lines 16, 586, 920, 926).

**Hedge underlyings:**

- Equity-index: `SPX`, `SPY`
- Credit: `HYG`, `JNK`, `LQD`
- Vol: `VIX`

**Hedge structure shapes (defined-risk only):**

- Long put (single): SPX, SPY, HYG, JNK, LQD
- Long call (single): VIX only
- Debit put spread: SPX, SPY, HYG, JNK, LQD (matching `options-structures.json` `Long Put Spread (Debit)` at line 1630)
- Debit call spread: VIX only (matching `Long Call Spread (Debit)` at line 1564)
- Long put butterfly: SPX, SPY (defined-risk wing protection; matches `Long Put Butterfly` at line 612)

**Predicate, suitable for `_is_hedge` Python literal:**

```python
_HEDGE_UNDERLYINGS_LONG_PUTS = {"SPX", "SPY", "HYG", "JNK", "LQD"}
_HEDGE_UNDERLYINGS_VIX_CALLS = {"VIX"}
_HEDGE_UNDERLYINGS_PUT_BFLY = {"SPX", "SPY"}

# Order qualifies as hedge iff:
#   - structure ∈ {long_put_single, long_put_debit_vertical, long_put_butterfly}
#     and underlying ∈ _HEDGE_UNDERLYINGS_LONG_PUTS, OR
#   - structure ∈ {long_call_single, long_call_debit_vertical}
#     and underlying ∈ _HEDGE_UNDERLYINGS_VIX_CALLS
# Naked/short structures on hedge underlyings (e.g. naked HYG short call) DO NOT qualify.
# Multi-leg fall-through: structural classification wins (spec § 4.5 clause).
```

For the structural classifier, reuse `docs/trading/options-structures.json` codes:

- `long_put_single`, `long_call_single`
- `bull_put_spread` (credit; **not a hedge** — buyer of risk)
- `bear_put_spread` (debit; **hedge** when underlying ∈ put-hedge set)
- `bull_call_spread` (debit; hedge when underlying = VIX)
- `long_put_butterfly` (hedge when underlying ∈ {SPX, SPY})

The exact mapping from `PreflightRequest` to a structure code lives in existing classifier logic (called by the wizard / portfolio surfaces); Phase 3 task 3.2 wires `_is_hedge` to that classifier rather than re-deriving structure-detection here.

## 4. Stale `data/cri.json` consumers

Comprehensive grep across `web/`, `src/`, `scripts/` for `data/cri.json`, `data/cri_scheduled`, and `cri.json`:

| File                                             | Line(s)     | Status                                                                                                                                                                                             | Action                                                                                                                                                                                |
| ------------------------------------------------ | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `web/app/api/regime/route.ts`                    | —           | **Already off file reads.** Proxies `xenonFetch('/regime')` and `/regime/scan`.                                                                                                                    | **No Phase 0 action.**                                                                                                                                                                |
| `web/app/api/internals/route.ts`                 | 13, 480–519 | Reads `data/cri.json` + `data/cri_scheduled/cri-*.json` for the **Market Internals** page (different surface — not the regime banner).                                                             | **Out of Phase 0 scope.** Backlog candidate; not gated by this spec.                                                                                                                  |
| `web/components/RegimePanel.tsx`                 | 245         | `computeCri()` from `@/lib/criCalc` — **display-only live overlay** (live VIX/VVIX/SPY → score) so the panel doesn't wait for the next scan tick.                                                  | **No Phase 0 action.** Spec § 4.9 explicitly allows display-only overlays to remain. Phase 2's binding tier comes from `/api/regime` server-side; the overlay never feeds it.         |
| `src/xenon/scanners/trend/cli.py`                | 411–420     | Reads `data/cri.json` for `market_context.regime`.                                                                                                                                                 | **Leave alone.** Trend scanner is **DEPRECATED** per root `CLAUDE.md` ("Code retained for repurposing; R2/ta_lib data source removed 2026-04-26. Scheduler removed from server.py."). |
| `src/xenon/scanners/repair_cri_rvol_cache.py`    | 44, 387     | Manual repair tool; one-off invocation.                                                                                                                                                            | Leave alone. Not a runtime read.                                                                                                                                                      |
| `src/xenon/api/server.py`                        | 594, 2605   | `_SCAN_TYPE_MAP["cri.json"] = "cri"` + `_write_scan_to_postgres("cri.json", …)`. The `"cri.json"` string is just a filename label keyed into the scan-type map for archival — **not a file read**. | No action.                                                                                                                                                                            |
| `src/xenon/shares/generate_regime_share.py`      | 34, 59–73   | Reads `data/cri.json` for share-card generation.                                                                                                                                                   | **Backlog candidate.** Not on the order path; not gated by this spec. Migrating it is a clean follow-up once `cri_series` has soaked.                                                 |
| `scripts/migrations/migrate_to_postgres.py`      | 429         | One-shot migration script (already shipped in PR #65).                                                                                                                                             | Leave alone.                                                                                                                                                                          |
| `web/tests/regime-corrupt-cache.test.ts`         | 9, 99, 117  | Test of legacy file fallback in `internals/route.ts`.                                                                                                                                              | Leave alone — tied to `/api/internals`, retires when that route migrates.                                                                                                             |
| `web/tests/cri-cache-selection.test.ts`          | 46, 69      | Test fixture path reference.                                                                                                                                                                       | Leave alone.                                                                                                                                                                          |
| `web/e2e/regime-cor1m-live-route.spec.ts`        | 7           | E2E test reading `data/cri.json` directly.                                                                                                                                                         | Leave alone.                                                                                                                                                                          |
| `web/e2e/regime-rvol-history-live-route.spec.ts` | 6           | Same.                                                                                                                                                                                              | Leave alone.                                                                                                                                                                          |
| `scripts/tests/test_dual_write_removal.py`       | 14          | Asserts `_write_cache(DATA_DIR / "cri.json"` does NOT appear in code (regression test for prior cleanup).                                                                                          | Leave alone — guarding existing PG-only invariant.                                                                                                                                    |

**Net Phase 0 web rewrite scope:** **None.** `web/app/api/regime/route.ts` already proxies FastAPI; `RegimePanel.tsx`'s overlay is allowed by spec. Phase 0.6 reduces to documenting that the rewrite was already shipped in an earlier PR and locking it in via a CI guard. See § 5 finding #2.

## 5. Open spec questions surfaced by the audit

1. **UW-daily has no advisory lock to refactor.** Spec § 4.1 / Phase 0.5 says "mirroring the UW daily job pattern (`server.py:335`)" and "refactor existing UW-daily worker guard at server.py:335 to use it". The actual UW-daily worker (`uw_analyze_daily_job.py`, 285 lines) has **zero multi-worker guard** today. Phase 0.5 should:
   - **Add** `src/xenon/api/services/advisory_lock.py` (`pg_try_advisory_lock` async context manager).
   - **Apply** it to `uw_daily_task` startup at `server.py:397` (new guard, not a refactor).
   - **Apply** it to the future VCG/CRI loop in Phase 4.
     The plan's "refactor" phrasing should read "introduce" instead. No design impact — only the diff size of Phase 0.5 changes (slightly smaller).

2. **Phase 0.6 is mostly a no-op.** Spec § 4.9 / Phase 0.6 claims `web/app/api/regime/route.ts` reads `data/cri.json`. It does not — it already proxies `xenonFetch('/regime')`. Concrete change: Phase 0.6 should be reduced to landing the CI guard from spec § 7.5 ("`/api/regime` no file reads") **early**, in Phase 0 instead of Phase 3, so the existing PG-only behavior is locked in before later phases start adding to the route. Drop the route rewrite from Phase 0.6; keep the CI guard.

3. **`web/app/api/internals/route.ts` is the real `data/cri.json` reader.** Out of Phase 0 scope (different surface, not the regime banner). Open backlog item: migrate Market Internals off `data/cri.json` once a Postgres canonical exists. Not blocking this work.

4. **`POST /regime/scan` has no cooldown** today (unlike VCG's 60-s lock at `server.py:2685`). Phase 0.4 introduces `cri.persist()` and could add a cooldown trivially, but the spec does not require one. **Defer.** If the consolidated Phase 4 loop hammers `POST /regime/scan` from outside (it shouldn't — the loop calls the scanner directly, not the route), revisit.

5. **`docs/todo-backlog.md` § 7 framing.** No change needed to the backlog item itself; the Phase 0 work corrects the design's assumptions and proceeds. Phase 5 closes out the backlog entry.

---

## Action items derived from this audit (for Phase 0 implementation)

- [ ] **0.2** CRI scanner: emit `crash_trigger.fired` (bool) + `cta.forced_reduction` (bool) — cri.py § "crash_trigger payload construction" and § "cta payload construction".
- [ ] **0.3** `xenon.scanners.cri.persist(payload, *, conn)` — `INSERT … ON CONFLICT (recorded_date) DO NOTHING` against `cri_series`.
- [ ] **0.4** Wire `cri.persist()` into `POST /regime/scan` at `server.py:2605`. Keep the existing `_write_scan_to_postgres("cri.json", …)` archive call so `GET /regime` keeps working until Phase 5.
- [ ] **0.5** New `src/xenon/api/services/advisory_lock.py`. Apply to `server.py:397` UW-daily startup as a _new_ guard. (Phase 4 reuses the helper for the VCG/CRI loop.)
- [ ] **0.6** Replace plan task with: "Land the `/api/regime` no-file-reads CI guard early." Skip the route rewrite — already done.
- [ ] **0.1** This audit doc — committed at the start of Phase 0.
