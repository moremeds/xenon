# S4 — Protect the post-ack persist (OP-2)

- **Date:** 2026-07-05
- **Proposed branch:** `fix/s4-protect-post-ack-persist`
- **Finding IDs:** OP-2 (High); companion test-coverage gap TS-1 (High) partially closed
- **Severity:** High
- **Goal (one line):** When the broker accepts an order but the follow-up DB write (`mark_submitted`) fails, return the broker ack to the client with a `persist_warning` flag and leave a recoverable `order_events` trail — never a 500 over a live order.

---

## 1. Context (what exists today)

The place path is `server.py::_orders_place_from_body` (async, currently begins at
`src/xenon/api/server.py:2139`). After all gates (preflight, quote gate, F4 reservation)
it reaches the broker-submit + persist region. There are **two** persist sites, both
calling `orders_store.mark_submitted(...)` with **no error handling**:

1. **Test-mode branch** — `if _is_test_mode():` (currently `server.py:2258-2274`). Builds
   synthetic order ids, calls `mark_submitted`, returns a constructed `{"status":"ok", ...}`
   dict. Every existing `/orders/place` route test runs through here (`XENON_API_TEST_MODE=1`).
2. **Post-ack branch** — `if result.data:` (currently `server.py:2325-2331`), reached after
   `result = await _run_ib_script_with_recovery("xenon-ib-place-order", ...)` returns a
   successful, non-error payload. **This is the OP-2 site**: the broker has already accepted
   the order; if `mark_submitted` then raises (e.g. Postgres `OperationalError`), the whole
   coroutine propagates the exception → FastAPI returns **HTTP 500**, the `order_submissions`
   row stays non-terminal (`PENDING`/`RESERVED`), and the UI shows failure even though a live
   order exists at the broker.

`mark_submitted` (`src/xenon/execution/orders_store.py:485`) opens its own sync engine
(`get_sync_engine()`) and runs two UPDATEs inside `engine.begin()` (one on
`order_submissions` → `state="WORKING"` + ids, one on `regime_overrides`). Any DB-layer
failure (connection loss, statement timeout, deadlock, pool exhaustion) surfaces as a
SQLAlchemy `OperationalError`/`DBAPIError`/`SQLAlchemyError` — all subclasses of
`sqlalchemy.exc.SQLAlchemyError`, itself a subclass of `Exception`.

`orders_store.record_event(submission_id, kind, detail)`
(`src/xenon/execution/orders_store.py:713`) inserts one append-only `order_events` row. It
**also** opens its own `get_sync_engine()` txn, so if the DB is fully down it will raise too —
that is the "compensating write also fails" case the response must survive.

The existing IB-reject branch (`server.py:2287-2324`) already demonstrates the pattern this
plan mirrors: `mark_terminal` then a **best-effort** `record_event` wrapped in
`try/except Exception` with a `logger.warning(..., exc_info=True)` fallback.

The web route `web/app/api/orders/place/route.ts` calls FastAPI `/orders/place` via
`xenonFetch`, then on success builds `NextResponse.json({...})` from named fields
(`orderId`, `permId`, `tif`, `initialStatus`, `message`, `orders`, `requestId`). It does
**not** currently forward any `persist_warning` field. The primary order-entry UI surface is
`web/components/ticker-detail/OrderTab.tsx`; its **four** success branches (single-leg
`placeOrder` ~line 519 / retry ~line 582, combo place ~line 1008 / combo retry ~line 1073)
all POST to `/api/orders/place` and call `setSuccess(...)`.

**What the executor does NOT need to understand:** the IB subprocess internals, the F2/F3/F4
gate logic, combo/BAG semantics, the regime-override plumbing, or how `_run_ib_script_with_recovery`
talks to the gateway. This change only wraps the persist call and threads one boolean flag
through to the toast.

---

## 2. Drift from review

- Fable cites `server.py:2325-2331` for OP-2. At HEAD the post-ack `mark_submitted` call is at
  **`server.py:2326-2331`** inside `if result.data:` — confirmed present, line numbers drifted
  by 1. Anchor on the `if result.data:` snippet, not the line number.
- The fable finding lists only the post-ack site. There is a **second** unprotected
  `mark_submitted` in the test-mode branch (`server.py:2260`). This plan protects **both** via a
  single shared helper — this is what makes case (e) testable through the standard test-mode
  route without spawning a real subprocess, and it is strictly safer. Not a contradiction of the
  finding; an extension noted here.
- No part of OP-2 is already fixed. Verified: neither `mark_submitted` call is wrapped today.

---

## 3. Goal / Non-goals

**Goal:** A `mark_submitted` failure after a broker ack returns the ack (HTTP 200) with
`persist_warning: true` + `persist_warning_reason`, writes a best-effort `PERSIST_FAILED`
`order_events` row, and logs loudly at ERROR. If the compensating write also fails, the response
still succeeds with the warning and logs a second ERROR — recovery then falls to S3's poller
sweep / boot rehydrate (via orderRef, delivered by S2).

