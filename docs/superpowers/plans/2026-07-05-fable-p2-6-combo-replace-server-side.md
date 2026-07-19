# Plan: P2.6 — Combo replace server-side + combo limit-band gate + one combo net-price implementation

**Date:** 2026-07-05
**Findings:** OP-6 (High, combo replace = cancel-then-place in Next.js with no recovery), OP-17 (Medium, quote gate skips combos), CX-3 (Medium, combo net-price math ×3 in web)
**Goal:** A combo leg-structure replace can never silently leave a naked position; fat-finger combo net prices are rejected before they reach IB; one implementation of the combo net-quote math in web.
**Reviewed at:** fable docs @ `4d864294`; all anchors below re-verified at HEAD 2026-07-05.

**This is a stated TWO-PR sequence** (justification in "PR split"):

- **PR 1 — `fix/combo-replace-server-side`** (OP-6): FastAPI `POST /orders/replace` with a durable two-phase **audit record + live-response recovery affordance** (plus a read-path orphan-detection query — see Step 1.7); Next route becomes a thin proxy; UI recovery path.
- **PR 2 — `feat/combo-net-price-gate`** (OP-17 + CX-3): `quote_guard.check_combo_payload` limit-band gate wired into `/orders/place` and (automatically) `/orders/replace`; one `computeSignedComboQuote` core in web with all three call sites rewired.

PR 1 must merge and be paper-verified before PR 2 starts. Do not combine: PR 1 rewires the order-path state machine (highest-risk change of the P2 batch, must be reviewable and revertable in isolation); PR 2 changes pricing/validation semantics across Python + 4 web files. Together they would be a ~1,500-line diff spanning both risk domains.

---

## Context — what exists today

- `web/app/api/orders/modify/route.ts` `POST`: when `body.replaceOrder` is present, the **Next.js route itself** loops `xenonFetch("/orders/cancel")` over `normalizeCancelTargets(...)` and then `xenonFetch("/orders/place")` with the raw `replaceOrder`. A place failure after cancel returns `{ error: "CRITICAL: Original order cancelled, replacement FAILED. Place a new order manually." }` with status 502 — a string, no durable record, no retry affordance. (Anchor: comment `// Cancel-then-place is unavoidable here`.)
- `src/xenon/api/server.py::_orders_place_from_body`: gate order is broker-scope 403 → `_run_preflight` (F2) → quote gate **only** `if body.get("type") != "combo"` (F3, anchor: `qv, quote_status = await _validate_non_combo_quote(body)`) → `client_attempt_id` required (F4 `reserve_attempt`) → test-mode short-circuit → subprocess `xenon-ib-place-order` → `mark_terminal`/`mark_submitted`.
- `src/xenon/api/server.py::_orders_cancel_from_body`: subprocess `xenon-ib-order-manage cancel`, `_classify_to_http` failure mapping, `_mark_submission_cancelled` on success.
- `src/xenon/execution/quote_guard.py::check_payload`: single-contract band — BUY cap `min(ask*1.05, ask+2*min_tick)`, SELL floor `max(bid*0.95, bid-2*min_tick)`; `LIMIT_OUT_OF_BAND`.
- Combo net-price math ×3 in web (CX-3, all verified present):
  1. `web/lib/optionsChainUtils.ts::computeNetOptionQuote` (leg list + prices dict; returns **abs-normalized** `{bid, ask, mid}`),
  2. `web/components/ticker-detail/OrderTab.tsx` `netPrices` useMemo (anchor: `const effectivelySelling = (action === "SELL") === (leg.direction === "LONG");`),
  3. `web/components/ModifyOrderModal.tsx::resolveOrderPriceData` BAG branch (anchor: `if (c.secType === "BAG") {`, two inner loops: `c.comboLegs` primary + portfolio-legs fallback).
- `orders_store.record_event(submission_id, kind, detail)` writes append-only `xenon.order_events` (FK → `order_submissions.submission_id`, NOT NULL). `reserve_attempt(user_id, cid, req_row, *, broker/account_env/broker_account, override_audit=None)` returns `ReservationOutcome(status="winner"|"duplicate"|"terminal", submission_id, state, duplicate_of, reason_code)` — the fresh-reservation status is **`"winner"`, not `"reserved"`** (verified `orders_store.py:154`). The sketches below only branch on `"terminal"`/`"duplicate"` and fall through on `"winner"`, so they are correct; do not introduce an `== "reserved"` check.
- Toast plumbing: `web/lib/OrderActionsContext.tsx::pushNotification({type, message, duration?})` → drained by `WorkspaceShell` → `addToast(type, message, duration)`; `web/lib/useToast.ts`: `duration = 5000` default, `if (duration > 0)` auto-dismiss — **`duration: 0` = persistent toast**. Error mapping copy lives in `web/lib/orderReasonCodes.ts::ORDER_REASON_CODES` + `getReasonToast`.
- The executor does NOT need to understand: the combo wizard (`wizard_sessions`), rehydrate/poller internals, Futu, or the relay.

## Drift from review

> **HEAD bug verdict (Pass 1 fact-check, 2026-07-06): CONFIRMED.** Traced end-to-end in the working tree:
>
> 1. `ReplaceComboOrder` (`web/lib/orderModify.ts:10`) has no `client_attempt_id` field — verified the whole type declaration.
> 2. `web/app/api/orders/modify/route.ts` cancels first (loop `xenonFetch("/orders/cancel", …)`, lines 146-153, anchor `// Cancel-then-place is unavoidable here`) then places `JSON.stringify(replaceOrder)` **verbatim** (line 160) — no cid injected.
> 3. `_orders_place_from_body` (`server.py:2139`) gate order: broker 403 → F2 `_run_preflight` → **F3 combo branch is `else: _override_detail = None`** (lines 2203-2204, quote gate skipped for combos) → F4 `cid = body.get("client_attempt_id")` → `if not cid:` returns `JSONResponse(status_code=400, {"detail": "client_attempt_id is required"})` (lines 2207-2212), which is **before** the test-mode short-circuit (line 2258) and before `reserve_attempt`.
> 4. Back in the Next route, the 400 makes `xenonFetch` throw → `catch (placeErr)` (line 163) → 502 `"CRITICAL: Original order cancelled, replacement FAILED."` (line 179).
>
> Net: for any otherwise-valid combo replacement the original is cancelled and the replacement deterministically 400s on the missing cid → the exact naked-position failure OP-6 describes. (Preflight passes for a covered vertical like the bull-call test body — `combo_uncovered_short_call_ratio ≤ 0` short-circuits `evaluate_combo` with an empty `PortfolioView`, so it never needs a portfolio snapshot; even if preflight rejected, it would still reject _after_ the cancel.) Live-unverified but the code path is unambiguous.

1. **Latent bug worse than OP-6 states (HIGH-confidence code reading, unverified live):** `ReplaceComboOrder` (`web/lib/orderModify.ts`) has **no `client_attempt_id`**, and the Next modify route forwards it verbatim to FastAPI `/orders/place`, which returns 400 `"client_attempt_id is required"` **before** the test-mode short-circuit. So at HEAD, every leg-structure combo replace against real FastAPI cancels the original and then **always fails placement** — the exact naked-position failure OP-6 warns about, deterministically. The harness test `web/tests/order-e2e.test.ts` ("accepts valid combo replacement (requires FastAPI)", expects 200) contradicts this; it is gated by `fastApiIt` and presumably skips when the harness is unavailable. PR 1 fixes this by construction (the replace endpoint requires and consumes a `client_attempt_id`); the plan adds a regression test. Do not "quick-fix" by injecting a cid into the old Next-route flow — the whole flow moves server-side.
2. `docs/fable/11-code-sketches.md` has **no sketch** for P2.6 (verified: no OP-6/OP-17 sketch entries). All code below is authored fresh in this plan.
3. Line numbers in the finding (`route.ts:105-204`, `server.py:2185-2202`) have drifted slightly; anchors above are function names + snippets. Everything else in OP-6/OP-17/CX-3 is confirmed at HEAD.

## Goal / Non-goals

**Goals**: as in the header. **Non-goals** (do NOT touch):

- OP-1/OP-2/OP-3 ack-protocol / UNCERTAIN state / orderRef (P1 items, separate plans).
- OP-16 retry-duplication; OP-8 expected_states sweep semantics.
- No `place-first-then-cancel`. **Decision: cancel-then-place with a durable two-phase record.** Rationale: placing the replacement while the original is still WORKING double-books the position in xenon's own preflight — `working_reservations_for` feeds `evaluate_combo`, so the replacement's short legs would see their cover consumed by the original's reservation and be spuriously blocked (or worse, pass and truly double margin at IB). Place-first is structurally incompatible with the existing naked-short guard; cancel-then-place + pre-cancel validation + durable recovery record is the design that can never leave an _unrecorded_ naked position.
- No changes to price/qty-only combo modify (already atomic via `modify_order`).
- No CX-3 regime dead-code deletion (that is P2.1).
- No new order state enum value (no `UNCERTAIN` here); recovery is `order_events` rows + response payload, not a schema migration. **No Alembic migration in either PR.**

## Key facts (all verified at HEAD)

