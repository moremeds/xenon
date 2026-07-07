# 6. Complexity, Duplication & Reuse

## 6.1 Validation / risk-gate map (Part 5)

Order of encounter for a SELL order, with duplication classification:

| #   | Gate                                                                | Layer / location                                                                                   | Server re-check?                              | Classification                                                                    |
| --- | ------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- | --------------------------------------------- | --------------------------------------------------------------------------------- |
| 1   | Numeric validity (qty>0, price sign)                                | React inline (`OrderTab.tsx:412-418,903-907`)                                                      | Yes (#5, #6)                                  | UI convenience — fine                                                             |
| 2   | Naked-short guard (memo + submit-time re-check)                     | `web/lib/nakedShortGuard.ts:165-289` via `OrderTab.tsx:439-485,909-967`                            | Yes (#7)                                      | **Dangerous semantic duplication** — third implementation of coverage math (CX-1) |
| 3   | TypeBox structural schema                                           | Next route (`placeOrderBodySchema.ts:26-72`)                                                       | FastAPI re-parses                             | Appropriate defensive                                                             |
| 4   | Business rules (required fields, ≥2 legs, price-sign-by-type)       | Next route (`place/route.ts:110-188`)                                                              | Partially (preflight covers different ground) | UI convenience, ad hoc (the dead `useOrderValidation` was meant to own this)      |
| 5   | Read-only + broker capability                                       | FastAPI (`guards.py:30-48`; `server.py:2140-2149`)                                                 | — authoritative                               | Single source ✓                                                                   |
| 6   | Gate-4 preflight (coverage, universe, snapshot staleness)           | FastAPI → `preflight.py:273-465`                                                                   | — authoritative                               | **Second** implementation of coverage math                                        |
| 7   | Quote gate (freshness ≤500 ms, tick grid, limit band, market hours) | FastAPI (`quote_guard.py:43-137`, non-combo only, OP-17)                                           | — authoritative                               | Single source ✓ (no client duplicate)                                             |
| 8   | Idempotency reservation                                             | FastAPI → `orders_store.reserve_attempt`                                                           | — authoritative                               | Single source ✓                                                                   |
| 9   | Modify monotonic sequence                                           | FastAPI pre-subprocess (`server.py:2530-2588`)                                                     | — authoritative                               | Single source ✓                                                                   |
| 10  | Post-sync naked-short audit                                         | `naked_short_audit.py:122-242` (cancels violators)                                                 | —                                             | **Third** implementation of coverage math; also bypasses state sync (OP-5)        |
| 11  | Regime gate                                                         | Server: **deleted** (PR #104). Client: ~200 ln retry logic ×2 forms + `regimeGate.ts` + stub modal | n/a                                           | **Obsolete duplication** — delete (CX-3)                                          |
| 12  | Reason-code → toast table                                           | `orderReasonCodes.ts` mirrored by hand against Python `ReasonCode`                                 | n/a                                           | Dangerous semantic duplication (drift = wrong toasts; past incident class)        |
| 13  | Tick size                                                           | Server stub returns 0.01 always (`server.py:1807-1836`); IB error 110 remapped to `LIMIT_OFF_TICK` | by design                                     | Deliberate delegation to broker — keep (documented)                               |

**Routes that bypass another route's validation (Part 5 attention list):**

- `_orders_*_from_body` helpers: in-process callers skip FastAPI `Depends` — a known
  incident class; currently mitigated by the caller-allowlist CI guard for
  `ib_place_order` only.
- `submit_combo` / wizard: goes through the same place body path (validated) — no bypass
  found this review beyond the wizard's own session machine.
- **`ib_execute.py`** — the real bypass: full place+persist outside preflight/quote
  gate/idempotency, invisible to the allowlist guard (OP-14).
- **Next.js combo replace** re-implements orchestration (cancel+place) that no FastAPI
  route owns (OP-6).
- **Test mode** (`XENON_API_TEST_MODE`) short-circuits before the subprocess — the reason
  the real place path is untested (TS-1) and the pattern that hid incident #22.

**One authoritative rule path without a slower UI:** keep the TS guard purely advisory
(it's what makes the submit button responsive), make Python `preflight.py` the single
implementation of coverage math, and hold the two in parity by _fixtures, not comments_:
export a JSON case table from the Python tests (the `gate4_parity.json` fixture already
exists — extend it) and run the same table through the TS guard in Vitest. Same for reason
codes: generate `orderReasonCodes.ts` from the Python `ReasonCode` enum (a 20-line codegen
script in `scripts/`), or at minimum a parity test that imports both lists.

## 6.2 Module/function complexity (Part 6) — evidence-based

| Candidate                                                                                                                            | Evidence                                             | Verdict                                                                                                                                                                                          |
| ------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `server.py` (3,248 ln, 25 endpoints, 132 defs; lifespan + inline Futu/portfolio/order services)                                      | Responsibility scan in backend audit                 | Extract continuation is already the team's stated direction (`routes/`, `services/` exist). Not line count — the problem is that order-path changes and Futu-sync changes collide in one module. |
| `_orders_place_from_body` (~193 ln, 7 responsibilities, 4-level nesting)                                                             | `server.py:2139-2332`                                | **Highest-value extraction.** See below.                                                                                                                                                         |
| `OrderTab.tsx` (1,731 ln; 31 hook sites; two forms with copy-pasted regime/naked-short blocks; wizard integration + P&L math inline) | frontend audit §7                                    | Delete regime code first (-~200 ln, zero risk — it's unreachable); then extract shared `useOrderSubmit`-style hook the abandoned unification already designed.                                   |
| `ib_realtime_server.js` (2,256 ln, ≥5 concerns, duplicated batching for L1 vs depth/tape)                                            | quote audit §11                                      | Extract batched-channel + subscription-registry cores (pure, unit-testable, pattern already proven by `ib_tick_handler.js`).                                                                     |
| `usePrices.ts` (1,116 ln) + `IBStatusContext.tsx` duplicate WS machinery                                                             | `usePrices.ts:114-115` = `IBStatusContext.tsx:55-56` | Extract one `useRelaySocket` core; two consumers.                                                                                                                                                |
| `orders_store.py` (902 ln)                                                                                                           | state-machine audit                                  | Fine internally; the gap is discipline (expected_states) not size. Add the `transition()` chokepoint rather than splitting the file.                                                             |
| `ib_client.py` (32.6 KB)                                                                                                             | backend audit                                        | Broad but coherent (connection + contract helpers); silent `except: pass` at `:573-575` should be fixed; no split needed.                                                                        |
| `cancel_order`/`modify_order` twin poll-loops                                                                                        | `ib_order_manage.py:184-277,280-447`                 | Extract `confirm_with_poll()` helper — small, real dedup.                                                                                                                                        |

### Extraction spec — `_orders_place_from_body`

1. **Current responsibility separated:** gate-running vs reservation vs subprocess-result
   interpretation vs persistence.
2. **New boundary:** `services/order_place.py` with `run_gates(body, scope) ->
GateVerdict`, `submit_reserved(submission, body) -> PlaceOutcome` (subprocess +
   classification), and `persist_outcome(submission, outcome)` (the only writer,
   using the `transition()` chokepoint).
3. **Dependency direction:** route → service → (orders_store, subprocess runner). The
   route keeps only HTTP concerns; nothing new imports the route.
4. **Protecting tests:** existing `test_idempotency_route` / `test_place_quote_gate` /
   `test_preflight_route` pin the gate behavior; add the fake-CLI route tests (TS-1) to
   pin `submit_reserved` before extracting.
5. **Cognitive win, not code motion:** yes — the ambiguous-ack fix (OP-1) has to modify
   exactly `submit_reserved` + `persist_outcome`; today it would be threaded through a
   193-line function that also does auth and quotes.

### Web dead code to delete (all confirmed unreachable)

- Regime: `regimeGate.ts`, `RegimeBlockModal.tsx` stub, both regime blocks in
  `OrderTab.tsx`, plus the vestigial `regime_overrides` write plumbing in Python
  (`override_audit=None` hardcoded, `server.py:2166`).
- Unified-order-system remnants: `useOrderPrices`, `useOrderValidation`,
  `OrderPriceButtons`, `OrderActionToggle`, `OrderQuantityInput`, `OrderPriceInput`
  (zero non-test consumers) — either finish the unification (preferred if OrderTab is
  being refactored anyway) or delete and update `ORDER_SYSTEM_ANALYSIS.md`.
- Python: `pool_order_manage` import in `server.py:55` (unused) — keep the module (it's
  the Option-B seed) but drop the dead import; fix `ib_pool.py:26-31` docstring.

### Combo net-price math — 3 implementations → 1

`computeNetOptionQuote()` (`optionsChainUtils.ts`), `ComboOrderForm.netPrices`
(`OrderTab.tsx:862-899`), `resolveOrderPriceData()` BAG branch (`ModifyOrderModal.tsx:141-265`).
All implement the cross-field natural-market algorithm `web/CLAUDE.md` documents. Keep
`computeNetOptionQuote` as the single export; the other two become call sites. Protected by
the existing `order-reliability.test.ts` net-price cases plus new fixture cases for the
modal's BID/MID/ASK triple.

## 6.3 Ranked refactoring candidates (value ÷ risk)

1. Delete regime dead code (web + vestigial Python) — pure win, zero behavior change.
2. `transition()` chokepoint + state CHECK + expected_states everywhere (enables OP-1 fix).
3. Decompose `_orders_place_from_body` per spec above.
4. Coverage-math parity fixtures + reason-code codegen (CX-1).
5. Combo net-price single implementation.
6. Relay batched-channel + subscription-registry extraction with tests.
7. `useRelaySocket` shared core for usePrices/IBStatusContext.
8. `confirm_with_poll()` in ib_order_manage.
9. OrderTab two-form shared submit hook (do together with #1).
10. Continue `server.py` → `routes/`+`services/` migration opportunistically (don't big-bang).