**Non-goals (explicitly NOT in this PR — one change, one PR):**

- OP-1 / S2: `UNCERTAIN` state on subprocess timeout, `client_attempt_id` as IB `orderRef`,
  staged-ack parsing. **This plan lands AFTER S2** (see §8 merge-order).
- OP-3 / S3: the reconciliation sweep in the activity poller. This plan _depends on_ S3+S2 for
  eventual recovery of the non-terminal row but does not implement it.
- A retry queue for the failed persist (the roadmap says "compensating event write + alert"; a
  durable retry queue is future work — the compensating event **is** the recoverable trail).
- The IB-reject branch (`server.py:2287-2324`) — already protected, left untouched.
- Surfacing `persist_warning` in every place-order caller. UI scope is the primary `OrderTab.tsx`
  surface only (browser-verifiable); other callers (`OptionsChainTab`, `BookTab`,
  `InstrumentDetailModal`, `PositionOrderModal`) keep their existing success toast — the
  backend flag is present in their response JSON for a later sweep, but no UI change here.

---

## 4. Key facts (verified against HEAD)

| Fact                             | Value                                                                                                                                                     | Verified at                                             |
| -------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------- |
| Place handler                    | `async def _orders_place_from_body(body: dict)`                                                                                                           | `src/xenon/api/server.py:2139`                          |
| Post-ack persist site            | `if result.data:` → `orders_store.mark_submitted(...)`                                                                                                    | `src/xenon/api/server.py:2325-2331`                     |
| Test-mode persist site           | `if _is_test_mode():` → `orders_store.mark_submitted(...)`                                                                                                | `src/xenon/api/server.py:2258-2265`                     |
| `mark_submitted` signature       | `mark_submitted(*, submission_id: str, ib_order_id: str, perm_id: str \| None, placing_client_id: int \| None) -> None`                                   | `src/xenon/execution/orders_store.py:485-491`           |
| `record_event` signature         | `record_event(submission_id: str, kind: str, detail: dict) -> None`                                                                                       | `src/xenon/execution/orders_store.py:713-717`           |
| Logger                           | `logger = logging.getLogger("xenon.api")`                                                                                                                 | `src/xenon/api/server.py:89`                            |
| ReasonCode enum                  | `class ReasonCode(StrEnum)`                                                                                                                               | `src/xenon/execution/preflight.py:24`                   |
| ScriptResult fields              | `.ok`, `.error`, `.data` (dataclass)                                                                                                                      | imported `server.py:64` from `xenon.api.subprocess`     |
| Exceptions from `mark_submitted` | `sqlalchemy.exc.SQLAlchemyError` (superclass of `OperationalError`, `DBAPIError`, `TimeoutError`), plus any `Exception` — catch broadly, re-raise nothing | verified: uses `get_sync_engine()` + `engine.begin()`   |
| Web route success builder        | `NextResponse.json({ status, orderId, permId, tif, initialStatus, message, orders, requestId })`                                                          | `web/app/api/orders/place/route.ts:238-247`             |
| Primary UI success branches      | `setSuccess("Order placed: ...")`                                                                                                                         | `web/components/ticker-detail/OrderTab.tsx:520`, `:582` |
| Dev ports                        | Next 3200 / FastAPI 8421 / relay 8866                                                                                                                     | root CLAUDE.md                                          |
| Response contract for success    | HTTP 200, body includes `persist_warning?: boolean`, `persist_warning_reason?: string`                                                                    | **new, defined here**                                   |

**Exact success-with-warning JSON (new contract):**

```json
{
  "status": "ok",
  "orderId": 123,
  "permId": 456,
  "initialStatus": "Submitted",
  "message": "...",
  "submission_id": "…",
  "persist_warning": true,
  "persist_warning_reason": "PERSIST_FAILED"
}
```

On the happy path, `persist_warning` is **absent** (not `false`) — callers treat missing as ok.

---

## 5. Steps (strictly ordered, TDD)

> All Python via `uv run …`. Do not commit. Do not touch any file outside those listed.

### Step 1 — Add the reason code (enum)

**File:** `src/xenon/execution/preflight.py`
**Anchor:** the line `SUBPROCESS_ERROR = "SUBPROCESS_ERROR"` (currently `preflight.py:61`, the last
member of `class ReasonCode`).

Add immediately after it:

```python
    # B5 — hard subprocess failure on /orders/place (non-2xx from runner).
    SUBPROCESS_ERROR = "SUBPROCESS_ERROR"
    # S4 (OP-2) — broker accepted the order but the post-ack DB persist
    # (mark_submitted) failed; ack is returned with persist_warning and the
    # row is left non-terminal for boot rehydrate / poller sweep recovery.
    PERSIST_FAILED = "PERSIST_FAILED"
```

