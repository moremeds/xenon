# P2.4 — Decompose `_orders_place_from_body` into a gates / submit / persist service

- **Date:** 2026-07-05
- **Proposed branch:** `refactor/decompose-place-handler`
- **Finding IDs:** CX-2 (Medium) — `docs/fable/06-complexity-and-reuse.md` §6.2 "Extraction spec — `_orders_place_from_body`"; roadmap item P2.4 / §6.3 rank #3.
- **Severity:** Medium.
- **Goal (one line):** Move the ~250-line `_orders_place_from_body` body out of `server.py` into a new `src/xenon/api/services/place_order.py` split into three stages — `run_gates` → `submit_to_broker` → `persist_outcome` — leaving a thin delegating `_orders_place_from_body` wrapper so every caller and every response is **contract-identical**. Pure refactor, **zero behavior change**.

---

## 0. ⛔ TRIPWIRE — prerequisites MUST be merged first (read before anything)

This plan describes the decomposition of `_orders_place_from_body` **as it exists AFTER four
prerequisite PRs have merged**. Those PRs rewrite the very region this plan relocates. If any of
them is **not** in the working tree when you execute, the anchors below will not match and the
decomposition would have to be redone — **STOP and report** instead of improvising.

Verify all four are present with this single gate (run it FIRST, before Step 0):

```bash
cd /Users/chenxi/projects/xenon
# S2 (UNCERTAIN state + staged ack):
grep -q "def mark_uncertain" src/xenon/execution/orders_store.py && \
grep -q "ORDER_STATUS_UNCERTAIN" src/xenon/execution/preflight.py && \
grep -q "def run_place_subprocess" src/xenon/api/subprocess.py && \
# S4 (persist-or-compensate):
grep -q "def _persist_submitted_or_compensate" src/xenon/api/server.py && \
# S6 (semaphore wrapper):
grep -q "def _run_order_subprocess" src/xenon/api/server.py && \
# P1.1 (stage logs):
grep -q "def _order_stage" src/xenon/api/server.py && \
echo "PREREQS-OK" || echo "PREREQS-MISSING — STOP"
```

Expected: `PREREQS-OK`. If it prints `PREREQS-MISSING — STOP`, halt and report which grep failed.

> The prerequisite plans are `docs/superpowers/plans/2026-07-05-fable-s2-uncertain-orderref.md`,
> `…-s4-protect-post-ack-persist.md`, `…-s6-order-subprocess-semaphore.md`,
> `…-p1-1-stage-timing-logs.md`. You do **not** need to re-read them — this plan already folds
> their post-merge shape into the reference in §5. You only need them merged.

---

## 1. Context — what exists today (verified at HEAD `4d864294`, pre-prereqs)

- The place path is: Next `web/app/api/orders/place/route.ts` → FastAPI `POST /orders/place`
  (`src/xenon/api/server.py:2113` `orders_place`) → `_orders_place_from_body`
  (`server.py:2139`) → subprocess `xenon-ib-place-order`.
- `_orders_place_from_body(body: dict)` at **`server.py:2139`** is one async function with seven
  responsibilities (verified by reading it): broker guard, account-scope guard, Gate-4 preflight,
  quote gate, idempotency reservation, broker submit (subprocess), and result
  classification + persistence + response building. At HEAD it is ~193 lines; **after the four
  prereqs it grows to ~250** (S2 adds the ack/UNCERTAIN branches, S4 the persist-warning wrap,
  S6 repoints the subprocess call, P1.1 sprinkles stage logs).
- **In-process callers that must keep working (zero-break shim):**
  - `src/xenon/execution/combo_wizard/session.py:327` → `server_mod._orders_place_from_body(payload)`
    (the combo wizard submit path; memory `in_process_route_bypass` — it deliberately reuses the
    validated place handler).
  - `server.py:2122` (the `orders_place` route) → `await _orders_place_from_body(body)`.
  - Tests monkeypatch `server_mod._orders_place_from_body` directly
    (`src/xenon/api/tests/test_wizard_routes.py:293,378`).
- `src/xenon/api/services/` already exists and holds plain-function business-logic modules
  (e.g. `ib_activity_mirror.py`, `journal_auto_import.py`); `services/__init__.py` is empty. This
  is the documented home for extracted logic (`src/xenon/api/CLAUDE.md` § Module Layout:
  "Business logic goes in `services/`, not inline in the route"). `routes/orders.py` exists but
  holds the **read** surface (`/orders`, `/orders/refresh`) — the write handler stays reachable as
  `server._orders_place_from_body` for the wizard, so the extraction target is a **service**, not
  a route.

**What the executor does NOT need to understand:** the IB subprocess internals, preflight /
quote-gate / naked-short logic, combo/BAG semantics, the UNCERTAIN/ack classification introduced
by S2, or the semaphore internals from S6. This is a **mechanical relocation** of an existing,
already-tested function body — you are not changing any branch, any write, any status code, or any
response field. The correctness gate is "every existing order test stays green + a new
contract-identical golden test."

---

## 2. Drift from review

1. **The fable spec (`06 §6.2`) proposes three stages `run_gates` / `submit_reserved` /
   `persist_outcome` where `persist_outcome` is "the only writer".** After S2 merged, the
   post-subprocess tail **interleaves** classification and writes (the ack→`mark_submitted`,
   ambiguous→`mark_uncertain`, reject→`mark_terminal`, clean→`_persist_submitted_or_compensate`
   branches each both decide and write). Forcing those writes into a separate pure `persist_outcome`
   would require **reshaping live-order branch logic** — a behavior-drift risk this plan refuses to
   take. **Adopted seam:** `run_gates` (pre-subprocess), `submit_to_broker` (test-mode short-circuit
   - the subprocess spawn, returning the raw `ScriptResult`), and `persist_outcome` (the entire
     post-`ScriptResult` classify-and-write tail, moved **verbatim**). The S4 helper
     `_persist_submitted_or_compensate` remains the single low-level write primitive both stages call —
     that IS the spec's "only writer" chokepoint, already in place. This is a deliberate, one-line
     simplicity call: relocate, don't reshape.
