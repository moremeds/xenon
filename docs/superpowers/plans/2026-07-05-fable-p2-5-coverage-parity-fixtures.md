# P2.5 — Coverage-math parity fixtures + reason-code codegen

- **Date:** 2026-07-05
- **Proposed branch:** `feat/naked-short-parity-fixtures`
- **Finding:** CX-1 (Duplication, **High**) — `docs/fable/03-findings-table.md` row CX-1; roadmap `docs/fable/10-roadmap.md` §P2.5
- **Goal (one line):** Lock the naked-short/coverage implementations together with a single checked-in JSON case table carrying a **per-implementation applicability + expected-verdict matrix**, driven by one Python parity test and one Vitest parity test, and replace the hand-maintained reason-code mirror with codegen — **without changing any guard behavior.**

---

## ⚠️ DIVERGENCES FOUND DURING PLANNING (read before executing)

Authoring the fixture table already did its job: tracing every case through all four coverage surfaces at HEAD `fb5b6d8` exposed **two real behavioral divergences** plus one input-shape divergence. All three are **fail-closed** (no naked exposure can slip through because of them) — **no BLOCKER**. They are encoded in the fixtures as _documented reality_ via `divergence` fields; the parity tests assert what the code actually does today, not a false uniformity. **Closing any divergence later means flipping that fixture's per-implementation expectation in the same PR as the code fix.**

**Non-negotiable framing (read this before touching anything):**

1. **Fixtures encode CURRENT behavior first.** Every `expected` verdict is what the code at HEAD `fb5b6d8` actually returns — hand-traced (D1 additionally re-derived below from `combo_uncovered_short_call_ratio`). The parity tests **document** the divergences; they do **not** silently change guard behavior. This PR ships **zero** guard-logic edits.
2. **Resolving each divergence is a separate, operator-gated decision.** Which side of D1/D2/D3 is "correct" is a judgment call, not a mechanical fix — see the **Operator decisions required** subsection below. This plan does not pick winners; it makes the disagreement visible and testable.

### D1 (tribunal M1) — SELL-envelope closing combos: TS allows, Python blocks

- **TS** `checkNakedShortRisk` (`web/lib/nakedShortGuard.ts`, combo branch): `if (order.action === "SELL") return { allowed: true };` — blanket allow for closing combos.
- **Python** `evaluate_combo` (`src/xenon/execution/preflight.py`): has **no envelope-action check at all** — it computes `combo_uncovered_short_call_ratio` from legs regardless of `req.action` and blocks. Verified by live trace at plan time: SPY SELL-envelope {SELL C, BUY P}, empty portfolio → `accept=False, ETF_CALL_UNCOVERED`.
- **Bonus doc bug:** `evaluate_combo`'s own docstring claims "Closing combo envelopes (SELL) reduce exposure and are allowed after universe validation" — the code does not implement that sentence.
- **Direction / risk:** UI allows → API blocks → user gets a 403 toast on a legitimate close attempt. **UX friction, fail-closed, LOW.** No naked exposure passes. Which side errs is a judgment call for the operator: the docstring + TS say "allow closes"; the code (leg-level distrust of the envelope, per the CLAUDE.md combo guardrails) says "block". Python (the API gate) is authoritative today.
- **Encoded in fixture:** case `C7_closing_combo_sell_envelope` (ts_guard `blocked:false`, py_preflight `blocked:true`).

### D2 (tribunal M2) — same-expiry long-call cover: TS `auditOpenOrders` lacks the vertical-spread branch

- **Python audit** `find_naked_short_violations` (`src/xenon/execution/naked_short_audit.py`) step 2 uses `_count_long_calls_at_expiry` → a short call covered by a same-expiry long call (different strike) is **not** a violation.
- **TS** `auditOpenOrders` (`web/lib/nakedShortGuard.ts`) only checks exact-match `countMatchingLongOptionContracts` then falls to shares — **no same-expiry branch** → it **flags** the covered vertical (false positive).
- **Mitigating fact (verified):** `auditOpenOrders` has **zero runtime callers** — grep of `web/` finds only the export and its tests. It is dormant code; the audit that actually cancels orders is the Python one. **False positive, fail-closed, LOW** (and currently unreachable in production).
- **Encoded in fixture:** cases `SL11` and `SL18` (py_audit `blocked:false`, ts_audit `blocked:true`).

### D3 (drift, out of scope) — Python audit reads position-level stock, others walk legs

`naked_short_audit._get_stock_shares` counts shares only from positions with `structure_type == "Stock"` (position-level `contracts`), while TS `countLongShares` and preflight `_count_long_shares` walk `legs` for `type=="Stock" && direction=="LONG"`. Stock nested inside a `structure_type:"Covered Call"` position is invisible to the Python audit → potential false-positive **cancel** of a genuinely covered short call. Fail-closed, but live-reachable. **Not fixed and not fixture-encoded here** (fixtures deliberately hold stock only as separate `Stock` positions — the shape all four surfaces read identically); needs its own investigation of the snapshot producer's real shape.

### Operator decisions required

These are **not** executor tasks. The executor's only job for D1–D3 is Step 8 (file backlog one-liners) — it must not pick a resolution. Each item below is a flagged decision for the operator to make in a **separate, future PR**; that PR flips the corresponding fixture expectation in lockstep with the code change.