(Keep the existing `SUBPROCESS_ERROR` line; only the two comment lines + `PERSIST_FAILED` line
are new.)

### Step 2 — Write the failing Python route test FIRST (case e)

**File (new):** `scripts/tests/test_orders_place_post_ack_persist.py`

This test drives the standard test-mode `/orders/place` route (same harness as
`test_place_quote_gate.py`), monkeypatches `orders_store.mark_submitted` to raise a SQLAlchemy
`OperationalError`, and asserts the ack still returns with the warning flag and that a
`PERSIST_FAILED` event row exists.

```python
import pytest

# This module forks its own committed DB semantics: it reads order_events back
# via a fresh engine, and drives the test-mode persist path end to end.
pytestmark = pytest.mark.committed_db

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    monkeypatch.setenv("XENON_DATA_DIR", str(tmp_path))
    yield


@pytest.fixture
def client():
    from xenon.api import server
    from xenon.execution import orders_store, quote_guard

    orders_store.init_store()
    # Tick rule cache so the quote gate can resolve a min-tick without IB.
    server._tick_rule_cache = quote_guard.TickRuleCache(
        source=lambda con_id: Decimal("0.01"),
        ttl_seconds=3600,
    )
    return TestClient(server.app)


def _place_body():
    # Real ticker at a real frozen price (AAPL close 2026-07-02 ~ 213.55).
    return {
        "type": "stock",
        "symbol": "AAPL",
        "action": "BUY",
        "quantity": 1,
        "limitPrice": 213.55,
        "tif": "DAY",
        "con_id": 265598,
        "client_attempt_id": "s4-persist-fail-1",
    }


def _stub_quote(monkeypatch):
    from xenon.api import server

    async def _fresh_quote(ticker: str, con_id: int):
        return {
            "bid": Decimal("213.50"),
            "ask": Decimal("213.60"),
            "bid_size": 100,
            "ask_size": 120,
        }

    monkeypatch.setattr(server, "_fetch_quote_snapshot", _fresh_quote)


def test_persist_failure_returns_ack_with_warning_and_compensating_event(
    client, monkeypatch
):
    from xenon.execution import orders_store

    _stub_quote(monkeypatch)

    def _boom(**_kwargs):
        raise OperationalError("SELECT 1", {}, Exception("db down"))

    monkeypatch.setattr(orders_store, "mark_submitted", _boom)

    resp = client.post("/orders/place", json=_place_body())

    # Ack still returned — not a 500.
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["persist_warning"] is True
    assert body["persist_warning_reason"] == "PERSIST_FAILED"
    submission_id = body["submission_id"]

    # Compensating trail exists.
    from xenon.db.engine import get_sync_engine
    from xenon.db.schema import order_events
    from sqlalchemy import select

    with get_sync_engine().connect() as conn:
        kinds = [
            r[0]
            for r in conn.execute(
                select(order_events.c.kind).where(
                    order_events.c.submission_id == submission_id
                )
            )
        ]
    assert "PERSIST_FAILED" in kinds

    # Row left recoverable: non-terminal state, broker ids NOT partially
    # persisted (mark_submitted's transaction rolled back atomically).
    from xenon.db.schema import order_submissions

    with get_sync_engine().connect() as conn:
        row = conn.execute(
            select(
                order_submissions.c.state, order_submissions.c.ib_order_id
            ).where(order_submissions.c.submission_id == submission_id)
        ).one()
    assert row[0] == "PENDING"
    assert row[1] is None


def test_persist_failure_on_real_post_ack_branch(client, monkeypatch):
    """Drive the REAL post-ack branch (OP-2 proper), not the test-mode branch:
    subprocess stub returns a broker ack, mark_submitted raises → 200 + warning.
    Without this, an executor could fix only the test-mode site and V1 would
    still pass."""
    from xenon.api import server
    from xenon.api.subprocess import ScriptResult
    from xenon.execution import orders_store

    _stub_quote(monkeypatch)
    monkeypatch.delenv("XENON_API_TEST_MODE")

    async def _fake_place(entry, args, timeout=15):
        return ScriptResult(
            ok=True,
            data={
                "status": "ok",
                "orderId": 123,
                "permId": 456,
                "clientId": 26,
                "initialStatus": "Submitted",
            },
        )

    monkeypatch.setattr(server, "_run_ib_script_with_recovery", _fake_place)

    def _boom(**_kwargs):
        raise OperationalError("SELECT 1", {}, Exception("db down"))

    monkeypatch.setattr(orders_store, "mark_submitted", _boom)

    body = {**_place_body(), "client_attempt_id": "s4-persist-fail-real-1"}
    resp = client.post("/orders/place", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["persist_warning"] is True
    assert data["persist_warning_reason"] == "PERSIST_FAILED"


def test_compensating_write_failure_still_returns_ack(client, monkeypatch):
    """DB fully down: mark_submitted AND record_event both raise — the ack
    must still return 200 with the warning (recovery falls to S2/S3)."""
    from xenon.execution import orders_store

    _stub_quote(monkeypatch)

    def _boom(*_args, **_kwargs):
        raise OperationalError("SELECT 1", {}, Exception("db down"))

    monkeypatch.setattr(orders_store, "mark_submitted", _boom)
    monkeypatch.setattr(orders_store, "record_event", _boom)

    body = {**_place_body(), "client_attempt_id": "s4-persist-fail-both-1"}
    resp = client.post("/orders/place", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["persist_warning"] is True


def test_happy_path_has_no_persist_warning(client, monkeypatch):
    _stub_quote(monkeypatch)
    resp = client.post("/orders/place", json=_place_body())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert "persist_warning" not in body
```