2. **`submit_reserved` renamed `submit_to_broker`** to match the brief's wording; behavior identical.
3. **The service references server-module helpers via attribute access (`_server.<name>`), not
   `from … import <name>`.** This is mandatory (not cosmetic): the entire existing test suite
   monkeypatches these symbols **on the `server` module** (`server._run_preflight`,
   `server._validate_non_combo_quote`, `server._run_ib_script_with_recovery`,
   `server._run_order_subprocess`, `orders_store.mark_submitted`, …). Attribute access at call time
   preserves every one of those patches; a bound `from` import would freeze the original and silently
   break ~6 test files. See §5 name-qualification table.
4. **S6's static call-site test must be updated.** `test_order_subprocess_semaphore.py::
test_call_sites_repointed_statically` asserts `inspect.getsource(server_mod).count(
"_run_order_subprocess(") == 4` (3 mutating call sites + the def). Moving the **place** call into
   the service drops server.py's count to **3** (cancel + modify + def). This test asserts an
   implementation-detail location, not behavior; updating it is correct and in-scope (Step 5).
5. No incident-history row: this is a refactor, not an order-path bug fix (§9).

---

## 3. Goal / Non-goals

**Goal:** `_orders_place_from_body` shrinks to a 2-line delegating wrapper; its body lives in
`src/xenon/api/services/place_order.py` as `run_gates` / `submit_to_broker` / `persist_outcome`
orchestrated by `place_order_from_body`. Every caller unchanged. Every response contract-identical.
Dependency direction: `server` (wrapper) → `place_order` service → (`orders_store`, `subprocess`
runner, and — via attribute access — the unchanged `server` helpers). Nothing new imports a route.

**Non-goals (explicitly NOT in this PR — one change, one PR):**

- Any behavior change, new status code, new reason code, or response-field change. **None.**
- Moving `_run_preflight`, `_validate_non_combo_quote`, `_run_ib_script_with_recovery`,
  `_run_order_subprocess`, `_persist_submitted_or_compensate`, `_order_stage`, `_is_test_mode`,
  `_next_test_order_ids`, or `_resolve_scope_kwargs` out of `server.py`. They **stay put**; the
  service calls them via `_server.`. (Moving them is a larger, separate migration — §6.3 rank #10
  "continue `server.py` → `routes/`+`services/` opportunistically, don't big-bang".)
- Decomposing `_orders_cancel_from_body` / `_orders_modify_from_body` (adjacent, out of scope).
- The other §6 candidates: `OrderTab.tsx` split, `usePrices`/`IBStatusContext`, relay extraction,
  combo net-price single-impl, `confirm_with_poll()`, coverage-math parity fixtures. Not here.
- Any `web/` change. This PR touches **no frontend** — the `/orders/place` HTTP contract is
  unchanged, so there is no UI-visible surface and **no E2E browser step is required** (stated
  explicitly so the executor does not fabricate a Playwright run).

---

## 4. Key facts (verified against the working tree)

| Fact                                    | Value                                                                                                                                                                        | Verified at                                                                                    |
| --------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Place handler                           | `async def _orders_place_from_body(body: dict)`                                                                                                                              | `server.py:2139`                                                                               |
| Route caller                            | `return await _orders_place_from_body(body)` inside `orders_place`                                                                                                           | `server.py:2122`                                                                               |
| Wizard caller                           | `server_mod._orders_place_from_body(payload)`                                                                                                                                | `combo_wizard/session.py:327`                                                                  |
| Test monkeypatch of handler             | `monkeypatch.setattr(server_mod, "_orders_place_from_body", …)`                                                                                                              | `api/tests/test_wizard_routes.py:293,378`                                                      |
| Services dir                            | exists, plain-function modules, empty `__init__.py`                                                                                                                          | `src/xenon/api/services/`                                                                      |
| Logger                                  | `logger = logging.getLogger("xenon.api")`                                                                                                                                    | `server.py:89`                                                                                 |
| `_is_test_mode`                         | reads `XENON_API_TEST_MODE` at call time                                                                                                                                     | `server.py:142`                                                                                |
| `_next_test_order_ids`                  | `-> tuple[int, int]`, mutates `test_order_counter`                                                                                                                           | `server.py:155`                                                                                |
| `_run_preflight`                        | `async def _run_preflight(body, user_id="local", cover_ratio=1.0) -> Verdict`                                                                                                | `server.py:1762`                                                                               |
| `_validate_non_combo_quote`             | `async def … -> tuple[quote_guard.QuoteVerdict, int]`                                                                                                                        | `server.py:2009`                                                                               |
| `_resolve_scope_kwargs`                 | `def _resolve_scope_kwargs() -> dict[str, str]`                                                                                                                              | `server.py:2125`                                                                               |
| `_run_ib_script_with_recovery`          | `async def …(entry, args, timeout=30) -> ScriptResult`                                                                                                                       | `server.py:3120`                                                                               |
| `ScriptResult` import                   | `from xenon.api.subprocess import ScriptResult, run_entry_point, run_module` (+ `run_place_subprocess` after S2)                                                             | `server.py:64`                                                                                 |
| Caller-allowlist guard                  | forbids the literal string `xenon-ib-place-order` in any non-test file under `src/` not in `_ALLOWLIST`                                                                      | `scripts/checks/order_path_caller_allowlist.py:62` (`re.compile(r"\bxenon-ib-place-order\b")`) |
| Guard allowlist                         | `_ALLOWLIST` frozenset; `src/xenon/api/server.py` is in it, `services/place_order.py` is **not** (must add)                                                                  | `scripts/checks/order_path_caller_allowlist.py:~44`                                            |
| Existing place-route tests (green gate) | `test_idempotency_route.py`, `test_place_quote_gate.py`, `test_preflight_route.py`, `test_orders_place_no_regime_gate.py`                                                    | `scripts/tests/` (all confirmed present)                                                       |
| Prereq-added tests (green gate)         | S2 `test_orders_place_uncertain_route.py`, S4 `test_orders_place_post_ack_persist.py`, P1.1 `test_order_stage_logging.py`, S6 `api/tests/test_order_subprocess_semaphore.py` | added by the prereqs                                                                           |
| Next incident row                       | N/A (refactor, no row)                                                                                                                                                       | —                                                                                              |

---

## 5. The post-prereq function shape (REFERENCE) + the relocation seams

> **This is a reference reconstruction** of what `_orders_place_from_body` looks like AFTER S2 +
> S4 + S6 + P1.1 have merged. **The authoritative source is the ACTUAL function in your working
> tree** — capture it first (Step 1) and relocate _whatever is there_. Do not hand-type this
> reference into the service; use it only to (a) confirm the three seams below are where you expect,
> and (b) orient the name-qualification. If the actual function's structure diverges from this
> reference in a way that breaks the three seams (e.g. a prereq merged the test-mode block below the
> subprocess call), **STOP and report** — a prereq merged differently than assumed.

Annotated reference (seam markers `# ── SEAM …` are NOT in the code — they mark the cut lines):

