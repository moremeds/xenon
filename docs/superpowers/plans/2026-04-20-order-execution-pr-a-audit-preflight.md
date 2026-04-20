# PR-A: Audit Parity + Preflight Gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver phases F1 (audit parity) and F2 (server-side preflight gate) of the Order Execution Foundation master plan as a single coordinated PR.

**Architecture:** A shared JSON fixture drives Gate 4 parity between the Python post-sync audit (`naked_short_audit.py`) and the TS pre-submit guard (`nakedShortGuard.ts`). A new `src/xenon/execution/preflight.py` module implements server-side Gate 4 as a pure function against an injected portfolio view and wires into FastAPI `/orders/place`. Working-order reservations are stubbed empty in F2; F4 backs them with `orders.duckdb`.

**Tech Stack:** Python 3.13 (pydantic v2, pytest, FastAPI), TypeScript (Vitest), IB Gateway paper port 4002.

**Source specs:**

- `docs/superpowers/specs/2026-04-20-single-leg-hardening-design.md` §2, §5, §13.1
- `docs/superpowers/specs/2026-04-20-leg-wizard-design.md` §11.1

**Dependencies / prior phase:** F0 shipped via PR #25 — `src/xenon/execution/universe.py` with `UNIVERSE`, `INDEX_UNIVERSE`, `is_known`, `is_index`, `get_multiplier` is on master. F0 also shipped `src/xenon/execution/contract_normalize.py` and the TS mirror generator.

---

## Scope boundary

**In scope:**

- F1: long-call-at-expiry cover recognition in `naked_short_audit.py`; `leg_wizard:*` order-tag skip; parity fixture with TS guard.
- F2: `preflight.py` module with universe check + Gate 4 (existing-short + same-expiry long-call cover) + stock SELL quantity check + cash-secured put check; wiring into FastAPI `/orders/place`; reason-code enum exposed for UI (mapping is F6).

**Out of scope (explicitly NOT in this PR):**

- Working-order reservation table (`orders_submissions`) — F4.
- Quote-token signing / limit-band gate — F3.
- Atomic idempotency INSERT — F4.
- UI reason-code toast mapping and `client_attempt_id` lifecycle — F6.
- Cancel/modify failure propagation — F5.
- Switching preflight's portfolio source from `data/portfolio.json` to live IB pool — F5 (matches TS guard source for parity in this PR).
- Pool clientId-25 coordination between concurrent audit and cancel calls (SL §13.1 second bullet) — F5. PR-A only delivers the audit's _coverage-rule_ parity; serialization at the ib_pool is a cancel/modify concern.
- BAG (combo) server-side preflight — PR-A explicitly bypasses combo orders in `_run_preflight`. TS guard still covers combo pre-submit. Server-side BAG gate is not scheduled to a specific phase.

If a task in this plan seems to require one of the above, stop and confirm — it's likely scope creep.

---

## Success criteria (writable gate per master plan §Verification)

1. **Python + TS parity**: the shared `gate4_parity.json` fixture has ≥18 cases spanning the Gate 4 table in `src/xenon/CLAUDE.md`. Both `scripts/tests/test_preflight.py::test_parity_fixture` and `web/tests/gate4-parity-fixture.test.ts` iterate the fixture and all pass.
2. **Long-call cover in audit**: the regression "SPY short call + SPY long call same expiry any strike → audit does NOT cancel" passes. Prior behaviour (cancels) is gone.
3. **Wizard tag skip in audit**: order with `orderRef` starting `leg_wizard:` is skipped by the audit.
4. **Server-side gate wired**: `POST /orders/place` with STK order for SPX returns HTTP 400 with `{reason_code: "INDEX_HAS_NO_STOCK"}` — no subprocess invocation. Verified by `test_preflight_route.py` using the same FastAPI harness pattern as existing order-route tests.
5. **No regression**: existing `scripts/tests/test_naked_short_audit.py` and `web/tests/naked-short-guard.test.ts` still pass unchanged.
6. **Coverage**: touched files ≥95% per Xenon policy.

## Smoke-test recipe (paper IB, port 4002)

Document only — executed manually before merge:

```bash
# Start IB Gateway on paper (via scripts/cloud.sh or local.sh), approve 2FA
# Then:
curl -X POST http://localhost:8321/orders/place \
  -H "Content-Type: application/json" \
  -d '{"type":"stock","symbol":"SPX","action":"BUY","quantity":1,"limitPrice":5000}'
# Expect: 400 {"reason_code":"INDEX_HAS_NO_STOCK", ...}

curl -X POST http://localhost:8321/orders/place \
  -H "Content-Type: application/json" \
  -d '{"type":"option","symbol":"SPX","action":"SELL","quantity":1,"right":"C","strike":5000,"expiry":"20260620","limitPrice":10}'
# With no long SPX call in portfolio.json → Expect: 400 {"reason_code":"INDEX_CALL_UNCOVERED"}

curl -X POST http://localhost:8321/orders/place \
  -H "Content-Type: application/json" \
  -d '{"type":"stock","symbol":"SPY","action":"BUY","quantity":1,"limitPrice":500}'
# Expect: upstream subprocess path (preflight returns ACCEPT, IB handles).
```

## Rollback recipe

```bash
# Single squash-merge commit on master. Revert via:
git revert <pr-a-merge-sha> -m 1
git push origin master
```

Rollback restores `naked_short_audit.py` prior behaviour (stock-cover only, no tag skip) and removes the FastAPI preflight hook. No schema migrations to undo — `orders.duckdb` is F4.

---

## File structure

**New files:**

- `scripts/tests/fixtures/gate4_parity.json` — shared Gate 4 parity cases (Python + TS read this).
- `scripts/tests/fixtures/__init__.py` — empty, marks fixtures as a package for clean imports.
- `src/xenon/execution/preflight.py` — module + models + `evaluate()`.
- `scripts/tests/test_preflight.py` — unit tests for `preflight.evaluate()`.
- `scripts/tests/test_preflight_route.py` — FastAPI integration test for the `/orders/place` wiring.
- `web/tests/gate4-parity-fixture.test.ts` — TS parity runner against the shared JSON.

**Modified files:**

- `src/xenon/execution/naked_short_audit.py` — add `_count_long_calls_at_expiry`, `_order_is_wizard_tagged`, extend `find_naked_short_violations`.
- `scripts/tests/test_naked_short_audit.py` — add long-call-cover and wizard-tag regressions; extend `make_option_position` with optional `expiry`.
- `src/xenon/api/server.py` — import and call `preflight.evaluate()` inside `orders_place` before subprocess invocation.
- `docs/superpowers/plans/2026-04-20-order-execution-foundation-master.md` — update F1/F2 status row to "PR-A bundled".

---

## Tasks

### Task 1: Shared Gate 4 parity fixture

**Files:**

- Create: `scripts/tests/fixtures/__init__.py`
- Create: `scripts/tests/fixtures/gate4_parity.json`

- [ ] **Step 1: Create the fixtures package marker**

```python
# scripts/tests/fixtures/__init__.py
"""Test fixtures — shared across Python and TS Gate 4 parity tests."""
```

- [ ] **Step 2: Write the JSON fixture**

Each case shape:

```json
{
  "name": "short case description",
  "request": {
    "type": "stock" | "option",
    "symbol": "SPY",
    "action": "BUY" | "SELL",
    "quantity": 1,
    "right": "C" | "P" | null,
    "expiry": "YYYYMMDD" | null,
    "strike": 200.0 | null,
    "multiplier": 100,
    "limitPrice": 10.0
  },
  "portfolio": {
    "positions": [
      {
        "ticker": "SPY",
        "structure_type": "Stock" | "Long Call" | "Short Call" | ...,
        "direction": "LONG" | "SHORT",
        "contracts": 100,
        "expiry": "YYYYMMDD" | null,
        "legs": [
          {"direction": "LONG", "type": "Stock"|"Call"|"Put", "contracts": 100, "strike": 0.0}
        ]
      }
    ]
  },
  "expected": {
    "accept": true | false,
    "reason_code": "UNIVERSE_UNKNOWN" | "INDEX_HAS_NO_STOCK" | "INSUFFICIENT_SHARES" |
                   "INSUFFICIENT_CASH" | "INDEX_CALL_UNCOVERED" | "ETF_CALL_UNCOVERED" | null
  }
}
```

Required cases (minimum 18 — one per row of the Gate 4 table in `src/xenon/CLAUDE.md` plus universe cases):