Run it — it MUST fail now (no `persist_warning` in the response, `mark_submitted` raise
propagates to 500):

```
uv run pytest scripts/tests/test_orders_place_post_ack_persist.py -xvs
```

**Expected before the fix:** `test_persist_failure_...` fails with a 500 (or KeyError on
`persist_warning`). If it PASSES before your change, **STOP — the anchor is wrong**.

> Note on frozen fixture: `con_id=265598` and the AAPL price are a hardcoded snapshot for the
> quote gate; the `_stub_quote` monkeypatch supplies the bid/ask so no network is hit. If the
> quote gate rejects for an unrelated reason (e.g. tick rule), STOP and report — do not loosen
> the gate.

### Step 3 — Add the shared persist-or-compensate helper

**File:** `src/xenon/api/server.py`
**Anchor:** insert immediately BEFORE `async def _orders_place_from_body(body: dict):`
(currently `server.py:2139`). Place the helper as a module-level `def` right above the handler.

```python
def _persist_submitted_or_compensate(
    *,
    submission_id: str,
    ib_order_id: str,
    perm_id: str | None,
    placing_client_id: int | None,
) -> tuple[bool, str | None]:
    """Persist WORKING state after a broker ack, surviving DB failure.

    S4 (OP-2): the broker has already accepted the order by the time we call
    this. A DB failure in ``mark_submitted`` must NOT become a 500 over a live
    order. On failure we (1) log loudly at ERROR, (2) best-effort write a
    compensating ``PERSIST_FAILED`` order_events row so the order is
    re-associable, and (3) signal the caller to return the ack with a
    ``persist_warning`` flag. If the compensating write ALSO fails (DB fully
    down) we log a second ERROR and still succeed — recovery then falls to the
    boot rehydrate / poller sweep via orderRef (S2/S3).

    Returns ``(persisted_ok, warning_reason)``. On success ``(True, None)``.
    """
    try:
        orders_store.mark_submitted(
            submission_id=submission_id,
            ib_order_id=ib_order_id,
            perm_id=perm_id,
            placing_client_id=placing_client_id,
        )
        return True, None
    except Exception:  # broad by design — broker already accepted; never re-raise
        logger.error(
            "POST-ACK PERSIST FAILED: broker accepted order but mark_submitted "
            "raised. submission=%s ib_order_id=%s perm_id=%s client_id=%s — order "
            "is LIVE; row left non-terminal for boot rehydrate / poller sweep.",
            submission_id,
            ib_order_id,
            perm_id,
            placing_client_id,
            exc_info=True,
        )
        try:
            orders_store.record_event(
                submission_id,
                "PERSIST_FAILED",
                {
                    "ib_order_id": ib_order_id,
                    "perm_id": perm_id,
                    "placing_client_id": placing_client_id,
                    "note": "broker ack received; mark_submitted failed",
                },
            )
        except Exception:  # pragma: no cover — DB fully down; nothing left to do
            logger.error(
                "POST-ACK COMPENSATING WRITE ALSO FAILED: submission=%s — "
                "recovery deferred to boot rehydrate / poller sweep via orderRef.",
                submission_id,
                exc_info=True,
            )
        return False, ReasonCode.PERSIST_FAILED.value
```

### Step 4 — Route the test-mode persist through the helper

**File:** `src/xenon/api/server.py`
**Anchor:** the `if _is_test_mode():` block (currently `server.py:2258-2274`).

Replace:

```python
    if _is_test_mode():
        order_id, perm_id = _next_test_order_ids()
        orders_store.mark_submitted(
            submission_id=submission_id,
            ib_order_id=str(order_id),
            perm_id=str(perm_id),
            placing_client_id=26,
        )
        return {
            "status": "ok",
            "orderId": order_id,
            "permId": perm_id,
            "initialStatus": "Submitted",
            "message": "Order accepted in test mode",
            "echo": body,
            "submission_id": submission_id,
        }
```