- `ReasonCode` is `StrEnum` in `src/xenon/execution/preflight.py` (members incl. `LIMIT_OUT_OF_BAND`, `QUOTE_UNAVAILABLE`, `OPTION_MARKET_CLOSED`, `IB_CONNECTION`, `SUBPROCESS_ERROR`).
- `_orders_place_from_body` reads `body.get("acknowledge_limit_override") is True` to bypass `LIMIT_OUT_OF_BAND` (records `PREFLIGHT_ACK_LIMIT` event post-reservation via `_override_detail`).
- Combo place body shape (consumed by `_body_to_combo_preflight_request` and `xenon-ib-place-order`): `{type:"combo", symbol, action:"BUY"|"SELL", quantity, limitPrice (signed: credits negative), tif?, legs:[{expiry, strike, right:"C"|"P", action:"BUY"|"SELL", ratio}], client_attempt_id}`.
- `_run_ib_script_with_recovery(name, args, timeout)` → object with `.ok`, `.data`, `.error`.
- `_is_test_mode()` + `XENON_API_TEST_MODE=1` short-circuit place/cancel; `_next_test_order_ids()` mints fake ids.
- Route deps: `/orders/cancel` and `/orders/modify` use `dependencies=[Depends(require_mode_verified)]`; `/orders/place` calls `require_mode_verified(request)` inline after broker check; all three check `is_read_only()` → `read_only_403()` (from `xenon.api.guards`, already imported in server.py).
- **In-process route bypass** (memory): `Depends` only fires over HTTP — the new `/orders/replace` must declare its own dep + read-only check; calling `_orders_cancel_from_body`/place helpers in-process skips route guards, which is fine because the replace route applies them itself.
- Toast helpers read `body.reason_code` **top-level** — use `JSONResponse(content={...})`, never `HTTPException(detail={...})` for new mappings.
- Web thin-proxy convention: `xenonFetch()` from `web/lib/xenonApi.ts`; error passthrough via `passThroughXenonError` preserves upstream JSON verbatim.
- `client_attempt_id` minting: the **browser** mints it (`crypto.randomUUID()` in ModifyOrderModal, once per confirmed replace intent — Step 1.5); the Next modify route only validates presence and passes it through. It never mints (a per-request route-minted cid would defeat F4 dedup on double-click/retry).
- Python test conventions: `scripts/tests/test_place_quote_gate.py` is the template — `pytestmark = pytest.mark.committed_db`, autouse env fixture setting `XENON_API_TEST_MODE=1` + `XENON_QUOTE_TOKEN_SECRET`, `TestClient(server.app)` (lifespan skipped — never rely on `app.state` beyond what tests set).
- Dev ports 3200/8421/8866; paper = `scripts/infra/dev.sh paper`, IB port 4002. All Python via `uv run`.
- web/CLAUDE.md combo quote invariant: `To BUY combo: pay ASK on BUY legs, receive BID on SELL legs; To SELL combo: receive BID on BUY legs, pay ASK on SELL legs`. Credits negative, debits positive; never `Math.abs()` away sign (the abs-normalization inside `computeNetOptionQuote` is display-layer bid<ask ordering, keep its output contract).
- Combo guardrail (root CLAUDE.md): combo-entry bugs REQUIRE a unit test for action/ratio/net-price semantics AND a browser test for displayed net price + submitted payload.

---

# PR 1 — `fix/combo-replace-server-side` (OP-6)

## Step 1.1 — Failing Python route tests first

Create `scripts/tests/test_orders_replace_route.py`:

```python
"""POST /orders/replace — server-side combo replace with durable two-phase record.

Regression for fable OP-6: the old Next.js-route cancel-then-place left a
cancelled original + a "CRITICAL" string on place failure. The server-side
flow must (a) validate the replacement BEFORE cancelling, (b) persist
REPLACE_STARTED / REPLACE_PLACE_FAILED / REPLACE_COMPLETED order_events,
(c) return top-level reason_code REPLACE_PLACE_FAILED plus a recovery
payload on the cancel-succeeded/place-failed path.
"""
import pytest

pytestmark = pytest.mark.committed_db

from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    monkeypatch.setenv("XENON_QUOTE_TOKEN_SECRET", "d" * 64)
    monkeypatch.setenv("XENON_DATA_DIR", str(tmp_path))
    yield


@pytest.fixture
def client():
    from xenon.api import server
    from xenon.execution import orders_store

    orders_store.init_store()
    return TestClient(server.app)


def _replace_body(cid: str) -> dict:
    # SPY 2026-06-18 bull call spread, real strikes; prices irrelevant in test mode.
    return {
        "cancelOrders": [{"orderId": 0, "permId": 1745392200}],
        "replaceOrder": {
            "type": "combo",
            "symbol": "SPY",
            "action": "BUY",
            "quantity": 1,
            "limitPrice": 2.35,
            "tif": "DAY",
            "legs": [
                {"expiry": "20260618", "strike": 620, "right": "C", "action": "BUY", "ratio": 1},
                {"expiry": "20260618", "strike": 630, "right": "C", "action": "SELL", "ratio": 1},
            ],
            "client_attempt_id": cid,
        },
    }


def _events_for(cid: str) -> list[tuple[str, dict]]:
    from sqlalchemy import select
    from xenon.db.engine import get_sync_engine
    from xenon.db.schema import order_events, order_submissions

    eng = get_sync_engine()
    with eng.connect() as conn:
        sid = conn.execute(
            select(order_submissions.c.submission_id).where(
                order_submissions.c.client_attempt_id == cid
            )
        ).scalar_one()
        rows = conn.execute(
            select(order_events.c.kind, order_events.c.detail)
            .where(order_events.c.submission_id == sid)
            .order_by(order_events.c.at)
        ).all()
    return [(r[0], r[1]) for r in rows]


def test_replace_success_records_started_and_completed(client):
    resp = client.post("/orders/replace", json=_replace_body("rep-ok-1"))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["orderId"] > 0
    kinds = [k for k, _ in _events_for("rep-ok-1")]
    assert "REPLACE_STARTED" in kinds
    assert "REPLACE_COMPLETED" in kinds


def test_replace_place_failure_returns_recovery_payload(client, monkeypatch):
    from xenon.api import server

    async def _boom(script, args, timeout=15):
        class R:
            ok = False
            data = None
            error = "injected subprocess failure"
        return R()

    # Test mode short-circuits the subprocess, so force real-path execution
    # for the place segment only.
    monkeypatch.setattr(server, "_is_test_mode", lambda: False)
    monkeypatch.setattr(server, "_run_ib_script_with_recovery", _boom)

    async def _cancel_ok(body):
        return {"status": "ok", "message": "cancelled"}

    monkeypatch.setattr(server, "_orders_cancel_from_body", _cancel_ok)

    resp = client.post("/orders/replace", json=_replace_body("rep-fail-1"))
    assert resp.status_code == 502
    body = resp.json()
    assert body["reason_code"] == "REPLACE_PLACE_FAILED"          # top-level
    assert body["recovery"]["replaceOrder"]["symbol"] == "SPY"     # one-click re-place data
    assert body["recovery"]["replaceOrder"]["legs"][1]["strike"] == 630
    kinds = [k for k, _ in _events_for("rep-fail-1")]
    assert "REPLACE_STARTED" in kinds
    assert "REPLACE_PLACE_FAILED" in kinds
    assert "REPLACE_COMPLETED" not in kinds


def test_replace_preflight_reject_cancels_nothing(client, monkeypatch):
    from xenon.api import server

    called = []

    async def _cancel_spy(body):
        called.append(body)
        return {"status": "ok"}

    monkeypatch.setattr(server, "_orders_cancel_from_body", _cancel_spy)

    bad = _replace_body("rep-preflight-1")
    bad["replaceOrder"]["legs"][0]["right"] = "X"  # INVALID_ORDER_BODY in preflight
    resp = client.post("/orders/replace", json=bad)
    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "INVALID_ORDER_BODY"
    assert called == []  # original never touched


def test_replace_cancel_failure_leaves_original_and_marks_attempt_failed(client, monkeypatch):
    from fastapi import HTTPException
    from xenon.api import server

    async def _cancel_fail(body):
        raise HTTPException(status_code=503, detail={"reason_code": "IB_CONNECTION", "message": "gw down"})

    monkeypatch.setattr(server, "_orders_cancel_from_body", _cancel_fail)

    resp = client.post("/orders/replace", json=_replace_body("rep-cancelfail-1"))
    assert resp.status_code == 503
    kinds = [k for k, _ in _events_for("rep-cancelfail-1")]
    assert "REPLACE_CANCEL_FAILED" in kinds
    # replacement attempt must be terminal FAILED, not left PENDING
    from sqlalchemy import select
    from xenon.db.engine import get_sync_engine
    from xenon.db.schema import order_submissions
    with get_sync_engine().connect() as conn:
        state = conn.execute(
            select(order_submissions.c.state).where(
                order_submissions.c.client_attempt_id == "rep-cancelfail-1"
            )
        ).scalar_one()
    assert state == "FAILED"


def test_replace_requires_client_attempt_id(client):
    body = _replace_body("unused")
    del body["replaceOrder"]["client_attempt_id"]
    resp = client.post("/orders/replace", json=body)
    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "INVALID_ORDER_BODY"  # top-level (toast invariant)


def test_replace_same_cid_concurrent_is_idempotent(client):
    # Scenarios 2/5: double-click / timeout-retry replays the SAME browser-minted
    # cid. Second request must hit the F4 duplicate short-circuit: exactly one
    # placement, no second cancel/place phase run.
    body = _replace_body("rep-dup-1")
    first = client.post("/orders/replace", json=body)
    assert first.status_code == 200, first.text
    second = client.post("/orders/replace", json=body)
    assert second.status_code in (200, 409)
    if second.status_code == 200:
        assert second.json().get("duplicate_of") or second.json().get("submission_id")
    kinds = [k for k, _ in _events_for("rep-dup-1")]
    assert kinds.count("REPLACE_STARTED") == 1  # duplicate never re-ran the phases


def test_replace_read_only_mode_refuses(client, monkeypatch):
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    resp = client.post("/orders/replace", json=_replace_body("rep-ro-1"))
    assert resp.status_code == 403
    assert resp.json()["reason_code"] == "READ_ONLY_MODE"
```