```json
{
  "version": 1,
  "cases": [
    {
      "name": "universe_unknown_ticker_blocks",
      "request": {
        "type": "stock",
        "symbol": "AAPL",
        "action": "BUY",
        "quantity": 1,
        "right": null,
        "expiry": null,
        "strike": null,
        "multiplier": 100,
        "limitPrice": 180.0
      },
      "portfolio": { "positions": [] },
      "expected": { "accept": false, "reason_code": "UNIVERSE_UNKNOWN" }
    },
    {
      "name": "spx_stk_buy_blocks",
      "request": {
        "type": "stock",
        "symbol": "SPX",
        "action": "BUY",
        "quantity": 1,
        "right": null,
        "expiry": null,
        "strike": null,
        "multiplier": 100,
        "limitPrice": 5000.0
      },
      "portfolio": { "positions": [] },
      "expected": { "accept": false, "reason_code": "INDEX_HAS_NO_STOCK" }
    },
    {
      "name": "spx_stk_sell_blocks",
      "request": {
        "type": "stock",
        "symbol": "SPX",
        "action": "SELL",
        "quantity": 1,
        "right": null,
        "expiry": null,
        "strike": null,
        "multiplier": 100,
        "limitPrice": 5000.0
      },
      "portfolio": { "positions": [] },
      "expected": { "accept": false, "reason_code": "INDEX_HAS_NO_STOCK" }
    },
    {
      "name": "spy_buy_always_ok",
      "request": {
        "type": "stock",
        "symbol": "SPY",
        "action": "BUY",
        "quantity": 100,
        "right": null,
        "expiry": null,
        "strike": null,
        "multiplier": 100,
        "limitPrice": 500.0
      },
      "portfolio": { "positions": [] },
      "expected": { "accept": true, "reason_code": null }
    },
    {
      "name": "spy_sell_stock_no_shares_blocks",
      "request": {
        "type": "stock",
        "symbol": "SPY",
        "action": "SELL",
        "quantity": 100,
        "right": null,
        "expiry": null,
        "strike": null,
        "multiplier": 100,
        "limitPrice": 500.0
      },
      "portfolio": { "positions": [] },
      "expected": { "accept": false, "reason_code": "INSUFFICIENT_SHARES" }
    },
    {
      "name": "spy_sell_stock_exceeds_held_blocks",
      "request": {
        "type": "stock",
        "symbol": "SPY",
        "action": "SELL",
        "quantity": 200,
        "right": null,
        "expiry": null,
        "strike": null,
        "multiplier": 100,
        "limitPrice": 500.0
      },
      "portfolio": {
        "positions": [
          {
            "ticker": "SPY",
            "structure_type": "Stock",
            "direction": "LONG",
            "contracts": 100,
            "expiry": null,
            "legs": [
              {
                "direction": "LONG",
                "type": "Stock",
                "contracts": 100,
                "strike": 0.0
              }
            ]
          }
        ]
      },
      "expected": { "accept": false, "reason_code": "INSUFFICIENT_SHARES" }
    },
    {
      "name": "spy_sell_stock_within_held_ok",
      "request": {
        "type": "stock",
        "symbol": "SPY",
        "action": "SELL",
        "quantity": 100,
        "right": null,
        "expiry": null,
        "strike": null,
        "multiplier": 100,
        "limitPrice": 500.0
      },
      "portfolio": {
        "positions": [
          {
            "ticker": "SPY",
            "structure_type": "Stock",
            "direction": "LONG",
            "contracts": 100,
            "expiry": null,
            "legs": [
              {
                "direction": "LONG",
                "type": "Stock",
                "contracts": 100,
                "strike": 0.0
              }
            ]
          }
        ]
      },
      "expected": { "accept": true, "reason_code": null }
    },
    {
      "name": "spy_sell_put_cash_secured_ok",
      "request": {
        "type": "option",
        "symbol": "SPY",
        "action": "SELL",
        "quantity": 1,
        "right": "P",
        "expiry": "20260620",
        "strike": 480.0,
        "multiplier": 100,
        "limitPrice": 5.0
      },
      "portfolio": { "positions": [] },
      "expected": { "accept": true, "reason_code": null }
    },
    {
      "name": "spx_sell_call_no_cover_blocks",
      "request": {
        "type": "option",
        "symbol": "SPX",
        "action": "SELL",
        "quantity": 1,
        "right": "C",
        "expiry": "20260620",
        "strike": 5100.0,
        "multiplier": 100,
        "limitPrice": 10.0
      },
      "portfolio": { "positions": [] },
      "expected": { "accept": false, "reason_code": "INDEX_CALL_UNCOVERED" }
    },
    {
      "name": "spx_sell_call_with_long_call_same_expiry_diff_strike_ok",
      "request": {
        "type": "option",
        "symbol": "SPX",
        "action": "SELL",
        "quantity": 1,
        "right": "C",
        "expiry": "20260620",
        "strike": 5100.0,
        "multiplier": 100,
        "limitPrice": 10.0
      },
      "portfolio": {
        "positions": [
          {
            "ticker": "SPX",
            "structure_type": "Long Call",
            "direction": "LONG",
            "contracts": 1,
            "expiry": "20260620",
            "legs": [
              {
                "direction": "LONG",
                "type": "Call",
                "contracts": 1,
                "strike": 5000.0
              }
            ]
          }
        ]
      },
      "expected": { "accept": true, "reason_code": null }
    },
    {
      "name": "spx_sell_call_long_call_different_expiry_blocks",
      "request": {
        "type": "option",
        "symbol": "SPX",
        "action": "SELL",
        "quantity": 1,
        "right": "C",
        "expiry": "20260620",
        "strike": 5100.0,
        "multiplier": 100,
        "limitPrice": 10.0
      },
      "portfolio": {
        "positions": [
          {
            "ticker": "SPX",
            "structure_type": "Long Call",
            "direction": "LONG",
            "contracts": 1,
            "expiry": "20260718",
            "legs": [
              {
                "direction": "LONG",
                "type": "Call",
                "contracts": 1,
                "strike": 5000.0
              }
            ]
          }
        ]
      },
      "expected": { "accept": false, "reason_code": "INDEX_CALL_UNCOVERED" }
    },
    {
      "name": "spy_sell_call_no_cover_blocks",
      "request": {
        "type": "option",
        "symbol": "SPY",
        "action": "SELL",
        "quantity": 1,
        "right": "C",
        "expiry": "20260620",
        "strike": 500.0,
        "multiplier": 100,
        "limitPrice": 5.0
      },
      "portfolio": { "positions": [] },
      "expected": { "accept": false, "reason_code": "ETF_CALL_UNCOVERED" }
    },
    {
      "name": "spy_sell_call_with_100_shares_ok",
      "request": {
        "type": "option",
        "symbol": "SPY",
        "action": "SELL",
        "quantity": 1,
        "right": "C",
        "expiry": "20260620",
        "strike": 500.0,
        "multiplier": 100,
        "limitPrice": 5.0
      },
      "portfolio": {
        "positions": [
          {
            "ticker": "SPY",
            "structure_type": "Stock",
            "direction": "LONG",
            "contracts": 100,
            "expiry": null,
            "legs": [
              {
                "direction": "LONG",
                "type": "Stock",
                "contracts": 100,
                "strike": 0.0
              }
            ]
          }
        ]
      },
      "expected": { "accept": true, "reason_code": null }
    },
    {
      "name": "spy_sell_call_100_shares_but_existing_short_exhausts_cover_blocks",
      "request": {
        "type": "option",
        "symbol": "SPY",
        "action": "SELL",
        "quantity": 1,
        "right": "C",
        "expiry": "20260620",
        "strike": 500.0,
        "multiplier": 100,
        "limitPrice": 5.0
      },
      "portfolio": {
        "positions": [
          {
            "ticker": "SPY",
            "structure_type": "Stock",
            "direction": "LONG",
            "contracts": 100,
            "expiry": null,
            "legs": [
              {
                "direction": "LONG",
                "type": "Stock",
                "contracts": 100,
                "strike": 0.0
              }
            ]
          },
          {
            "ticker": "SPY",
            "structure_type": "Short Call",
            "direction": "SHORT",
            "contracts": 1,
            "expiry": "20260515",
            "legs": [
              {
                "direction": "SHORT",
                "type": "Call",
                "contracts": 1,
                "strike": 510.0
              }
            ]
          }
        ]
      },
      "expected": { "accept": false, "reason_code": "ETF_CALL_UNCOVERED" }
    },
    {
      "name": "spy_sell_call_with_long_call_vertical_ok",
      "request": {
        "type": "option",
        "symbol": "SPY",
        "action": "SELL",
        "quantity": 1,
        "right": "C",
        "expiry": "20260620",
        "strike": 510.0,
        "multiplier": 100,
        "limitPrice": 2.0
      },
      "portfolio": {
        "positions": [
          {
            "ticker": "SPY",
            "structure_type": "Long Call",
            "direction": "LONG",
            "contracts": 1,
            "expiry": "20260620",
            "legs": [
              {
                "direction": "LONG",
                "type": "Call",
                "contracts": 1,
                "strike": 500.0
              }
            ]
          }
        ]
      },
      "expected": { "accept": true, "reason_code": null }
    },
    {
      "name": "spy_sell_put_overlapping_held_cash_ok_universe_known",
      "request": {
        "type": "option",
        "symbol": "QQQ",
        "action": "SELL",
        "quantity": 1,
        "right": "P",
        "expiry": "20260620",
        "strike": 400.0,
        "multiplier": 100,
        "limitPrice": 3.0
      },
      "portfolio": { "positions": [] },
      "expected": { "accept": true, "reason_code": null }
    },
    {
      "name": "spy_buy_call_always_ok",
      "request": {
        "type": "option",
        "symbol": "SPY",
        "action": "BUY",
        "quantity": 1,
        "right": "C",
        "expiry": "20260620",
        "strike": 500.0,
        "multiplier": 100,
        "limitPrice": 5.0
      },
      "portfolio": { "positions": [] },
      "expected": { "accept": true, "reason_code": null }
    },
    {
      "name": "spy_sell_to_close_exact_match_ok",
      "request": {
        "type": "option",
        "symbol": "SPY",
        "action": "SELL",
        "quantity": 1,
        "right": "C",
        "expiry": "20260620",
        "strike": 500.0,
        "multiplier": 100,
        "limitPrice": 5.0
      },
      "portfolio": {
        "positions": [
          {
            "ticker": "SPY",
            "structure_type": "Long Call",
            "direction": "LONG",
            "contracts": 1,
            "expiry": "20260620",
            "legs": [
              {
                "direction": "LONG",
                "type": "Call",
                "contracts": 1,
                "strike": 500.0
              }
            ]
          }
        ]
      },
      "expected": { "accept": true, "reason_code": null }
    }
  ]
}
```