with:

```python
    if _is_test_mode():
        order_id, perm_id = _next_test_order_ids()
        persisted, warn_reason = _persist_submitted_or_compensate(
            submission_id=submission_id,
            ib_order_id=str(order_id),
            perm_id=str(perm_id),
            placing_client_id=26,
        )
        payload: dict = {
            "status": "ok",
            "orderId": order_id,
            "permId": perm_id,
            "initialStatus": "Submitted",
            "message": "Order accepted in test mode",
            "echo": body,
            "submission_id": submission_id,
        }
        if not persisted:
            payload["persist_warning"] = True
            payload["persist_warning_reason"] = warn_reason
        return payload
```

### Step 5 — Route the post-ack persist through the helper

**File:** `src/xenon/api/server.py`
**Anchor:** the tail of `_orders_place_from_body` (currently `server.py:2325-2332`):

```python
    if result.data:
        orders_store.mark_submitted(
            submission_id=submission_id,
            ib_order_id=str(result.data.get("orderId") or ""),
            perm_id=str(result.data.get("permId") or ""),
            placing_client_id=int(result.data.get("clientId") or 26),
        )
    return result.data
```

Replace with:

```python
    if result.data:
        # Normalize ack fields BEFORE the helper — a malformed clientId must
        # not raise a ValueError over a live order (that would be OP-2 again,
        # one expression earlier).
        _raw_client = result.data.get("clientId")
        try:
            _client_id = int(_raw_client) if _raw_client is not None else 26
        except (TypeError, ValueError):
            _client_id = 26
        persisted, warn_reason = _persist_submitted_or_compensate(
            submission_id=submission_id,
            ib_order_id=str(result.data.get("orderId") or ""),
            perm_id=str(result.data.get("permId") or ""),
            placing_client_id=_client_id,
        )
        if not persisted:
            data = dict(result.data)
            data["persist_warning"] = True
            data["persist_warning_reason"] = warn_reason
            return data
    return result.data
```

Run the Python test — it MUST now pass:

```
uv run pytest scripts/tests/test_orders_place_post_ack_persist.py -xvs
```

**Expected:** both tests green.

### Step 6 — Forward the flag in the web route

**File:** `web/app/api/orders/place/route.ts`
**Anchor:** the success `NextResponse.json({...})` block (currently lines 238-247).

Replace:

```ts
const response = NextResponse.json({
  status: "ok",
  orderId: orderResult.orderId,
  permId: orderResult.permId,
  tif: orderResult.tif ?? orderPayload.tif,
  initialStatus: orderResult.initialStatus,
  message: orderResult.message,
  orders: refreshedOrders,
  requestId,
});
```

with:

```ts
const response = NextResponse.json({
  status: "ok",
  orderId: orderResult.orderId,
  permId: orderResult.permId,
  tif: orderResult.tif ?? orderPayload.tif,
  initialStatus: orderResult.initialStatus,
  message: orderResult.message,
  orders: refreshedOrders,
  // S4 (OP-2): broker accepted but FastAPI could not persist WORKING state.
  // Order is live; surface a non-fatal warning so the operator knows the
  // terminal row may lag until boot rehydrate / poller sweep reconciles.
  persist_warning: orderResult.persist_warning === true ? true : undefined,
  persist_warning_reason:
    orderResult.persist_warning === true
      ? orderResult.persist_warning_reason
      : undefined,
  requestId,
});
```

(`undefined` fields are dropped by `NextResponse.json`, preserving "absent on happy path".)

### Step 7 — Write the failing Vitest for the route forwarding FIRST, then confirm green

**File (new):** `web/tests/order-place-persist-warning.test.ts`

Mirror the existing place-route test style (mock `xenonFetch`). Search for an existing route
test to copy the mock harness: `rg -l "orders/place" web/tests`. If a place-route test exists,
add a case there instead of a new file (append; do not duplicate the harness). The assertion:

```ts
import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock xenonFetch so the FastAPI call returns a persist_warning payload.
vi.mock("@/lib/xenonApi", () => ({
  xenonFetch: vi.fn(),
}));

import { xenonFetch } from "@/lib/xenonApi";
import { POST } from "@/app/api/orders/place/route";

const baseBody = {
  type: "stock",
  symbol: "AAPL",
  action: "BUY",
  quantity: 1,
  limitPrice: 213.55,
  client_attempt_id: "vt-s4-1",
};

function req(body: unknown): Request {
  return new Request("http://localhost/api/orders/place", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

describe("place route persist_warning passthrough (S4/OP-2)", () => {
  beforeEach(() => vi.clearAllMocks());

  it("forwards persist_warning + reason from FastAPI on success", async () => {
    (xenonFetch as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({
        status: "ok",
        orderId: 123,
        permId: 456,
        initialStatus: "Submitted",
        persist_warning: true,
        persist_warning_reason: "PERSIST_FAILED",
      })
      .mockResolvedValueOnce({}); // /orders/refresh
    const res = await POST(req(baseBody));
    const json = await res.json();
    expect(res.status).toBe(200);
    expect(json.persist_warning).toBe(true);
    expect(json.persist_warning_reason).toBe("PERSIST_FAILED");
  });

  it("omits persist_warning on the happy path", async () => {
    (xenonFetch as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({
        status: "ok",
        orderId: 123,
        permId: 456,
        initialStatus: "Submitted",
      })
      .mockResolvedValueOnce({});
    const res = await POST(req(baseBody));
    const json = await res.json();
    expect(res.status).toBe(200);
    expect(json.persist_warning).toBeUndefined();
  });
});
```