```python
async def _orders_place_from_body(body: dict):
    _stage_t0 = time.monotonic()                                   # P1.1
    _stage_cid = str(body.get("client_attempt_id") or "")          # P1.1
    broker = str(getattr(app.state, "broker", "IB") or "IB").upper()
    if broker != "IB":
        return JSONResponse(status_code=403, content={... READ_ONLY_BROKER ...})
    scope = _resolve_scope_kwargs()
    if scope["broker"] != "IB":
        return JSONResponse(status_code=403, content={... READ_ONLY_BROKER ...})
    cover_ratio_for_preflight = 1.0
    override_audit = None
    verdict = await _run_preflight(body, cover_ratio=cover_ratio_for_preflight)
    if not verdict.accept:
        return JSONResponse(status_code=400, content={... preflight ...})
    if body.get("type") != "combo":
        qv, quote_status = await _validate_non_combo_quote(body)
        _override_detail = None
        if not qv.accept:
            if qv.reason_code == ReasonCode.LIMIT_OUT_OF_BAND and body.get("acknowledge_limit_override") is True:
                _override_detail = {...}
            else:
                return JSONResponse(status_code=quote_status, content={...})
    else:
        _override_detail = None
    cid = body.get("client_attempt_id")
    if not cid:
        return JSONResponse(status_code=400, content={"detail": "client_attempt_id is required"})
    _order_stage("gates_done", _stage_t0, op="place", client_attempt_id=_stage_cid)   # P1.1
    user_id = "local"
    security_type = "STK" if body.get("type") == "stock" else "BAG" if body.get("type") == "combo" else "OPT"
    req_row = orders_store.RequestRow(...)
    outcome = orders_store.reserve_attempt(user_id, cid, req_row, override_audit=override_audit, **_resolve_scope_kwargs())
    if outcome.status == "terminal":
        return JSONResponse(status_code=409, content={... ATTEMPT_ID_TERMINAL ...})
    if outcome.status == "duplicate":
        return JSONResponse(status_code=200, content={... duplicate ...})
    submission_id = outcome.submission_id
    _order_stage("reserved", _stage_t0, op="place", client_attempt_id=_stage_cid, submission_id=submission_id)  # P1.1
    if _override_detail is not None:
        orders_store.record_event(submission_id, "PREFLIGHT_ACK_LIMIT", _override_detail)
    # ── SEAM A: end of run_gates. Everything above returns either an early Response,
    #            or (fall-through) proceeds with `submission_id`.
    if _is_test_mode():                                            # S4-wrapped test-mode branch
        order_id, perm_id = _next_test_order_ids()
        persisted, warn_reason = _persist_submitted_or_compensate(...)
        _order_stage("persisted", _stage_t0, op="place", client_attempt_id=_stage_cid, submission_id=submission_id)  # P1.1
        payload = {"status": "ok", "orderId": order_id, "permId": perm_id, "initialStatus": "Submitted",
                   "message": "Order accepted in test mode", "echo": body, "submission_id": submission_id}
        if not persisted:
            payload["persist_warning"] = True
            payload["persist_warning_reason"] = warn_reason
        return payload
    order_json = json.dumps(body)
    _order_stage("subprocess_spawned", _stage_t0, op="place", client_attempt_id=_stage_cid, submission_id=submission_id)  # P1.1
    result = await _run_order_subprocess("xenon-ib-place-order", ["--json", order_json], timeout=15, runner=run_place_subprocess)  # S6+S2 (exact merged form may vary)
    # ── SEAM B: end of submit_to_broker. Test-mode returned `payload`; real path returns `result` (ScriptResult).
    if result.ack:                                                 # S2 ack persist
        orders_store.mark_submitted(... from result.ack ...)
        try:
            orders_store.record_event(submission_id, "IB_ACK", {...})
        except Exception:
            logger.warning("Failed to record IB_ACK event for %s", submission_id, exc_info=True)
    if not result.ok:
        if result.ambiguous:
            if result.ack is not None:
                return {"status": "ok", ... , "submission_id": submission_id}      # ack-then-died → WORKING
            orders_store.mark_uncertain(submission_id=submission_id, detail={...}, expected_states=("PENDING",))
            return JSONResponse(status_code=502, content={... ORDER_STATUS_UNCERTAIN ...})
        orders_store.mark_terminal(submission_id=submission_id, state="FAILED", reason_code="SUBPROCESS_ERROR",
                                   filled_qty=0, avg_fill_price=None, expected_states=("PENDING",))
        raise HTTPException(status_code=502, detail=result.error)
    if result.data and result.data.get("status") == "error":
        ...  # REJECTED / LIMIT_OFF_TICK / IB_REJECT, record_event, raise HTTPException(502, ib_message)
    if result.data and not result.ack:                             # S4-wrapped clean persist
        persisted, warn_reason = _persist_submitted_or_compensate(... from result.data ...)
        _order_stage("persisted", _stage_t0, op="place", client_attempt_id=_stage_cid, submission_id=submission_id)  # P1.1
        if not persisted:
            data = dict(result.data)
            data["persist_warning"] = True
            data["persist_warning_reason"] = warn_reason
            return data
    return result.data
    # ── (end) everything from SEAM B to here is persist_outcome.
```