- [ ] **Step 3: Commit**

```bash
git add scripts/tests/fixtures/__init__.py scripts/tests/fixtures/gate4_parity.json
git commit -m "test(preflight): add shared Gate 4 parity fixture (F1+F2 PR-A)"
```

---

### Task 2: Extend `make_option_position` to carry expiry

**Files:**

- Modify: `scripts/tests/test_naked_short_audit.py:61-76`

- [ ] **Step 1: Read the file**

Read `scripts/tests/test_naked_short_audit.py` to confirm current `make_option_position` signature.

- [ ] **Step 2: Extend signature (backward-compatible default `None`)**

```python
def make_option_position(ticker, direction, opt_type, contracts, strike=100.0, expiry=None):
    """Build an option position (e.g. SHORT call leg in portfolio)."""
    return {
        "ticker": ticker,
        "structure_type": "Long Call" if direction == "LONG" else "Short Call",
        "contracts": contracts,
        "direction": direction,
        "expiry": expiry,
        "legs": [
            {
                "direction": direction,
                "contracts": contracts,
                "type": opt_type,
                "strike": strike,
            }
        ],
    }
```

Also update `make_order` to accept `order_ref: str | None = None` and include in the returned dict:

```python
def make_order(
    order_id, perm_id, symbol, sec_type, action, qty, status="Submitted",
    right="?", strike=0.0, expiry=None, order_ref=None
):
    return {
        "orderId": order_id,
        "permId": perm_id,
        "symbol": symbol,
        "orderRef": order_ref,
        "contract": {
            "conId": 100000 + order_id,
            "symbol": symbol,
            "secType": sec_type,
            "strike": strike,
            "right": right,
            "expiry": expiry,
        },
        "action": action,
        "orderType": "LMT",
        "totalQuantity": float(qty),
        "limitPrice": 10.0,
        "auxPrice": 0.0,
        "status": status,
        "filled": 0.0,
        "remaining": float(qty),
        "avgFillPrice": 0.0,
        "tif": "GTC",
    }
```

- [ ] **Step 3: Run existing tests — must still pass**

```bash
python3.13 -m pytest scripts/tests/test_naked_short_audit.py -xvs
```

Expected: all pre-existing tests PASS (signature is backward compatible).

- [ ] **Step 4: Commit**

```bash
git add scripts/tests/test_naked_short_audit.py
git commit -m "test(naked-short): extend fixtures with expiry + orderRef for F1 parity tests"
```

---

### Task 3: Add failing test — long-call cover at same expiry

**Files:**

- Modify: `scripts/tests/test_naked_short_audit.py` — append new test.

- [ ] **Step 1: Write the failing test**

Append to the end of `scripts/tests/test_naked_short_audit.py`:

```python
# ---------------------------------------------------------------------------
# F1: long-call cover at same expiry (parity with nakedShortGuard.ts:254-260)
# ---------------------------------------------------------------------------


def test_short_call_with_long_call_same_expiry_not_violation():
    """Parity with TS guard: long call at same expiry (any strike) covers short call."""
    orders = [
        make_order(
            1, 1001, "SPY", "OPT", "SELL", 1,
            right="C", strike=510.0, expiry="20260620",
        ),
    ]
    positions = [
        make_option_position(
            "SPY", direction="LONG", opt_type="Call",
            contracts=1, strike=500.0, expiry="20260620",
        ),
    ]

    violations = find_naked_short_violations(orders, positions)

    assert violations == [], (
        "SPY short call with SPY long call same expiry (vertical spread) "
        "must not be flagged as naked — parity with nakedShortGuard.ts"
    )


def test_short_call_with_long_call_different_expiry_still_violation():
    """Different-expiry long call is NOT cover — still violation."""
    orders = [
        make_order(
            2, 1002, "SPY", "OPT", "SELL", 1,
            right="C", strike=510.0, expiry="20260620",
        ),
    ]
    positions = [
        make_option_position(
            "SPY", direction="LONG", opt_type="Call",
            contracts=1, strike=500.0, expiry="20260718",  # different expiry
        ),
    ]

    violations = find_naked_short_violations(orders, positions)

    assert len(violations) == 1
    assert violations[0]["symbol"] == "SPY"


def test_short_call_exact_match_sell_to_close_not_violation():
    """Selling exactly the long call you hold (same strike+expiry) is a close, not a naked short."""
    orders = [
        make_order(
            3, 1003, "SPY", "OPT", "SELL", 1,
            right="C", strike=500.0, expiry="20260620",
        ),
    ]
    positions = [
        make_option_position(
            "SPY", direction="LONG", opt_type="Call",
            contracts=1, strike=500.0, expiry="20260620",
        ),
    ]

    violations = find_naked_short_violations(orders, positions)

    assert violations == []
```

- [ ] **Step 2: Run and verify failure**

```bash
python3.13 -m pytest scripts/tests/test_naked_short_audit.py::test_short_call_with_long_call_same_expiry_not_violation -xvs
```

Expected: **FAIL** — current audit only checks stock shares; long call is not recognised as cover.

- [ ] **Step 3: Don't commit yet** — Task 4 implements the fix.

---

### Task 4: Implement long-call-at-expiry cover in audit

**Files:**

- Modify: `src/xenon/execution/naked_short_audit.py` — add helpers, extend `find_naked_short_violations`.

- [ ] **Step 1: Add normalization + two new helpers**

Insert after the existing `_get_short_call_contracts` function (around line 48):

```python
def _normalize_expiry(expiry: str | None) -> str | None:
    """Canonicalize expiry to YYYYMMDD. Returns None if missing or wrong shape."""
    if not expiry:
        return None
    clean = expiry.replace("-", "")
    return clean if len(clean) == 8 and clean.isdigit() else None


def _count_long_calls_at_expiry(positions: list, ticker: str, expiry: str | None) -> int:
    """Sum LONG call contracts for ticker at the given expiry (any strike).

    Matches web/lib/nakedShortGuard.ts countLongCallsAtExpiry() for parity.
    """
    normalized = _normalize_expiry(expiry)
    if normalized is None:
        return 0

    total = 0
    for pos in positions:
        if pos.get("ticker", pos.get("symbol", "")).upper() != ticker.upper():
            continue
        if _normalize_expiry(pos.get("expiry")) != normalized:
            continue
        for leg in pos.get("legs", []):
            if leg.get("direction") == "LONG" and leg.get("type") == "Call":
                total += int(leg.get("contracts", 0))
    return total


def _count_matching_long_options(
    positions: list, ticker: str, expiry: str | None, strike: float | None, right: str
) -> int:
    """Sum LONG option contracts for the exact (expiry, strike, right) — selling-to-close detector.

    Matches web/lib/nakedShortGuard.ts countMatchingLongOptionContracts() for parity.
    `right` is the IB single-letter: "C" or "P".
    """
    normalized = _normalize_expiry(expiry)
    if normalized is None or strike is None or right not in ("C", "P"):
        return 0

    expected_type = "Call" if right == "C" else "Put"
    total = 0
    for pos in positions:
        if pos.get("ticker", pos.get("symbol", "")).upper() != ticker.upper():
            continue
        if _normalize_expiry(pos.get("expiry")) != normalized:
            continue
        for leg in pos.get("legs", []):
            if (
                leg.get("direction") == "LONG"
                and leg.get("type") == expected_type
                and float(leg.get("strike", 0.0)) == float(strike)
            ):
                total += int(leg.get("contracts", 0))
    return total
```