| ID  | Decision the operator must make                                                                                                                                                     | Believed-correct side (author's read, LOW confidence — operator owns the call)                                                                                                                                                                                                                                                                                                                                                                  | Fixture that flips when resolved                          |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| D1  | Should a SELL-envelope closing combo whose legs still net an uncovered short call be **allowed** (TS behavior + `evaluate_combo` docstring) or **blocked** (`evaluate_combo` code)? | **Block** — the CLAUDE.md combo guardrail says the BAG envelope action is untrusted; `combo_uncovered_short_call_ratio`'s own docstring (`preflight.py` L145–153) codifies that. Python (the API gate) is authoritative today. The `evaluate_combo` docstring (L282–283) is the doc bug and should be corrected to match, OR a genuine-close check added. TS's blanket `if (order.action === "SELL") return {allowed:true}` is the weaker side. | `combo/C7` (`ts_guard.blocked`)                           |
| D2  | Should `auditOpenOrders` gain the same-expiry long-call (vertical) cover branch, or should it be **deleted**?                                                                       | **Delete `auditOpenOrders`** — it has zero runtime callers (grep: export + tests only); the Python `find_naked_short_violations` is the surface that actually cancels. Adding the branch keeps dead code alive; ponytail says cut it. Either way `py_audit` stays authoritative.                                                                                                                                                                | `single_leg/SL11`, `single_leg/SL18` (`ts_audit.blocked`) |
| D3  | Should `naked_short_audit._get_stock_shares` walk `legs` (like TS/preflight) instead of reading position-level `structure_type=="Stock"`?                                           | **Yes, but investigate first** — this one is live-reachable and can false-positive-**cancel** a covered short call whose stock is nested in a `Covered Call` position. Needs a look at the snapshot producer's real shape before changing. Not fixture-encoded here (fixtures hold stock only as separate `Stock` positions).                                                                                                                   | (none yet — add nested-cover fixtures in the fix PR)      |

**Executor action for D1–D3:** append three dated one-liners to `docs/todo-backlog.md` Inbox (Step 8). Do **not** change any guard logic in this PR.

---

## Per-implementation applicability + expected-verdict matrix

This is the human-readable rendering of `config/naked-short-parity-cases.json`'s `expected` block — the single source of truth is the JSON; this table must stay consistent with it. **Rows** = fixture scenarios. **Columns** = the three guard implementations, plus the TS audit mirror (column 4) where divergence D2 lives:

- **TS UI guard** = `checkNakedShortRisk` (`web/lib/nakedShortGuard.ts`) — the pre-submission guard.
- **Python API gate** = `evaluate` / `evaluate_combo` (`src/xenon/execution/preflight.py`) — the 403 gate; **authoritative**.
- **Python audit** = `find_naked_short_violations` (`src/xenon/execution/naked_short_audit.py`) — the post-sync canceller.
- **TS audit (mirror)** = `auditOpenOrders` (same file as the TS UI guard) — the dormant TS counterpart of the Python audit; **zero runtime callers**.

Each cell is `APPLIES:BLOCK`, `APPLIES:ALLOW`, or `N/A` (surface does not police this shape). Cells that disagree with the authoritative Python API gate for the same logical shape are marked **⚠️** and footnoted. `(reason)` on the Python API gate column is the emitted `ReasonCode`.

| Scenario                                              | TS UI guard (`checkNakedShortRisk`) | Python API gate (`evaluate`/`evaluate_combo`)      | Python audit (`find_naked_short_violations`) | TS audit mirror (`auditOpenOrders`) |
| ----------------------------------------------------- | ----------------------------------- | -------------------------------------------------- | -------------------------------------------- | ----------------------------------- |
| SL1 buy stock                                         | ALLOW                               | ALLOW                                              | N/A                                          | N/A                                 |
| SL2 buy call                                          | ALLOW                               | ALLOW                                              | N/A                                          | N/A                                 |
| SL3 sell stock, no long shares                        | BLOCK                               | BLOCK (INSUFFICIENT_SHARES)                        | BLOCK                                        | N/A                                 |
| SL4 sell 50 of 100 held shares                        | ALLOW                               | ALLOW                                              | ALLOW                                        | N/A                                 |
| SL5 sell 200, only 100 held                           | BLOCK                               | BLOCK (INSUFFICIENT_SHARES)                        | BLOCK                                        | N/A                                 |
| SL6 sell call, no cover                               | BLOCK                               | BLOCK (ETF_CALL_UNCOVERED)                         | BLOCK                                        | BLOCK                               |
| SL7 covered call (200 shares, sell 1)                 | ALLOW                               | ALLOW                                              | ALLOW                                        | ALLOW                               |
| SL8 short a tail (200 shares, sell 5)                 | BLOCK                               | BLOCK (ETF_CALL_UNCOVERED)                         | BLOCK                                        | BLOCK                               |
| SL9 sell put (cash-secured)                           | ALLOW                               | ALLOW                                              | ALLOW                                        | N/A (puts filtered out)             |
| SL10 stock + existing shorts, exact cover             | ALLOW                               | ALLOW                                              | ALLOW                                        | ALLOW                               |
| **SL11 long call same-expiry vertical cover**         | ALLOW                               | ALLOW                                              | ALLOW                                        | **⚠️ BLOCK** [D2]                   |
| SL12 more shorts than long calls                      | BLOCK                               | BLOCK (ETF_CALL_UNCOVERED)                         | BLOCK                                        | BLOCK                               |
| SL13 long call different expiry (no spread)           | BLOCK                               | BLOCK (ETF_CALL_UNCOVERED)                         | BLOCK                                        | BLOCK                               |
| SL14 sell-to-close exact match                        | ALLOW                               | ALLOW                                              | ALLOW                                        | ALLOW                               |
| SL15 under-covered with existing shorts               | BLOCK                               | BLOCK (ETF_CALL_UNCOVERED)                         | BLOCK                                        | BLOCK                               |
| SL16 exact cover with existing shorts                 | ALLOW                               | ALLOW                                              | ALLOW                                        | ALLOW                               |
| SL17 index sell call, no cover                        | BLOCK                               | BLOCK (INDEX_CALL_UNCOVERED)                       | BLOCK                                        | BLOCK                               |
| **SL18 index sell call, same-expiry long-call cover** | ALLOW                               | ALLOW                                              | ALLOW                                        | **⚠️ BLOCK** [D2]                   |
| C1 bull call spread                                   | ALLOW                               | ALLOW                                              | N/A                                          | N/A                                 |
| C2 short risk reversal, no stock                      | BLOCK                               | BLOCK (ETF_CALL_UNCOVERED)                         | N/A                                          | N/A                                 |
| C3 short risk reversal, stock covers                  | ALLOW                               | ALLOW                                              | N/A                                          | N/A                                 |
| C4 jade lizard                                        | ALLOW                               | ALLOW                                              | N/A                                          | N/A                                 |
| C5 ratio 1×2, no stock                                | BLOCK                               | BLOCK (ETF_CALL_UNCOVERED)                         | N/A                                          | N/A                                 |
| C6 ratio 1×2, stock covers                            | ALLOW                               | ALLOW                                              | N/A                                          | N/A                                 |
| **C7 closing combo, SELL envelope (nets short call)** | **⚠️ ALLOW** [D1]                   | **BLOCK (ETF_CALL_UNCOVERED)** [D1, authoritative] | N/A                                          | N/A                                 |
| C8 index ratio 1×2, uncovered                         | BLOCK                               | BLOCK (INDEX_CALL_UNCOVERED)                       | N/A                                          | N/A                                 |

**⚠️ divergent cells:**

- **[D1] C7** — TS UI guard **allows** (early-return `if (order.action === "SELL") return { allowed: true }`, `nakedShortGuard.ts` `checkNakedShortRisk` combo branch); Python API gate **blocks** because `combo_uncovered_short_call_ratio` counts leg actions and ignores the SELL envelope. **Believed correct: block** (Python is the authoritative gate and matches the "envelope is untrusted" guardrail). Net effect today: UI lets the ticket through, API 403s it → UX friction, fail-closed, LOW. Resolve by deciding envelope-close semantics (D1) and flipping `C7.ts_guard.blocked`.
- **[D2] SL11, SL18** — Python audit **allows** (same-expiry long call = vertical cover, via `_count_long_calls_at_expiry`); TS audit mirror **flags** (no same-expiry branch — `auditOpenOrders` goes exact-match → shares only). **Believed correct: allow** (the covered vertical is genuinely covered; Python audit is the surface that actually cancels). TS audit is dormant (zero runtime callers) so this false-positive is unreachable in production. Resolve by deciding D2 (add branch or delete `auditOpenOrders`) and flipping the two `ts_audit.blocked` cells.

> **Non-divergent rows are true parity** — every non-⚠️ cell across applicable surfaces was hand-traced to agree at HEAD. A red on any of those in Step 2/3 is a **new** divergence → STOP (Tripwire 1).

---

## Context (what exists today, verified at HEAD `fb5b6d8`)

The naked-short / coverage rule has **four** surfaces across three files, kept in sync only by comments:

1. **TS UI guard** — `web/lib/nakedShortGuard.ts`:
   - `checkNakedShortRisk(order, portfolio)` → `{ allowed: boolean; reason?: string }` (pre-submission; single-leg + combo).
   - `auditOpenOrders(orders, portfolio)` → `{orderId, permId, reason}[]` (SELL call options only; **no runtime callers**, see D2).
2. **Python API gate** — `src/xenon/execution/preflight.py`:
   - `evaluate(req: PreflightRequest, portfolio: PortfolioView, ...)` → `Verdict(accept, reason_code, reason_detail)` (single-leg).
   - `evaluate_combo(req: ComboPreflightRequest, portfolio: PortfolioView, ...)` → `Verdict` (combo).
   - Comment at lines 435-439 literally cites `web/lib/nakedShortGuard.ts lines 273-283` as the "parity" mechanism.
3. **Python post-sync audit** — `src/xenon/execution/naked_short_audit.py`:
   - `find_naked_short_violations(orders, positions)` → `[{order_id, perm_id, symbol, reason}]` (single-leg SELL stock + SELL call; BAG skipped; puts skipped).

Reason codes are mirrored by hand: Python `ReasonCode` StrEnum (`preflight.py:24-61`) ↔ TS `ORDER_REASON_CODES` (`web/lib/orderReasonCodes.ts`). Two lockstep tests exist: `web/tests/order-reason-codes.test.ts` (hardcodes a `PYTHON_REASON_CODES` array) and `scripts/tests/test_preflight_reason_codes.py`.

There is an **existing codegen pattern to copy**: `scripts/infra/dev/generate_universe_ts.py` generates `web/lib/universe.ts` from `src/xenon/execution/universe.py`; wired into `web/package.json` `predev`/`prebuild`/`pretest`/`pretypecheck` via the `universe:gen` script, marked `linguist-generated=true` in `.gitattributes`, drift-guarded by `scripts/tests/test_universe_ts_drift.py` (checked-in file must byte-equal `render()`).

**What the executor does NOT need to understand:** IB Gateway wiring, the FastAPI route that calls `evaluate`, RegimeGate `cover_ratio`, the cancel path (`cancel_violations`), or how snapshots are produced. This task adds **tests + a fixture file + a codegen script**; it modifies **one** product source line (adds one missing toast copy string) and **one** test file (swaps a hardcoded array for a generated import). No guard logic changes.

---

## Drift from review

1. **Reason-code drift already exists and the "parity" test is green anyway.** Python `ReasonCode` has 26 members including `PORTFOLIO_SNAPSHOT_STALE` (`preflight.py:56`, emitted live at `src/xenon/api/server.py:1631`). TS `ORDER_REASON_CODES` has only 25 — it is **missing `PORTFOLIO_SNAPSHOT_STALE`**. The lockstep test passes only because its own `PYTHON_REASON_CODES` array _also_ omits the code (both sides hand-maintained → both drifted the same way → false green). Consequence today: a live `PORTFOLIO_SNAPSHOT_STALE` 403 renders the generic fallback toast "Unknown error — see logs." Step 6 adds the missing copy (completing the mirror — **not** a guard behavior change).
2. **Behavioral divergences D1/D2/D3** — see the top section. CX-1's "drift in a safety-critical rule" concern is not hypothetical; it is present at HEAD.

---

## Goal / Non-goals

**Goal:** Single JSON case table (`config/naked-short-parity-cases.json`) with a per-implementation applicability/expected-verdict matrix drives one Python parity test and one Vitest parity test across all four surfaces; known divergences are explicitly documented in the data; reason-code parity becomes codegen-enforced (generated names list + drift test), fixing the latent `PORTFOLIO_SNAPSHOT_STALE` gap.

**Pass criterion:** every case matches its **per-implementation** expected verdict. A red means either (a) a **new** divergence appeared (STOP tripwire) or (b) you are intentionally closing a documented divergence — in which case the fixture expectation flips in the same PR as the code fix.

**Non-goals (explicitly NOT done here — one change, one PR):**

- Consolidating the implementations into one shared module (the risky rewrite `docs/fable/06-complexity-and-reuse.md` and `12-final-verdict.md` advise against). Python stays source of truth; TS stays UX-only, proven equivalent where it claims to be.
- **Closing D1, D2, or D3** — each is a separate finding/PR; the fixtures document them.
- Changing any coverage/guard verdict, threshold, reason string, or the universe/currency/index gating.
- Futu-aware guard (roadmap P3 item 5).
- Touching `combo_close_covered_by_portfolio` (regime-gate helper, not a coverage-verdict path).

---

## Key facts (verified against the working tree)

### V1 universe (gates preflight, NOT the TS guard or the audits) — `src/xenon/execution/universe.py:43-55`

`UNIVERSE` keys = `SPX, NDX, RUT` (index, `is_index=True`) and `SPY, QQQ, IWM, GLD, USO, SIL` (ETF). All multiplier `100`. `is_known(t)` = `t in UNIVERSE`; `is_index(t)` raises `KeyError` for unknown tickers.

**Critical:** `preflight.evaluate`/`evaluate_combo` block any non-universe ticker with `UNIVERSE_UNKNOWN` _before_ coverage math; the TS guard and both audits have **no** universe gate. Therefore **every fixture case uses a universe ticker** (`SPY`/`QQQ` for equity-cover, `SPX` for index) so all surfaces reach the same coverage branch.

### Input shapes (verified)

- **`PreflightRequest`** (`preflight.py:64-79`): `ticker, security_type("STK"|"OPT"), action("BUY"|"SELL"), quantity(>0), right("C"|"P"|None), expiry(str|None), strike(Decimal|None), multiplier=100, limit_price(Decimal), currency="USD", exchange=None`.
- **`ComboPreflightRequest`** (`preflight.py:90-95`): `ticker, action, quantity(>0), multiplier=100, legs=[ComboPreflightLeg]`; `ComboPreflightLeg` (`82-87`): `expiry(str|None), strike(Decimal|None), right("C"|"P"), action("BUY"|"SELL"), ratio(int>0)`.
- **`PortfolioView`** (`preflight.py:119-127`): `positions=[PortfolioPosition], available_funds=Decimal`. `PortfolioPosition` (`105-116`): `ticker, structure_type, direction="LONG", contracts, expiry=None, legs=[PortfolioLeg]`. `PortfolioLeg` (`98-102`): `direction("LONG"|"SHORT"), type("Stock"|"Call"|"Put"), contracts, strike=Decimal("0")`.
- **`Verdict`** (`preflight.py:139-142`): `accept(bool), reason_code(ReasonCode|None), reason_detail(str|None)`.
- **TS `OrderPayload`** (`nakedShortGuard.ts:21-37`): `type("stock"|"option"|"combo"), symbol, action, quantity, limitPrice, expiry?, strike?, right?("C"|"P"), legs?[{expiry,strike,right,action,ratio}]`.
- **TS `NakedShortPortfolio`** (`nakedShortGuard.ts:39-55`): `{positions:[{ticker, structure_type, contracts, direction, expiry?, legs:[{direction, type("Call"|"Put"|"Stock"), contracts, strike(number|null)}]}]}`.
- **TS `NakedShortOpenOrder`** (`nakedShortGuard.ts:57-70`): `{orderId, permId, symbol, action, totalQuantity, contract:{secType, right, strike, expiry, symbol}}`.
- **Python audit `find_naked_short_violations(orders, positions)`** (`naked_short_audit.py:122`): `orders` = list of dicts `{status, action, totalQuantity, orderId, permId, orderRef, contract:{secType, symbol, right, expiry, strike}}`; `positions` = plain dicts of the PortfolioView positions shape. Only `status in {"Submitted","PreSubmitted"}` + `action=="SELL"` checked; `secType=="BAG"` skipped; `right=="P"` skipped.

### Per-surface applicability rules (drive the fixture matrix; verified at HEAD)

| Surface                                        | Inspects                                                                      | Not applicable to        |
| ---------------------------------------------- | ----------------------------------------------------------------------------- | ------------------------ |
| `ts_guard` = `checkNakedShortRisk`             | everything (single-leg + combo)                                               | —                        |
| `py_preflight` = `evaluate` / `evaluate_combo` | everything                                                                    | —                        |
| `py_audit` = `find_naked_short_violations`     | single-leg SELL, secType STK or OPT (puts inspected-but-never-flagged)        | BUY orders; BAG/combos   |
| `ts_audit` = `auditOpenOrders`                 | single-leg SELL, secType OPT, right C **only** (`nakedShortGuard.ts:302-303`) | BUY; stock; puts; combos |

This table resolves tribunal M3: applicability is stored **explicitly per case per surface** in the fixture, not derived by the harnesses.

### Reason codes — Python `ReasonCode` in declaration order (verified via `uv run python`)

```
UNIVERSE_UNKNOWN, INDEX_HAS_NO_STOCK, INSUFFICIENT_SHARES, INSUFFICIENT_CASH,
INDEX_CALL_UNCOVERED, ETF_CALL_UNCOVERED, INVALID_ORDER_BODY, STALE_QUOTE,
OPTION_MARKET_CLOSED, QUOTE_CONTRACT_MISMATCH, QUOTE_UNAVAILABLE, LIMIT_OUT_OF_BAND,
LIMIT_OFF_TICK, ATTEMPT_ID_TERMINAL, IB_CONNECTION, OWNERSHIP, IB_REJECT, MODIFY_STALE,
MODIFY_SEQUENCE_REQUIRED, ORDER_NOT_FOUND, ORDER_IDENTIFIER_REQUIRED,
PORTFOLIO_SNAPSHOT_REQUIRED, PORTFOLIO_SNAPSHOT_STALE, READ_ONLY_BROKER,
PENDING_TIMEOUT, SUBPROCESS_ERROR
```

(26 members; `StrEnum` iteration preserves definition order.)

### Repo invariants that apply

- All Python via `uv run …`. Vitest via `cd web && npm test`.
- Tests use **real tickers** (`SPY`, `QQQ`, `SPX`) at frozen values; no network at runtime. Coverage math ignores `limit_price`; limit values are non-load-bearing placeholders.
- No new JSON read/write on the **order path** — the CI guards scan `web/app/api/`, `web/lib/order/`, `src/xenon/api/`, `src/xenon/execution/`. The fixture lives in `config/` and is read only by **test files** in `scripts/tests/` and `web/tests/` — outside every guarded directory. Verified by running the guards (Verification matrix).
- Pure functions, no DB, no subprocess → **no `@pytest.mark.committed_db`**, no `pg_test_engine`.

---

## Steps (strictly ordered)

### Step 0 — Branch

```bash
cd /Users/chenxi/projects/xenon
git checkout -b feat/naked-short-parity-fixtures
```

### Step 1 — Add the parity fixture case table (checked-in data)

Create **`config/naked-short-parity-cases.json`** with the exact content below.

Schema per case:

- `id`, `desc`
- `portfolio`: `{positions, available_funds}` (shapes above; stock ONLY as separate `structure_type:"Stock"` positions, existing shorts ONLY as separate single-leg `Short Call` positions — per D3)
- `order`: single-leg `{ticker, security_type, action, right, strike, expiry, quantity, multiplier, limit_price}`; combo `{ticker, action, quantity, multiplier, legs:[...]}`
- `expected`: **per-implementation matrix** — keys `ts_guard`, `py_preflight`, `py_audit`, `ts_audit`, each `{applicable: bool, blocked: bool}`; `py_preflight` additionally carries `reason_code` (string or null = not asserted). `applicable:false` entries carry no verdict.
- optional case-level `divergence`: `{status:"known", tracking:"<why + which side is authoritative>"}` — present on `C7`, `SL11`, `SL18`.

```json
{
  "_comment": "Parity fixtures for the naked-short/coverage rule. Single source of truth for web/tests/naked-short-parity-fixtures.test.ts AND scripts/tests/test_naked_short_parity_fixtures.py. Each case declares, per surface (ts_guard=checkNakedShortRisk, py_preflight=evaluate/evaluate_combo, py_audit=find_naked_short_violations, ts_audit=auditOpenOrders), whether that surface polices the case and what verdict it returns at HEAD. Cases with a 'divergence' field encode DOCUMENTED disagreements between surfaces (see plan 2026-07-05-fable-p2-5 Divergences section); closing a divergence means flipping the expectation here in the same PR as the code fix. Tickers MUST be in the V1 universe (src/xenon/execution/universe.py). Stock is held ONLY as separate structure_type:'Stock' positions because naked_short_audit._get_stock_shares reads position-level structure_type (plan D3).",
  "single_leg": [
    {
      "id": "SL1_buy_stock_allowed",
      "desc": "BUY stock never creates short exposure",
      "portfolio": { "positions": [], "available_funds": 0 },
      "order": {
        "ticker": "SPY",
        "security_type": "STK",
        "action": "BUY",
        "right": null,
        "strike": null,
        "expiry": null,
        "quantity": 100,
        "multiplier": 100,
        "limit_price": 500
      },
      "expected": {
        "ts_guard": { "applicable": true, "blocked": false },
        "py_preflight": {
          "applicable": true,
          "blocked": false,
          "reason_code": null
        },
        "py_audit": { "applicable": false },
        "ts_audit": { "applicable": false }
      }
    },
    {
      "id": "SL2_buy_call_allowed",
      "desc": "BUY call never creates short exposure",
      "portfolio": { "positions": [], "available_funds": 0 },
      "order": {
        "ticker": "SPY",
        "security_type": "OPT",
        "action": "BUY",
        "right": "C",
        "strike": 510,
        "expiry": "20260417",
        "quantity": 10,
        "multiplier": 100,
        "limit_price": 3.0
      },
      "expected": {
        "ts_guard": { "applicable": true, "blocked": false },
        "py_preflight": {
          "applicable": true,
          "blocked": false,
          "reason_code": null
        },
        "py_audit": { "applicable": false },
        "ts_audit": { "applicable": false }
      }
    },
    {
      "id": "SL3_sell_stock_no_position_blocked",
      "desc": "SELL stock with no long shares -> naked short stock",
      "portfolio": { "positions": [], "available_funds": 0 },
      "order": {
        "ticker": "SPY",
        "security_type": "STK",
        "action": "SELL",
        "right": null,
        "strike": null,
        "expiry": null,
        "quantity": 100,
        "multiplier": 100,
        "limit_price": 500
      },
      "expected": {
        "ts_guard": { "applicable": true, "blocked": true },
        "py_preflight": {
          "applicable": true,
          "blocked": true,
          "reason_code": "INSUFFICIENT_SHARES"
        },
        "py_audit": { "applicable": true, "blocked": true },
        "ts_audit": { "applicable": false }
      }
    },
    {
      "id": "SL4_sell_stock_within_position_allowed",
      "desc": "SELL 50 of 100 held shares -> allowed",
      "portfolio": {
        "positions": [
          {
            "ticker": "SPY",
            "structure_type": "Stock",
            "contracts": 100,
            "direction": "LONG",
            "legs": [
              {
                "direction": "LONG",
                "type": "Stock",
                "contracts": 100,
                "strike": 0
              }
            ]
          }
        ],
        "available_funds": 0
      },
      "order": {
        "ticker": "SPY",
        "security_type": "STK",
        "action": "SELL",
        "right": null,
        "strike": null,
        "expiry": null,
        "quantity": 50,
        "multiplier": 100,
        "limit_price": 500
      },
      "expected": {
        "ts_guard": { "applicable": true, "blocked": false },
        "py_preflight": {
          "applicable": true,
          "blocked": false,
          "reason_code": null
        },
        "py_audit": { "applicable": true, "blocked": false },
        "ts_audit": { "applicable": false }
      }
    },
    {
      "id": "SL5_sell_stock_exceeds_position_blocked",
      "desc": "SELL 200 shares but only 100 held -> blocked",
      "portfolio": {
        "positions": [
          {
            "ticker": "SPY",
            "structure_type": "Stock",
            "contracts": 100,
            "direction": "LONG",
            "legs": [
              {
                "direction": "LONG",
                "type": "Stock",
                "contracts": 100,
                "strike": 0
              }
            ]
          }
        ],
        "available_funds": 0
      },
      "order": {
        "ticker": "SPY",
        "security_type": "STK",
        "action": "SELL",
        "right": null,
        "strike": null,
        "expiry": null,
        "quantity": 200,
        "multiplier": 100,
        "limit_price": 500
      },
      "expected": {
        "ts_guard": { "applicable": true, "blocked": true },
        "py_preflight": {
          "applicable": true,
          "blocked": true,
          "reason_code": "INSUFFICIENT_SHARES"
        },
        "py_audit": { "applicable": true, "blocked": true },
        "ts_audit": { "applicable": false }
      }
    },
    {
      "id": "SL6_sell_call_no_cover_blocked",
      "desc": "SELL call with no stock/long-call cover -> naked short call",
      "portfolio": { "positions": [], "available_funds": 0 },
      "order": {
        "ticker": "SPY",
        "security_type": "OPT",
        "action": "SELL",
        "right": "C",
        "strike": 510,
        "expiry": "20260417",
        "quantity": 5,
        "multiplier": 100,
        "limit_price": 3.0
      },
      "expected": {
        "ts_guard": { "applicable": true, "blocked": true },
        "py_preflight": {
          "applicable": true,
          "blocked": true,
          "reason_code": "ETF_CALL_UNCOVERED"
        },
        "py_audit": { "applicable": true, "blocked": true },
        "ts_audit": { "applicable": true, "blocked": true }
      }
    },
    {
      "id": "SL7_covered_call_allowed",
      "desc": "SELL 1 call, 200 shares held -> covered",
      "portfolio": {
        "positions": [
          {
            "ticker": "SPY",
            "structure_type": "Stock",
            "contracts": 200,
            "direction": "LONG",
            "legs": [
              {
                "direction": "LONG",
                "type": "Stock",
                "contracts": 200,
                "strike": 0
              }
            ]
          }
        ],
        "available_funds": 0
      },
      "order": {
        "ticker": "SPY",
        "security_type": "OPT",
        "action": "SELL",
        "right": "C",
        "strike": 510,
        "expiry": "20260417",
        "quantity": 1,
        "multiplier": 100,
        "limit_price": 3.0
      },
      "expected": {
        "ts_guard": { "applicable": true, "blocked": false },
        "py_preflight": {
          "applicable": true,
          "blocked": false,
          "reason_code": null
        },
        "py_audit": { "applicable": true, "blocked": false },
        "ts_audit": { "applicable": true, "blocked": false }
      }
    },
    {
      "id": "SL8_short_a_tail_blocked",
      "desc": "SELL 5 calls, 200 shares cover only 2 -> short a tail",
      "portfolio": {
        "positions": [
          {
            "ticker": "SPY",
            "structure_type": "Stock",
            "contracts": 200,
            "direction": "LONG",
            "legs": [
              {
                "direction": "LONG",
                "type": "Stock",
                "contracts": 200,
                "strike": 0
              }
            ]
          }
        ],
        "available_funds": 0
      },
      "order": {
        "ticker": "SPY",
        "security_type": "OPT",
        "action": "SELL",
        "right": "C",
        "strike": 510,
        "expiry": "20260417",
        "quantity": 5,
        "multiplier": 100,
        "limit_price": 3.0
      },
      "expected": {
        "ts_guard": { "applicable": true, "blocked": true },
        "py_preflight": {
          "applicable": true,
          "blocked": true,
          "reason_code": "ETF_CALL_UNCOVERED"
        },
        "py_audit": { "applicable": true, "blocked": true },
        "ts_audit": { "applicable": true, "blocked": true }
      }
    },
    {
      "id": "SL9_sell_put_cash_secured_allowed",
      "desc": "SELL put is cash-secured -> allowed everywhere; ts_audit filters puts out entirely",
      "portfolio": { "positions": [], "available_funds": 0 },
      "order": {
        "ticker": "SPY",
        "security_type": "OPT",
        "action": "SELL",
        "right": "P",
        "strike": 480,
        "expiry": "20260417",
        "quantity": 10,
        "multiplier": 100,
        "limit_price": 4.0
      },
      "expected": {
        "ts_guard": { "applicable": true, "blocked": false },
        "py_preflight": {
          "applicable": true,
          "blocked": false,
          "reason_code": null
        },
        "py_audit": { "applicable": true, "blocked": false },
        "ts_audit": { "applicable": false }
      }
    },
    {
      "id": "SL10_stock_plus_existing_short_exact_cover_allowed",
      "desc": "300 shares + 2 existing short calls, SELL 1 more -> total 3 == 3 cover -> allowed",
      "portfolio": {
        "positions": [
          {
            "ticker": "SPY",
            "structure_type": "Stock",
            "contracts": 300,
            "direction": "LONG",
            "legs": [
              {
                "direction": "LONG",
                "type": "Stock",
                "contracts": 300,
                "strike": 0
              }
            ]
          },
          {
            "ticker": "SPY",
            "structure_type": "Short Call",
            "contracts": 2,
            "direction": "SHORT",
            "expiry": "20260718",
            "legs": [
              {
                "direction": "SHORT",
                "type": "Call",
                "contracts": 2,
                "strike": 505
              }
            ]
          }
        ],
        "available_funds": 0
      },
      "order": {
        "ticker": "SPY",
        "security_type": "OPT",
        "action": "SELL",
        "right": "C",
        "strike": 520,
        "expiry": "20260417",
        "quantity": 1,
        "multiplier": 100,
        "limit_price": 2.0
      },
      "expected": {
        "ts_guard": { "applicable": true, "blocked": false },
        "py_preflight": {
          "applicable": true,
          "blocked": false,
          "reason_code": null
        },
        "py_audit": { "applicable": true, "blocked": false },
        "ts_audit": { "applicable": true, "blocked": false }
      }
    },
    {
      "id": "SL11_long_call_same_expiry_vertical",
      "desc": "LONG 125 calls same expiry (diff strike), SELL 125 -> vertical spread cover. DIVERGENCE D2: ts_audit lacks the same-expiry branch and flags it.",
      "portfolio": {
        "positions": [
          {
            "ticker": "QQQ",
            "structure_type": "Long Call",
            "contracts": 125,
            "direction": "LONG",
            "expiry": "20260402",
            "legs": [
              {
                "direction": "LONG",
                "type": "Call",
                "contracts": 125,
                "strike": 460
              }
            ]
          }
        ],
        "available_funds": 0
      },
      "order": {
        "ticker": "QQQ",
        "security_type": "OPT",
        "action": "SELL",
        "right": "C",
        "strike": 470,
        "expiry": "20260402",
        "quantity": 125,
        "multiplier": 100,
        "limit_price": 2.0
      },
      "expected": {
        "ts_guard": { "applicable": true, "blocked": false },
        "py_preflight": {
          "applicable": true,
          "blocked": false,
          "reason_code": null
        },
        "py_audit": { "applicable": true, "blocked": false },
        "ts_audit": { "applicable": true, "blocked": true }
      },
      "divergence": {
        "status": "known",
        "tracking": "D2/M2: ts_audit (auditOpenOrders) has no _count_long_calls_at_expiry equivalent -> false-positives covered verticals. py_audit is authoritative (it is the surface that actually cancels); ts_audit has zero runtime callers. Fail-closed, LOW. Fix in a separate PR and flip this expectation there."
      }
    },
    {
      "id": "SL12_more_shorts_than_long_calls_blocked",
      "desc": "LONG 50 calls same expiry, SELL 125, no shares -> 75 uncovered -> blocked",
      "portfolio": {
        "positions": [
          {
            "ticker": "QQQ",
            "structure_type": "Long Call",
            "contracts": 50,
            "direction": "LONG",
            "expiry": "20260402",
            "legs": [
              {
                "direction": "LONG",
                "type": "Call",
                "contracts": 50,
                "strike": 460
              }
            ]
          }
        ],
        "available_funds": 0
      },
      "order": {
        "ticker": "QQQ",
        "security_type": "OPT",
        "action": "SELL",
        "right": "C",
        "strike": 470,
        "expiry": "20260402",
        "quantity": 125,
        "multiplier": 100,
        "limit_price": 2.0
      },
      "expected": {
        "ts_guard": { "applicable": true, "blocked": true },
        "py_preflight": {
          "applicable": true,
          "blocked": true,
          "reason_code": "ETF_CALL_UNCOVERED"
        },
        "py_audit": { "applicable": true, "blocked": true },
        "ts_audit": { "applicable": true, "blocked": true }
      }
    },
    {
      "id": "SL13_long_call_different_expiry_no_spread_blocked",
      "desc": "LONG calls April, SELL calls June, no shares -> not a spread -> blocked",
      "portfolio": {
        "positions": [
          {
            "ticker": "QQQ",
            "structure_type": "Long Call",
            "contracts": 125,
            "direction": "LONG",
            "expiry": "20260402",
            "legs": [
              {
                "direction": "LONG",
                "type": "Call",
                "contracts": 125,
                "strike": 460
              }
            ]
          }
        ],
        "available_funds": 0
      },
      "order": {
        "ticker": "QQQ",
        "security_type": "OPT",
        "action": "SELL",
        "right": "C",
        "strike": 470,
        "expiry": "20260620",
        "quantity": 125,
        "multiplier": 100,
        "limit_price": 2.0
      },
      "expected": {
        "ts_guard": { "applicable": true, "blocked": true },
        "py_preflight": {
          "applicable": true,
          "blocked": true,
          "reason_code": "ETF_CALL_UNCOVERED"
        },
        "py_audit": { "applicable": true, "blocked": true },
        "ts_audit": { "applicable": true, "blocked": true }
      }
    },
    {
      "id": "SL14_sell_to_close_exact_match_allowed",
      "desc": "SELL exactly the long call held (same strike+expiry) -> close, not naked (all four surfaces have the exact-match branch)",
      "portfolio": {
        "positions": [
          {
            "ticker": "SPY",
            "structure_type": "Long Call",
            "contracts": 77,
            "direction": "LONG",
            "expiry": "20270115",
            "legs": [
              {
                "direction": "LONG",
                "type": "Call",
                "contracts": 77,
                "strike": 500
              }
            ]
          }
        ],
        "available_funds": 0
      },
      "order": {
        "ticker": "SPY",
        "security_type": "OPT",
        "action": "SELL",
        "right": "C",
        "strike": 500,
        "expiry": "20270115",
        "quantity": 77,
        "multiplier": 100,
        "limit_price": 5.0
      },
      "expected": {
        "ts_guard": { "applicable": true, "blocked": false },
        "py_preflight": {
          "applicable": true,
          "blocked": false,
          "reason_code": null
        },
        "py_audit": { "applicable": true, "blocked": false },
        "ts_audit": { "applicable": true, "blocked": false }
      }
    },
    {
      "id": "SL15_undercovered_with_existing_shorts_blocked",
      "desc": "500 shares + 3 existing short calls (diff expiry), SELL 5 -> total 8 > 5 cover -> blocked",
      "portfolio": {
        "positions": [
          {
            "ticker": "SPY",
            "structure_type": "Stock",
            "contracts": 500,
            "direction": "LONG",
            "legs": [
              {
                "direction": "LONG",
                "type": "Stock",
                "contracts": 500,
                "strike": 0
              }
            ]
          },
          {
            "ticker": "SPY",
            "structure_type": "Short Call",
            "contracts": 3,
            "direction": "SHORT",
            "expiry": "20260718",
            "legs": [
              {
                "direction": "SHORT",
                "type": "Call",
                "contracts": 3,
                "strike": 505
              }
            ]
          }
        ],
        "available_funds": 0
      },
      "order": {
        "ticker": "SPY",
        "security_type": "OPT",
        "action": "SELL",
        "right": "C",
        "strike": 510,
        "expiry": "20260620",
        "quantity": 5,
        "multiplier": 100,
        "limit_price": 3.0
      },
      "expected": {
        "ts_guard": { "applicable": true, "blocked": true },
        "py_preflight": {
          "applicable": true,
          "blocked": true,
          "reason_code": "ETF_CALL_UNCOVERED"
        },
        "py_audit": { "applicable": true, "blocked": true },
        "ts_audit": { "applicable": true, "blocked": true }
      }
    },
    {
      "id": "SL16_exact_cover_with_existing_shorts_allowed",
      "desc": "500 shares + 3 existing short calls (diff expiry), SELL 2 -> total 5 == 5 cover -> allowed",
      "portfolio": {
        "positions": [
          {
            "ticker": "SPY",
            "structure_type": "Stock",
            "contracts": 500,
            "direction": "LONG",
            "legs": [
              {
                "direction": "LONG",
                "type": "Stock",
                "contracts": 500,
                "strike": 0
              }
            ]
          },
          {
            "ticker": "SPY",
            "structure_type": "Short Call",
            "contracts": 3,
            "direction": "SHORT",
            "expiry": "20260718",
            "legs": [
              {
                "direction": "SHORT",
                "type": "Call",
                "contracts": 3,
                "strike": 505
              }
            ]
          }
        ],
        "available_funds": 0
      },
      "order": {
        "ticker": "SPY",
        "security_type": "OPT",
        "action": "SELL",
        "right": "C",
        "strike": 510,
        "expiry": "20260620",
        "quantity": 2,
        "multiplier": 100,
        "limit_price": 3.0
      },
      "expected": {
        "ts_guard": { "applicable": true, "blocked": false },
        "py_preflight": {
          "applicable": true,
          "blocked": false,
          "reason_code": null
        },
        "py_audit": { "applicable": true, "blocked": false },
        "ts_audit": { "applicable": true, "blocked": false }
      }
    },
    {
      "id": "SL17_index_sell_call_no_cover_blocked",
      "desc": "Index SPX SELL call, no long-call cover -> blocked everywhere (preflight: index needs long-call cover; others: no shares)",
      "portfolio": { "positions": [], "available_funds": 0 },
      "order": {
        "ticker": "SPX",
        "security_type": "OPT",
        "action": "SELL",
        "right": "C",
        "strike": 5600,
        "expiry": "20260620",
        "quantity": 1,
        "multiplier": 100,
        "limit_price": 20.0
      },
      "expected": {
        "ts_guard": { "applicable": true, "blocked": true },
        "py_preflight": {
          "applicable": true,
          "blocked": true,
          "reason_code": "INDEX_CALL_UNCOVERED"
        },
        "py_audit": { "applicable": true, "blocked": true },
        "ts_audit": { "applicable": true, "blocked": true }
      }
    },
    {
      "id": "SL18_index_sell_call_long_call_cover",
      "desc": "Index SPX SELL call covered by same-expiry long call (vertical) -> allowed. DIVERGENCE D2 again: ts_audit flags it (no same-expiry branch).",
      "portfolio": {
        "positions": [
          {
            "ticker": "SPX",
            "structure_type": "Long Call",
            "contracts": 1,
            "direction": "LONG",
            "expiry": "20260620",
            "legs": [
              {
                "direction": "LONG",
                "type": "Call",
                "contracts": 1,
                "strike": 5500
              }
            ]
          }
        ],
        "available_funds": 0
      },
      "order": {
        "ticker": "SPX",
        "security_type": "OPT",
        "action": "SELL",
        "right": "C",
        "strike": 5600,
        "expiry": "20260620",
        "quantity": 1,
        "multiplier": 100,
        "limit_price": 20.0
      },
      "expected": {
        "ts_guard": { "applicable": true, "blocked": false },
        "py_preflight": {
          "applicable": true,
          "blocked": false,
          "reason_code": null
        },
        "py_audit": { "applicable": true, "blocked": false },
        "ts_audit": { "applicable": true, "blocked": true }
      },
      "divergence": {
        "status": "known",
        "tracking": "D2/M2: same missing same-expiry branch as SL11. py_audit authoritative; ts_audit dormant. Fail-closed, LOW."
      }
    }
  ],
  "combo": [
    {
      "id": "C1_bull_call_spread_allowed",
      "desc": "BUY C + SELL C vertical -> allowed",
      "portfolio": { "positions": [], "available_funds": 0 },
      "order": {
        "ticker": "SPY",
        "action": "BUY",
        "quantity": 5,
        "multiplier": 100,
        "legs": [
          {
            "expiry": "20260417",
            "strike": 500,
            "right": "C",
            "action": "BUY",
            "ratio": 1
          },
          {
            "expiry": "20260417",
            "strike": 510,
            "right": "C",
            "action": "SELL",
            "ratio": 1
          }
        ]
      },
      "expected": {
        "ts_guard": { "applicable": true, "blocked": false },
        "py_preflight": {
          "applicable": true,
          "blocked": false,
          "reason_code": null
        },
        "py_audit": { "applicable": false },
        "ts_audit": { "applicable": false }
      }
    },
    {
      "id": "C2_short_risk_reversal_no_stock_blocked",
      "desc": "BUY envelope, SELL C + BUY P legs, no stock -> uncovered short call -> blocked",
      "portfolio": { "positions": [], "available_funds": 0 },
      "order": {
        "ticker": "SPY",
        "action": "BUY",
        "quantity": 1,
        "multiplier": 100,
        "legs": [
          {
            "expiry": "20260620",
            "strike": 520,
            "right": "C",
            "action": "SELL",
            "ratio": 1
          },
          {
            "expiry": "20260620",
            "strike": 470,
            "right": "P",
            "action": "BUY",
            "ratio": 1
          }
        ]
      },
      "expected": {
        "ts_guard": { "applicable": true, "blocked": true },
        "py_preflight": {
          "applicable": true,
          "blocked": true,
          "reason_code": "ETF_CALL_UNCOVERED"
        },
        "py_audit": { "applicable": false },
        "ts_audit": { "applicable": false }
      }
    },
    {
      "id": "C3_short_risk_reversal_stock_covers_allowed",
      "desc": "SELL C + BUY P, 100 shares cover the short call -> allowed",
      "portfolio": {
        "positions": [
          {
            "ticker": "SPY",
            "structure_type": "Stock",
            "contracts": 100,
            "direction": "LONG",
            "legs": [
              {
                "direction": "LONG",
                "type": "Stock",
                "contracts": 100,
                "strike": 0
              }
            ]
          }
        ],
        "available_funds": 0
      },
      "order": {
        "ticker": "SPY",
        "action": "BUY",
        "quantity": 1,
        "multiplier": 100,
        "legs": [
          {
            "expiry": "20260620",
            "strike": 520,
            "right": "C",
            "action": "SELL",
            "ratio": 1
          },
          {
            "expiry": "20260620",
            "strike": 470,
            "right": "P",
            "action": "BUY",
            "ratio": 1
          }
        ]
      },
      "expected": {
        "ts_guard": { "applicable": true, "blocked": false },
        "py_preflight": {
          "applicable": true,
          "blocked": false,
          "reason_code": null
        },
        "py_audit": { "applicable": false },
        "ts_audit": { "applicable": false }
      }
    },
    {
      "id": "C4_jade_lizard_allowed",
      "desc": "BUY C + SELL higher C + SELL P -> call spread covers short call, put cash-secured -> allowed",
      "portfolio": { "positions": [], "available_funds": 0 },
      "order": {
        "ticker": "SPY",
        "action": "BUY",
        "quantity": 1,
        "multiplier": 100,
        "legs": [
          {
            "expiry": "20260417",
            "strike": 510,
            "right": "C",
            "action": "BUY",
            "ratio": 1
          },
          {
            "expiry": "20260417",
            "strike": 520,
            "right": "C",
            "action": "SELL",
            "ratio": 1
          },
          {
            "expiry": "20260417",
            "strike": 480,
            "right": "P",
            "action": "SELL",
            "ratio": 1
          }
        ]
      },
      "expected": {
        "ts_guard": { "applicable": true, "blocked": false },
        "py_preflight": {
          "applicable": true,
          "blocked": false,
          "reason_code": null
        },
        "py_audit": { "applicable": false },
        "ts_audit": { "applicable": false }
      }
    },
    {
      "id": "C5_ratio_1x2_no_stock_blocked",
      "desc": "BUY 1C + SELL 2C, no stock -> 1 uncovered short call -> blocked",
      "portfolio": { "positions": [], "available_funds": 0 },
      "order": {
        "ticker": "SPY",
        "action": "BUY",
        "quantity": 1,
        "multiplier": 100,
        "legs": [
          {
            "expiry": "20260417",
            "strike": 500,
            "right": "C",
            "action": "BUY",
            "ratio": 1
          },
          {
            "expiry": "20260417",
            "strike": 510,
            "right": "C",
            "action": "SELL",
            "ratio": 2
          }
        ]
      },
      "expected": {
        "ts_guard": { "applicable": true, "blocked": true },
        "py_preflight": {
          "applicable": true,
          "blocked": true,
          "reason_code": "ETF_CALL_UNCOVERED"
        },
        "py_audit": { "applicable": false },
        "ts_audit": { "applicable": false }
      }
    },
    {
      "id": "C6_ratio_1x2_stock_covers_allowed",
      "desc": "BUY 1C + SELL 2C, 100 shares cover the 1 uncovered call -> allowed",
      "portfolio": {
        "positions": [
          {
            "ticker": "SPY",
            "structure_type": "Stock",
            "contracts": 100,
            "direction": "LONG",
            "legs": [
              {
                "direction": "LONG",
                "type": "Stock",
                "contracts": 100,
                "strike": 0
              }
            ]
          }
        ],
        "available_funds": 0
      },
      "order": {
        "ticker": "SPY",
        "action": "BUY",
        "quantity": 1,
        "multiplier": 100,
        "legs": [
          {
            "expiry": "20260417",
            "strike": 500,
            "right": "C",
            "action": "BUY",
            "ratio": 1
          },
          {
            "expiry": "20260417",
            "strike": 510,
            "right": "C",
            "action": "SELL",
            "ratio": 2
          }
        ]
      },
      "expected": {
        "ts_guard": { "applicable": true, "blocked": false },
        "py_preflight": {
          "applicable": true,
          "blocked": false,
          "reason_code": null
        },
        "py_audit": { "applicable": false },
        "ts_audit": { "applicable": false }
      }
    },
    {
      "id": "C7_closing_combo_sell_envelope",
      "desc": "Closing combo (envelope action=SELL, legs SELL C + BUY P), empty portfolio. DIVERGENCE D1: TS blanket-allows SELL envelopes; Python evaluate_combo ignores the envelope and blocks the uncovered short-call leg.",
      "portfolio": { "positions": [], "available_funds": 0 },
      "order": {
        "ticker": "SPY",
        "action": "SELL",
        "quantity": 1,
        "multiplier": 100,
        "legs": [
          {
            "expiry": "20260620",
            "strike": 520,
            "right": "C",
            "action": "SELL",
            "ratio": 1
          },
          {
            "expiry": "20260620",
            "strike": 470,
            "right": "P",
            "action": "BUY",
            "ratio": 1
          }
        ]
      },
      "expected": {
        "ts_guard": { "applicable": true, "blocked": false },
        "py_preflight": {
          "applicable": true,
          "blocked": true,
          "reason_code": "ETF_CALL_UNCOVERED"
        },
        "py_audit": { "applicable": false },
        "ts_audit": { "applicable": false }
      },
      "divergence": {
        "status": "known",
        "tracking": "D1/M1: nakedShortGuard.ts early-returns allow on SELL-envelope combos; evaluate_combo has no envelope check (its docstring claims it does — doc bug) and blocks. Python (API gate) is authoritative -> UI allows, API 403s = UX friction, fail-closed, LOW. Verified by live trace 2026-07-05: accept=False ETF_CALL_UNCOVERED. Fix in a separate PR and flip this expectation there."
      }
    },
    {
      "id": "C8_index_ratio_uncovered_blocked",
      "desc": "Index SPX ratio 1x2, no cover -> uncovered index short call -> blocked",
      "portfolio": { "positions": [], "available_funds": 0 },
      "order": {
        "ticker": "SPX",
        "action": "BUY",
        "quantity": 1,
        "multiplier": 100,
        "legs": [
          {
            "expiry": "20260620",
            "strike": 5500,
            "right": "C",
            "action": "BUY",
            "ratio": 1
          },
          {
            "expiry": "20260620",
            "strike": 5600,
            "right": "C",
            "action": "SELL",
            "ratio": 2
          }
        ]
      },
      "expected": {
        "ts_guard": { "applicable": true, "blocked": true },
        "py_preflight": {
          "applicable": true,
          "blocked": true,
          "reason_code": "INDEX_CALL_UNCOVERED"
        },
        "py_audit": { "applicable": false },
        "ts_audit": { "applicable": false }
      }
    }
  ]
}
```

> **Executor rule:** every `expected` verdict above was hand-traced against HEAD (D1 additionally verified by live execution). If a case goes red in Step 2 or 3, do **not** edit the fixture to make it pass — see Tripwires.

### Step 2 — Python parity test

Create **`scripts/tests/test_naked_short_parity_fixtures.py`**:

```python
"""Parity fixtures: config/naked-short-parity-cases.json declares, per coverage surface,
whether the surface polices each case and what verdict it returns at HEAD. This test runs
the two Python surfaces (preflight.evaluate/evaluate_combo, find_naked_short_violations);
web/tests/naked-short-parity-fixtures.test.ts runs the two TS surfaces off the same JSON.

Known divergences between surfaces are DOCUMENTED in the fixture via `divergence` fields
(plan 2026-07-05-fable-p2-5, Divergences section) — the expectations encode reality, not
uniformity. A red here means the code moved relative to the documented expectation: either
a NEW divergence (STOP and report) or you are closing a documented one (flip the fixture
expectation in the same PR as the code fix).
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from xenon.execution.naked_short_audit import find_naked_short_violations
from xenon.execution.preflight import (
    ComboPreflightLeg,
    ComboPreflightRequest,
    PortfolioLeg,
    PortfolioPosition,
    PortfolioView,
    PreflightRequest,
    evaluate,
    evaluate_combo,
)

_FIXTURE = Path(__file__).resolve().parents[2] / "config" / "naked-short-parity-cases.json"
_CASES = json.loads(_FIXTURE.read_text())

_SURFACES = {"ts_guard", "py_preflight", "py_audit", "ts_audit"}


def _dec(v):
    return None if v is None else Decimal(str(v))


def _portfolio_view(pf: dict) -> PortfolioView:
    positions = [
        PortfolioPosition(
            ticker=p["ticker"],
            structure_type=p["structure_type"],
            direction=p.get("direction", "LONG"),
            contracts=int(p["contracts"]),
            expiry=p.get("expiry"),
            legs=[
                PortfolioLeg(
                    direction=leg["direction"],
                    type=leg["type"],
                    contracts=int(leg["contracts"]),
                    strike=Decimal(str(leg.get("strike", 0))),
                )
                for leg in p["legs"]
            ],
        )
        for p in pf["positions"]
    ]
    return PortfolioView(positions=positions, available_funds=Decimal(str(pf.get("available_funds", 0))))


def _audit_order(order: dict) -> dict:
    return {
        "status": "Submitted",
        "action": order["action"],
        "totalQuantity": order["quantity"],
        "orderId": 1,
        "permId": 1001,
        "orderRef": None,
        "contract": {
            "secType": order["security_type"],
            "symbol": order["ticker"],
            "right": order["right"] or "",
            "expiry": order["expiry"],
            "strike": order["strike"] or 0,
        },
    }


@pytest.mark.parametrize(
    "case", _CASES["single_leg"] + _CASES["combo"], ids=lambda c: c["id"]
)
def test_fixture_schema(case):
    """Every case must declare all four surfaces explicitly (tribunal M3 guard)."""
    assert set(case["expected"].keys()) == _SURFACES, f"{case['id']}: expected matrix incomplete"
    for name, exp in case["expected"].items():
        assert isinstance(exp["applicable"], bool)
        if exp["applicable"]:
            assert isinstance(exp["blocked"], bool), f"{case['id']}.{name}: applicable needs a verdict"


@pytest.mark.parametrize("case", _CASES["single_leg"], ids=lambda c: c["id"])
def test_single_leg_parity(case):
    order = case["order"]
    pf = _portfolio_view(case["portfolio"])

    exp = case["expected"]["py_preflight"]
    assert exp["applicable"], f"{case['id']}: py_preflight polices every case"
    req = PreflightRequest(
        ticker=order["ticker"],
        security_type=order["security_type"],
        action=order["action"],
        quantity=int(order["quantity"]),
        right=order["right"],
        expiry=order["expiry"],
        strike=_dec(order["strike"]),
        multiplier=int(order.get("multiplier", 100)),
        limit_price=Decimal(str(order["limit_price"])),
    )
    verdict = evaluate(req, pf)
    assert verdict.accept is (not exp["blocked"]), (
        f"{case['id']}: py_preflight verdict mismatch (got accept={verdict.accept}, "
        f"reason={verdict.reason_code})"
    )
    if exp.get("reason_code") is not None:
        assert verdict.reason_code == exp["reason_code"], f"{case['id']}: reason_code mismatch"

    exp = case["expected"]["py_audit"]
    if exp["applicable"]:
        violations = find_naked_short_violations([_audit_order(order)], case["portfolio"]["positions"])
        assert (len(violations) > 0) is exp["blocked"], (
            f"{case['id']}: py_audit verdict mismatch (violations={violations})"
        )


@pytest.mark.parametrize("case", _CASES["combo"], ids=lambda c: c["id"])
def test_combo_parity(case):
    order = case["order"]
    pf = _portfolio_view(case["portfolio"])

    exp = case["expected"]["py_preflight"]
    assert exp["applicable"], f"{case['id']}: py_preflight polices every case"
    req = ComboPreflightRequest(
        ticker=order["ticker"],
        action=order["action"],
        quantity=int(order["quantity"]),
        multiplier=int(order.get("multiplier", 100)),
        legs=[
            ComboPreflightLeg(
                expiry=leg["expiry"],
                strike=_dec(leg["strike"]),
                right=leg["right"],
                action=leg["action"],
                ratio=int(leg["ratio"]),
            )
            for leg in order["legs"]
        ],
    )
    verdict = evaluate_combo(req, pf)
    assert verdict.accept is (not exp["blocked"]), (
        f"{case['id']}: evaluate_combo verdict mismatch (got accept={verdict.accept}, "
        f"reason={verdict.reason_code})"
    )
    if exp.get("reason_code") is not None:
        assert verdict.reason_code == exp["reason_code"], f"{case['id']}: reason_code mismatch"

    # Both audits skip BAG combos by design — the fixture must say so.
    assert case["expected"]["py_audit"]["applicable"] is False
    assert case["expected"]["ts_audit"]["applicable"] is False
```

Run: `uv run pytest scripts/tests/test_naked_short_parity_fixtures.py -xvs` → expect **all parametrized cases pass** (including `C7`, whose documented expectation is `py_preflight blocked=true`).

### Step 3 — Vitest parity test (same JSON, TS surfaces)

Create **`web/tests/naked-short-parity-fixtures.test.ts`**:

```ts
/**
 * Parity fixtures: config/naked-short-parity-cases.json declares, per coverage surface,
 * whether the surface polices each case and what verdict it returns at HEAD. This suite
 * runs the two TS surfaces (checkNakedShortRisk, auditOpenOrders); the Python surfaces
 * run in scripts/tests/test_naked_short_parity_fixtures.py off the same JSON.
 *
 * Known divergences (fixture `divergence` fields; plan Divergences section: D1=C7,
 * D2=SL11/SL18) encode documented reality. A red means the code moved relative to the
 * documented expectation: a NEW divergence (STOP and report) or you are closing a
 * documented one (flip the fixture expectation in the same PR as the code fix).
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { describe, it, expect } from "vitest";
import {
  checkNakedShortRisk,
  auditOpenOrders,
  type OrderPayload,
  type NakedShortPortfolio,
  type NakedShortOpenOrder,
} from "../lib/nakedShortGuard";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE = path.resolve(
  __dirname,
  "../../config/naked-short-parity-cases.json",
);
const CASES = JSON.parse(readFileSync(FIXTURE, "utf-8"));

const SURFACES = ["ts_guard", "py_preflight", "py_audit", "ts_audit"];

function toPortfolio(pf: any): NakedShortPortfolio {
  return {
    positions: pf.positions.map((p: any) => ({
      ticker: p.ticker,
      structure_type: p.structure_type,
      contracts: p.contracts,
      direction: p.direction ?? "LONG",
      expiry: p.expiry ?? null,
      legs: p.legs.map((l: any) => ({
        direction: l.direction,
        type: l.type,
        contracts: l.contracts,
        strike: l.strike ?? null,
      })),
    })),
  };
}

function singleLegOrder(o: any): OrderPayload {
  return {
    type: o.security_type === "STK" ? "stock" : "option",
    symbol: o.ticker,
    action: o.action,
    quantity: o.quantity,
    limitPrice: o.limit_price,
    expiry: o.expiry ?? undefined,
    strike: o.strike ?? undefined,
    right: o.right ?? undefined,
  };
}

function comboOrder(o: any): OrderPayload {
  return {
    type: "combo",
    symbol: o.ticker,
    action: o.action,
    quantity: o.quantity,
    limitPrice: 0,
    legs: o.legs.map((l: any) => ({
      expiry: l.expiry,
      strike: l.strike,
      right: l.right,
      action: l.action,
      ratio: l.ratio,
    })),
  };
}

function auditOrder(o: any): NakedShortOpenOrder {
  return {
    orderId: 1,
    permId: 1001,
    symbol: o.ticker,
    action: o.action,
    totalQuantity: o.quantity,
    contract: {
      secType: o.security_type,
      right: o.right ?? null,
      strike: o.strike ?? null,
      expiry: o.expiry ?? null,
      symbol: o.ticker,
    },
  };
}

describe("fixture schema (tribunal M3 guard)", () => {
  for (const c of [...CASES.single_leg, ...CASES.combo]) {
    it(`${c.id} declares all four surfaces`, () => {
      expect(Object.keys(c.expected).sort()).toEqual([...SURFACES].sort());
    });
  }
});

describe("naked-short parity fixtures — single leg (TS surfaces)", () => {
  for (const c of CASES.single_leg) {
    it(c.id, () => {
      const portfolio = toPortfolio(c.portfolio);

      const g = c.expected.ts_guard;
      expect(g.applicable, `${c.id}: ts_guard polices every case`).toBe(true);
      const guard = checkNakedShortRisk(singleLegOrder(c.order), portfolio);
      expect(guard.allowed, `${c.id}: ts_guard verdict`).toBe(!g.blocked);

      const a = c.expected.ts_audit;
      if (a.applicable) {
        const violations = auditOpenOrders([auditOrder(c.order)], portfolio);
        expect(violations.length > 0, `${c.id}: ts_audit verdict`).toBe(
          a.blocked,
        );
      }
    });
  }
});

describe("naked-short parity fixtures — combo (TS surfaces)", () => {
  for (const c of CASES.combo) {
    it(c.id, () => {
      const portfolio = toPortfolio(c.portfolio);
      const g = c.expected.ts_guard;
      expect(g.applicable, `${c.id}: ts_guard polices every case`).toBe(true);
      const guard = checkNakedShortRisk(comboOrder(c.order), portfolio);
      expect(guard.allowed, `${c.id}: ts_guard combo verdict`).toBe(!g.blocked);
      // auditOpenOrders never polices combos (nakedShortGuard.ts secType==="OPT" filter).
      expect(c.expected.ts_audit.applicable).toBe(false);
    });
  }
});
```

Run: `cd web && npm test -- naked-short-parity-fixtures` → expect **all cases pass** (including `C7` with ts_guard `blocked:false` and `SL11`/`SL18` with ts_audit `blocked:true` — the documented divergences).

### Step 4 — Reason-code codegen script (mirror `generate_universe_ts.py`)

Create **`scripts/infra/dev/generate_reason_codes_ts.py`**:

```python
#!/usr/bin/env python3
"""Generate web/lib/generated/reasonCodeNames.ts from the Python ReasonCode enum.

Runs as a prebuild/predev/pretest hook from web/package.json (via `codegen`).
The TS file is marked generated in .gitattributes. Do not hand-edit.

This is the single source of truth for which reason codes exist; the human-facing
toast copy stays in web/lib/orderReasonCodes.ts and is checked against this list by
web/tests/order-reason-codes.test.ts.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
# parents[0]=dev, [1]=infra, [2]=scripts, [3]=repo root
_REPO_ROOT = _HERE.parents[3]
assert (_REPO_ROOT / "web" / "package.json").exists(), (
    f"repo root detection failed; resolved {_REPO_ROOT} but no web/package.json there"
)
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from xenon.execution.preflight import ReasonCode  # noqa: E402

HEADER = """\
// AUTO-GENERATED by scripts/infra/dev/generate_reason_codes_ts.py
// Do not hand-edit. Source of truth: ReasonCode in src/xenon/execution/preflight.py
// Regenerate with: uv run python scripts/infra/dev/generate_reason_codes_ts.py

"""


def render() -> str:
    lines = [HEADER, "export const REASON_CODE_NAMES = ["]
    for member in ReasonCode:
        lines.append(f'  "{member.value}",')
    lines.append("] as const;")
    lines.append("")
    lines.append("export type ReasonCodeName = (typeof REASON_CODE_NAMES)[number];")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    out_path = _REPO_ROOT / "web" / "lib" / "generated" / "reasonCodeNames.ts"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(), encoding="utf-8")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

Generate the file (it gets committed):

```bash
uv run python scripts/infra/dev/generate_reason_codes_ts.py
```

Creates `web/lib/generated/reasonCodeNames.ts` (26 entries, ending `"SUBPROCESS_ERROR",`).

### Step 5 — Wire codegen into npm hooks + gitattributes

Edit **`web/package.json`** scripts block. Replace:

```json
    "universe:gen": "uv run --project .. python ../scripts/infra/dev/generate_universe_ts.py",
    "predev": "npm run universe:gen",
    "prebuild": "npm run universe:gen",
    "pretest": "npm run universe:gen",
    "pretypecheck": "npm run universe:gen"
```

with:

```json
    "universe:gen": "uv run --project .. python ../scripts/infra/dev/generate_universe_ts.py",
    "reasoncodes:gen": "uv run --project .. python ../scripts/infra/dev/generate_reason_codes_ts.py",
    "codegen": "npm run universe:gen && npm run reasoncodes:gen",
    "predev": "npm run codegen",
    "prebuild": "npm run codegen",
    "pretest": "npm run codegen",
    "pretypecheck": "npm run codegen"
```

Edit **`.gitattributes`** — add next to the universe line:

```
web/lib/generated/reasonCodeNames.ts linguist-generated=true
```

### Step 6 — Fix the latent reason-code drift + swap the parity test to the generated list

**6a.** Edit **`web/lib/orderReasonCodes.ts`** — add the missing `PORTFOLIO_SNAPSHOT_STALE` entry (enum member exists in Python and is emitted live at `server.py:1631`; only the toast copy was missing). Insert immediately after the `PORTFOLIO_SNAPSHOT_REQUIRED` block (before `READ_ONLY_BROKER`):

```ts
  PORTFOLIO_SNAPSHOT_STALE: {
    severity: "error",
    copy: "Portfolio snapshot too old — refresh before placing short-exposure orders.",
  },
```

**6b.** Edit **`web/tests/order-reason-codes.test.ts`** — replace the hand-maintained `PYTHON_REASON_CODES` literal array (the `const PYTHON_REASON_CODES = [ ... ];` block and its preceding comment, lines 4-39) with:

```ts
import { describe, it, expect } from "vitest";
import { ORDER_REASON_CODES, getReasonToast } from "../lib/orderReasonCodes";
import { REASON_CODE_NAMES } from "../lib/generated/reasonCodeNames";

// Source of truth: generated from Python ReasonCode via
// scripts/infra/dev/generate_reason_codes_ts.py. The two lists cannot drift because
// this array is codegen'd from the enum and the codegen is drift-guarded by
// scripts/tests/test_reason_codes_ts_drift.py.
const PYTHON_REASON_CODES = [...REASON_CODE_NAMES];
```

Leave the two `describe` blocks unchanged — with `PORTFOLIO_SNAPSHOT_STALE` now generated into the list AND added to `ORDER_REASON_CODES` (6a), `missingInTs`/`extraInTs` both stay `[]`.

### Step 7 — Python drift test for the generated file (mirror `test_universe_ts_drift.py`)

Create **`scripts/tests/test_reason_codes_ts_drift.py`**:

```python
"""Regression: web/lib/generated/reasonCodeNames.ts must match what
generate_reason_codes_ts.py would produce right now. Guards against silent drift
between the Python ReasonCode enum and the checked-in TS mirror.
"""

from pathlib import Path

from scripts.infra.dev.generate_reason_codes_ts import render


def test_checked_in_reason_code_names_ts_matches_codegen():
    repo_root = Path(__file__).resolve().parents[2]
    checked_in = (repo_root / "web" / "lib" / "generated" / "reasonCodeNames.ts").read_text()
    expected = render()
    assert checked_in == expected, (
        "web/lib/generated/reasonCodeNames.ts is stale. Regenerate with: "
        "uv run python scripts/infra/dev/generate_reason_codes_ts.py"
    )
```

> The existing `test_universe_ts_drift.py` imports `from scripts.infra.dev.generate_universe_ts import render` and passes in CI, so the import path works — do not add `__init__.py` or `sys.path` hacks.

### Step 8 — Backlog entries for the divergences (documentation, not fixes)

Append to the **Inbox** section at the bottom of `docs/todo-backlog.md` (dated 2026-07-05):

```markdown
- 2026-07-05 — D1 (fable P2.5/M1): SELL-envelope closing combos — TS nakedShortGuard blanket-allows, Python evaluate_combo has no envelope check and blocks (its docstring falsely claims it allows). UI allows → API 403 = UX friction, fail-closed, LOW. Fixture C7 documents it; fixing means deciding envelope-close semantics and flipping C7 in the same PR.
  - **Notes:** verified live 2026-07-05 (`accept=False ETF_CALL_UNCOVERED`). Candidate fix directions: teach evaluate_combo a genuine-close check (reuse combo_close_covered_by_portfolio?) or drop TS's blanket allow. Beware CLAUDE.md combo guardrail: envelope action is untrusted.
- 2026-07-05 — D2 (fable P2.5/M2): ts_audit auditOpenOrders lacks the same-expiry long-call (vertical) cover branch py_audit has → false-positives covered verticals. Fail-closed, LOW.
  - **Notes:** dormant — zero runtime callers of auditOpenOrders (export + tests only). Fixtures SL11/SL18 document it; consider deleting auditOpenOrders instead of fixing (ponytail).
- 2026-07-05 — D3 (fable P2.5): naked_short_audit.\_get_stock_shares reads position-level structure_type=='Stock' and misses stock nested inside a Covered Call position → can false-positive-cancel a covered short call; TS/preflight walk legs. Investigate the snapshot producer's real shape before fixing.
  - **Notes:** live-reachable (the audit CLI cancels). Fixtures deliberately avoid nested-stock shapes; a fix PR should add nested-covered-call fixture cases.
```

---

## Verification matrix (run ALL; every command + exact expected outcome)

| #   | Command                                                                                                                                                                       | Expected                                                                                                                                                             |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | `cd /Users/chenxi/projects/xenon && uv run python scripts/infra/dev/generate_reason_codes_ts.py`                                                                              | prints `wrote .../web/lib/generated/reasonCodeNames.ts`; exit 0; 26 quoted entries ending `"SUBPROCESS_ERROR",`                                                      |
| 2   | `uv run pytest scripts/tests/test_naked_short_parity_fixtures.py -xvs`                                                                                                        | all params pass: 26 schema checks + 18 single-leg + 8 combo. `C7` passes with `py_preflight blocked=true`; `SL11`/`SL18` pass with `py_audit blocked=false`. Exit 0. |
| 3   | `uv run pytest scripts/tests/test_reason_codes_ts_drift.py -xvs`                                                                                                              | 1 passed                                                                                                                                                             |
| 4   | `uv run pytest scripts/tests/test_universe_ts_drift.py -xvs`                                                                                                                  | 1 passed (universe codegen intact after hook change)                                                                                                                 |
| 5   | `uv run pytest scripts/tests/test_preflight_reason_codes.py scripts/tests/test_naked_short_audit.py scripts/tests/test_preflight.py scripts/tests/test_preflight_route.py -q` | all passed (no behavior regressions)                                                                                                                                 |
| 6   | `cd web && npm test -- naked-short-parity-fixtures`                                                                                                                           | all cases pass, **including** `C7` (ts_guard allows) and `SL11`/`SL18` (ts_audit blocks) — the documented divergences                                                |
| 7   | `cd web && npm test -- order-reason-codes`                                                                                                                                    | `orderReasonCodes parity` + `getReasonToast fallback` pass (now backed by the generated list)                                                                        |
| 8   | `cd web && npm test -- naked-short-guard`                                                                                                                                     | all existing guard tests still pass (file unchanged)                                                                                                                 |
| 9   | `cd web && npx tsc --noEmit`                                                                                                                                                  | exit 0 (`pretypecheck`→`codegen` runs first; generated `as const` tuple typechecks)                                                                                  |
| 10  | `cd web && npm run lint`                                                                                                                                                      | exit 0                                                                                                                                                               |
| 11  | `uv run python scripts/checks/no_json_fallback_on_order_path.py`                                                                                                              | exit 0 (fixture read only by test files outside guarded dirs)                                                                                                        |
| 12  | `uv run python scripts/checks/no_json_write_on_order_path.py`                                                                                                                 | exit 0                                                                                                                                                               |
| 13  | `uv run python scripts/checks/order_path_caller_allowlist.py`                                                                                                                 | exit 0                                                                                                                                                               |
| 14  | `uv run python -m json.tool config/naked-short-parity-cases.json > /dev/null`                                                                                                 | exit 0 (valid JSON)                                                                                                                                                  |
| 15  | `uv run python scripts/infra/dev/run_pytest_affected.py`                                                                                                                      | scoped suite passed                                                                                                                                                  |

**Negative / drift-catch proofs (each must FAIL, then be reverted):**

| #   | Command                                                                                                                                    | Expected                                                                                                                           |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------- |
| 16  | Temporarily append `ZZ_TEST = "ZZ_TEST"` to `ReasonCode` in `preflight.py`; `uv run pytest scripts/tests/test_reason_codes_ts_drift.py -x` | **FAILS** "reasonCodeNames.ts is stale" (drift caught). Revert: `git checkout src/xenon/execution/preflight.py`                    |
| 17  | Temporarily delete the `PORTFOLIO_SNAPSHOT_STALE` block from 6a; `cd web && npm test -- order-reason-codes`                                | **FAILS** with `missingInTs` containing `PORTFOLIO_SNAPSHOT_STALE` (the previously-silent drift is now caught). Restore the block. |
| 18  | Temporarily flip `C7`'s `ts_guard.blocked` to `true` in the fixture; `cd web && npm test -- naked-short-parity-fixtures`                   | **FAILS** on `C7` (proves the harness asserts per-implementation expectations, not a shared verdict). Restore.                     |

**No UI, no route, no live-IB, no migration, no relay** in this change — those matrix rows are N/A (pure functions; nothing renders).

---

## Tripwires / abort criteria (STOP and report — do not paper over)

1. **A case fails against its documented per-implementation expectation** in Step 2 or 3. That means an implementation's behavior at your HEAD differs from what this plan verified at `fb5b6d8` — a **new** divergence or a rebase drift. STOP. Do NOT edit `expected` to go green. Report: the failing `id`, the surface, expected vs actual verdict.
2. **You find yourself wanting to "fix" D1/D2/D3** while implementing. DON'T — each is a separate finding/PR (Step 8 files them in the backlog). Both behavioral divergences are fail-closed/LOW; **if you discover any divergence that is fail-OPEN (an order creating naked exposure that the authoritative Python API gate accepts), that is a BLOCKER — stop everything and report it as a standalone operator finding immediately.**
3. **More than the listed files need editing.** Complete edit set: `config/naked-short-parity-cases.json` (new), `scripts/tests/test_naked_short_parity_fixtures.py` (new), `web/tests/naked-short-parity-fixtures.test.ts` (new), `scripts/infra/dev/generate_reason_codes_ts.py` (new), `web/lib/generated/reasonCodeNames.ts` (generated), `scripts/tests/test_reason_codes_ts_drift.py` (new), `web/package.json` (hooks), `.gitattributes` (1 line), `web/lib/orderReasonCodes.ts` (1 entry), `web/tests/order-reason-codes.test.ts` (swap array for import), `docs/todo-backlog.md` (3 Inbox entries). If a change requires touching `nakedShortGuard.ts`, `preflight.py`, or `naked_short_audit.py` **logic** — STOP (non-goal: no behavior change).
4. **`test_universe_ts_drift.py` breaks after the package.json hook edit** — the `codegen` composition broke `universe:gen`. STOP and fix the hook, not the universe file.
5. **Any live-IB step appears necessary** — it does not for this change. If you believe otherwise, STOP; nothing here needs a broker. (Were one ever needed elsewhere: PAPER only, `scripts/infra/dev.sh paper`, port 4002 — never live.)

---

## Rollback

Pure additive + tiny edits; no schema, no migration, no runtime path.

```bash
git checkout master
git branch -D feat/naked-short-parity-fixtures   # discard everything
```

If partially committed: revert `web/package.json` + `.gitattributes`, delete the new files (`config/naked-short-parity-cases.json`, both parity tests, the codegen script, `web/lib/generated/reasonCodeNames.ts`, the drift test), restore `web/tests/order-reason-codes.test.ts` + `web/lib/orderReasonCodes.ts` from `master`. No data or DB state touched.

---

## Incident-history row

Not an order-path _bug_ fix (no behavior change), so no `docs/reference/order-path-incident-history.md` row is required. The divergences D1–D3 are filed in `docs/todo-backlog.md` (Step 8); whichever future PR closes D1 or D3 should add its own incident-history row then. CHANGELOG entry for this PR: "Coverage-math parity fixtures + reason-code codegen (CX-1): single JSON case table with per-surface expected verdicts drives the Python and TS naked-short guards; documents divergences D1 (SELL-envelope combos) and D2 (ts_audit vertical cover); `reasonCodeNames.ts` generated from `ReasonCode`; fixes silently-missing `PORTFOLIO_SNAPSHOT_STALE` toast."