**The three seams:**

- **SEAM A** — after the `if _override_detail is not None: … record_event(…)` block and the
  `submission_id = outcome.submission_id` assignment; **before** `if _is_test_mode():`.
  → `run_gates` owns everything **above** SEAM A.
- **SEAM B** — immediately after the `result = await _run_order_subprocess(…)` assignment.
  → `submit_to_broker` owns everything **between** SEAM A and SEAM B (the test-mode block + the
  `order_json`/`subprocess_spawned`/`result = await …` lines).
  → `persist_outcome` owns everything **below** SEAM B (from `if result.ack:` to `return result.data`).

### Name-qualification table (apply to EVERY relocated line)

When a relocated line references one of these **server-module** names, prefix it with `_server.`
(the service does `import xenon.api.server as _server` **lazily inside each function** — see Step 2):

| Referenced as (in server.py)                                   | Rewrite to (in service)                    |
| -------------------------------------------------------------- | ------------------------------------------ |
| `app` (i.e. `app.state.broker`)                                | `_server.app`                              |
| `_resolve_scope_kwargs`                                        | `_server._resolve_scope_kwargs`            |
| `_run_preflight`                                               | `_server._run_preflight`                   |
| `_validate_non_combo_quote`                                    | `_server._validate_non_combo_quote`        |
| `_is_test_mode`                                                | `_server._is_test_mode`                    |
| `_next_test_order_ids`                                         | `_server._next_test_order_ids`             |
| `_persist_submitted_or_compensate`                             | `_server._persist_submitted_or_compensate` |
| `_order_stage`                                                 | `_server._order_stage`                     |
| `_run_order_subprocess`                                        | `_server._run_order_subprocess`            |
| `_run_ib_script_with_recovery` (if present in the merged call) | `_server._run_ib_script_with_recovery`     |

These names are imported **directly** into the service (module-level `import`/`from`), because they
are either external libs or the **same module object** the tests patch (so attribute access on them
stays patch-safe) — do **NOT** prefix with `_server.`:

| Name                                                                                                                                        | Service import                                                              |
| ------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| `orders_store` (and `orders_store.RequestRow`, `.reserve_attempt`, `.record_event`, `.mark_submitted`, `.mark_uncertain`, `.mark_terminal`) | `from xenon.execution import orders_store`                                  |
| `ReasonCode`                                                                                                                                | `from xenon.execution.preflight import ReasonCode`                          |
| `ScriptResult`, `run_place_subprocess`                                                                                                      | `from xenon.api.subprocess import ScriptResult, run_place_subprocess`       |
| `JSONResponse`                                                                                                                              | `from fastapi.responses import JSONResponse`                                |
| `HTTPException`                                                                                                                             | `from fastapi import HTTPException`                                         |
| `Decimal`                                                                                                                                   | `from decimal import Decimal`                                               |
| `json`, `time`, `logging`                                                                                                                   | `import json` / `import time` / `import logging`                            |
| `logger`                                                                                                                                    | `logger = logging.getLogger("xenon.api")` (same name → identical log lines) |

> **Why `orders_store` / `ScriptResult` are safe as direct imports but the server helpers are not:**
> tests patch `orders_store.mark_submitted` _on the `orders_store` module_ — the service imports the
> same module object, so `orders_store.mark_submitted` resolves the patched attribute. Tests patch
> `_run_preflight` _on the `server` module_ — only `_server._run_preflight` sees that patch.

---

## 6. Steps (strictly ordered — capture-first, TDD golden test, then relocate, then green)