- [ ] **Step 2: Update the SELL-CALL branch of `find_naked_short_violations`**

Replace the existing SELL-call block (lines ~115-144) with:

```python
        # SELL option
        if sec_type == "OPT":
            right = contract.get("right", "").upper()
            expiry = contract.get("expiry")
            strike = contract.get("strike")

            # SELL put is cash-secured — never a violation
            if right == "P":
                continue

            # SELL call — parity with web/lib/nakedShortGuard.ts
            if right == "C":
                # 1. Sell-to-close exact match → allowed
                closing_long = _count_matching_long_options(
                    positions, symbol, expiry, strike, "C"
                )
                remaining_after_close = max(qty - closing_long, 0)
                if remaining_after_close == 0:
                    continue

                # 2. Vertical spread cover: long calls at same expiry, any strike
                long_calls_at_expiry = _count_long_calls_at_expiry(
                    positions, symbol, expiry
                )
                spread_cover = max(long_calls_at_expiry - closing_long, 0)
                remaining_after_spread = max(remaining_after_close - spread_cover, 0)
                if remaining_after_spread == 0:
                    continue

                # 3. Fall back to stock cover
                shares_held = _get_stock_shares(positions, symbol)
                if shares_held == 0 and spread_cover == 0:
                    violations.append(
                        {
                            "order_id": order_id,
                            "perm_id": perm_id,
                            "symbol": symbol,
                            "reason": (
                                f"SELL {qty} call(s) on {symbol}: no long stock or "
                                f"vertical-spread cover at expiry {expiry} — naked short call"
                            ),
                        }
                    )
                    continue

                existing_short_calls = _get_short_call_contracts(positions, symbol)
                total_short_contracts = existing_short_calls + remaining_after_spread
                covered_contracts = shares_held // 100

                if total_short_contracts > covered_contracts:
                    violations.append(
                        {
                            "order_id": order_id,
                            "perm_id": perm_id,
                            "symbol": symbol,
                            "reason": (
                                f"SELL {qty} call(s) on {symbol}: total short "
                                f"({total_short_contracts}) exceeds stock cover "
                                f"({covered_contracts}) after vertical-spread accounting — "
                                f"under-covered"
                            ),
                        }
                    )
            continue
```

- [ ] **Step 3: Run the new tests**

```bash
python3.13 -m pytest scripts/tests/test_naked_short_audit.py::test_short_call_with_long_call_same_expiry_not_violation -xvs
python3.13 -m pytest scripts/tests/test_naked_short_audit.py::test_short_call_with_long_call_different_expiry_still_violation -xvs
python3.13 -m pytest scripts/tests/test_naked_short_audit.py::test_short_call_exact_match_sell_to_close_not_violation -xvs
```

Expected: all PASS.

- [ ] **Step 4: Run the FULL file to ensure no regressions**

```bash
python3.13 -m pytest scripts/tests/test_naked_short_audit.py -xvs
```

Expected: all prior tests still PASS.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/execution/naked_short_audit.py scripts/tests/test_naked_short_audit.py
git commit -m "feat(audit): recognize long-call cover at same expiry (F1 parity with TS guard)

Extends naked_short_audit with _count_long_calls_at_expiry and
_count_matching_long_options helpers matching nakedShortGuard.ts lines
254-260. SELL call with long call at same expiry (any strike) or exact
sell-to-close is no longer flagged as a violation.

Part of PR-A (F1 audit parity + F2 preflight gate) of the Order
Execution Foundation master plan."
```

---

### Task 5: Add leg_wizard tag skip

**Files:**

- Modify: `src/xenon/execution/naked_short_audit.py`
- Modify: `scripts/tests/test_naked_short_audit.py`

- [ ] **Step 1: Write the failing test**

Append to `test_naked_short_audit.py`:

```python
# ---------------------------------------------------------------------------
# F1: leg_wizard tag skip (wizard-owned orders are governed server-side)
# ---------------------------------------------------------------------------


def test_leg_wizard_tagged_order_is_skipped():
    """Order with orderRef starting 'leg_wizard:' is skipped even if apparently naked."""
    orders = [
        make_order(
            10, 2001, "SPY", "OPT", "SELL", 1,
            right="C", strike=510.0, expiry="20260620",
            order_ref="leg_wizard:session_abc123",
        ),
    ]
    positions = []  # no cover — would normally violate

    violations = find_naked_short_violations(orders, positions)

    assert violations == [], (
        "Wizard-tagged orders are governed by server-side Gate 4; "
        "the post-sync audit must skip them per spec Wiz §11.1"
    )


def test_non_wizard_order_still_checked():
    """Control: same naked order without the tag is still flagged."""
    orders = [
        make_order(
            11, 2002, "SPY", "OPT", "SELL", 1,
            right="C", strike=510.0, expiry="20260620",
            order_ref=None,
        ),
    ]
    positions = []

    violations = find_naked_short_violations(orders, positions)

    assert len(violations) == 1


def test_leg_wizard_tag_prefix_must_match_exactly():
    """orderRef must start with literal 'leg_wizard:' — partial matches don't skip."""
    orders = [
        make_order(
            12, 2003, "SPY", "OPT", "SELL", 1,
            right="C", strike=510.0, expiry="20260620",
            order_ref="manual_leg_wizard_misleading",
        ),
    ]
    positions = []

    violations = find_naked_short_violations(orders, positions)

    assert len(violations) == 1, "Only exact 'leg_wizard:' prefix skips"
```

- [ ] **Step 2: Verify failure**

```bash
python3.13 -m pytest scripts/tests/test_naked_short_audit.py::test_leg_wizard_tagged_order_is_skipped -xvs
```

Expected: FAIL.

- [ ] **Step 3: Implement the skip**

In `naked_short_audit.py`, add after the helpers:

```python
WIZARD_TAG_PREFIX = "leg_wizard:"


def _order_is_wizard_tagged(order: dict) -> bool:
    """True if the order is owned by the leg-wizard and should be skipped by the audit.

    The wizard applies server-side Gate 4 per-leg (see Wiz spec §11.1); the post-sync
    audit must not race the wizard by cancelling in-flight wizard legs.
    """
    ref = order.get("orderRef") or ""
    return isinstance(ref, str) and ref.startswith(WIZARD_TAG_PREFIX)
```

Then inside `find_naked_short_violations` loop, add as the first check (just after `if order.get("status") not in ACTIVE_STATUSES: continue`):

```python
        if _order_is_wizard_tagged(order):
            continue
```

- [ ] **Step 4: Verify all three new tests pass**

```bash
python3.13 -m pytest scripts/tests/test_naked_short_audit.py -xvs -k "wizard"
python3.13 -m pytest scripts/tests/test_naked_short_audit.py -xvs
```

- [ ] **Step 5: Commit**

```bash
git add src/xenon/execution/naked_short_audit.py scripts/tests/test_naked_short_audit.py
git commit -m "feat(audit): skip leg_wizard-tagged orders (F1 wizard awareness)