Run: `uv run pytest scripts/tests/test_orders_replace_route.py -x` → **all fail with 404** (route doesn't exist). If any passes, STOP — anchors are wrong.

## Step 1.2 — ReasonCode additions

`src/xenon/execution/preflight.py`, inside `class ReasonCode(StrEnum)` — anchor: line `SUBPROCESS_ERROR = "SUBPROCESS_ERROR"` — append after it:

```python
    # OP-6 — server-side combo replace (two-phase cancel-then-place)
    REPLACE_PLACE_FAILED = "REPLACE_PLACE_FAILED"
    # OP-6 — original filled in the preflight→cancel / cancel-request→ack window;
    # placing the replacement would double the position. Abort BEFORE place.
    REPLACE_FILL_RACE = "REPLACE_FILL_RACE"
```

(Cancel-phase failures keep their upstream codes — `IB_CONNECTION`/`OWNERSHIP`/`IB_REJECT` — passed through; no new code needed there.)

## Step 1.3 — Refactor `_orders_place_from_body` into 3 composable pieces

In `src/xenon/api/server.py`. Mechanical split — **no behavior change**; existing tests must stay green after this step alone.

1. Extract everything from the broker-scope checks through the F3 quote-gate block (i.e. from `broker = str(getattr(app.state, "broker", ...)` down to and including the `else: _override_detail = None` after `# F3:`) into:

```python
async def _validate_place_gates(body: dict) -> tuple[JSONResponse | None, dict | None]:
    """Broker scope + preflight (F2) + quote gate (F3). Returns
    (error_response, override_detail). error_response is None on accept."""
```

Return `(JSONResponse(...), None)` for each existing early-return, and `(None, _override_detail)` on accept. Keep all response payloads byte-identical.

2. Extract everything after a successful `reserve_attempt` (from `if _override_detail is not None:` through the final `return result.data`) into:

```python
async def _execute_reserved_place(submission_id: str, body: dict, _override_detail: dict | None):
    """Test-mode short-circuit, subprocess place, mark_submitted/mark_terminal.
    Precondition: submission_id already reserved."""
```

3. Rebuild `_orders_place_from_body` as: gates → cid check → `reserve_attempt` (unchanged, incl. duplicate/terminal handling) → `return await _execute_reserved_place(submission_id, body, _override_detail)`.

Verify no drift: `uv run pytest scripts/tests/test_place_quote_gate.py scripts/tests/test_preflight_route.py scripts/tests/test_orders_place_no_regime_gate.py -x` → green.

## Step 1.4 — The `/orders/replace` endpoint

Add to `src/xenon/api/server.py`, directly after the `orders_place` route function:

```python
@app.post("/orders/replace", dependencies=[Depends(require_mode_verified)])
async def orders_replace(request: Request):
    """Replace a working combo order's leg structure (OP-6).

    IB has no atomic leg-restructure, so this is cancel-then-place — but with
    the phases ordered and recorded so a partial failure is never silent:

      1. Validate the replacement FIRST (preflight + quote gates). A bad
         replacement never cancels the original.
      2. Reserve the replacement attempt (F4) → durable PENDING row.
      3. REPLACE_STARTED event on the new submission: cancel targets + the
         full replacement payload (the recovery record).
      4. Cancel the original(s). Failure → original intact, attempt FAILED,
         REPLACE_CANCEL_FAILED event, upstream error passed through.
      5. Place. Failure → REPLACE_PLACE_FAILED event + 502 with top-level
         reason_code and a `recovery` payload the UI can one-click re-place.
    """
    if is_read_only():
        return read_only_403()
    body = await request.json()
    return await _orders_replace_from_body(body)


async def _orders_replace_from_body(body: dict):
    replace_order = body.get("replaceOrder") or {}
    cancel_targets = [
        t for t in (body.get("cancelOrders") or [])
        if int(t.get("orderId") or 0) > 0 or int(t.get("permId") or 0) > 0
    ]
    limit_price = replace_order.get("limitPrice")
    limit_ok = (
        limit_price is not None
        and isinstance(limit_price, (int, float))
        and limit_price == limit_price  # not NaN
        and limit_price != 0
    )
    if (
        replace_order.get("type") != "combo"
        or not replace_order.get("symbol")
        or not replace_order.get("action")
        or not replace_order.get("quantity")
        or not limit_ok
        or len(replace_order.get("legs") or []) < 2
        or not cancel_targets
    ):
        return JSONResponse(
            status_code=400,
            content={
                "detail": "Invalid combo replacement payload",
                "reason_code": ReasonCode.INVALID_ORDER_BODY.value,
            },
        )
    cid = replace_order.get("client_attempt_id")
    if not cid:
        # Toast invariant: top-level reason_code (body.reason_code).
        return JSONResponse(
            status_code=400,
            content={
                "detail": "client_attempt_id is required",
                "reason_code": ReasonCode.INVALID_ORDER_BODY.value,
            },
        )

    # Phase 1 — validate the replacement BEFORE touching the original.
    error_resp, _override_detail = await _validate_place_gates(replace_order)
    if error_resp is not None:
        return error_resp

    # Phase 2 — durable reservation for the replacement.
    security_type = "BAG"
    req_row = orders_store.RequestRow(
        ticker=str(replace_order.get("symbol", "")).upper(),
        security_type=security_type,
        action=str(replace_order.get("action", "")).upper(),
        quantity=int(replace_order.get("quantity", 0)),
        expiry=None,
        strike=None,
        right=None,
        multiplier=int(replace_order.get("multiplier", 100)),
        con_id=None,
        limit_price=Decimal(str(replace_order.get("limitPrice", "0"))),
    )
    outcome = orders_store.reserve_attempt(
        "local", cid, req_row, override_audit=None, **_resolve_scope_kwargs()
    )
    if outcome.status == "terminal":
        return JSONResponse(
            status_code=409,
            content={
                "detail": f"attempt {cid} already in terminal state {outcome.state}",
                "reason_code": ReasonCode.ATTEMPT_ID_TERMINAL.value,
                "state": outcome.state,
            },
        )
    if outcome.status == "duplicate":
        return JSONResponse(
            status_code=200,
            content={
                "duplicate_of": outcome.duplicate_of,
                "state": outcome.state,
                "submission_id": outcome.submission_id,
            },
        )
    submission_id = outcome.submission_id

    # Phase 3 — recovery record: everything needed to re-place lives in PG
    # before the original is cancelled.
    orders_store.record_event(
        submission_id,
        "REPLACE_STARTED",
        {"cancel_targets": cancel_targets, "replace_order": replace_order},
    )

    # Phase 4 — cancel the original(s). Any failure: original is intact
    # (or partially cancelled with upstream detail preserved); terminal-fail
    # the replacement attempt so it can't be resumed by mistake.
    for target in cancel_targets:
        try:
            await _orders_cancel_from_body(
                {"orderId": target.get("orderId", 0), "permId": target.get("permId", 0)}
            )
        except HTTPException as exc:
            orders_store.record_event(
                submission_id,
                "REPLACE_CANCEL_FAILED",
                {"target": target, "detail": jsonable_encoder(exc.detail)},
            )
            orders_store.mark_terminal(
                submission_id=submission_id,
                state="FAILED",
                reason_code="REPLACE_CANCEL_FAILED",
                filled_qty=0,
                avg_fill_price=None,
            )
            detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "detail": detail.get("message") or "Cancel failed — original order NOT replaced",
                    "reason_code": detail.get("reason_code") or ReasonCode.IB_CONNECTION.value,
                    "phase": "cancel",
                    "upstream": jsonable_encoder(detail),
                },
            )

    # Phase 4.5 — FILL-RACE GUARD (mandatory; see adversarial scenario 3).
    # A working order can fill (partial or full) in the preflight→cancel or
    # cancel-request→ack window. IB/subprocess reports "order gone from open
    # orders" as a SUCCESSFUL cancel (cancel/modify semantics rule 4) and
    # `_mark_submission_cancelled` even stamps filled_qty=0 — so a fill is
    # SILENTLY masked as a clean cancel. Placing the replacement on top would
    # DOUBLE the position — the exact failure this plan's Goal forbids.
    #
    # Before placing, re-read the actual fill state of each cancelled original
    # (query `xenon.order_fills` by (perm_id, scope); do NOT trust the cancel
    # result's filled_qty=0). If ANY original has filled_qty > 0:
    #   - record REPLACE_FILL_RACE event {targets, filled_qty per target},
    #   - mark the replacement attempt terminal FAILED (reason_code
    #     REPLACE_FILL_RACE),
    #   - return 409 with top-level reason_code REPLACE_FILL_RACE + a recovery
    #     payload naming the filled leg(s), and DO NOT place.
    # Executor: there is no ready `filled_qty_for_perm_id` facade in
    # orders_store (verified — only per-fill `record_fill` / snapshot readers).
    # Add one to the orders_store facade (preserve its signature convention)
    # that SUMs `order_fills.filled_qty` for (perm_id, **scope**); unit-test it.
    # Add a regression test to Step 1.1: cancel echoes success but a seeded
    # order_fills row for the target's perm_id exists → replace 409s
    # REPLACE_FILL_RACE, place subprocess never invoked, no double-book.
    # NOTE (cross-ref, tribunal): the underlying clobber — `_mark_submission_cancelled`
    # (server.py ~2464) writes CANCELLED + filled_qty=0 with NO expected_states
    # on the ORIGINAL row, inside the shared cancel path Phase 4 invokes — is a
    # PRE-EXISTING defect on that shared path. Do NOT "fix it in passing" here;
    # this guard only covers the replace flow's exposure. That write gains
    # expected_states in P2.2's chokepoint migration.

    # Phase 5 — place the replacement.
    try:
        result = await _execute_reserved_place(submission_id, replace_order, _override_detail)
    except HTTPException as exc:
        orders_store.record_event(
            submission_id,
            "REPLACE_PLACE_FAILED",
            {"detail": jsonable_encoder(exc.detail), "replace_order": replace_order},
        )
        return JSONResponse(
            status_code=502,
            content={
                "detail": (
                    "Original order cancelled but replacement placement failed. "
                    "Use the recovery payload to re-place."
                ),
                "reason_code": ReasonCode.REPLACE_PLACE_FAILED.value,
                "phase": "place",
                "upstream": jsonable_encoder(exc.detail),
                "recovery": {
                    "replaceOrder": {k: v for k, v in replace_order.items() if k != "client_attempt_id"},
                    "cancelled": cancel_targets,
                    "submission_id": submission_id,
                },
            },
        )
    orders_store.record_event(
        submission_id, "REPLACE_COMPLETED", {"cancelled": cancel_targets}
    )
    return result
```

Notes for the executor:

- `jsonable_encoder`: **it is NOT currently imported in `server.py`** (verified — only `from fastapi import …` at line 25, `fastapi.responses`/`fastapi.middleware` below it). Add `from fastapi.encoders import jsonable_encoder` to the fastapi import block.
- `_execute_reserved_place` already terminal-marks (`FAILED`/`REJECTED`) before raising — the replace handler only adds the event + recovery response; do **not** double-`mark_terminal` in Phase 5.
- Test-mode: `_execute_reserved_place` short-circuits to fake ids and `_orders_cancel_from_body` returns the test-mode echo, so the success-path test needs no monkeypatching.
- `expected_states` invariant: `mark_terminal` here only fires on the fresh PENDING replacement row, never on the original's row — no clobber risk.
- **Recovery-surfacing scope (adversarial scenario 1 — read before claiming "durable recovery").** The `REPLACE_STARTED` / `REPLACE_PLACE_FAILED` rows are durable in `order_events`, but **nothing reads them back on boot**: `_run_rehydrate_on_boot` reconciles single-leg + combo-**wizard** rows only (verified `server.py:163`), not BAG replace rows, and the recovery banner (Step 1.7) is driven **solely by the live HTTP response** reaching the browser. So if the process crashes/restarts in the cancel→place window, the original is cancelled at IB, the replacement row is left `PENDING` (later auto-`FAILED` by `PENDING_TIMEOUT`), and **no UI ever surfaces the recovery affordance** — the user just silently loses the order. This is NOT a naked-position (cancelling a _working order_ removes an unfilled order, it does not uncover a position), so the plan's headline Goal holds — but the word "durable **recovery**" is overstated: the record is durable for _post-hoc audit / manual `psql` query_, not for _automated resumption_. **ADJUDICATED (review pass 3): do BOTH, minimally.** (a) The wording throughout this plan is "durable audit record + live-response recovery affordance" — never "durable recovery". (b) **REQUIRED** lightweight read-path detection — no new boot machinery: Step 1.7 point 4 specs a query on the existing `GET /orders` read surface (`src/xenon/api/routes/orders.py::orders_payload_for_scope`) that also returns submissions with an orphaned `REPLACE_STARTED` event, and the UI renders the same recovery banner from that payload. A restart therefore re-surfaces the recovery affordance on the next orders poll.
- **Idempotency vs. double-submit (adversarial scenarios 2 & 5 — ADJUDICATED, cid design APPLIED).** If the Next route minted a fresh `client_attempt_id` per HTTP request, two concurrent `/orders/modify` calls for the same order — double-click, or a client retry after the 45s timeout while the first is still in flight — would carry **different** cids → two `reserve_attempt` winners → **double-cancel + double-place** (idempotency only dedupes _identical_ cids). Therefore the design is: the **browser mints ONE stable `client_attempt_id` when the user confirms the replace** (held in ModifyOrderModal state for the lifetime of that intent, carried on `ReplaceComboOrder`), and the Next route **passes it through — it never mints** (Step 1.5). A replay of the same intent collapses at F4 into the `duplicate`/`terminal` short-circuit. The recovery banner's retry mints a fresh cid **only after** the reconciliation gate in Step 1.7 point 2 confirms nothing was placed. Also disable the modal submit + banner buttons while a request is in flight. Route test (in Step 1.1, `test_replace_same_cid_concurrent_is_idempotent`): two posts with the SAME cid → exactly one placement / one `REPLACE_STARTED`, second response is the duplicate body.

Run Step 1.1 tests: `uv run pytest scripts/tests/test_orders_replace_route.py -x` → green.

## Step 1.5 — Next.js modify route becomes a thin proxy

Rewrite the `if (replaceOrder) { ... }` block of `web/app/api/orders/modify/route.ts` (everything from `if (replaceOrder) {` through its closing `}` before the `if (newPrice == null && ...)` check) to:

```ts
if (replaceOrder) {
  // OP-6: replace is server-side (FastAPI POST /orders/replace) — the
  // two-phase cancel-then-place, recovery record, and failure taxonomy
  // live there. This route is a pure proxy. The idempotency key
  // (client_attempt_id) is minted in the BROWSER when the user confirms the
  // replace — stable across double-click/timeout-retry of the same intent —
  // and arrives on replaceOrder. This route NEVER mints one: a route-minted
  // cid would give each retry a fresh key and defeat F4 dedup (scenario 2/5).
  if (!replaceOrder.client_attempt_id) {
    return NextResponse.json(
      { error: "replaceOrder.client_attempt_id is required", requestId },
      { status: 400 },
    );
  }
  const result = await xenonFetch<Record<string, unknown>>("/orders/replace", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      cancelOrders: normalizeCancelTargets(body.cancelOrders, orderId, permId),
      replaceOrder,
    }),
    timeout: 45_000,
  });

  try {
    await xenonFetch("/orders/refresh", { method: "POST", timeout: 10_000 });
  } catch {
    // Non-fatal
  }
  const orders = await fetchOrders();

  return NextResponse.json({
    status: "ok",
    message: result.message,
    orderId: result.orderId,
    permId: result.permId,
    orders,
  });
}
```

- **No `randomUUID` import in this route** — the cid comes from the browser (see below). Add `client_attempt_id: string` to `ReplaceComboOrder` in `web/lib/orderModify.ts`; ModifyOrderModal mints it (`crypto.randomUUID()`) ONCE when the user confirms the replace and holds it in modal state for the lifetime of that intent, so a double-click or timeout-retry resubmits the SAME cid and collapses at F4. A new intent (user edits legs and confirms again) mints a new cid.
- Delete the now-unused client-side validation block (`limitPriceValid`, the `Invalid combo replacement payload` 400, the empty-cancel-targets 400) — FastAPI owns validation now; `passThroughXenonError` in the outer catch forwards its 400/502/503 JSON (including top-level `reason_code` and `recovery`) verbatim.
- Keep `normalizeCancelTargets` (still used to fold the fallback orderId/permId in).
- If FastAPI replace fails, `xenonFetch` throws and the outer `catch` → `passThroughXenonError(error, requestId)` — verify that helper preserves status + body (it does; anchor: `// Preserve upstream JSON body verbatim`).

## Step 1.6 — Web unit tests for the thin proxy + recovery UI

1. Update `web/tests/order-e2e.test.ts` combo-replacement describe: the harness test asserting 200 must now expect the proxy to hit `/orders/replace`; in FastAPI test mode this returns 200 with fake ids — keep `expect(res.status).toBe(200)`. The two 400-validation tests (`missing limitPrice`, `wrong type`) now get their 400 from FastAPI passthrough — keep `expect(res.status).toBe(400)` but mark them `fastApiIt` (they now require the harness). Update `web/tests/order-reliability.test.ts` and `web/tests/orders-modify-pg.test.ts` replaceOrder cases the same way: mock/assert a single POST to `/orders/replace`, and assert **no** direct `/orders/cancel` + `/orders/place` calls from the route (that is the regression this PR locks in).
2. New `web/tests/replace-recovery-toast.test.tsx`: render a component using `OrderActionsContext.requestModify` with `fetch` mocked to return status 502 body `{"reason_code":"REPLACE_PLACE_FAILED","detail":"...","recovery":{"replaceOrder":{...}}}`; assert `drainNotifications()` yields an error notification whose `duration` is `0` (persistent) and whose message matches the new toast copy (Step 1.7).

## Step 1.7 — Recovery UI (loud state + one-click re-place)

1. `web/lib/orderReasonCodes.ts` — anchor `SUBPROCESS_ERROR: {` — add after that entry:

```ts
  // OP-6 — combo replace: cancel succeeded, place failed. Recovery data is in
  // the response body; the OrderActions layer surfaces a persistent re-place path.
  REPLACE_PLACE_FAILED: {
    severity: "error",
    copy: "Combo replace failed AFTER cancel — original order is gone. Re-place from the recovery banner.",
  },
```

2. `web/lib/OrderActionsContext.tsx`:
   - Extend `Notification` usage: in `requestModify`'s `!res.ok` branch, when `reasonCode === "REPLACE_PLACE_FAILED"`, call `pushNotification({ type: "error", message, duration: 0 })` (persistent) instead of the default.
   - Add state `failedReplace: { order: OpenOrder; replaceOrder: ReplaceComboOrder; submissionId: string } | null` + setter, populated from `request.replaceOrder` + `body.recovery.submission_id` when that reason code is seen; expose `failedReplace`, `retryReplace: () => Promise<ActionResult>`, and `clearFailedReplace()` on the context value. Disable the retry button while a retry is in flight.
   - **`retryReplace` must NOT go through `/orders/replace` again** (tribunal MAJOR). The original is already cancelled — the replace route's Phase 4 cancel would hit IB 10147/10148, which the cancel path maps to **404** (`_IB_REJECT_NOT_FOUND_CODES` at `server.py:2336-2343`; classification in `ib_order_manage.py:27-29`), so the retry would die before ever placing. Instead the banner action is a **guarded direct re-place** of the STORED replacement (`recovery.replaceOrder` — same payload persisted in the `REPLACE_STARTED`/`REPLACE_PLACE_FAILED` events):
     1. **Reconciliation gate first:** `GET /api/orders` and check (a) the failed replacement's `submission_id` state and (b) open orders for a matching working replacement (symbol + BAG + legs). If anything already placed/working — e.g. the user manually re-placed via the chain, or a prior retry landed — show that order in the banner ("Replacement already working — nothing to do") and do NOT place. This gate is what makes pressing the button twice, or after a manual re-place, safe (scenario 5).
     2. Only if the gate finds nothing: POST `/api/orders/place` with the stored `replaceOrder` payload and a **fresh** `client_attempt_id`; on success, call the API to record a `REPLACE_RETRY` order_event on the ORIGINAL failed `submission_id` linking `{retry_client_attempt_id, new_submission_id}` (small FastAPI addition: accept an optional `replace_retry_of: <submission_id>` field on the place body; when present, `_orders_place_from_body` records the `REPLACE_RETRY` event on that submission after reservation). Clear the banner on success.
3. Recovery banner: in `web/components/WorkspaceShell.tsx` (it already consumes `useOrderActions`), render a fixed banner when `failedReplace != null`: text `Combo replace failed — {symbol} original cancelled, replacement NOT placed`, buttons `Re-place order` (→ `retryReplace()`, disabled in flight, clear on success) and `Dismiss` (→ `clearFailedReplace()`). Use existing brand tokens/classes from the adjacent toast markup — no raw hex, 4px radius max.
4. **Read-path orphan detection (adversarial scenario 1, adjudicated REQUIRED).** A crash between cancel and place leaves a `REPLACE_STARTED` row that no live HTTP response will ever surface. Detection rides the existing orders read surface — no new boot machinery:
   - In `src/xenon/api/routes/orders.py::orders_payload_for_scope`, add a third payload key `orphaned_replaces`: submissions in this scope whose events contain `REPLACE_STARTED` but no later `REPLACE_COMPLETED` or `REPLACE_RETRY`, restricted to state `PENDING` (crash before place resolved) or `FAILED` (place failed; includes live-surfaced `REPLACE_PLACE_FAILED` rows deliberately — a page refresh loses the in-memory banner, and the reconciliation gate makes re-surfacing them safe). Query shape (one round-trip, scope-filtered like the open-orders query):

     ```sql
     SELECT s.submission_id, s.ticker, e.detail
     FROM xenon.order_submissions s
     JOIN xenon.order_events e ON e.submission_id = s.submission_id AND e.kind = 'REPLACE_STARTED'
     WHERE s.broker = :broker AND s.account_env = :account_env AND s.broker_account = :broker_account
       AND s.state IN ('PENDING', 'FAILED')
       AND NOT EXISTS (
         SELECT 1 FROM xenon.order_events e2
         WHERE e2.submission_id = s.submission_id
           AND e2.kind IN ('REPLACE_COMPLETED', 'REPLACE_RETRY')
       )
     ```

     (`e.detail` carries the stored `replace_order` + `cancel_targets` — everything the banner needs. `REPLACE_RETRY` in the exclusion keeps already-recovered rows out.)
   - The orders polling path in the web app feeds `orphaned_replaces` rows into the SAME `failedReplace` banner state (first orphan wins; `retryReplace` above already handles it via the reconciliation gate).
   - Route test (add to Step 1.1 file): seed a submission + `REPLACE_STARTED` event with no completion event, `GET /orders` → payload `orphaned_replaces` contains it; add a `REPLACE_COMPLETED` event → it disappears.

## Step 1.8 — Browser test (repo guardrail: submitted payload)

New `web/e2e/combo-replace-recovery.spec.ts` (Playwright, follows `web/e2e/open-order-combo.spec.ts` conventions):

1. `page.route("**/api/orders/modify", ...)` first fulfilling 502 `{"reason_code":"REPLACE_PLACE_FAILED","detail":"...","recovery":{...}}`, capturing the request body.
2. Open the workspace open-orders panel with a seeded BAG open order (reuse the existing spec's seeding approach), open ModifyOrderModal, change a leg strike (leg restructure), submit.
3. Assert captured request body: `replaceOrder.type === "combo"`, both legs present with `action` preserved (`BUY`/`SELL` per structure — never derived from debit/credit), and `limitPrice` sign preserved.
4. Assert on-screen literal text `Combo replace failed` (banner) is visible; screenshot → `output/playwright/combo-replace-recovery-2026-07-05.png`.
5. Re-route to fulfill 200, click `Re-place order`, assert a second `/api/orders/modify` request fired with the same `replaceOrder` legs and the banner disappears.

## Step 1.9 — PR 1 verification matrix

| Check                   | Command                                                                                                                                                                                                                              | Expected                                                                                 |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------- |
| New route tests         | `uv run pytest scripts/tests/test_orders_replace_route.py -xvs`                                                                                                                                                                      | 9 passed (7 spec'd above + fill-race guard test from Phase 4.5 + orphan-detection GET /orders test from Step 1.7 point 4)                                                                                 |
| No-drift refactor       | `uv run pytest scripts/tests/test_place_quote_gate.py scripts/tests/test_preflight_route.py scripts/tests/test_orders_place_no_regime_gate.py scripts/tests/test_read_only_mode.py -x` — plus `uv run pytest src/xenon/api/tests -x` | all pass                                                                                 |
| Scoped suite            | `uv run python scripts/infra/dev/run_pytest_affected.py`                                                                                                                                                                             | exit 0                                                                                   |
| CI order-path guards    | `uv run python scripts/checks/no_json_fallback_on_order_path.py && uv run python scripts/checks/no_json_write_on_order_path.py && uv run python scripts/checks/order_path_caller_allowlist.py`                                       | exit 0 ×3                                                                                |
| Web unit                | `cd web && npm test -- order-e2e order-reliability orders-modify-pg replace-recovery-toast modify-order-combo-routing`                                                                                                               | all pass                                                                                 |
| Typecheck/lint          | `cd web && npx tsc --noEmit && npm run lint`                                                                                                                                                                                         | exit 0                                                                                   |
| Browser                 | `cd web && npx playwright test e2e/combo-replace-recovery.spec.ts`                                                                                                                                                                   | 1 passed; screenshot exists at `output/playwright/combo-replace-recovery-2026-07-05.png` |
| Live probe (PAPER ONLY) | `scripts/infra/dev.sh paper`; place a 2-leg SPY call vertical via UI; ModifyOrderModal → change a leg strike → submit; then `curl -s localhost:8421/orders \| jq '.open_orders[].orderId'`                                           | replacement order visible; original gone                                                 |
| Recovery record (PAPER) | `psql "$DATABASE_URL_PAPER" -c "SELECT kind FROM xenon.order_events e JOIN xenon.order_submissions s ON s.submission_id=e.submission_id WHERE s.security_type='BAG' ORDER BY e.at DESC LIMIT 5"`                                     | rows include `REPLACE_STARTED`, `REPLACE_COMPLETED`                                      |
| Read-only negative      | with `XENON_READ_ONLY=1`: `curl -s -X POST localhost:8421/orders/replace -H 'Content-Type: application/json' -d '{}' -o /dev/null -w '%{http_code}'`                                                                                 | `403`                                                                                    |

## Step 1.10 — Incident-history row (append to `docs/reference/order-path-incident-history.md`, next row number 24)

```
| 24  | 2026-07-05 (this PR)                           | Combo leg-restructure "replace" ran cancel-then-place inside the Next.js modify route; a place failure after cancel left the position naked with only a "CRITICAL" error string — and at HEAD the replace place call was missing `client_attempt_id`, so it deterministically 400'd AFTER the cancel succeeded | Broker orchestration in the web proxy layer: no durable record between the two phases, no pre-cancel validation of the replacement, no idempotency key on the replacement payload | FastAPI `POST /orders/replace`: validate replacement first (preflight + gates), reserve F4 attempt, `REPLACE_STARTED` event with full recovery payload BEFORE cancel, `REPLACE_PLACE_FAILED` event + top-level `reason_code` + `recovery` body on partial failure; Next route is a pure pass-through proxy (browser mints one stable `client_attempt_id` per replace intent → F4 dedup absorbs double-click/retry); persistent UI banner with reconciliation-gated re-place + `GET /orders` orphan detection for crash windows | `test_orders_replace_route.py` (failure injection → recovery record), `replace-recovery-toast.test.tsx`, `e2e/combo-replace-recovery.spec.ts` (submitted payload + banner) |
```

## PR 1 tripwires

- STOP if `uv run pytest scripts/tests/test_orders_replace_route.py` passes **before** Step 1.4 — the route already exists / anchors wrong.
- STOP if the Step 1.3 refactor requires touching any file other than `src/xenon/api/server.py`.
- STOP if `order_path_caller_allowlist.py` fails — the replace endpoint must not import `xenon.execution.ib_place_order` directly; it goes through `_execute_reserved_place`'s subprocess call only.
- STOP if any live verification would run against port 4001 / `dev.sh live` — PAPER ONLY (4002).
- STOP if `reserve_attempt`'s `RequestRow` signature differs from `ticker/security_type/action/quantity/expiry/strike/right/multiplier/con_id/limit_price` — re-read `orders_store.py:86` and adapt.

## PR 1 rollback

Branch discard (`git branch -D fix/combo-replace-server-side`); no migration. If merged and broken in prod: revert commit — the Next route regains the old cancel-then-place block; `order_events` rows written meanwhile are append-only and harmless.

---

# PR 2 — `feat/combo-net-price-gate` (OP-17 + CX-3)

Branch from master **after PR 1 merges**.

## Step 2.1 — Failing unit tests for `quote_guard.check_combo_payload`

Append to `scripts/tests/test_quote_guard.py` (real frozen quotes: SPY 2026-06-18 620/630 call vertical, leg quotes frozen from IB paper 2026-07-03: 620C bid 14.10 / ask 14.35; 630C bid 8.95 / ask 9.15 — net debit market ≈ bid 4.95 / ask 5.40):

```python
class TestComboBand:
    def _legs(self):
        from decimal import Decimal as D
        from xenon.execution import quote_guard
        return [
            quote_guard.ComboLegQuote(
                action="BUY", ratio=1, bid=D("14.10"), ask=D("14.35"), bid_size=12, ask_size=9
            ),
            quote_guard.ComboLegQuote(
                action="SELL", ratio=1, bid=D("8.95"), ask=D("9.15"), bid_size=22, ask_size=18
            ),
        ]

    def test_zero_size_leg_rejected(self):
        # Parity with single-leg check_payload (quote_guard.py ~69): a zero-size
        # or crossed leg quote is stale/unusable — QUOTE_UNAVAILABLE, not a band pass.
        from decimal import Decimal as D
        from xenon.execution import quote_guard
        legs = self._legs()
        legs[0] = quote_guard.ComboLegQuote(
            action="BUY", ratio=1, bid=D("14.10"), ask=D("14.35"), bid_size=0, ask_size=9
        )
        v = quote_guard.check_combo_payload(
            legs=legs, envelope_action="BUY", limit_price=D("5.40")
        )
        assert not v.accept
        assert v.reason_code.value == "QUOTE_UNAVAILABLE"

    def test_buy_within_band_accepts(self):
        from decimal import Decimal as D
        from xenon.execution import quote_guard
        v = quote_guard.check_combo_payload(
            legs=self._legs(), envelope_action="BUY", limit_price=D("5.40")
        )
        assert v.accept

    def test_buy_fat_finger_rejected(self):
        # netAsk = 14.35 - 8.95 = 5.40; mid = (4.95+5.40)/2 = 5.175
        # slack = max(0.05*5.175, 2*0.01*2) = 0.25875 → cap ≈ 5.65875
        from decimal import Decimal as D
        from xenon.execution import quote_guard
        v = quote_guard.check_combo_payload(
            legs=self._legs(), envelope_action="BUY", limit_price=D("54.00")
        )
        assert not v.accept
        assert v.reason_code.value == "LIMIT_OUT_OF_BAND"

    def test_sell_below_floor_rejected(self):
        # netBid = 14.10 - 9.15 = 4.95; floor ≈ 4.95 - 0.25875 = 4.69125
        from decimal import Decimal as D
        from xenon.execution import quote_guard
        v = quote_guard.check_combo_payload(
            legs=self._legs(), envelope_action="SELL", limit_price=D("0.49")
        )
        assert not v.accept
        assert v.reason_code.value == "LIMIT_OUT_OF_BAND"

    def test_credit_combo_signed_band(self):
        # BUY-envelope credit spread (dominant SELL leg): SPY 620/630 put credit,
        # frozen 2026-07-03: long 620P bid 4.20/ask 4.35, short 630P bid 7.60/ask 7.80.
        # netAsk = 4.35 - 7.60 = -3.25 (receive 3.25, worst); netBid = 4.20 - 7.80 = -3.60.
        # Fat finger: limit +3.25 (sign flip) must be rejected on BUY (cap ≈ -3.25+0.21 = -3.04).
        from decimal import Decimal as D
        from xenon.execution import quote_guard
        legs = [
            quote_guard.ComboLegQuote(
                action="BUY", ratio=1, bid=D("4.20"), ask=D("4.35"), bid_size=35, ask_size=28
            ),
            quote_guard.ComboLegQuote(
                action="SELL", ratio=1, bid=D("7.60"), ask=D("7.80"), bid_size=41, ask_size=33
            ),
        ]
        assert quote_guard.check_combo_payload(
            legs=legs, envelope_action="BUY", limit_price=D("-3.25")
        ).accept
        assert not quote_guard.check_combo_payload(
            legs=legs, envelope_action="BUY", limit_price=D("3.25")
        ).accept
```

`uv run pytest scripts/tests/test_quote_guard.py -k ComboBand -x` → fails (ImportError / AttributeError).

## Step 2.2 — Implement in `src/xenon/execution/quote_guard.py`

Append after `check_payload`:

```python
class ComboLegQuote(BaseModel):
    """Structural leg (ComboLeg.action semantics — LONG→BUY/SHORT→SELL,
    independent of envelope direction) with its live single-leg quote.

    Carries sizes so the combo gate keeps the single-leg check_payload
    invariant (tribunal minor): crossed OR zero-size leg quotes are stale
    and must reject QUOTE_UNAVAILABLE, never pass the band."""

    action: Literal["BUY", "SELL"]
    ratio: int
    bid: Decimal
    ask: Decimal
    bid_size: int
    ask_size: int


COMBO_BAND_PCT = Decimal("0.05")
COMBO_TICK = Decimal("0.01")  # matches _lookup_min_tick_via_pool's by-design stub


def combo_net_quote(legs: list[ComboLegQuote]) -> tuple[Decimal, Decimal]:
    """Signed (netBid, netAsk) of the package as defined by its structural legs.

    web/CLAUDE.md invariant: to BUY the combo you pay ask on BUY legs and
    receive bid on SELL legs (netAsk); to SELL it you receive bid on BUY legs
    and pay ask on SELL legs (netBid). Signed: credits are negative. Always
    netBid <= netAsk in signed terms.
    """
    net_bid = Decimal("0")
    net_ask = Decimal("0")
    for leg in legs:
        r = Decimal(leg.ratio)
        if leg.action == "BUY":
            net_ask += leg.ask * r
            net_bid += leg.bid * r
        else:
            net_ask -= leg.bid * r
            net_bid -= leg.ask * r
    return net_bid, net_ask


def check_combo_payload(
    *,
    legs: list[ComboLegQuote],
    envelope_action: Literal["BUY", "SELL"],
    limit_price: Decimal,
) -> QuoteVerdict:
    """Limit-band sanity for combo net prices (fable OP-17).

    Band: slack = max(5% of |net mid|, 2 ticks x n_legs). Additive leg quote
    noise justifies per-leg tick slack; abs(mid) keeps the percent term sane
    for credit (negative) packages; max() (not the single-leg min()) avoids a
    zero-width band on near-zero-cost combos. BUY envelope: reject
    limit > netAsk + slack. SELL envelope: reject limit < netBid - slack.
    Signed comparisons throughout — a sign-flipped credit price is exactly
    the fat-finger class this gate exists to catch.
    """
    for leg in legs:
        if leg.bid > leg.ask or leg.bid_size <= 0 or leg.ask_size <= 0:
            # Same invariant as single-leg check_payload (crossed/zero-size).
            return QuoteVerdict(
                accept=False,
                reason_code=ReasonCode.QUOTE_UNAVAILABLE,
                reason_detail="crossed or zero-size leg quote",
            )
    net_bid, net_ask = combo_net_quote(legs)
    net_mid = (net_bid + net_ask) / 2
    slack = max(COMBO_BAND_PCT * abs(net_mid), Decimal(2 * len(legs)) * COMBO_TICK)
    if envelope_action == "BUY":
        cap = net_ask + slack
        if limit_price > cap:
            return QuoteVerdict(
                accept=False,
                reason_code=ReasonCode.LIMIT_OUT_OF_BAND,
                reason_detail=f"combo BUY limit {limit_price} > cap {cap} (net ask {net_ask})",
            )
    else:
        floor = net_bid - slack
        if limit_price < floor:
            return QuoteVerdict(
                accept=False,
                reason_code=ReasonCode.LIMIT_OUT_OF_BAND,
                reason_detail=f"combo SELL limit {limit_price} < floor {floor} (net bid {net_bid})",
            )
    return QuoteVerdict(accept=True)
```

Step 2.1 tests → green.

## Step 2.3 — Failing route tests, then wire into the place path

Append to `scripts/tests/test_place_quote_gate.py`:

```python
def _combo_body(cid: str, limit: float, action: str = "BUY") -> dict:
    return {
        "type": "combo",
        "symbol": "SPY",
        "action": action,
        "quantity": 1,
        "limitPrice": limit,
        "client_attempt_id": cid,
        "legs": [
            {"expiry": "20260618", "strike": 620, "right": "C", "action": "BUY", "ratio": 1},
            {"expiry": "20260618", "strike": 630, "right": "C", "action": "SELL", "ratio": 1},
        ],
    }


def _patch_combo_leg_quotes(monkeypatch):
    from xenon.api import server

    async def _leg_quotes(body):
        # Frozen SPY 620/630C vertical quotes (paper, 2026-07-03).
        return [
            {"action": "BUY", "ratio": 1, "bid": Decimal("14.10"), "ask": Decimal("14.35"), "bid_size": 12, "ask_size": 9},
            {"action": "SELL", "ratio": 1, "bid": Decimal("8.95"), "ask": Decimal("9.15"), "bid_size": 22, "ask_size": 18},
        ]

    monkeypatch.setattr(server, "_fetch_combo_leg_quotes", _leg_quotes)


def _rth(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from xenon.api import server
    midday = datetime(2026, 7, 2, 13, 0, tzinfo=ZoneInfo("America/New_York"))
    monkeypatch.setattr(server, "_now", lambda: midday, raising=False)


def test_combo_within_band_places(client, monkeypatch):
    _rth(monkeypatch); _patch_combo_leg_quotes(monkeypatch)
    resp = client.post("/orders/place", json=_combo_body("combo-band-ok", 5.40))
    assert resp.status_code == 200, resp.text


def test_combo_fat_finger_rejected_LIMIT_OUT_OF_BAND(client, monkeypatch):
    _rth(monkeypatch); _patch_combo_leg_quotes(monkeypatch)
    resp = client.post("/orders/place", json=_combo_body("combo-band-fat", 54.00))
    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "LIMIT_OUT_OF_BAND"


def test_combo_band_override_acknowledged(client, monkeypatch):
    _rth(monkeypatch); _patch_combo_leg_quotes(monkeypatch)
    body = _combo_body("combo-band-ack", 54.00)
    body["acknowledge_limit_override"] = True
    resp = client.post("/orders/place", json=body)
    assert resp.status_code == 200, resp.text


def test_combo_leg_quote_unavailable_503(client, monkeypatch):
    from fastapi import HTTPException
    from xenon.api import server
    _rth(monkeypatch)

    async def _no_quote(body):
        raise HTTPException(status_code=503, detail="No quote available for SPY leg")

    monkeypatch.setattr(server, "_fetch_combo_leg_quotes", _no_quote)
    resp = client.post("/orders/place", json=_combo_body("combo-band-noq", 5.40))
    assert resp.status_code == 503
    assert resp.json()["reason_code"] == "QUOTE_UNAVAILABLE"
```

Run → fail (`_fetch_combo_leg_quotes` missing / combo branch skips gate).

Then in `src/xenon/api/server.py`:

1. Add the leg-quote fetcher next to `_fetch_order_quote_snapshot_with_client`:

```python
def _fetch_combo_leg_quotes_with_client(client: Any, body: dict) -> list[dict]:
    """Qualify each combo leg Option and snapshot its quote (one pool acquire)."""
    _ensure_thread_event_loop()
    symbol = str(body.get("symbol", "")).upper()
    out: list[dict] = []
    for leg in body.get("legs") or []:
        contract = Option(
            symbol=symbol,
            lastTradeDateOrContractMonth=str(leg.get("expiry") or ""),
            strike=float(leg.get("strike") or 0),
            right=str(leg.get("right") or "").upper(),
            exchange="SMART",
            currency="USD",
        )
        qualified = client.qualify_contract(contract)
        con_id = int(getattr(qualified, "conId", 0) or 0)
        if con_id <= 0:
            raise HTTPException(status_code=503, detail=f"Could not qualify combo leg for {symbol}")
        tk = client.get_quote(qualified, snapshot=True)
        snap = _ticker_to_quote_snapshot(symbol, con_id, tk)
        out.append(
            {
                "action": str(leg.get("action", "")).upper(),
                "ratio": int(leg.get("ratio") or 1),
                "bid": snap["bid"],
                "ask": snap["ask"],
                # _ticker_to_quote_snapshot already returns bid_size/ask_size
                # (verified server.py:1872-1877) — forward them so
                # check_combo_payload can enforce the zero-size invariant.
                "bid_size": snap["bid_size"],
                "ask_size": snap["ask_size"],
            }
        )
    return out


async def _fetch_combo_leg_quotes(body: dict) -> list[dict]:
    pool = ib_pool
    if pool is None:
        raise HTTPException(status_code=503, detail="IB data role unavailable")
    try:
        async with pool.acquire("data") as client:
            return await pool.run_sync(
                "data", _fetch_combo_leg_quotes_with_client, client, dict(body)
            )
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
```

2. Add `_validate_combo_quote` next to `_validate_non_combo_quote`:

```python
async def _validate_combo_quote(body: dict) -> tuple[quote_guard.QuoteVerdict, int]:
    """OP-17: market-hours + net-price limit-band gate for combo orders."""
    market = quote_guard.check_market_hours(security_type="OPT", now=_now())
    if not market.accept:
        return market, 400
    try:
        leg_quotes = await _fetch_combo_leg_quotes(body)
    except HTTPException as exc:
        return (
            quote_guard.QuoteVerdict(
                accept=False,
                reason_code=ReasonCode.QUOTE_UNAVAILABLE,
                reason_detail=str(exc.detail),
            ),
            exc.status_code if exc.status_code >= 500 else 400,
        )
    legs = [quote_guard.ComboLegQuote(**lq) for lq in leg_quotes]
    verdict = quote_guard.check_combo_payload(
        legs=legs,
        envelope_action=str(body.get("action", "")).upper(),
        limit_price=Decimal(str(body.get("limitPrice", "0"))),
    )
    return verdict, 400
```

3. In `_validate_place_gates` (post-PR-1 home of the F3 block), replace the combo `else: _override_detail = None` branch with the same accept/override/reject structure as the non-combo branch, calling `_validate_combo_quote(body)` and honoring `acknowledge_limit_override` for `LIMIT_OUT_OF_BAND` only. Reuse the non-combo branch verbatim except the validator call.

Route tests → green. Because `/orders/replace` calls `_validate_place_gates` pre-cancel (PR 1 design), a fat-finger replacement net price now 400s **before** the original is cancelled — add one test to `test_orders_replace_route.py` asserting exactly that (monkeypatch `_fetch_combo_leg_quotes`, post replace body with `limitPrice: 54.00`, assert 400 + cancel spy not called).

## Step 2.4 — CX-3: one combo net-quote implementation (web)

1. Add the signed core to `web/lib/optionsChainUtils.ts` above `computeNetOptionQuote`:

```ts
export type SignedQuoteLeg = {
  side: "BUY" | "SELL"; // structural/effective execution side for pricing
  bid: number;
  ask: number;
  last?: number | null;
  qty: number;
};

export type SignedNetQuote = {
  netBid: number; // signed proceeds selling the package at market
  netAsk: number; // signed cost buying the package at market
  netMid: number;
  netLast: number;
};

/**
 * Single implementation of the combo net-quote math (fable CX-3 — this
 * existed x3). web/CLAUDE.md: to BUY the combo pay ASK on BUY legs, receive
 * BID on SELL legs; to SELL receive BID on BUY legs, pay ASK on SELL legs.
 * Signed output: credits negative. Display layers normalize as needed.
 */
export function computeSignedComboQuote(
  legs: SignedQuoteLeg[],
): SignedNetQuote {
  let netBid = 0;
  let netAsk = 0;
  let netLast = 0;
  for (const leg of legs) {
    const sign = leg.side === "BUY" ? 1 : -1;
    if (leg.side === "BUY") {
      netAsk += leg.ask * leg.qty;
      netBid += leg.bid * leg.qty;
    } else {
      netAsk -= leg.bid * leg.qty;
      netBid -= leg.ask * leg.qty;
    }
    netLast += sign * (leg.last ?? (leg.bid + leg.ask) / 2) * leg.qty;
  }
  return { netBid, netAsk, netMid: (netBid + netAsk) / 2, netLast };
}
```

2. Rewire the three duplicates onto it (keep each caller's **output contract** identical — this is a pure internal consolidation, zero visual change):
   - `computeNetOptionQuote` (same file): keep its signature, key-resolution loop, `priceManuallySet` handling, and null-fallout; replace the accumulation + abs-normalization tail with: build `SignedQuoteLeg[]` (side = `leg.action`, qty = `leg.quantity`) → `const q = computeSignedComboQuote(signedLegs);` → `bid = Math.min(Math.abs(q.netBid), Math.abs(q.netAsk))`, `ask = Math.max(...)`, `mid = (bid+ask)/2` (unchanged display normalization).
   - `OrderTab.tsx` `netPrices` useMemo: keep the key/availability guards. Mapping (verified against the current accumulation): the existing code does `netBid += lp.bid; netAsk += lp.ask` when `effectivelySelling` is true, which is exactly what the core produces for `side: "BUY"`. So map each position leg to `SignedQuoteLeg` with `side: effectivelySelling ? "BUY" : "SELL"`, `qty: 1` (OrderTab's sign convention is inverted relative to structural action — do not "fix" it, mirror it). Then keep the existing tail: `bid = Math.min(q.netBid, q.netAsk)`, `ask = Math.max(q.netBid, q.netAsk)`, `mid = (bid + ask) / 2`. Add a unit test pinning old-vs-new equality on the frozen SPY 620/630 quotes before switching (write the test against the OLD memo extraction first — red/green).
   - `ModifyOrderModal.tsx::resolveOrderPriceData` BAG branch: both loops (comboLegs primary, portfolio-legs fallback) map to `SignedQuoteLeg` (`side` = `cl.action` / `leg.direction === "LONG" ? "BUY" : "SELL"`, qty 1) → `computeSignedComboQuote` → keep the existing `Math.abs` lo/hi + rounding tail.
3. Tests: extend `web/tests/options-chain-utils.test.ts` with direct `computeSignedComboQuote` cases (debit vertical, credit vertical sign, ratio 2 leg) using the same frozen SPY quotes as Step 2.1; keep/extend `modify-order-quote.test.ts` and `order-tab-combo-sign.test.ts` — they must pass **unchanged outputs** (that is the no-regression proof).

## Step 2.5 — Browser test (repo guardrail: displayed net price + submitted payload)

New `web/e2e/combo-net-price-band.spec.ts` (or extend `e2e/iwm-ticker-detail-combo-sign.spec.ts` if its fixtures fit):

1. Seed a 2-leg vertical in the chain builder with mocked/live-paper leg prices; assert the displayed combo net bid/mid/ask literals match `computeSignedComboQuote` of those prices (displayed via the OrderTab ticket).
2. Intercept `POST /api/orders/place`, submit within band → assert payload `limitPrice` sign + legs' `action`/`ratio` unchanged.
3. Stub the place response as 400 `{"reason_code":"LIMIT_OUT_OF_BAND","detail":"combo BUY limit ..."}` → assert the toast copy for `LIMIT_OUT_OF_BAND` appears on screen.
4. Screenshot → `output/playwright/combo-net-price-band-2026-07-05.png`.

## Step 2.6 — PR 2 verification matrix

| Check                        | Command                                                                                                   | Expected                                                                                                                               |
| ---------------------------- | --------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| Band unit                    | `uv run pytest scripts/tests/test_quote_guard.py -k ComboBand -xvs`                                       | 5 passed                                                                                                                               |
| Route gate                   | `uv run pytest scripts/tests/test_place_quote_gate.py -xvs`                                               | all pass incl. 4 new combo tests                                                                                                       |
| Replace pre-cancel gate      | `uv run pytest scripts/tests/test_orders_replace_route.py -xvs`                                           | all pass incl. new band test (cancel spy not called)                                                                                   |
| Scoped suite                 | `uv run python scripts/infra/dev/run_pytest_affected.py`                                                  | exit 0                                                                                                                                 |
| CI guards                    | the three `scripts/checks/*.py`                                                                           | exit 0 ×3                                                                                                                              |
| Web unit                     | `cd web && npm test -- options-chain-utils modify-order-quote order-tab-combo-sign chain-combo`           | all pass, outputs unchanged                                                                                                            |
| Typecheck/lint               | `cd web && npx tsc --noEmit && npm run lint`                                                              | exit 0                                                                                                                                 |
| Browser                      | `cd web && npx playwright test e2e/combo-net-price-band.spec.ts`                                          | passed; screenshot saved                                                                                                               |
| Live probe (PAPER ONLY, RTH) | `dev.sh paper`; build SPY vertical, set net limit 10× mid, submit                                         | 400 toast "Limit price is outside the allowed band…" (existing `LIMIT_OUT_OF_BAND` copy); order NOT in `curl -s localhost:8421/orders` |
| Live positive (PAPER, RTH)   | same combo at displayed mid                                                                               | 200; order visible                                                                                                                     |
| Grep-clean (CX-3 accept)     | `grep -rn "netAsk += \|netBid += " web/components web/lib --include='*.ts*' \| grep -v optionsChainUtils` | no output                                                                                                                              |

## PR 2 tripwires

- STOP if `test_combo_fat_finger_rejected_LIMIT_OUT_OF_BAND` passes before Step 2.3's server wiring — the gate already exists.
- STOP if rewiring OrderTab/ModifyOrderModal changes any existing test's expected numbers — the consolidation must be output-identical; report instead of "fixing" the expectation.
- STOP if `_fetch_combo_leg_quotes` needs more than one `pool.acquire` per order or any `asyncio.to_thread` around ib_async — reuse the `run_sync` pattern only.
- STOP if outside RTH during live probes — the market-hours gate will 400 everything; do not weaken it to test.
- PAPER ONLY (port 4002) for all live checks.

## PR 2 rollback

Branch discard; no migration. If merged and the band gate misfires on legitimate combos (e.g. wide-market symbols): the `acknowledge_limit_override` escape hatch already works; a revert of the `_validate_place_gates` combo branch restores the old skip without touching PR 1.

## PR 2 incident-history row (append as row 25)

```
| 25  | 2026-07-05 (this PR)                           | Quote gate (freshness/band/market-hours) applied to non-combo only; combo fat-finger net prices reached IB, protected only by preflight + IB rejection | F3 branch `if body.get("type") != "combo"` skipped all quote validation for BAG orders; no combo net-quote source server-side | `quote_guard.check_combo_payload` (signed net band: slack = max(5% |mid|, 2 ticks x n_legs)) + `_validate_combo_quote` fed by per-leg pool snapshots; wired into `_validate_place_gates` so `/orders/place` AND `/orders/replace` (pre-cancel) both gate; same `acknowledge_limit_override` escape hatch; web combo net-price math consolidated to `computeSignedComboQuote` (was x3) | `test_quote_guard.py::TestComboBand`, combo cases in `test_place_quote_gate.py`, `e2e/combo-net-price-band.spec.ts` (displayed net price + submitted payload) |
```

---

## Invariants both PRs must respect (repeat)

- Combo/BAG: never derive `Order.action` from debit/credit; `ComboLeg.action` encodes structure. The replace endpoint forwards leg actions untouched.
- Credits negative / debits positive; never `Math.abs()` away sign on stored/submitted prices (display normalization inside the existing util tail is the only allowed abs).
- Toast reads `body.reason_code` top-level → all new error paths use `JSONResponse(content={...})`.
- `XENON_READ_ONLY=1` refuses `/orders/replace` (tested).
- All Python via `uv run`; PAPER-only live checks; branch + PR, never push master; no AI attribution trailers.
- Frozen real prices in tests (SPY 620/630 verticals above are authoring-time paper quotes, as-of 2026-07-03); no network at test runtime.