> **Tribunal-mandated hardening of this gate:**
>
> 1. The capture harness must cover SEVEN branches, not just the happy path: success,
>    duplicate (same client_attempt_id), preflight block, quote-gate block, UNCERTAIN
>    (staged runner returns ambiguous no-ack), IB reject (code 110), and persist-warning
>    (mark_submitted raising). Capture status code + full JSON body before the refactor,
>    assert equality after (excluding volatile fields: submission_id, timestamps).
> 2. ACK-THEN-KILL + PERSIST-FAILURE tripwire: with the staged runner faked to return
>    `ScriptResult(ok=False, ambiguous=True, ack={...})` AND `mark_submitted` patched to
>    raise, the refactored service must return 200 with `persist_warning: true` — i.e. the
>    S2 early-ack persist must be routed through S4's `_persist_submitted_or_compensate`
>    (S4's plan §8 mandates wrapping all three persist sites). If the pre-refactor handler
>    does NOT behave this way, STOP — the S2/S4 integration is incomplete at HEAD and must
>    be fixed before decomposing.


> All Python via `uv run …`. Do not edit `.env`. Do not commit until the user says so. No new
> Python entry point is added, so no `uv sync` is needed.

### Step 0 — Prereq gate + branch

Run the §0 tripwire gate. Only if it prints `PREREQS-OK`:

```bash
cd /Users/chenxi/projects/xenon
git checkout -b refactor/decompose-place-handler
```

STOP if the working tree has unrelated dirty changes — report and wait.

### Step 1 — Capture the exact current function (your relocation source of truth)

```bash
cd /Users/chenxi/projects/xenon
grep -n "async def _orders_place_from_body" src/xenon/api/server.py    # note the start line
grep -n "^# F5.4 — cancel/modify failure classification" src/xenon/api/server.py  # first line AFTER the function
```

Read the function between those two anchors (`Read src/xenon/api/server.py` with the offset/limit
from the grep). This exact text is what you relocate — the §5 reference is only a map. Confirm you
can locate **SEAM A** (`if _is_test_mode():`) and **SEAM B** (the `result = await …` line for
`xenon-ib-place-order`). If either seam marker is absent or the test-mode block sits _after_ the
subprocess call, **STOP** — a prereq merged differently than §5 assumes.

### Step 2 — Write the contract-identical golden test FIRST (red)

Create `scripts/tests/test_place_order_golden_response.py`. It pins the **exact** test-mode
response dict (the one surface that returns a fully-constructed body) so the refactor cannot alter a
single field. It runs BEFORE the refactor to record the baseline (green pre-refactor — this is a
characterization test, not a red-first bug test) and again after.

```python
"""P2.4 golden characterization: the /orders/place test-mode response is
contract-identical before and after the service decomposition. Real ticker at a
frozen price (AAPL close 2026-07-02 ~213.55); no network — test mode short-
circuits before any subprocess. _next_test_order_ids is pinned so the orderId/
permId fields are deterministic."""

import pytest

pytestmark = pytest.mark.committed_db  # place path uses orders_store's own engine

from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    yield


@pytest.fixture
def client(monkeypatch):
    from xenon.api import server
    from xenon.execution import orders_store

    orders_store.init_store()
    # Pin the synthetic id generator so the response is deterministic.
    monkeypatch.setattr(server, "_next_test_order_ids", lambda: (900001, 8900001))
    return TestClient(server.app)


def _body(cid: str) -> dict:
    return {
        "type": "stock",
        "symbol": "AAPL",
        "action": "BUY",
        "quantity": 1,
        "limitPrice": 213.55,
        "con_id": 265598,
        "client_attempt_id": cid,
    }


def test_test_mode_place_response_is_exact(client):
    resp = client.post("/orders/place", json=_body("golden-p24-1"))
    assert resp.status_code == 200, resp.text
    j = resp.json()
    # submission_id is a fresh uuid each run — assert it is present, then drop it
    # before the exact-dict comparison.
    assert isinstance(j.get("submission_id"), str) and j["submission_id"]
    submission_id = j.pop("submission_id")
    assert j == {
        "status": "ok",
        "orderId": 900001,
        "permId": 8900001,
        "initialStatus": "Submitted",
        "message": "Order accepted in test mode",
        "echo": _body("golden-p24-1"),
    }
    # No persist_warning on the happy path (S4 invariant: absent, not False).
    assert "persist_warning" not in j
```

Run it — it MUST pass **now** (pre-refactor baseline):

```bash
uv run pytest scripts/tests/test_place_order_golden_response.py -xvs
```

> If this **fails** pre-refactor, the test-mode response shape already differs from the assertion —
> a prereq changed it. **STOP** and reconcile the golden dict to the actual pre-refactor response
> before proceeding (the point is contract-identical before/after; capture whatever "before" is).

### Step 3 — Create the service module

Create `src/xenon/api/services/place_order.py` with the scaffolding below. The **bodies** of
`run_gates` / `submit_to_broker` / `persist_outcome` are filled by **relocating the captured
regions from Step 1**, applying the §5 name-qualification table and the exact return
transformations noted in the docstrings. Everything between the seams moves **verbatim** except the
qualifications and the three return-shape changes.

```python
"""Place-order service — decomposed from server._orders_place_from_body (P2.4/CX-2).

Pure relocation of the place handler body into three stages so an order-path
change touches one small stage, not a 250-line function. ZERO behavior change:
every branch, write, status code, and response field is contract-identical to the
pre-refactor handler.

Dependency direction: server (thin wrapper) -> this service -> orders_store /
subprocess runner / (via attribute access) the unchanged server helpers.

IMPORTANT — monkeypatch safety: server-private helpers are referenced as
`_server.<name>` (attribute access on the live server module) because the test
suite patches them on that module; a bound `from xenon.api.server import <name>`
would freeze the original and defeat those patches. See the plan's §5 table.
"""

from __future__ import annotations

import json
import logging
import time
from decimal import Decimal  # noqa: F401 — used by relocated RequestRow construction
from typing import Union

from fastapi import HTTPException  # noqa: F401 — used by relocated persist_outcome
from fastapi.responses import JSONResponse

from xenon.api.subprocess import ScriptResult, run_place_subprocess  # noqa: F401
from xenon.execution import orders_store
from xenon.execution.preflight import ReasonCode  # noqa: F401 — used by relocated branches

logger = logging.getLogger("xenon.api")


async def run_gates(body: dict, t0: float, stage_cid: str) -> Union[JSONResponse, str]:
    """Pre-subprocess gating: broker guard, scope guard, preflight, quote gate,
    idempotency reservation, override-event side effect, gates_done/reserved
    stage logs.

    Returns EITHER a ``JSONResponse`` to return immediately (any early reject /
    409 terminal / 200 duplicate), OR the ``submission_id`` string to proceed.
    The orchestrator discriminates with ``isinstance(x, str)``.

    RELOCATION: paste the captured region ABOVE seam A verbatim. Two changes only:
      (1) apply the §5 name-qualification table;
      (2) the final fall-through — where the original had
          ``submission_id = outcome.submission_id`` and continued — becomes, at
          the END of this function, ``return submission_id`` (after the
          override-event block). Every early ``return JSONResponse(...)`` stays
          verbatim. Replace the original entry lines
          ``_stage_t0 = time.monotonic()`` / ``_stage_cid = ...`` with using the
          passed-in ``t0`` / ``stage_cid`` (do NOT recapture the clock here).
    """
    import xenon.api.server as _server  # lazy: avoids import cycle
    raise NotImplementedError  # replace this whole body per the RELOCATION note


async def submit_to_broker(
    body: dict, submission_id: str, t0: float, stage_cid: str
) -> Union[dict, ScriptResult]:
    """Test-mode short-circuit + the real subprocess spawn.

    Returns EITHER the fully-built test-mode ``payload`` dict (when
    ``_is_test_mode()``), OR the raw ``ScriptResult`` from the subprocess. The
    orchestrator discriminates with ``isinstance(x, ScriptResult)``.

    RELOCATION: paste the captured region BETWEEN seam A and seam B verbatim
    (the ``if _is_test_mode():`` block through the ``result = await ...`` line).
    Two changes only:
      (1) apply the §5 name-qualification table;
      (2) the last line becomes ``return result`` (the original fell through into
          the classification tail; here that tail is persist_outcome). The
          test-mode ``return payload`` stays verbatim.
    """
    import xenon.api.server as _server  # lazy: avoids import cycle
    raise NotImplementedError  # replace this whole body per the RELOCATION note


def persist_outcome(
    submission_id: str, result: ScriptResult, t0: float, stage_cid: str
) -> Union[dict, JSONResponse]:
    """Classify the broker ScriptResult and persist — the post-subprocess tail.

    Handles: early-ack mark_submitted + IB_ACK event; ambiguous-with-ack ->
    WORKING 200; ambiguous-no-ack -> mark_uncertain + 502; not-ok -> mark_terminal
    FAILED + 502; status==error -> REJECTED (+LIMIT_OFF_TICK/IB_REJECT) 502; clean
    -> _persist_submitted_or_compensate + persisted stage; returns result.data.

    Returns the response object (dict or JSONResponse) OR raises HTTPException
    exactly as the original tail did.

    RELOCATION: paste the captured region BELOW seam B verbatim (from
    ``if result.ack:`` to ``return result.data``). One change only: apply the §5
    name-qualification table. Do NOT restructure any branch. This function is
    ``def`` (not ``async``) — the relocated tail contains no ``await``; if the
    captured tail DOES contain an ``await``, keep this ``async`` and make the
    orchestrator ``await`` it (STOP and note the divergence).
    """
    import xenon.api.server as _server  # lazy: avoids import cycle
    raise NotImplementedError  # replace this whole body per the RELOCATION note


async def place_order_from_body(body: dict) -> object:
    """Orchestrator — the extracted equivalent of _orders_place_from_body.

    Captures the P1.1 stage clock + correlation id once, then runs the three
    stages, short-circuiting on any stage that returns a response.
    """
    t0 = time.monotonic()
    stage_cid = str(body.get("client_attempt_id") or "")
    gate = await run_gates(body, t0, stage_cid)
    if not isinstance(gate, str):
        return gate  # early reject / 409 / 200-duplicate
    submission_id = gate
    submitted = await submit_to_broker(body, submission_id, t0, stage_cid)
    if not isinstance(submitted, ScriptResult):
        return submitted  # test-mode payload
    return persist_outcome(submission_id, submitted, t0, stage_cid)
```

**Fill the three bodies** by relocating the captured regions per each docstring's RELOCATION note.
Delete each `raise NotImplementedError` and the placeholder line as you paste.

> **Do NOT re-derive the branch logic from the §5 reference — copy from your Step-1 capture.** The
> §5 reference elides details (`{...}`); your capture is complete and correct.

### Step 4 — Replace `_orders_place_from_body` with the delegating wrapper

In `src/xenon/api/server.py`, replace the **entire** `_orders_place_from_body` function body
(everything from `async def _orders_place_from_body(body: dict):` down to its final
`return result.data`, i.e. the region you captured in Step 1) with this 3-line wrapper:

```python
async def _orders_place_from_body(body: dict):
    # P2.4/CX-2: body decomposed into services/place_order.py (run_gates ->
    # submit_to_broker -> persist_outcome). This wrapper is the zero-break shim —
    # the route (server.py) and the combo wizard (session.py:327) call it
    # unchanged; tests still monkeypatch it on the server module.
    from xenon.api.services import place_order as _place_svc  # lazy: import cycle
    return await _place_svc.place_order_from_body(body)
```

Leave the `orders_place` route (`server.py:2113`), `_resolve_scope_kwargs`, and every helper
in §3 non-goals **untouched**. Do not remove any now-unused imports from `server.py` yet — several
(`json`, `Decimal`, `ReasonCode`, `orders_store`, `JSONResponse`, `HTTPException`) are still used by
the cancel/modify handlers and other routes. **Do not touch server.py imports at all** (avoids
accidental breakage; unused-import lint is not enforced on server.py — verify with V-lint if
worried, but the default is: leave them).

### Step 5 — Update S6's static call-site test (the one test the move legitimately invalidates)

In `src/xenon/api/tests/test_order_subprocess_semaphore.py`, function
`test_call_sites_repointed_statically`. The place `_run_order_subprocess(` call moved out of
`server.py` into the service, so server.py's count drops from 4 → 3. Make these edits:

- The place-literal assertion `assert '_run_ib_script_with_recovery("xenon-ib-place-order"' not in src`
  — **keep** (still true).
- `assert src.count("_run_order_subprocess(") == 4` → change to `== 3`, and update its comment to:
  `# place moved to services/place_order.py (P2.4); server keeps cancel + modify calls + the def`.
- Add, immediately after that assertion, a positive assertion that the place call now lives in the
  service and still goes through the bounded wrapper:

```python
    # P2.4: the place call was extracted to the service and still uses the
    # bounded wrapper (attribute access on the server module keeps the S6 patch
    # in effect).
    from xenon.api.services import place_order as _place_svc
    place_src = inspect.getsource(_place_svc)
    assert "_run_order_subprocess(" in place_src
    assert '"xenon-ib-place-order"' in place_src
```

- The `assert src.count("_run_ib_script_with_recovery(") == 8` assertion — **verify, do not blindly
  keep.** The place body did NOT call `_run_ib_script_with_recovery` directly (it went through
  `_run_order_subprocess`), so moving it out should leave that count unchanged at 8. Run the test; if
  it now reports a different count, the merged place body _did_ reference the raw helper — adjust the
  literal to the observed count and note it in the PR. (Expected: still 8.)

> This is the ONLY behavior-neutral test edit permitted. Every other existing test must pass
> **unchanged**. If any other test needs editing to go green, **STOP** — that means the refactor
> changed behavior.

### Step 6 — Add the service to the caller-allowlist guard

`services/place_order.py` now contains the literal `"xenon-ib-place-order"`, which
`scripts/checks/order_path_caller_allowlist.py` forbids outside its `_ALLOWLIST`. Add the module —
it is a **legitimate** runtime caller (it runs only after preflight + quote gate + reservation, all
of which precede it in `run_gates`). In `_ALLOWLIST` (the `frozenset({...})`), add, right after the
`"src/xenon/api/server.py",` entry:

```python
        # The place handler body, extracted from server.py (P2.4). Invokes
        # xenon-ib-place-order via subprocess only after run_gates (preflight +
        # quote gate + reservation) has approved.
        "src/xenon/api/services/place_order.py",
```

### Step 7 — Green: golden test + full order-path suite

```bash
# Golden contract-identical (Step 2) — now exercises the extracted path:
uv run pytest scripts/tests/test_place_order_golden_response.py -xvs
# The load-bearing gates — S2 fake-CLI route tests + all prereq + legacy place tests:
uv run pytest \
  scripts/tests/test_orders_place_uncertain_route.py \
  scripts/tests/test_orders_place_post_ack_persist.py \
  scripts/tests/test_order_stage_logging.py \
  scripts/tests/test_idempotency_route.py \
  scripts/tests/test_place_quote_gate.py \
  scripts/tests/test_preflight_route.py \
  scripts/tests/test_orders_place_no_regime_gate.py \
  src/xenon/api/tests/test_order_subprocess_semaphore.py \
  src/xenon/api/tests/test_wizard_routes.py \
  src/xenon/api/tests/test_wizard_mode_guard.py \
  -x
```

All must pass. If `test_orders_place_uncertain_route.py` (the S2 fake-CLI tests) shows any failure,
the extraction changed the real subprocess/classify path — **STOP** and diff your relocated
`persist_outcome` against the Step-1 capture line-by-line.

---

## 7. Verification matrix (run every row; exact command + exact expected outcome)

| #   | Check                                                 | Exact command                                                                                                   | Expected                                                                               |
| --- | ----------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| V0  | Prereq gate                                           | the §0 grep chain                                                                                               | prints `PREREQS-OK`                                                                    |
| V1  | Golden contract-identical (pre-refactor baseline, Step 2) | `uv run pytest scripts/tests/test_place_order_golden_response.py -xvs` (run before Step 3)                      | `1 passed`                                                                             |
| V2  | Golden contract-identical (post-refactor)                 | same command (after Step 6)                                                                                     | `1 passed`, identical assertion                                                        |
| V3  | S2 fake-CLI route tests (LOAD-BEARING)                | `uv run pytest scripts/tests/test_orders_place_uncertain_route.py -x`                                           | all pass, unchanged                                                                    |
| V4  | S4 persist-warning route tests                        | `uv run pytest scripts/tests/test_orders_place_post_ack_persist.py -x`                                          | all pass, unchanged                                                                    |
| V5  | P1.1 stage-log tests                                  | `uv run pytest scripts/tests/test_order_stage_logging.py -x`                                                    | all pass, unchanged (place stages still fire)                                          |
| V6  | S6 semaphore + updated static test                    | `uv run pytest src/xenon/api/tests/test_order_subprocess_semaphore.py -x`                                       | all pass (static test edited in Step 5)                                                |
| V7  | Idempotency route                                     | `uv run pytest scripts/tests/test_idempotency_route.py -x`                                                      | all pass, unchanged                                                                    |
| V8  | Quote-gate route                                      | `uv run pytest scripts/tests/test_place_quote_gate.py -x`                                                       | all pass, unchanged                                                                    |
| V9  | Preflight route                                       | `uv run pytest scripts/tests/test_preflight_route.py -x`                                                        | all pass, unchanged                                                                    |
| V10 | No-regime-gate route + static                         | `uv run pytest scripts/tests/test_orders_place_no_regime_gate.py -x`                                            | all pass (its static assert scans server.py for deleted regime symbols — still absent) |
| V11 | Wizard in-process caller intact                       | `uv run pytest src/xenon/api/tests/test_wizard_routes.py src/xenon/api/tests/test_wizard_mode_guard.py -x`      | all pass — proves the shim keeps `session.py:327` working                              |
| V12 | Order-store store tests                               | `uv run pytest scripts/tests/test_orders_submissions_store.py -x`                                               | all pass                                                                               |
| V13 | Scoped affected suite                                 | `uv run python scripts/infra/dev/run_pytest_affected.py`                                                        | exit 0                                                                                 |
| V14 | CI guard — fallback reads                             | `uv run python scripts/checks/no_json_fallback_on_order_path.py`                                                | exit 0                                                                                 |
| V15 | CI guard — writes                                     | `uv run python scripts/checks/no_json_write_on_order_path.py`                                                   | exit 0                                                                                 |
| V16 | CI guard — caller allowlist (the one this PR extends) | `uv run python scripts/checks/order_path_caller_allowlist.py`                                                   | `OK — no unauthorized callers of ib_place_order.` exit 0                               |
| V17 | Guard sees the new allowlist entry                    | `uv run python scripts/checks/order_path_caller_allowlist.py --show-allowlist \| grep place_order.py`           | prints `src/xenon/api/services/place_order.py`                                         |
| V18 | Import-cycle smoke                                    | `uv run python -c "import xenon.api.server; from xenon.api.services import place_order; print('ok')"`           | prints `ok` (no ImportError)                                                           |
| V19 | Wizard delegate smoke (attribute still present)       | `uv run python -c "import xenon.api.server as s; assert callable(s._orders_place_from_body); print('shim ok')"` | prints `shim ok`                                                                       |

**Negative / both-directions coverage.** V3 already spans both directions of the real path
(ack-then-kill → WORKING **and** timeout-no-ack → UNCERTAIN 502 **and** clean reject → 502 **and**
clean success → 200), and V1/V2 pin the success test-mode body. V11 proves the shim (in-process
caller works). V16+V18 prove the guard/allowlist direction (place literal is authorized in the new
location AND the module imports without cycling). There is no auth/guard toggle introduced here, so
no auth negative test applies.

**Web / typecheck / lint / E2E:** N/A — this PR is Python-only, server-side, with **no** HTTP
contract change and **no** UI-visible surface. No Vitest, no Playwright, no `tsc`, no `npm run
lint`. State this explicitly in the PR description so reviewers do not expect browser evidence.

**Live paper probe (OPTIONAL, PAPER ONLY — not required for merge):** if smoke-testing, start
`scripts/infra/dev.sh paper` (IB port 4002, Next :3200, FastAPI :8421), place one paper stock order
via the UI, and confirm it fills / appears in open orders exactly as before. **Never run against
live IB (port 4001)** — order-path checks are paper-only per repo policy.

---

## 8. Tripwires / abort criteria

- **STOP if the §0 prereq gate prints `PREREQS-MISSING`** — S2/S4/S6/P1.1 not all merged; the
  decomposition would target a stale shape and have to be redone. Report which grep failed.
- **STOP if Step 1 cannot locate SEAM A or SEAM B**, or the test-mode block sits _after_ the
  subprocess call — a prereq merged into a different structure than §5 assumes; re-plan needed.
- **STOP if any existing test (V3–V12) goes red and the fix is anything other than the single
  Step-5 static-count edit.** A red behavioral test means the relocation changed behavior — diff the
  relocated stage against the Step-1 capture; do not weaken the test.
- **STOP if the golden test (V1) fails PRE-refactor** — reconcile the golden dict to the actual
  pre-refactor response first (the invariant is before==after, whatever "before" is).
- **STOP if V18 raises ImportError (cycle)** — you used a module-level `import xenon.api.server` in
  the service or a module-level `from xenon.api.services.place_order import …` in server.py. Both
  server-references and the wrapper's service-reference MUST be **lazy** (inside functions), per
  Steps 2–4.
- **STOP if V16 fails** — the guard flags the new service module; you missed the Step-6 allowlist
  entry (or added it with a wrong path — it must be `src/xenon/api/services/place_order.py`,
  posix-relative to repo root).
- **STOP if more than these files change:** `src/xenon/api/services/place_order.py` (new),
  `src/xenon/api/server.py` (wrapper only), `src/xenon/api/tests/test_order_subprocess_semaphore.py`
  (Step-5 static edit), `scripts/checks/order_path_caller_allowlist.py` (allowlist entry),
  `scripts/tests/test_place_order_golden_response.py` (new). Any other file → the refactor is
  leaking scope; report.
- **Never run a live-IB step.** Paper only (`dev.sh paper`, port 4002).
- **Do NOT reshape any branch** in `persist_outcome` to "make it cleaner". Verbatim relocation only —
  cleanliness is the _structural_ win (three named functions), not a rewrite of the S2 classification.

---

## 9. Incident-history row

**N/A** — this is a pure refactor (function extraction), not an order-path bug fix. Per
`docs/reference/order-path-incident-history.md`'s scope (bugs with root-cause + regression-test
lineage), no row is appended. If a reviewer wants a trace-forward breadcrumb, the golden test
`scripts/tests/test_place_order_golden_response.py` + the contract-identical verification matrix are the
lineage.

---

## 10. Rollback

Pure code move, no schema/migration, no data touched. Revert by discarding the branch:

```bash
git checkout master && git branch -D refactor/decompose-place-handler
```

If already merged: revert the squash commit — deleting `services/place_order.py`, restoring the
inlined `_orders_place_from_body` body, reverting the S6 static-test count and the allowlist entry
restores exact prior state. No residual state (no rows, no files written).

---

## 11. Commit / PR

Commit message (no AI-attribution trailer, per global policy):

```
refactor(orders): extract place handler into services/place_order.py (CX-2)

_orders_place_from_body body moved into a run_gates -> submit_to_broker ->
persist_outcome service; server keeps a 2-line delegating shim so the route and
the combo-wizard in-process caller are unchanged. Byte-identical responses
(golden + full place-route suite green). Service added to the ib_place_order
caller allowlist; S6 static call-site test updated for the moved place call.
No behavior change.
```

Push the branch and open a PR (never `git push origin master`); wait for green CI before merge.