Adds WIZARD_TAG_PREFIX detection so post-sync audit does not cancel
in-flight wizard legs governed by server-side Gate 4. Per Wiz spec §11.1."
```

---

### Task 6: Preflight models + universe check

**Files:**

- Create: `src/xenon/execution/preflight.py`
- Create: `scripts/tests/test_preflight.py`

- [ ] **Step 1: Write the failing tests**

Create `scripts/tests/test_preflight.py`:

```python
"""Unit tests for src/xenon/execution/preflight.py (F2 server-side Gate 4)."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from xenon.execution.preflight import (
    PreflightRequest,
    PortfolioView,
    ReasonCode,
    Verdict,
    evaluate,
)


def _stock_position(ticker: str, contracts: int) -> dict:
    return {
        "ticker": ticker,
        "structure_type": "Stock",
        "direction": "LONG",
        "contracts": contracts,
        "expiry": None,
        "legs": [
            {"direction": "LONG", "type": "Stock", "contracts": contracts, "strike": 0.0}
        ],
    }


def _long_call_position(ticker: str, strike: float, expiry: str, contracts: int = 1) -> dict:
    return {
        "ticker": ticker,
        "structure_type": "Long Call",
        "direction": "LONG",
        "contracts": contracts,
        "expiry": expiry,
        "legs": [
            {"direction": "LONG", "type": "Call", "contracts": contracts, "strike": strike}
        ],
    }


def _short_call_position(ticker: str, strike: float, expiry: str, contracts: int = 1) -> dict:
    return {
        "ticker": ticker,
        "structure_type": "Short Call",
        "direction": "SHORT",
        "contracts": contracts,
        "expiry": expiry,
        "legs": [
            {"direction": "SHORT", "type": "Call", "contracts": contracts, "strike": strike}
        ],
    }


def _make_request(**overrides) -> PreflightRequest:
    base = dict(
        ticker="SPY",
        security_type="STK",
        action="BUY",
        quantity=1,
        right=None,
        expiry=None,
        strike=None,
        multiplier=100,
        limit_price=500.0,
    )
    base.update(overrides)
    return PreflightRequest(**base)


def test_universe_unknown_ticker_blocks():
    verdict = evaluate(_make_request(ticker="AAPL"), PortfolioView(positions=[]))
    assert verdict.accept is False
    assert verdict.reason_code == ReasonCode.UNIVERSE_UNKNOWN


def test_index_stk_buy_blocks():
    verdict = evaluate(
        _make_request(ticker="SPX", security_type="STK", action="BUY"),
        PortfolioView(positions=[]),
    )
    assert verdict.accept is False
    assert verdict.reason_code == ReasonCode.INDEX_HAS_NO_STOCK


def test_index_stk_sell_blocks():
    verdict = evaluate(
        _make_request(ticker="NDX", security_type="STK", action="SELL"),
        PortfolioView(positions=[]),
    )
    assert verdict.accept is False
    assert verdict.reason_code == ReasonCode.INDEX_HAS_NO_STOCK
```

- [ ] **Step 2: Verify import fails (module doesn't exist)**

```bash
python3.13 -m pytest scripts/tests/test_preflight.py -xvs
```

Expected: **ImportError** — module doesn't exist yet.

- [ ] **Step 3: Create the module**

Create `src/xenon/execution/preflight.py`:

```python
"""Server-side Gate 4 preflight evaluation.

Spec: docs/superpowers/specs/2026-04-20-single-leg-hardening-design.md §5

This is a pure function over an injected PortfolioView so it's trivially
testable. Wiring into FastAPI /orders/place is in src/xenon/api/server.py.

Working-order reservations are stubbed empty in F2 (see WorkingReservations
below). Phase F4 will replace the stub with a duckdb-backed read from
orders_submissions per spec §12.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from xenon.execution.universe import UNIVERSE, is_index, is_known


class ReasonCode(StrEnum):
    """Preflight block reasons. UI copy maps these in F6.

    Only codes relevant to F2 are defined here. F3 adds STALE_QUOTE /
    LIMIT_OUT_OF_BAND / LIMIT_OFF_TICK; F4 adds ATTEMPT_ID_TERMINAL;
    F5 adds IB_CONNECTION / OWNERSHIP; F6 adds MODIFY_STALE.
    """

    UNIVERSE_UNKNOWN = "UNIVERSE_UNKNOWN"
    INDEX_HAS_NO_STOCK = "INDEX_HAS_NO_STOCK"
    INSUFFICIENT_SHARES = "INSUFFICIENT_SHARES"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    INDEX_CALL_UNCOVERED = "INDEX_CALL_UNCOVERED"
    ETF_CALL_UNCOVERED = "ETF_CALL_UNCOVERED"


class PreflightRequest(BaseModel):
    """Server-side input to evaluate(). Constructed from the /orders/place body."""

    ticker: str
    security_type: Literal["STK", "OPT"]
    action: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    right: Literal["C", "P"] | None = None
    expiry: str | None = None
    strike: Decimal | None = None
    multiplier: int = 100
    limit_price: Decimal


class PortfolioLeg(BaseModel):
    direction: Literal["LONG", "SHORT"]
    type: Literal["Stock", "Call", "Put"]
    contracts: int
    strike: float = 0.0


class PortfolioPosition(BaseModel):
    ticker: str
    structure_type: str
    direction: Literal["LONG", "SHORT"] = "LONG"
    contracts: int
    expiry: str | None = None
    legs: list[PortfolioLeg]


class PortfolioView(BaseModel):
    """Snapshot injected into evaluate(). Matches data/portfolio.json shape.

    F5 migrates the source to live IB pool; for F2 the callsite
    (server.py) loads from portfolio.json for parity with the TS guard.
    """

    positions: list[PortfolioPosition] = Field(default_factory=list)
    available_funds: Decimal = Decimal("0")


class WorkingReservations(BaseModel):
    """Placeholder for F4. Always empty in F2."""

    stock_sell_qty: int = 0
    short_call_qty: int = 0
    short_put_cash_required: Decimal = Decimal("0")
    long_call_close_qty_same_exp: int = 0


class Verdict(BaseModel):
    accept: bool
    reason_code: ReasonCode | None = None
    reason_detail: str | None = None


def _normalize_expiry(expiry: str | None) -> str | None:
    if not expiry:
        return None
    clean = expiry.replace("-", "")
    return clean if len(clean) == 8 and clean.isdigit() else None


def _count_long_shares(positions: list[PortfolioPosition], ticker: str) -> int:
    total = 0
    for pos in positions:
        if pos.ticker.upper() != ticker.upper():
            continue
        for leg in pos.legs:
            if leg.type == "Stock" and leg.direction == "LONG":
                total += leg.contracts
    return total


def _count_long_calls_at_expiry(
    positions: list[PortfolioPosition], ticker: str, expiry: str | None
) -> int:
    normalized = _normalize_expiry(expiry)
    if normalized is None:
        return 0
    total = 0
    for pos in positions:
        if pos.ticker.upper() != ticker.upper():
            continue
        if _normalize_expiry(pos.expiry) != normalized:
            continue
        for leg in pos.legs:
            if leg.direction == "LONG" and leg.type == "Call":
                total += leg.contracts
    return total


def _count_matching_long_options(
    positions: list[PortfolioPosition],
    ticker: str,
    expiry: str | None,
    strike: Decimal | None,
    right: Literal["C", "P"],
) -> int:
    normalized = _normalize_expiry(expiry)
    if normalized is None or strike is None:
        return 0
    expected = "Call" if right == "C" else "Put"
    total = 0
    for pos in positions:
        if pos.ticker.upper() != ticker.upper():
            continue
        if _normalize_expiry(pos.expiry) != normalized:
            continue
        for leg in pos.legs:
            if (
                leg.direction == "LONG"
                and leg.type == expected
                and Decimal(str(leg.strike)) == strike
            ):
                total += leg.contracts
    return total


def _count_existing_short_calls(positions: list[PortfolioPosition], ticker: str) -> int:
    total = 0
    for pos in positions:
        if pos.ticker.upper() != ticker.upper():
            continue
        for leg in pos.legs:
            if leg.type == "Call" and leg.direction == "SHORT":
                total += leg.contracts
    return total