Run:

```
cd web && npm test -- order-place-persist-warning
```

**Expected before Step 6:** first test fails (`persist_warning` undefined). After Step 6: green.

> If the existing place route already has a Vitest with a mock harness that differs from the
> above (e.g. different mock path for `xenonFetch`), adapt the mock target to match that file
> and append the two cases there. Do NOT invent a second harness that conflicts.

### Step 8 — Surface the warning in the primary UI toast

**File:** `web/components/ticker-detail/OrderTab.tsx`
**Anchor 1:** the `placeOrder` success branch (currently line 519-527), specifically:

```ts
      } else {
        setSuccess(
          `Order placed: ${action} ${parsedQty} ${ticker} @ ${fmtPrice(parsedPrice)}`,
        );
```

Replace the `setSuccess(...)` call with:

```ts
      } else {
        const persistWarn = json?.persist_warning === true;
        setSuccess(
          `Order placed: ${action} ${parsedQty} ${ticker} @ ${fmtPrice(parsedPrice)}` +
            (persistWarn
              ? " — sent to broker, but the terminal could not confirm it yet; it will reconcile shortly."
              : ""),
        );
```

**Anchor 2:** the `retryRegimeOrder` success branch (currently line 582-584):

```ts
setSuccess(
  `Order placed: ${requestBody.action} ${requestBody.quantity} ${ticker} @ ${fmtPrice(Number(requestBody.limitPrice))}`,
);
```

Replace with:

```ts
const persistWarn = json?.persist_warning === true;
setSuccess(
  `Order placed: ${requestBody.action} ${requestBody.quantity} ${ticker} @ ${fmtPrice(Number(requestBody.limitPrice))}` +
    (persistWarn
      ? " — sent to broker, but the terminal could not confirm it yet; it will reconcile shortly."
      : ""),
);
```

**Anchor 3:** the combo `placeOrder` success branch (currently ~line 1008):

```ts
setSuccess(
  `Combo order placed: ${action} ${parsedQty}x ${position.structure} @ ${fmtSignedPrice(parsedPrice)}`,
);
```

**Anchor 4:** the combo retry success branch (currently ~line 1073):

```ts
setSuccess(
  `Combo order placed: ${requestBody.action} ${requestBody.quantity}x ${position.structure} @ ${fmtSignedPrice(Number(requestBody.limitPrice))}`,
);
```

Apply the same transformation to both: prefix with `const persistWarn = json?.persist_warning === true;`
and append the identical suffix string conditionally. All four success branches in `OrderTab.tsx`
(single-leg place/retry + combo place/retry) get the warning; they all POST to `/api/orders/place`
and all have `json` in scope (`const json = await res.json().catch(() => null);` at lines 498,
559, 986, 1049).

Typecheck + lint:

```
cd web && npx tsc --noEmit && npm run lint
```

---

## 6. Verification matrix