def evaluate(
    req: PreflightRequest,
    portfolio: PortfolioView,
    reservations: WorkingReservations | None = None,
) -> Verdict:
    """Evaluate Gate 4 server-side. Pure function.

    F2: universe + Gate 4 using `portfolio` (live-like view) and empty-by-default
    `reservations` (F4 replaces the stub with duckdb reads).
    """
    reservations = reservations or WorkingReservations()

    # ① Universe
    if not is_known(req.ticker):
        return Verdict(
            accept=False,
            reason_code=ReasonCode.UNIVERSE_UNKNOWN,
            reason_detail=f"{req.ticker} not in V1 universe",
        )

    if req.security_type == "STK" and is_index(req.ticker):
        return Verdict(
            accept=False,
            reason_code=ReasonCode.INDEX_HAS_NO_STOCK,
            reason_detail=f"{req.ticker} is an index — no stock leg exists",
        )

    # ② BUY never creates short exposure
    if req.action == "BUY":
        return Verdict(accept=True)

    # ③ Stock SELL — must be covered by shares (minus working sells)
    if req.security_type == "STK":
        held = _count_long_shares(portfolio.positions, req.ticker)
        available = held - reservations.stock_sell_qty
        if req.quantity > available:
            return Verdict(
                accept=False,
                reason_code=ReasonCode.INSUFFICIENT_SHARES,
                reason_detail=(
                    f"SELL {req.quantity} shares of {req.ticker} exceeds "
                    f"{available} available ({held} held, "
                    f"{reservations.stock_sell_qty} reserved)"
                ),
            )
        return Verdict(accept=True)

    # ④ Option SELL
    # SELL put — cash-secured; F2 accepts unconditionally (F4 will enforce funds)
    if req.right == "P":
        return Verdict(accept=True)

    # SELL call — Gate 4
    if req.right == "C":
        # Sell-to-close exact match
        closing = _count_matching_long_options(
            portfolio.positions, req.ticker, req.expiry, req.strike, "C"
        )
        remaining_after_close = max(req.quantity - closing, 0)
        if remaining_after_close == 0:
            return Verdict(accept=True)

        # Vertical spread cover at same expiry
        long_at_expiry = _count_long_calls_at_expiry(
            portfolio.positions, req.ticker, req.expiry
        )
        working_closes = reservations.long_call_close_qty_same_exp
        long_cover_available = max(long_at_expiry - closing - working_closes, 0)
        remaining_after_spread = max(remaining_after_close - long_cover_available, 0)
        if remaining_after_spread == 0:
            return Verdict(accept=True)

        # Index: stock cover impossible
        if is_index(req.ticker):
            return Verdict(
                accept=False,
                reason_code=ReasonCode.INDEX_CALL_UNCOVERED,
                reason_detail=(
                    f"SELL {req.quantity} {req.ticker} call(s) at expiry {req.expiry}: "
                    f"index options require long-call cover (same expiry); "
                    f"{long_cover_available} contracts available"
                ),
            )

        # ETF: fall back to stock cover
        existing_short = _count_existing_short_calls(portfolio.positions, req.ticker)
        shares = _count_long_shares(portfolio.positions, req.ticker)
        share_cover_units = max(
            shares - reservations.stock_sell_qty, 0
        ) // req.multiplier

        total_cover = share_cover_units + long_cover_available
        total_short_after = (
            existing_short + reservations.short_call_qty + remaining_after_spread
        )
        if total_cover < total_short_after:
            return Verdict(
                accept=False,
                reason_code=ReasonCode.ETF_CALL_UNCOVERED,
                reason_detail=(
                    f"SELL {req.quantity} {req.ticker} call(s): total short after fill "
                    f"({total_short_after}) exceeds cover ({total_cover}) — "
                    f"{share_cover_units} from shares + {long_cover_available} from long calls"
                ),
            )
        return Verdict(accept=True)

    # Option SELL with no right shouldn't reach here thanks to pydantic validation,
    # but return a safe reject:
    return Verdict(
        accept=False,
        reason_code=ReasonCode.UNIVERSE_UNKNOWN,
        reason_detail="option SELL without right (C/P) is not permitted",
    )
```

- [ ] **Step 4: Run the three universe tests**

```bash
python3.13 -m pytest scripts/tests/test_preflight.py -xvs -k "universe or index_stk"
```

Expected: all three PASS.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/execution/preflight.py scripts/tests/test_preflight.py
git commit -m "feat(preflight): add module with universe gate (F2)

Introduces preflight.evaluate() pure function with PreflightRequest,
PortfolioView, Verdict, and ReasonCode. F2 implements universe check
and scaffolds the Gate 4 logic; subsequent tasks land BUY bypass,
stock SELL, and option SELL branches with tests."
```

---

### Task 7: Preflight — stock SELL quantity-vs-held

**Files:**

- Modify: `scripts/tests/test_preflight.py`

- [ ] **Step 1: Write failing tests**

Append to `test_preflight.py`:

```python
def test_stock_buy_always_ok():
    v = evaluate(
        _make_request(ticker="SPY", security_type="STK", action="BUY", quantity=100),
        PortfolioView(positions=[]),
    )
    assert v.accept is True


def test_stock_sell_no_shares_blocks():
    v = evaluate(
        _make_request(ticker="SPY", security_type="STK", action="SELL", quantity=100),
        PortfolioView(positions=[]),
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.INSUFFICIENT_SHARES


def test_stock_sell_within_held_ok():
    v = evaluate(
        _make_request(ticker="SPY", security_type="STK", action="SELL", quantity=100),
        PortfolioView(positions=[_stock_position("SPY", 100)]),
    )
    assert v.accept is True


def test_stock_sell_exceeds_held_blocks():
    v = evaluate(
        _make_request(ticker="SPY", security_type="STK", action="SELL", quantity=200),
        PortfolioView(positions=[_stock_position("SPY", 100)]),
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.INSUFFICIENT_SHARES
```

- [ ] **Step 2: Run — all pass (implementation already covers these)**

```bash
python3.13 -m pytest scripts/tests/test_preflight.py -xvs -k "stock"
```

Expected: all PASS. (The module was implemented complete in Task 6; this task documents test coverage.)

- [ ] **Step 3: Commit**

```bash
git add scripts/tests/test_preflight.py
git commit -m "test(preflight): stock SELL quantity-vs-held coverage"
```

---

### Task 8: Preflight — option SELL branches

**Files:**

- Modify: `scripts/tests/test_preflight.py`

- [ ] **Step 1: Write failing tests**

Append:

```python
def test_sell_put_cash_secured_ok():
    v = evaluate(
        _make_request(
            ticker="SPY", security_type="OPT", action="SELL", quantity=1,
            right="P", expiry="20260620", strike=480.0, limit_price=5.0,
        ),
        PortfolioView(positions=[]),
    )
    assert v.accept is True


def test_index_short_call_no_cover_blocks():
    v = evaluate(
        _make_request(
            ticker="SPX", security_type="OPT", action="SELL", quantity=1,
            right="C", expiry="20260620", strike=5100.0, limit_price=10.0,
        ),
        PortfolioView(positions=[]),
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.INDEX_CALL_UNCOVERED


def test_index_short_call_with_same_expiry_long_call_ok():
    v = evaluate(
        _make_request(
            ticker="SPX", security_type="OPT", action="SELL", quantity=1,
            right="C", expiry="20260620", strike=5100.0, limit_price=10.0,
        ),
        PortfolioView(positions=[_long_call_position("SPX", 5000.0, "20260620")]),
    )
    assert v.accept is True


def test_index_short_call_different_expiry_long_call_blocks():
    v = evaluate(
        _make_request(
            ticker="SPX", security_type="OPT", action="SELL", quantity=1,
            right="C", expiry="20260620", strike=5100.0, limit_price=10.0,
        ),
        PortfolioView(positions=[_long_call_position("SPX", 5000.0, "20260718")]),
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.INDEX_CALL_UNCOVERED


def test_etf_short_call_no_cover_blocks():
    v = evaluate(
        _make_request(
            ticker="SPY", security_type="OPT", action="SELL", quantity=1,
            right="C", expiry="20260620", strike=500.0, limit_price=5.0,
        ),
        PortfolioView(positions=[]),
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.ETF_CALL_UNCOVERED


def test_etf_short_call_100_shares_ok():
    v = evaluate(
        _make_request(
            ticker="SPY", security_type="OPT", action="SELL", quantity=1,
            right="C", expiry="20260620", strike=500.0, limit_price=5.0,
        ),
        PortfolioView(positions=[_stock_position("SPY", 100)]),
    )
    assert v.accept is True


def test_etf_short_call_existing_short_exhausts_cover_blocks():
    v = evaluate(
        _make_request(
            ticker="SPY", security_type="OPT", action="SELL", quantity=1,
            right="C", expiry="20260620", strike=500.0, limit_price=5.0,
        ),
        PortfolioView(
            positions=[
                _stock_position("SPY", 100),
                _short_call_position("SPY", 510.0, "20260515"),
            ]
        ),
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.ETF_CALL_UNCOVERED


def test_etf_short_call_vertical_spread_ok():
    v = evaluate(
        _make_request(
            ticker="SPY", security_type="OPT", action="SELL", quantity=1,
            right="C", expiry="20260620", strike=510.0, limit_price=2.0,
        ),
        PortfolioView(positions=[_long_call_position("SPY", 500.0, "20260620")]),
    )
    assert v.accept is True


def test_sell_to_close_exact_match_ok():
    v = evaluate(
        _make_request(
            ticker="SPY", security_type="OPT", action="SELL", quantity=1,
            right="C", expiry="20260620", strike=500.0, limit_price=5.0,
        ),
        PortfolioView(positions=[_long_call_position("SPY", 500.0, "20260620")]),
    )
    assert v.accept is True
```

- [ ] **Step 2: Run — all pass**

```bash
python3.13 -m pytest scripts/tests/test_preflight.py -xvs
```

- [ ] **Step 3: Commit**

```bash
git add scripts/tests/test_preflight.py
git commit -m "test(preflight): option SELL Gate 4 branches (index, ETF, vertical, sell-to-close)"
```