| #   | Check                                  | Exact command                                                                                                                                       | Expected                                                                                                                                                                                                                                                             |
| --- | -------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| V1  | New Python route tests                 | `uv run pytest scripts/tests/test_orders_place_post_ack_persist.py -xvs`                                                                            | 4 passed: test-mode persist failure (warning + event + row still `PENDING` with `ib_order_id IS NULL`), REAL post-ack branch failure (subprocess stubbed, warning returned), double failure (`record_event` also raises → still 200 + warning), happy path (no flag) |
| V2  | Red-first proof                        | Same command, run BEFORE Steps 3-5                                                                                                                  | `test_persist_failure_...` FAILS (500 or KeyError). If it passes, STOP (anchor wrong)                                                                                                                                                                                |
| V3  | Existing place/quote tests unbroken    | `uv run pytest scripts/tests/test_place_quote_gate.py scripts/tests/test_idempotency_route.py scripts/tests/test_orders_place_no_regime_gate.py -q` | all passed                                                                                                                                                                                                                                                           |
| V4  | Enum import sanity                     | `uv run python -c "from xenon.execution.preflight import ReasonCode; print(ReasonCode.PERSIST_FAILED.value)"`                                       | prints `PERSIST_FAILED`                                                                                                                                                                                                                                              |
| V5  | Scoped Python suite                    | `uv run python scripts/infra/dev/run_pytest_affected.py`                                                                                            | exit 0                                                                                                                                                                                                                                                               |
| V6  | Vitest route forwarding                | `cd web && npm test -- order-place-persist-warning`                                                                                                 | 2 passed                                                                                                                                                                                                                                                             |
| V7  | Web typecheck                          | `cd web && npx tsc --noEmit`                                                                                                                        | exit 0, no errors                                                                                                                                                                                                                                                    |
| V8  | Web lint                               | `cd web && npm run lint`                                                                                                                            | exit 0                                                                                                                                                                                                                                                               |
| V9  | Order-path CI guard (fallback read)    | `uv run python scripts/checks/no_json_fallback_on_order_path.py`                                                                                    | exit 0                                                                                                                                                                                                                                                               |
| V10 | Order-path CI guard (write)            | `uv run python scripts/checks/no_json_write_on_order_path.py`                                                                                       | exit 0                                                                                                                                                                                                                                                               |
| V11 | Order-path CI guard (caller allowlist) | `uv run python scripts/checks/order_path_caller_allowlist.py`                                                                                       | exit 0                                                                                                                                                                                                                                                               |
| V12 | E2E browser (see below)                | Playwright/chrome-cdp click-path                                                                                                                    | success toast shows the reconcile suffix; screenshot saved                                                                                                                                                                                                           |

### V12 — E2E browser (mandatory, UI-visible change)

Use PAPER stack (`scripts/infra/dev.sh paper`, IB port 4002) — **never live**. Because forcing a
real `mark_submitted` DB failure against paper is impractical, verify the UI branch by driving
the test-mode route through the browser with an injected failure, OR by a Playwright unit-DOM
test that stubs `fetch("/api/orders/place")`. Preferred: a Playwright spec that mocks the place
response.

**File (new or appended):** a Playwright spec under **`web/e2e/`** — verified:
`web/playwright.config.ts:8` sets `testDir: "./e2e"` relative to `web/`. Do NOT put it under
`web/tests/` (those are Vitest files; Playwright will not discover it there).

Click-path (chrome-cdp fallback if Playwright route-mocking is unavailable):

1. `scripts/infra/dev.sh paper` → wait for `curl http://localhost:8421/health` → `ib_gateway.port_listening: true`.
2. Navigate to a ticker detail page with the Order tab (e.g. `http://localhost:3200/ticker/AAPL`).
3. Intercept `POST /api/orders/place` and fulfill with
   `{ "status":"ok","orderId":1,"permId":2,"initialStatus":"Submitted","persist_warning":true,"persist_warning_reason":"PERSIST_FAILED" }`.
4. Fill a BUY 1 AAPL limit order, submit, confirm.
5. **Assert on-screen text contains:** `sent to broker, but the terminal could not confirm it yet`.
6. Screenshot → `output/playwright/s4-persist-warning-2026-07-05.png`.

Also assert the **happy path** (no `persist_warning` in the mocked response) shows the plain
`Order placed: BUY 1 AAPL @ …` toast with **no** suffix — screenshot
`output/playwright/s4-happy-path-2026-07-05.png`.

### Negative / both-directions coverage

- V1 covers **failure** direction (persist raises → warning + event).
- V1 `test_happy_path_...` + V6 second case + V12 happy-path cover **success** direction
  (no warning, absent field).

---

## 7. Tripwires / abort criteria

- **STOP** if `test_persist_failure_...` passes before Steps 3-5 land — the persist site moved;
  re-locate `mark_submitted` in `_orders_place_from_body` and re-anchor.
- **STOP** if `_orders_place_from_body` no longer exists or has been split (S2 may have renamed
  it) — see §8; re-anchor on the function that contains the post-ack `mark_submitted` and the
  `if result.data:` snippet, do not guess line numbers.
- **STOP** if more than this allowed edit set is needed: `preflight.py`, `server.py`,
  `web/app/api/orders/place/route.ts`, `web/components/ticker-detail/OrderTab.tsx`, the two new
  test files, the Playwright spec under `web/e2e/`, and the `docs/reference/order-path-incident-history.md`
  row append (§10). If any OTHER production file needs changing, report first.
- **STOP** and use PAPER only if any step appears to require live IB. Order-path live checks are
  PAPER-ONLY (`dev.sh paper`, port 4002).
- **STOP** if the quote gate rejects the AAPL fixture for a reason other than the stubbed quote —
  do not weaken the gate to make the test pass.
- Do **not** add a JSON read/write on the order path (CI guards V9-V11 will fail).
- Do **not** re-raise inside `_persist_submitted_or_compensate` — the broker already accepted.