---

### Task 9: Preflight — parity fixture test

**Files:**

- Modify: `scripts/tests/test_preflight.py`

- [ ] **Step 1: Add fixture-driven test**

Append (the `json`, `Decimal`, and `Path` imports were added in Task 6 Step 3; do NOT re-import):

```python
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "gate4_parity.json"


def _request_from_fixture(case: dict) -> PreflightRequest:
    r = case["request"]
    right_map = {"C": "C", "P": "P", None: None}
    return PreflightRequest(
        ticker=r["symbol"],
        security_type="STK" if r["type"] == "stock" else "OPT",
        action=r["action"],
        quantity=r["quantity"],
        right=right_map.get(r.get("right")),
        expiry=r.get("expiry"),
        strike=Decimal(str(r["strike"])) if r.get("strike") is not None else None,
        multiplier=r.get("multiplier", 100),
        limit_price=Decimal(str(r["limitPrice"])),
    )


def _portfolio_from_fixture(case: dict) -> PortfolioView:
    return PortfolioView(**case["portfolio"])


@pytest.mark.parametrize("case", json.loads(FIXTURE_PATH.read_text())["cases"], ids=lambda c: c["name"])
def test_parity_fixture(case):
    req = _request_from_fixture(case)
    portfolio = _portfolio_from_fixture(case)
    verdict = evaluate(req, portfolio)

    expected = case["expected"]
    assert verdict.accept == expected["accept"], (
        f"{case['name']}: expected accept={expected['accept']}, got {verdict.accept} "
        f"(reason={verdict.reason_code})"
    )
    if expected["reason_code"] is None:
        assert verdict.reason_code is None
    else:
        assert verdict.reason_code == ReasonCode(expected["reason_code"])
```

Add `from decimal import Decimal` to the imports if not already present.

- [ ] **Step 2: Run fixture test — all 18 cases pass**

```bash
python3.13 -m pytest scripts/tests/test_preflight.py::test_parity_fixture -xvs
```

Expected: 18 cases, all PASS.

- [ ] **Step 3: Commit**

```bash
git add scripts/tests/test_preflight.py
git commit -m "test(preflight): drive 18 parity cases from shared fixture JSON"
```

---

### Task 10: Wire preflight into FastAPI /orders/place

**Files:**

- Modify: `src/xenon/api/server.py` — import and call `preflight.evaluate()` before subprocess invocation.
- Create: `scripts/tests/test_preflight_route.py`

- [ ] **Step 1: Locate the exact pre-subprocess hook point**

```bash
grep -n "async def orders_place" src/xenon/api/server.py
grep -n "XENON_API_TEST_MODE" src/xenon/api/server.py
grep -n "xenon-ib-place-order\|create_subprocess_exec\|subprocess.run" src/xenon/api/server.py
```

Record three things before moving on:

1. The line range of the `orders_place` handler body.
2. Whether the handler already branches on `XENON_API_TEST_MODE` (if yes, integration tests can rely on that stub — no mock needed).
3. The exact symbol name used to launch the `xenon-ib-place-order` subprocess (we'll use it as the assertion target, not a patch target).

- [ ] **Step 2: Write the integration test**

Per the root CLAUDE.md "Order-route integration tests use `web/tests/fastapiHarness.ts` with `XENON_API_TEST_MODE` to stub broker calls — no live IB required." We rely on that stubbing path rather than patching internal symbols.

Create `scripts/tests/test_preflight_route.py`:

```python
"""Integration test: /orders/place preflight wiring (F2).

Uses FastAPI TestClient with XENON_API_TEST_MODE=1 to stub the subprocess
call. Verifies that the preflight gate returns HTTP 400 with the reason
code BEFORE any subprocess invocation.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _test_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    # Point data dir at an empty portfolio
    portfolio = {"positions": []}
    pf_file = tmp_path / "portfolio.json"
    pf_file.write_text(json.dumps(portfolio))
    monkeypatch.setenv("XENON_DATA_DIR", str(tmp_path))
    yield


@pytest.fixture
def client():
    # Defer import until env vars are set
    from xenon.api.server import app
    return TestClient(app)


def test_spx_stock_buy_blocked_by_preflight(client):
    resp = client.post(
        "/orders/place",
        json={
            "type": "stock",
            "symbol": "SPX",
            "action": "BUY",
            "quantity": 1,
            "limitPrice": 5000.0,
        },
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["reason_code"] == "INDEX_HAS_NO_STOCK"


def test_unknown_ticker_blocked_by_preflight(client):
    resp = client.post(
        "/orders/place",
        json={
            "type": "stock",
            "symbol": "AAPL",
            "action": "BUY",
            "quantity": 1,
            "limitPrice": 180.0,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "UNIVERSE_UNKNOWN"


def test_spy_buy_passes_preflight(client):
    """Preflight ACCEPTs SPY BUY. Under XENON_API_TEST_MODE=1 the handler
    stubs the IB subprocess, so a 200 response proves we reached the
    post-preflight path. We don't assert on the body payload shape —
    only that preflight did not block and the stub was reached.
    """
    resp = client.post(
        "/orders/place",
        json={
            "type": "stock",
            "symbol": "SPY",
            "action": "BUY",
            "quantity": 1,
            "limitPrice": 500.0,
        },
    )
    # Preflight blocks would return 400 with a reason_code. A 200/500 from
    # the stubbed subprocess layer both prove we got past preflight. The
    # one outcome we reject here is 400 with a preflight reason_code.
    assert resp.status_code != 400 or "reason_code" not in resp.json(), (
        f"SPY BUY should not be blocked by preflight; got {resp.status_code} {resp.json()}"
    )
```

> **Note on the subprocess stub:** the root CLAUDE.md states `XENON_API_TEST_MODE` already stubs broker subprocess calls in the FastAPI server. Step 1 of this task verifies that handling. If that env-var branching does NOT exist in `server.py`, the executing subagent stops and raises the gap — do not guess at an internal symbol name to monkeypatch. The fix (add `XENON_API_TEST_MODE` stub to the handler) is a legitimate sub-task of PR-A if needed.

- [ ] **Step 3: Run test — preflight call not yet wired, expect FAIL on SPX case**

```bash
python3.13 -m pytest scripts/tests/test_preflight_route.py -xvs
```

Expected: `test_spx_stock_buy_blocked_by_preflight` FAILS (handler still forwards to subprocess or returns a different shape).

- [ ] **Step 4: Wire preflight into `orders_place`**

In `src/xenon/api/server.py`, import near the top:

```python
from xenon.execution import preflight
from xenon.execution.preflight import (
    PortfolioView,
    PreflightRequest,
    ReasonCode,
    Verdict,
)
```

Inside the `orders_place` handler, after the request body is parsed and before any subprocess invocation, add:

```python
    # F2: server-side Gate 4. Wrap in a helper so tests can stub portfolio loading.
    verdict = _run_preflight(body)
    if not verdict.accept:
        return JSONResponse(
            status_code=400,
            content={
                "reason_code": verdict.reason_code.value if verdict.reason_code else None,
                "reason_detail": verdict.reason_detail,
            },
        )
```

Then add the helper below the route:

```python
def _load_portfolio_view() -> PortfolioView:
    """Load portfolio snapshot for preflight. Matches TS guard's data/portfolio.json source.

    F5 will replace this with a live IB-pool call per SL spec §5.2.
    """
    data_dir = Path(os.environ.get("XENON_DATA_DIR", str(DATA_DIR)))
    pf_file = data_dir / "portfolio.json"
    if not pf_file.exists():
        return PortfolioView(positions=[])
    raw = json.loads(pf_file.read_text())
    # portfolio.json structure: {"positions": [...]}
    return PortfolioView.model_validate(raw)


def _body_to_preflight_request(body: dict) -> PreflightRequest:
    """Translate /orders/place body to PreflightRequest. Combo (BAG) orders are skipped by
    preflight in F2 — the TS guard still gates them; server-side BAG gate is F2.5 / wizard work."""
    sec_type = "STK" if body.get("type") == "stock" else "OPT"
    right_raw = (body.get("right") or "").upper()
    right = right_raw if right_raw in ("C", "P") else None
    limit = body.get("limitPrice")
    return PreflightRequest(
        ticker=str(body.get("symbol", "")).upper(),
        security_type=sec_type,
        action=str(body.get("action", "")).upper(),
        quantity=int(body.get("quantity", 0)),
        right=right,
        expiry=body.get("expiry"),
        strike=Decimal(str(body["strike"])) if body.get("strike") is not None else None,
        multiplier=int(body.get("multiplier", 100)),
        limit_price=Decimal(str(limit)) if limit is not None else Decimal("0"),
    )


def _run_preflight(body: dict) -> Verdict:
    # Skip combo orders in F2 — the Next.js TS guard still covers them; server-side
    # BAG preflight is tracked separately (not a PR-A deliverable).
    if body.get("type") == "combo":
        return Verdict(accept=True)
    req = _body_to_preflight_request(body)
    portfolio = _load_portfolio_view()
    return preflight.evaluate(req, portfolio)
```

If `DATA_DIR`, `json`, `os`, `Path`, `Decimal`, or `JSONResponse` aren't already imported at the top of `server.py`, add them.

- [ ] **Step 5: Run integration tests**

```bash
python3.13 -m pytest scripts/tests/test_preflight_route.py -xvs
```

Expected: all PASS. If `test_spy_buy_passes_preflight` fails because the handler actually launches a subprocess despite `XENON_API_TEST_MODE=1`, stop and add the env-var stub to the handler — do not work around it with ad-hoc monkeypatching.

- [ ] **Step 6: Run the full Python suite — no regressions**

```bash
python3.13 scripts/infra/dev/run_pytest_affected.py
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/xenon/api/server.py scripts/tests/test_preflight_route.py
git commit -m "feat(api): wire preflight gate into /orders/place (F2)

Server-side Gate 4 runs before any subprocess invocation. Blocks
return HTTP 400 with {reason_code, reason_detail}. Combo (BAG) orders
bypass preflight for now — TS guard covers them, server-side BAG
gate is tracked separately.

Portfolio source matches the TS guard (data/portfolio.json); F5 will
switch to live IB-pool reads per SL spec §5.2."
```

---

### Task 11: TS consumes shared fixture

**Files:**

- Create: `web/tests/gate4-parity-fixture.test.ts`

- [ ] **Step 1: Write the TS parity runner**

```typescript
// web/tests/gate4-parity-fixture.test.ts
import { describe, expect, test } from "vitest";
import fixture from "../../scripts/tests/fixtures/gate4_parity.json";
import { checkNakedShortRisk } from "../lib/nakedShortGuard";
import type { NakedShortPortfolio, OrderPayload } from "../lib/nakedShortGuard";

type FixtureCase = {
  name: string;
  request: {
    type: "stock" | "option" | "combo";
    symbol: string;
    action: "BUY" | "SELL";
    quantity: number;
    right: "C" | "P" | null;
    expiry: string | null;
    strike: number | null;
    multiplier: number;
    limitPrice: number;
  };
  portfolio: NakedShortPortfolio;
  expected: {
    accept: boolean;
    reason_code: string | null;
  };
};

function toOrderPayload(r: FixtureCase["request"]): OrderPayload {
  return {
    type: r.type,
    symbol: r.symbol,
    action: r.action,
    quantity: r.quantity,
    right: r.right ?? undefined,
    expiry: r.expiry ?? undefined,
    strike: r.strike ?? undefined,
    limitPrice: r.limitPrice,
  } as OrderPayload;
}

describe("Gate 4 parity fixture — TS guard matches Python preflight", () => {
  for (const c of (fixture as { cases: FixtureCase[] }).cases) {
    // The TS guard does not distinguish UNIVERSE_UNKNOWN / INDEX_HAS_NO_STOCK —
    // that's a server-only gate. Skip those two reason codes in the TS runner;
    // parity for Gate 4 coverage cases (INDEX_CALL_UNCOVERED, ETF_CALL_UNCOVERED,
    // INSUFFICIENT_SHARES) is the point of this test.
    if (
      c.expected.reason_code === "UNIVERSE_UNKNOWN" ||
      c.expected.reason_code === "INDEX_HAS_NO_STOCK"
    ) {
      continue;
    }
    test(c.name, () => {
      const result = checkNakedShortRisk(
        toOrderPayload(c.request),
        c.portfolio,
      );
      expect(result.allowed).toBe(c.expected.accept);
    });
  }
});
```

- [ ] **Step 2: Run Vitest — verify each non-universe case matches**

```bash
cd web && npx vitest run tests/gate4-parity-fixture.test.ts
```

Expected: all cases PASS.

> **Note on reason-code granularity:** the TS guard returns `{allowed, reason}` with freeform reason strings, not a machine-readable code enum. The parity test checks `allowed` only — exact reason-string mapping is F6 work. If a specific case has a different `allowed` verdict than Python, the TS guard is out of parity and must be fixed in the same PR (or excluded with a documented reason).

- [ ] **Step 3: Commit**

```bash
git add web/tests/gate4-parity-fixture.test.ts
git commit -m "test(gate4): TS guard parity against shared fixture (F1)"
```

---

### Task 12: Update master plan with PR-A bundling note

**Files:**

- Modify: `docs/superpowers/plans/2026-04-20-order-execution-foundation-master.md` — update F1 and F2 status rows.

- [ ] **Step 1: Edit the master plan**

In the Sub-plans table (around line 72-88), update the F1 and F2 rows:

```markdown
| **F1** | `2026-04-20-order-execution-pr-a-audit-preflight.md` | **bundled into PR-A** (written; see sub-plan) | SL §13.1, Wiz §11.1 | F2, W1 |
| **F2** | `2026-04-20-order-execution-pr-a-audit-preflight.md` | **bundled into PR-A** (written; see sub-plan) | SL §5 | F3, F5, W1 |
```

Add a note below the table:

```markdown
**PR-A bundling (post-kickoff decision, 2026-04-21):** F1 and F2 ship as a
single coordinated PR. Both target Gate 4 parity (F1 in the post-sync audit,
F2 as a pre-submit gate) so they share fixture infrastructure and a
semantic seam. F3–F7 remain separate phases. Rationale and bundling
analysis in the conversation where PR-A was kicked off.
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/plans/2026-04-20-order-execution-foundation-master.md
git commit -m "docs(plan): mark F1+F2 as bundled PR-A in master plan"
```

---

### Task 13: Full test sweep + coverage check

- [ ] **Step 1: Run full Python test suite**

```bash
python3.13 -m pytest scripts/tests/ -xvs
```

Expected: all PASS.

- [ ] **Step 2: Run Vitest**

```bash
cd web && npm test
```

Expected: all PASS.

- [ ] **Step 3: Coverage check on touched files**

```bash
python3.13 -m pytest \
  --cov=src/xenon/execution/preflight \
  --cov=src/xenon/execution/naked_short_audit \
  --cov-report=term-missing \
  scripts/tests/test_preflight.py scripts/tests/test_naked_short_audit.py \
  scripts/tests/test_preflight_route.py
```

Expected: coverage ≥95% on both modules. If any uncovered line is in a defensive branch that can't be triggered from the public API, document it inline with a short comment; otherwise add a targeted test.

- [ ] **Step 4: If any coverage gap, add a test and commit**

```bash
git add scripts/tests/
git commit -m "test(preflight): close coverage gaps for 95% threshold"
```

---

### Task 14: Self-review before codex

Not a code task — this is the human/Claude review checkpoint. Before invoking `/codex-review`:

- [ ] Confirm every success criterion from the top of this plan has a corresponding passing test.
- [ ] Confirm the smoke-test recipe is runnable if a reviewer has paper IB available.
- [ ] Confirm no out-of-scope modules were created (no `quote_guard.py`, `orders.duckdb`, etc.).
- [ ] Confirm the Gate 4 table in `src/xenon/CLAUDE.md` is satisfied by fixture cases.
- [ ] Confirm the commit sequence tells a coherent story (each commit compiles + tests).

---

## Notes on parity with the TS guard (for implementers)

The TS guard's `checkNakedShortRisk` has one subtle difference from Python preflight:

- The TS guard treats the **combo (BAG)** case first with leg-level inspection. Python `preflight.evaluate()` does NOT handle BAG orders — combo preflight is outside F2's scope (see scope boundary). The FastAPI wiring bypasses preflight for `body.type == "combo"`.
- The TS guard does NOT know about `UNIVERSE_UNKNOWN` or `INDEX_HAS_NO_STOCK` — those are server-only gates. The fixture `TS` runner skips those two cases for that reason.

If a future phase widens preflight to BAG, that's explicitly scoped out of PR-A.

## Notes on the working-reservations stub

`WorkingReservations()` with all-zero fields is a documented F2 stub. F4 will:

1. Create `data/orders.duckdb` with the `orders_submissions` table.
2. Replace the `reservations = reservations or WorkingReservations()` default with a real query:
   ```python
   reservations = _load_working_reservations(req.ticker, user_id)
   ```
3. Add concurrency tests (two simultaneous `SELL 100 SPY` → one ACCEPT, one BLOCK).

PR-A deliberately leaves this seam clean.