---

## 8. Merge-order assumption (coordinate with S2)

**This plan lands AFTER S2** (`fix/…uncertain-orderref`), which rewrites the same handler region
(staged-ack parsing + `UNCERTAIN` state + `orderRef`). To make rebasing trivial:

- Anchor every edit on **function/snippet names**, never bare line numbers: the module-level
  helper goes directly above `async def _orders_place_from_body`; the two persist edits target
  the `if _is_test_mode():` block and the `if result.data:` block.
- Keep the helper self-contained (no shared state with S2's ack-staging), so if S2 has already
  extracted the post-ack persist into its own function, wrap `mark_submitted` there instead — the
  helper body is unchanged; only the call site's location differs.
- **S2 adds a THIRD persist site — the EARLY-ACK branch** (`if result.ack:` →
  `mark_submitted(...)` from the ack fields). That is also a persist AFTER a broker ack, so it
  MUST route through `_persist_submitted_or_compensate` too: a DB failure there would otherwise
  raise over a live order (exactly OP-2). Post-S2 the full wrap set is: test-mode persist,
  early-ack persist, clean-result persist. Add a route test: staged runner returns
  `ScriptResult(ok=False, ambiguous=True, ack={...})` (ack-then-kill) AND `mark_submitted`
  raises → response is still 200 with `persist_warning: true` and the row stays PENDING.
- If S2 introduces an `UNCERTAIN` return before `if result.data:`, that path is a _different_
  branch (timeout, no broker confirmation) and must **not** be wrapped by this helper — S4 only
  protects the confirmed-ack persist. Leave S2's UNCERTAIN branch alone.
- The recovery story is shared: when the compensating write also fails (DB fully down), the
  non-terminal row is reconciled by **S3's poller sweep / boot rehydrate matching on `orderRef`
  (delivered by S2)**. Spell this out in the PR description so the reviewer sees the dependency
  chain S2 → S4 → (S3 recovery).

If S2 has **not** merged when this executes: proceed anyway — the anchors are independent of S2's
changes; just note in the PR that S2 is not yet in and the UNCERTAIN branch does not exist to
avoid.

---

## 9. Rollback

- Pure code change, no migration, no schema change (the new `ReasonCode.PERSIST_FAILED` is an
  enum string constant; `order_events.kind` is free-text `Text`, so `"PERSIST_FAILED"` rows need
  no DDL).
- Revert by discarding the branch: `git checkout master && git branch -D fix/s4-protect-post-ack-persist`.
- Any `PERSIST_FAILED` `order_events` rows already written are harmless audit rows; no cleanup
  required.

---

## 10. Incident-history row (append to `docs/reference/order-path-incident-history.md`)

Append as row **24** (verify the next id — the current last row is 23):

```
| 24  | 2026-07-05 fix/s4-protect-post-ack-persist | `POST /orders/place` returned HTTP 500 when the post-broker-ack DB write (`mark_submitted`) failed (e.g. Postgres `OperationalError`), even though the order was already LIVE at IB; the `order_submissions` row stayed non-terminal (PENDING/RESERVED) and the UI showed failure over a live order (OP-2). | `_orders_place_from_body` called `orders_store.mark_submitted` with no error handling at both the test-mode and post-ack (`if result.data:`) persist sites; any DB-layer exception propagated out of the coroutine → FastAPI 500, dropping the broker ack the client needed to see. | Added `_persist_submitted_or_compensate` helper in `server.py`: wraps `mark_submitted` in `try/except Exception` (never re-raises — broker already accepted); on failure logs at ERROR, best-effort writes a `PERSIST_FAILED` `order_events` row, and returns `(False, "PERSIST_FAILED")` so the handler returns HTTP 200 with `persist_warning: true` + `persist_warning_reason`. Both persist sites route through the helper. `web/app/api/orders/place/route.ts` forwards the flag; `OrderTab.tsx` appends a non-fatal "sent to broker, terminal could not confirm yet" suffix to the success toast. If the compensating write also fails (DB fully down), the response still succeeds and a second ERROR logs — the non-terminal row is reconciled by boot rehydrate / poller sweep via `orderRef` (S2/S3). New `ReasonCode.PERSIST_FAILED`. | `scripts/tests/test_orders_place_post_ack_persist.py` (persist-failure → ack + warning + compensating event; happy-path → no warning); `web/tests/order-place-persist-warning.test.ts` (route forwards / omits the flag); Playwright e2e `s4-persist-warning`. **Watch pattern:** a broker-side side effect (live order) followed by a local persist means the persist can NEVER turn a broker ack into a 500 — wrap post-side-effect writes, return the ack with a warning, and leave a recoverable audit trail. |
```

(Match the existing table's column order: id | date/branch | symptom | root cause | solution |
tests/watch. Verify the header columns before inserting.)
