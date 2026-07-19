# Plan: S2 — Timeout ≠ terminal: `UNCERTAIN` state + IB `orderRef` correlation + event-driven ack

**Date:** 2026-07-05
**Branch:** `fix/order-uncertain-state-orderref`
**Findings:** OP-1 (Critical), OP-11 (Medium) — `docs/fable/03-findings-table.md` §3.1
**Severity:** Critical
**Goal (one line):** When the place subprocess is SIGKILLed/times out after IB may have
accepted the order, stop recording a terminal `FAILED` row — persist the early broker ack
(→ `WORKING` with real ib ids) when it arrived, and a new non-terminal `UNCERTAIN` row
(HTTP 502, "Do NOT resubmit") when it did not; set `order.orderRef = client_attempt_id`
for broker-side correlation and replace the blind 2s/5s ack sleep with an event-driven wait.

This is a **live order-path change**. Diagnose and verify on **PAPER only**
(`scripts/infra/dev.sh paper`, IB port 4002). Never test against live money.

---

## 1. Context — what exists today (verified at HEAD)

The place path is: Next `web/app/api/orders/place/route.ts` → FastAPI
`POST /orders/place` → `_orders_place_from_body` (`src/xenon/api/server.py:2139`) →
subprocess `xenon-ib-place-order` (`src/xenon/execution/ib_place_order.py`).

Today, `_orders_place_from_body` (verified `server.py:2276-2332`):

```python
order_json = json.dumps(body)
result = await _run_ib_script_with_recovery("xenon-ib-place-order", ["--json", order_json], timeout=15)
if not result.ok:
    orders_store.mark_terminal(submission_id=submission_id, state="FAILED",
                               reason_code="SUBPROCESS_ERROR", filled_qty=0, avg_fill_price=None)
    raise HTTPException(status_code=502, detail=result.error)
if result.data and result.data.get("status") == "error":
    ...  # IB reject → mark_terminal REJECTED (LIMIT_OFF_TICK / IB_REJECT) → 502
if result.data:
    orders_store.mark_submitted(submission_id=..., ib_order_id=..., perm_id=..., placing_client_id=...)
return result.data
```

`_run_ib_script_with_recovery` (`server.py:3120`) does two fast-fail health checks
(cooldown, gateway pre-check) and then calls `run_entry_point` (`src/xenon/api/subprocess.py:54`).
`run_entry_point` uses `proc.communicate()` under `asyncio.wait_for(timeout=15)`; **on
`TimeoutError` it kills the process and discards all partial stdout** — so an early ack line
is lost, and the handler's `not result.ok` branch writes `FAILED/SUBPROCESS_ERROR`. If IB had
already accepted the order, the row is a **false terminal FAILED with no ib ids**, and a user
retry under a new `client_attempt_id` places a **duplicate live order** (OP-1).

The CLI (`ib_place_order.py:143-153`) places the order then does a **blind sleep**
(`client.sleep(5 if combo else 2)`) before reading `orderStatus` — fixed 2–5 s latency and
`initialStatus` = whatever happened to arrive by then (OP-11). It never sets `order.orderRef`.

`orders_store.py` writers (verified): `reserve_attempt` (INSERT `state="PENDING"`),
`mark_submitted` (→ `WORKING` + ib ids, `orders_store.py:485`), `mark_terminal`
(terminal states, optimistic `expected_states=` guard, `orders_store.py:529`),
`record_event(submission_id, kind, detail)` (`orders_store.py:713`). `_TERMINAL_STATES =
{"REJECTED", "CANCELLED", "FAILED"}` (`orders_store.py:63`). There is **no `UNCERTAIN`
state and no writer for it**.

`order_submissions.state` is **free text — no CHECK constraint** (`schema.py:576-632`; OP-9).
So adding a new state value needs **no migration**.

The executor does **not** need to understand: the combo wizard, the cancel/modify subprocess,
the relay/quote stream, Futu, or the naked-short guard. Do not touch them.

---

## 2. Drift from review (deltas vs the fable docs)

1. **`07-testing-review.md` §7.4 item 1(a) conflicts with `10-roadmap.md` S2 acceptance —
   this plan follows the roadmap.** §7.4(a) says "ack printed then process killed → row must
   become `UNCERTAIN`". `10-roadmap.md` S2 accept says the SIGKILL-**after-ack** drill "ends
   with the row **WORKING** and correct ib ids". These disagree. The roadmap is correct: the
   ack line carries IB's `permId`, which **is** the openOrder broker confirmation — a row with
   a `permId` is genuinely live and belongs in `WORKING`, not `UNCERTAIN`. `UNCERTAIN` is
   reserved for the **no-ack** ambiguous case (subprocess died before any `permId` arrived, so
   we cannot know whether IB received the order). This plan encodes: **ack present → WORKING;
   no ack + no result → UNCERTAIN.** The §7.4(a) test-case wording is adapted accordingly.

2. **`11-code-sketches.md` §1 uses a generic `transition()` helper and §5 a full `LEGAL`
   edge-map + `transition()` chokepoint + a `state` CHECK constraint.** Those are **P2.2**
   (a later roadmap item), explicitly _not_ S2. This plan does **not** introduce the generic
   `transition()` function or the CHECK constraint. It adds one narrow writer
   (`mark_uncertain`) and reuses the existing `mark_submitted` / `mark_terminal`. When P2.2
   lands the CHECK constraint, `UNCERTAIN` must be in its allowed set (noted below).

3. **No new `order_ref` schema column.** `order.orderRef` is set to `client_attempt_id`,
   which is **already persisted** as `order_submissions.client_attempt_id`. So "persist
   orderRef" is satisfied by the existing column; S3's reconciliation reads
   `trade.order.orderRef` back and matches it to `client_attempt_id`. Adding a duplicate
   column would be dead weight. This is a deliberate simplification, not a gap.

4. Cited line numbers in `03-findings-table.md` have drifted (`server.py:2277-2286`,
   `subprocess.py:125-132`, `ib_place_order.py:147-148`). Anchors below use function names +
   unique snippets, verified at HEAD.

---

## 3. Goal / Non-goals

### Goal

- Add a non-terminal `UNCERTAIN` order state + a single writer (`orders_store.mark_uncertain`).
- Add a staged subprocess runner (`run_place_subprocess`) that streams the CLI's stdout
  line-by-line so an **early ack line survives a SIGKILL/timeout**.
- CLI: set `order.orderRef = client_attempt_id`; emit an early `{"stage":"ack",...}` stdout
  line as soon as `permId` arrives; replace the blind sleep with an **event-driven** wait.
- Place handler: ack present → `mark_submitted` (WORKING + ib ids) even if the process later
  died; spawned-but-ambiguous with no ack → `mark_uncertain` + **502 `ORDER_STATUS_UNCERTAIN`**
  ("Do NOT resubmit"); never-spawned/hard-connection failure → keep today's
  `FAILED/SUBPROCESS_ERROR` (order was definitively not sent, retryable).
- Make `UNCERTAIN` **visible** in the open-orders UI (it must not be an invisible orphan).
- Register `ORDER_STATUS_UNCERTAIN` reason code (Python + TS parity).

### Non-goals (adjacent findings NOT fixed here — one change, one PR)

- **OP-3 / S3** — reconciliation sweep that resolves `UNCERTAIN`/`PENDING` rows in the poller
  via `orderRef`. This plan persists+emits `orderRef` and leaves an `UNCERTAIN` row visible;
  it does **not** add the sweep. (An `UNCERTAIN` row simply stays visible until S3 or a manual
  reconcile.)
- **OP-8 (full `expected_states` sweep) / OP-9 (state CHECK constraint) / the generic
  `transition()` chokepoint** — P2.2. This plan only adds `expected_states=` to the specific
  terminal writes it touches, to avoid clobbering a `WORKING` row set by a late ack.
- **OP-2** (mark_submitted error handling), **OP-12** (place-CLI failure classification),
  **OP-7** (execution semaphore) — separate roadmap items.
- Cancel/modify path, combo wizard internals, relay, Futu.

---

## 4. Key facts (verified against the working tree / installed `.venv`)

- `ib_async.Order.orderRef: str = ""` exists — `.venv/lib/python3.13/site-packages/ib_async/order.py:38`.
  Set it on the `LimitOrder` before `placeOrder`; readable later as `trade.order.orderRef`.
- `trade.order.permId` is `0` client-side until IB's `openOrder` ack (memory
  `ib_async_permid_race`) — so polling `permId != 0` is the correct ack signal.
- `IBClient._last_client_id` holds the actually-allocated clientId after `connect(...,
client_id="auto")` (`src/xenon/clients/ib_client.py:274,326`). `IBClient.sleep(s)` pumps the
  ib_async event loop (`ib_client.py:948`). `IBClient.place_order` returns the `Trade`
  (`ib_client.py:579`). The CLI is **synchronous by design** — do not convert to async.
- `run_entry_point`/`run_module` live in `src/xenon/api/subprocess.py`; `ScriptResult` is the
  return dataclass (`subprocess.py:46`). `VENV_BIN = PROJECT_ROOT/".venv"/"bin"`
  (`subprocess.py:23`) — tests point a fake CLI here by monkeypatching this module global.
- `server.py` imports `from xenon.api.subprocess import ScriptResult, run_entry_point,
run_module` (`server.py:64`).
- Open-orders UI reads `orders_payload_for_scope` → `select(...).where(...
state.in_(ACTIVE_STATES))` with `ACTIVE_STATES = {"PENDING","WORKING","PARTIALLY_FILLED"}`
  (`src/xenon/api/routes/orders.py:22,316`). `_status_from_state` maps state→display string
  (`routes/orders.py:98`). `working_orders_for` (per-ticker) filters
  `state.in_(["PENDING","WORKING","PARTIALLY_FILLED"])` (`src/xenon/db/queries/orders.py:211`).
- Reason-code parity is enforced by two tests that must stay in lockstep:
  `web/tests/order-reason-codes.test.ts` (asserts `Object.keys(ORDER_REASON_CODES)` ==
  `PYTHON_REASON_CODES` exactly) and `scripts/tests/test_preflight_reason_codes.py`.
- FastAPI 502 `JSONResponse(content={..., "reason_code": ...})` passes through Next verbatim
  (`web/lib/passThroughXenonError.ts`) to the browser toast, which reads **top-level**
  `body.reason_code` (memory `httpexception_dict_detail_breaks_toast`).
- `_is_test_mode()` reads `XENON_API_TEST_MODE` (`server.py:142`). With it **unset**, the
  place handler runs the real subprocess. `TestClient(app)` (no `with`) skips lifespan.
- Place-path route tests that touch `orders_store` (its own `get_sync_engine`) use
  `@pytest.mark.committed_db` (precedent: `scripts/tests/test_orders_place_no_regime_gate.py:30`).

---

## 5. Steps (strictly ordered — TDD: failing test first, then implement, then green)

> All Python via `uv run …`. Do not edit `.env`. Do not commit until the user says so.
> After adding/changing a CLI entry point or route, no `uv sync` is needed (no new entry
> point is added here).

### Step 0 — Branch

```bash
cd /Users/chenxi/projects/xenon
git checkout -b fix/order-uncertain-state-orderref
```

STOP if the working tree is dirty with unrelated changes — report and wait.

---

### Step 1 — `orders_store.mark_uncertain` (writer for the new state)

**1a. Failing test** — append to `scripts/tests/test_orders_submissions_store.py`
(mark the module or the test `committed_db` only if the file's existing tests are; check the
top of the file — if it already has DB fixtures matching the other store tests, follow them).
Add:

```python
def _state_of(sid: str) -> dict:
    # There is NO orders_store.get_submission — read the row directly.
    from xenon.db.engine import get_sync_engine
    from xenon.db.schema import order_submissions
    from sqlalchemy import select
    with get_sync_engine().connect() as conn:
        r = conn.execute(
            select(order_submissions).where(order_submissions.c.submission_id == sid)
        ).first()
    return dict(r._mapping)


def test_mark_uncertain_transitions_pending_and_writes_event():
    # Seed a PENDING row via reserve_attempt (see the reserve_attempt tests in
    # this file for the exact RequestRow/scope construction — copy that style).
    from xenon.execution import orders_store
    sid = _seed_pending_row()  # local helper you write, mirroring existing tests
    rc = orders_store.mark_uncertain(
        submission_id=sid,
        detail={"reason": "subprocess did not return a result"},
    )
    assert rc == 1
    row = _state_of(sid)
    assert row["state"] == "UNCERTAIN"
    assert row["reason_code"] == "ORDER_STATUS_UNCERTAIN"


def test_mark_uncertain_does_not_clobber_working():
    # A row already WORKING (ack path won) must NOT be downgraded to UNCERTAIN.
    from xenon.execution import orders_store
    sid = _seed_pending_row()
    orders_store.mark_submitted(
        submission_id=sid, ib_order_id="1", perm_id="2", placing_client_id=24
    )  # → WORKING
    rc = orders_store.mark_uncertain(submission_id=sid, detail={"reason": "x"})
    assert rc == 0
    assert _state_of(sid)["state"] == "WORKING"
```

> `_seed_pending_row()` is a small helper you write at the top of the added block — call
> `orders_store.reserve_attempt(...)` exactly as the neighbouring `reserve_attempt` tests in
> this file do (same `RequestRow`, same `broker/account_env/broker_account` scope) and return
> `outcome.submission_id`. Do NOT invent new production helpers.

Run it — it MUST fail (`AttributeError: mark_uncertain`). If it passes, STOP (anchor wrong).

**1b. Implement** — add to `src/xenon/execution/orders_store.py` immediately after
`mark_terminal` (which ends at `return result.rowcount`). `order_events`, `update`, `insert`,
`get_sync_engine`, `datetime`, `timezone` are already imported at the top of the file.

```python
def mark_uncertain(
    *,
    submission_id: str,
    detail: dict,
    expected_states: tuple[str, ...] = ("PENDING",),
) -> int:
    """Transition a submission to the non-terminal ``UNCERTAIN`` state.

    Used when the place subprocess died/timed out *after IB may have accepted*
    the order but *before* any ``permId`` ack was captured — so the broker
    outcome is genuinely unknown. The ``expected_states`` guard means an order
    that already reached ``WORKING`` (its early ack won the race) is never
    downgraded. Writes an append-only ``AMBIGUOUS_ACK`` event in the same
    transaction. Returns the affected rowcount (0 = guard blocked the write).
    """
    now = datetime.now(timezone.utc)
    engine = get_sync_engine()
    with engine.begin() as conn:
        res = conn.execute(
            update(order_submissions)
            .where(
                order_submissions.c.submission_id == submission_id,
                order_submissions.c.state.in_(expected_states),
            )
            .values(state="UNCERTAIN", reason_code="ORDER_STATUS_UNCERTAIN", updated_at=now)
        )
        if res.rowcount:
            conn.execute(
                insert(order_events).values(
                    submission_id=submission_id,
                    kind="AMBIGUOUS_ACK",
                    detail=detail,
                )
            )
    return res.rowcount
```

**Gate:** `uv run pytest scripts/tests/test_orders_submissions_store.py -x` green.

---

### Step 2 — CLI: `orderRef` + event-driven ack + early ack line

**2a. Failing test** — append to `scripts/tests/test_ib_place_order.py` (it already stubs
`IBClient`; read it first to match its fake-client style). Add a case asserting the CLI sets
`order.orderRef` from `client_attempt_id` and prints a `stage=="ack"` line once `permId`
appears. Sketch (adapt to the file's existing fake):

```python
def test_place_order_sets_orderref_and_emits_ack(monkeypatch, capsys):
    # Fake trade whose order.permId flips to non-zero after the first sleep().
    ...  # build fake IBClient like the existing test; fake trade.order.permId=0 initially
    monkeypatch.setattr(ib_place_order, "IBClient", lambda: fake_client)
    result = ib_place_order.place_order({
        "type": "stock", "symbol": "AAPL", "action": "BUY", "quantity": 1,
        "limitPrice": 210.0, "tif": "DAY", "client_attempt_id": "cid-orderref-1",
    })
    assert fake_order.orderRef == "cid-orderref-1"
    out = capsys.readouterr().out
    ack_lines = [l for l in out.splitlines() if '"stage": "ack"' in l or '"stage":"ack"' in l]
    assert ack_lines, out
    ack = json.loads(ack_lines[0][ack_lines[0].find("{"):])
    assert ack["permId"] == fake_order.permId
```

Run — MUST fail. If it passes, STOP.

**2b. Implement** — `src/xenon/execution/ib_place_order.py`.

(i) Add `import time` under `import sys` (line 14):

```python
import json
import sys
import time
from pathlib import Path
```

(ii) Add module constants under `PORT = DEFAULT_GATEWAY_PORT` (line 26):

```python
# Event-driven ack (OP-11): poll for the openOrder ack (permId) instead of a
# blind fixed sleep. Combos need more headroom — IB routes each leg + risk
# checks. Both deadlines stay well under the route's 15s subprocess timeout.
ACK_WAIT_S = 3.0
ACK_WAIT_COMBO_S = 6.0
ACK_POLL_S = 0.1
```

(iii) In `place_order`, set `orderRef` on the order. Find the `order = LimitOrder(...)` block
(lines 128-134) and append after it (before the `if order_type == "combo":` at line 136):

```python
        # Broker-side correlation key (OP-1 / S3): carries the idempotency id
        # into IB so an orphaned order is re-associable to its reservation.
        order.orderRef = str(params.get("client_attempt_id") or "")
```

(iv) Replace the blind-sleep block. Find (lines 143-153):

```python
        # Place
        trade = client.place_order(contract, order)

        # Combo orders need extra time: IB routes each leg independently and
        # risk checks take longer — 2 s is not enough to get an ack.
        wait_secs = 5 if order_type == "combo" else 2
        client.sleep(wait_secs)

        order_id = trade.order.orderId
        perm_id = trade.order.permId
        accepted_tif = getattr(trade.order, "tif", None) or tif
        status = trade.orderStatus.status if trade.orderStatus else "Unknown"
```

Replace with:

```python
        # Place
        trade = client.place_order(contract, order)

        # Event-driven ack (OP-11) + early ack line (OP-1). Poll the ib_async
        # event loop up to a deadline. The moment permId is known, print an
        # `ack` stage line and FLUSH it, so the parent can persist WORKING + ib
        # ids even if this subprocess is SIGKILLed before the final result line.
        # IMPORTANT: do NOT exit the loop just because the ack arrived — the
        # pre-change code waited a full 2s/5s and then surfaced ib_errors, so a
        # near-immediate reject (Inactive / tick rule) was caught. Keep pumping
        # after the ack until (a) a reject error fires, (b) orderStatus reaches
        # a definitive accepted state, or (c) the deadline — this preserves the
        # existing reject-detection window while still exiting early on a clean
        # acceptance.
        ack_deadline = time.monotonic() + (
            ACK_WAIT_COMBO_S if order_type == "combo" else ACK_WAIT_S
        )
        ack_emitted = False
        while time.monotonic() < ack_deadline:
            client.sleep(ACK_POLL_S)
            if trade.order.permId and not ack_emitted:
                print(
                    json.dumps(
                        {
                            "stage": "ack",
                            "orderId": trade.order.orderId,
                            "permId": trade.order.permId,
                            # getattr guard: test fakes may not define it.
                            "clientId": getattr(client, "_last_client_id", 0),
                            "orderRef": order.orderRef,
                        }
                    ),
                    flush=True,
                )
                ack_emitted = True
            if ib_errors:
                # Reject/error arrived — stop pumping; the existing post-wait
                # ib_errors handling below classifies it exactly as before.
                break
            status_now = trade.orderStatus.status if trade.orderStatus else ""
            if ack_emitted and status_now in ("PreSubmitted", "Submitted"):
                break  # definitive acceptance — safe to exit early

        order_id = trade.order.orderId
        perm_id = trade.order.permId
        accepted_tif = getattr(trade.order, "tif", None) or tif
        status = trade.orderStatus.status if trade.orderStatus else "Unknown"
```

> Note: `ib_errors` is populated by the `_on_error` handler registered a few lines above
> (line 125). It is in scope here. Do not move that registration. The code AFTER this block
> that surfaces `ib_errors` as a `status:"error"` result is unchanged — the loop's `break`
> on `ib_errors` hands off to it exactly as the old fixed sleep did.
>
> **Existing-test compatibility (verified risk):** `scripts/tests/test_ib_place_order.py`'s
> fake client has NO `_last_client_id` (hence the `getattr(..., 0)` guard above) and its fake
> trade starts with a NON-ZERO `permId` — the ack line will therefore be emitted immediately
> in existing tests, and the loop exits via the accepted-status branch or the deadline.
> BEFORE implementing, run `uv run pytest scripts/tests/test_ib_place_order.py -x` to baseline;
> AFTER the change, if any existing case fails, fix the FAKE (add `_last_client_id = 24`, set
> the fake `orderStatus.status` to `"Submitted"` so the loop exits fast, keep `sleep()` a
> no-op) — never weaken the production code to accommodate a fake. A fake whose `sleep()` is a
> no-op makes the deadline loop spin ~30 iterations of pure Python (3s wall) — if that slows
> the suite, set the fake status to `"Submitted"` (early exit) rather than shrinking ACK_WAIT_S.

The final result line is still printed by `main()` via `print(json.dumps(result))` — a JSON
dict with a `"status"` key, which the parent distinguishes from the `stage=="ack"` line.

**Gate:** `uv run pytest scripts/tests/test_ib_place_order.py scripts/tests/test_ib_place_order_contract.py -x` green.

---

### Step 3 — Staged subprocess runner `run_place_subprocess`

**3a. Failing test** — new file `scripts/tests/test_run_place_subprocess.py`. Point
`VENV_BIN` at a tmp dir holding a fake `xenon-ib-place-order` executable that emits scripted
stages, then assert the returned `ScriptResult` fields. Full test:

```python
"""run_place_subprocess must recover an early ack line even when the CLI is
SIGKILLed before printing its final result (OP-1)."""
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from xenon.api import subprocess as sp


def _write_fake_cli(tmp_path: Path, body: str) -> Path:
    """Drop an executable fake `xenon-ib-place-order` into a fake .venv/bin."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "xenon-ib-place-order"
    script.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return bin_dir


@pytest.mark.asyncio
async def test_clean_success_returns_data(monkeypatch, tmp_path):
    bin_dir = _write_fake_cli(tmp_path, """
        import json, sys
        print(json.dumps({"stage": "ack", "orderId": 111, "permId": 222, "clientId": 24}), flush=True)
        print(json.dumps({"status": "ok", "orderId": 111, "permId": 222, "initialStatus": "Submitted"}))
    """)
    monkeypatch.setattr(sp, "VENV_BIN", bin_dir)
    res = await sp.run_place_subprocess("xenon-ib-place-order", ["--json", "{}"], timeout=10)
    assert res.ok is True
    assert res.ambiguous is False
    assert res.data["status"] == "ok"
    assert res.ack == {"stage": "ack", "orderId": 111, "permId": 222, "clientId": 24}


@pytest.mark.asyncio
async def test_ack_then_hang_is_ambiguous_with_ack(monkeypatch, tmp_path):
    bin_dir = _write_fake_cli(tmp_path, """
        import json, sys, time
        print(json.dumps({"stage": "ack", "orderId": 111, "permId": 222, "clientId": 24}), flush=True)
        time.sleep(30)  # never prints a result; parent must SIGKILL
    """)
    monkeypatch.setattr(sp, "VENV_BIN", bin_dir)
    res = await sp.run_place_subprocess("xenon-ib-place-order", ["--json", "{}"], timeout=1)
    assert res.ok is False
    assert res.ambiguous is True
    assert res.ack is not None and res.ack["permId"] == 222
    assert res.data is None


@pytest.mark.asyncio
async def test_timeout_no_output_is_ambiguous_no_ack(monkeypatch, tmp_path):
    bin_dir = _write_fake_cli(tmp_path, """
        import time
        time.sleep(30)
    """)
    monkeypatch.setattr(sp, "VENV_BIN", bin_dir)
    res = await sp.run_place_subprocess("xenon-ib-place-order", ["--json", "{}"], timeout=1)
    assert res.ok is False and res.ambiguous is True and res.ack is None


@pytest.mark.asyncio
async def test_garbage_stdout_exit0_is_ambiguous_no_ack(monkeypatch, tmp_path):
    bin_dir = _write_fake_cli(tmp_path, """
        print("not json at all")
    """)
    monkeypatch.setattr(sp, "VENV_BIN", bin_dir)
    res = await sp.run_place_subprocess("xenon-ib-place-order", ["--json", "{}"], timeout=10)
    assert res.ok is False and res.ambiguous is True and res.ack is None


@pytest.mark.asyncio
async def test_reject_result_returns_ok_with_error_data(monkeypatch, tmp_path):
    bin_dir = _write_fake_cli(tmp_path, """
        import json
        print(json.dumps({"status": "error", "code": 110, "message": "tick"}))
    """)
    monkeypatch.setattr(sp, "VENV_BIN", bin_dir)
    res = await sp.run_place_subprocess("xenon-ib-place-order", ["--json", "{}"], timeout=10)
    assert res.ok is True and res.ambiguous is False
    assert res.data["status"] == "error" and res.data["code"] == 110
```

> `pytest-asyncio` is a dev dep (`src/xenon/CLAUDE.md`). If `@pytest.mark.asyncio` needs
> `asyncio_mode`, mirror an existing async test file (e.g. any `test_*` using
> `@pytest.mark.asyncio` — grep `scripts/tests`). Do not add new pytest config.

Run — MUST fail (`AttributeError: run_place_subprocess` / `ScriptResult.ambiguous`).

**3b. Implement** — `src/xenon/api/subprocess.py`.

(i) Add the two new `ScriptResult` fields (extend the dataclass at line 46):

```python
@dataclass
class ScriptResult:
    ok: bool
    data: Optional[dict] = None
    error: Optional[str] = None
    exit_code: Optional[int] = None
    ack: Optional[dict] = None        # early {"stage":"ack",...} line, if captured
    ambiguous: bool = False           # subprocess spawned but returned no final result
```

(ii) Add `run_place_subprocess` after `run_entry_point` (before `run_module`):

```python
async def run_place_subprocess(
    entry: str,
    args: Optional[List[str]] = None,
    timeout: float = 30.0,
    cwd: Optional[str] = None,
) -> ScriptResult:
    """Run the place CLI, streaming stdout line-by-line so an early ack line
    survives a timeout/SIGKILL.

    Unlike ``run_entry_point`` (which uses ``communicate()`` and loses all
    partial output on timeout), this parses each stdout line as it arrives:

    - a ``{"stage": "ack", ...}`` line  → captured into ``result.ack``
    - any JSON dict with a ``"status"`` key → captured as the final ``result.data``

    Outcomes:
    - final result line seen  → ``ok=True``, ``data=<result>``, ``ambiguous=False``
      (a ``status=="error"`` reject is still ``ok=True`` so the caller classifies it,
      matching ``run_entry_point``'s structured-error convention).
    - no final result line    → ``ok=False``, ``ambiguous=True``; ``ack`` set iff an ack
      line was captured before the process died. This is the OP-1 ambiguous case.
    """
    entry_path = VENV_BIN / entry
    if not entry_path.exists():
        return ScriptResult(ok=False, error=f"Entry point not found: {entry}")

    cmd = [str(entry_path)] + (args or [])
    work_dir = cwd or str(PROJECT_ROOT)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir,
        )
    except Exception as exc:  # spawn failure — order definitively NOT sent
        return ScriptResult(ok=False, error=f"Failed to spawn {entry}: {exc}")

    ack: Optional[dict] = None
    result: Optional[dict] = None
    stderr_chunks: List[bytes] = []

    async def _read_stdout() -> None:
        nonlocal ack, result
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            brace = line.find("{")
            if brace == -1:
                continue
            try:
                obj = json.loads(line[brace:])
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("stage") == "ack":
                ack = obj
            elif "status" in obj:
                result = obj

    async def _read_stderr() -> None:
        assert proc.stderr is not None
        async for raw in proc.stderr:
            stderr_chunks.append(raw)

    try:
        await asyncio.wait_for(
            asyncio.gather(_read_stdout(), _read_stderr()), timeout=timeout
        )
        await proc.wait()
        timed_out = False
    except asyncio.TimeoutError:
        timed_out = True
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
    except asyncio.CancelledError:
        # The FastAPI request task was cancelled (client disconnect / shutdown).
        # Never leave an orphan place subprocess running against the gateway —
        # kill it, reap it, then propagate the cancellation.
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        raise

    if result is not None:
        # Clean final line — success or a classified reject. Mirror
        # run_entry_point: structured status → ok=True regardless of exit code.
        return ScriptResult(
            ok=True, data=result, ack=ack, exit_code=proc.returncode
        )

    # No final result line: genuinely ambiguous. The order MAY be live at IB.
    stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    if timed_out:
        err = f"Place subprocess timed out after {timeout}s before returning a result"
    else:
        err = _extract_error_message(
            "", stderr, "Place subprocess ended before returning a result"
        )
    logger.error("run_place_subprocess ambiguous (%s): ack=%s", err, ack is not None)
    return ScriptResult(
        ok=False, error=err, ack=ack, ambiguous=True, exit_code=proc.returncode
    )
```

**Gate:** `uv run pytest scripts/tests/test_run_place_subprocess.py -x` green.

---

### Step 4 — `_run_ib_script_with_recovery`: pluggable runner

The place handler must route through `run_place_subprocess` while every other caller keeps
`run_entry_point`. Add an optional `runner` param; do NOT change any other call site.

**4a.** Edit `src/xenon/api/server.py`.

(i) Import (line 64):

```python
from xenon.api.subprocess import ScriptResult, run_entry_point, run_module, run_place_subprocess
```

(ii) Change the signature of `_run_ib_script_with_recovery` (line 3120):

```python
async def _run_ib_script_with_recovery(
    entry: str, args: list, timeout: float = 30, runner=run_entry_point
) -> ScriptResult:
```

(iii) Replace **both** internal calls to `run_entry_point(entry, args, timeout=timeout)`
inside that function (there are exactly two — the primary call ~line 3164 and the
post-restart retry ~line 3218) with `runner(entry, args, timeout=timeout)`. Verify with:

```bash
grep -n "run_entry_point(entry, args, timeout=timeout)" src/xenon/api/server.py
```

Expect **0** matches after the edit (both replaced). If you see any other call site elsewhere
that you did not intend to change, STOP — only the two inside `_run_ib_script_with_recovery`
change.

> No new test for this step alone — it is exercised by the route tests in Step 6. A wrong
> edit here fails those.

---

### Step 5 — Place handler: ack → WORKING, ambiguous → UNCERTAIN

**5a. Implement** — `src/xenon/api/server.py`, `_orders_place_from_body`. Replace the block
from `order_json = json.dumps(body)` (line 2276) through `return result.data` (line 2332)
with:

```python
    order_json = json.dumps(body)
    result = await _run_ib_script_with_recovery(
        "xenon-ib-place-order",
        ["--json", order_json],
        timeout=15,
        runner=run_place_subprocess,
    )

    # (OP-1) Persist the early ack the instant we have it — even if the
    # subprocess later died before its final result line. A captured `permId`
    # is IB's openOrder acknowledgement: the order is live, so the row belongs
    # in WORKING with real ib ids, never FAILED.
    if result.ack:
        orders_store.mark_submitted(
            submission_id=submission_id,
            ib_order_id=str(result.ack.get("orderId") or ""),
            perm_id=str(result.ack.get("permId") or ""),
            placing_client_id=int(result.ack.get("clientId") or 26),
        )
        try:
            orders_store.record_event(
                submission_id,
                "IB_ACK",
                {
                    "orderId": result.ack.get("orderId"),
                    "permId": result.ack.get("permId"),
                    "clientId": result.ack.get("clientId"),
                    "orderRef": result.ack.get("orderRef"),
                },
            )
        except Exception:  # pragma: no cover — event writes are best-effort
            logger.warning("Failed to record IB_ACK event for %s", submission_id, exc_info=True)

    if not result.ok:
        if result.ambiguous:
            if result.ack is not None:
                # Died AFTER the ack — order is live and already WORKING. Return
                # an accepted response; fills/terminal status are picked up by
                # the poller/rehydrate. No FAILED, no duplicate-order prompt.
                return {
                    "status": "ok",
                    "orderId": result.ack.get("orderId"),
                    "permId": result.ack.get("permId"),
                    "initialStatus": "Submitted",
                    "message": "Order accepted; final confirmation pending reconciliation.",
                    "submission_id": submission_id,
                }
            # Spawned but NO ack and NO result — broker outcome unknown. Do NOT
            # mark terminal (the order may be live). UNCERTAIN + 502 "do not
            # resubmit". expected_states guards against clobbering a WORKING row.
            orders_store.mark_uncertain(
                submission_id=submission_id,
                detail={"reason": "subprocess returned no result", "error": result.error},
                expected_states=("PENDING",),
            )
            return JSONResponse(
                status_code=502,
                content={
                    "detail": "Broker outcome unknown — reconciling. Do NOT resubmit.",
                    "reason_code": ReasonCode.ORDER_STATUS_UNCERTAIN.value,
                    "reason_detail": result.error,
                    "submission_id": submission_id,
                },
            )
        # Not ambiguous → never spawned (cooldown / gateway down) or hard
        # connection failure: the order was definitively NOT sent. Keep today's
        # terminal FAILED (retryable). Guard so a late ack that flipped the row
        # to WORKING is never clobbered.
        orders_store.mark_terminal(
            submission_id=submission_id,
            state="FAILED",
            reason_code="SUBPROCESS_ERROR",
            filled_qty=0,
            avg_fill_price=None,
            expected_states=("PENDING",),
        )
        raise HTTPException(status_code=502, detail=result.error)

    if result.data and result.data.get("status") == "error":
        # B6 — IB reject. reason_code the UI toast map can resolve; raw IB code
        # + message go into the orders_events audit row.
        ib_code = result.data.get("code")
        ib_message = result.data.get("message", "Order failed")
        if str(ib_code) == "110":
            reason_code = ReasonCode.LIMIT_OFF_TICK.value
            logger.warning(
                "IB rejected order with tick-rule violation: submission=%s ib_code=110 ib_message=%s",
                submission_id,
                ib_message,
            )
        else:
            reason_code = ReasonCode.IB_REJECT.value
        orders_store.mark_terminal(
            submission_id=submission_id,
            state="REJECTED",
            reason_code=reason_code,
            filled_qty=0,
            avg_fill_price=None,
            expected_states=("PENDING", "WORKING"),
        )
        try:
            orders_store.record_event(
                submission_id, "IB_REJECT", {"ib_code": ib_code, "ib_message": ib_message}
            )
        except Exception:  # pragma: no cover — event writes are best-effort
            logger.warning(
                "Failed to record IB_REJECT event for submission %s", submission_id, exc_info=True
            )
        raise HTTPException(status_code=502, detail=ib_message)

    if result.data and not result.ack:
        # Clean final result and no ack line was captured — persist WORKING now.
        # (If ack was present it already set WORKING above with the same ids.)
        orders_store.mark_submitted(
            submission_id=submission_id,
            ib_order_id=str(result.data.get("orderId") or ""),
            perm_id=str(result.data.get("permId") or ""),
            placing_client_id=int(result.data.get("clientId") or 26),
        )
    return result.data
```

> The ack values (`orderId`/`permId`) come straight from `trade.order.orderId/permId` (ints)
> in the ack JSON, so they are returned as-is — no coercion helper needed (there is no
> `_int_or_none` in `server.py`; do not invent one).

The reject and FAILED writes now carry `expected_states=` (defense against clobbering a
WORKING row an ack created) — a scoped-down slice of OP-8, justified because this handler is
the one place where an ack and a later reject/failure can race.

---

### Step 6 — Route tests with the fake place CLI (TS-1, the highest-value gate)

**6a.** New file `scripts/tests/test_orders_place_uncertain_route.py`. These run with test mode
**OFF** so the real staged runner + fake CLI execute, but the IB health checks and gates are
bypassed so no live IB is touched. Mark `committed_db` (place path uses its own engine).

```python
"""OP-1 route coverage: SIGKILL/timeout after IB may have accepted must NOT
produce a terminal FAILED row; ack-then-kill → WORKING with ids; no ack → UNCERTAIN.

No live IB: a fake `xenon-ib-place-order` CLI is dropped into a fake .venv/bin and
VENV_BIN is monkeypatched; the gateway health check and preflight/quote gates are
stubbed to accept."""
import stat
import sys
import textwrap
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.committed_db

REAL_AAPL_LIMIT = 210.0  # frozen real-ish AAPL level, authoring date 2026-07-05


def _write_fake_cli(tmp_path: Path, body: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "xenon-ib-place-order"
    script.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return bin_dir


@pytest.fixture
def client(monkeypatch):
    # Test mode OFF → real subprocess path. Everything else stubbed to accept.
    monkeypatch.delenv("XENON_API_TEST_MODE", raising=False)
    from xenon.api import server
    # Bypass IB health pre-check so run_place_subprocess actually spawns.
    monkeypatch.setattr(server, "_pool_has_any_connection", lambda: True)
    monkeypatch.setattr(server, "_ib_last_failure", 0.0, raising=False)

    # Stub the two gates to accept (they are exercised by their own suites).
    class _Accept:
        accept = True
        reason_code = None
        reason_detail = None

    async def _ok_preflight(body, cover_ratio=1.0):
        return _Accept()

    async def _ok_quote(body):
        return _Accept(), 200

    monkeypatch.setattr(server, "_run_preflight", _ok_preflight)
    monkeypatch.setattr(server, "_validate_non_combo_quote", _ok_quote)
    return TestClient(server.app)


def _place_body(cid: str) -> dict:
    return {
        "type": "stock", "symbol": "AAPL", "action": "BUY", "quantity": 1,
        "limitPrice": REAL_AAPL_LIMIT, "con_id": 265598, "client_attempt_id": cid,
    }


def _row(sid: str) -> dict:
    from xenon.db.engine import get_sync_engine
    from xenon.db.schema import order_submissions
    from sqlalchemy import select
    with get_sync_engine().connect() as conn:
        r = conn.execute(
            select(order_submissions).where(order_submissions.c.submission_id == sid)
        ).first()
    return dict(r._mapping)


def test_ack_then_kill_ends_working_with_ids(client, monkeypatch, tmp_path):
    from xenon.api import subprocess as sp
    bin_dir = _write_fake_cli(tmp_path, """
        import json, time
        print(json.dumps({"stage": "ack", "orderId": 555, "permId": 777, "clientId": 24}), flush=True)
        time.sleep(30)
    """)
    monkeypatch.setattr(sp, "VENV_BIN", bin_dir)
    # The fake CLI sleeps 30s and the route passes timeout=15 — too slow for a
    # unit test. Wrap _run_ib_script_with_recovery to force timeout=1 so the
    # SIGKILL fires fast. (The wrapper preserves the runner= plumbing.)
    from xenon.api import server
    real = server._run_ib_script_with_recovery
    async def _fast(entry, args, timeout=30, runner=None):
        return await real(entry, args, timeout=1, runner=runner)
    monkeypatch.setattr(server, "_run_ib_script_with_recovery", _fast)

    resp = client.post("/orders/place", json=_place_body("cid-ack-kill-1"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    sid = body["submission_id"]
    row = _row(sid)
    assert row["state"] == "WORKING", row
    assert str(row["ib_order_id"]) == "555"
    assert str(row["perm_id"]) == "777"
    # No FAILED anywhere for this row.
    assert row["reason_code"] != "SUBPROCESS_ERROR"


def test_timeout_no_ack_ends_uncertain_502(client, monkeypatch, tmp_path):
    from xenon.api import subprocess as sp
    from xenon.api import server
    bin_dir = _write_fake_cli(tmp_path, """
        import time
        time.sleep(30)
    """)
    monkeypatch.setattr(sp, "VENV_BIN", bin_dir)
    real = server._run_ib_script_with_recovery
    async def _fast(entry, args, timeout=30, runner=None):
        return await real(entry, args, timeout=1, runner=runner)
    monkeypatch.setattr(server, "_run_ib_script_with_recovery", _fast)

    resp = client.post("/orders/place", json=_place_body("cid-uncertain-1"))
    assert resp.status_code == 502, resp.text
    body = resp.json()
    assert body["reason_code"] == "ORDER_STATUS_UNCERTAIN"
    row = _row(body["submission_id"])
    assert row["state"] == "UNCERTAIN"
    assert row["reason_code"] == "ORDER_STATUS_UNCERTAIN"


def test_garbage_stdout_ends_uncertain_502(client, monkeypatch, tmp_path):
    from xenon.api import subprocess as sp
    from xenon.api import server
    bin_dir = _write_fake_cli(tmp_path, """
        print("garbage not json")
    """)
    monkeypatch.setattr(sp, "VENV_BIN", bin_dir)
    real = server._run_ib_script_with_recovery
    async def _fast(entry, args, timeout=30, runner=None):
        return await real(entry, args, timeout=5, runner=runner)
    monkeypatch.setattr(server, "_run_ib_script_with_recovery", _fast)
    resp = client.post("/orders/place", json=_place_body("cid-garbage-1"))
    assert resp.status_code == 502
    assert resp.json()["reason_code"] == "ORDER_STATUS_UNCERTAIN"


def test_clean_reject_code110_maps_limit_off_tick(client, monkeypatch, tmp_path):
    from xenon.api import subprocess as sp
    from xenon.api import server
    bin_dir = _write_fake_cli(tmp_path, """
        import json
        print(json.dumps({"status": "error", "code": 110, "message": "tick rule"}))
    """)
    monkeypatch.setattr(sp, "VENV_BIN", bin_dir)
    resp = client.post("/orders/place", json=_place_body("cid-reject-110"))
    assert resp.status_code == 502
    # detail carries the IB message; the row is REJECTED with LIMIT_OFF_TICK.
    from xenon.db.engine import get_sync_engine
    from xenon.db.schema import order_submissions
    from sqlalchemy import select
    with get_sync_engine().connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(
            select(order_submissions).where(order_submissions.c.client_attempt_id == "cid-reject-110")
        ).all()]
    assert rows and rows[0]["state"] == "REJECTED"
    assert rows[0]["reason_code"] == "LIMIT_OFF_TICK"


def test_clean_success_ends_working(client, monkeypatch, tmp_path):
    from xenon.api import subprocess as sp
    bin_dir = _write_fake_cli(tmp_path, """
        import json
        print(json.dumps({"stage": "ack", "orderId": 900, "permId": 901, "clientId": 24}), flush=True)
        print(json.dumps({"status": "ok", "orderId": 900, "permId": 901, "initialStatus": "Submitted"}))
    """)
    monkeypatch.setattr(sp, "VENV_BIN", bin_dir)
    resp = client.post("/orders/place", json=_place_body("cid-ok-1"))
    assert resp.status_code == 200, resp.text
```

> **Verify the stub attribute names** the handler reads: `_run_preflight` returns an object
> with `.accept` / `.reason_code` / `.reason_detail`; `_validate_non_combo_quote` returns
> `(verdict, status_int)` where verdict has `.accept` / `.reason_code` / `.reason_detail`
> (server.py:2169-2202). If your `_Accept` needs `.reason_code` to be a `ReasonCode` (it reads
> `.value`), guard: the accept path never touches `.value` because `verdict.accept` is True and
> the code short-circuits — confirm by reading lines 2170-2202 before finalizing.

**Gate:** `uv run pytest scripts/tests/test_orders_place_uncertain_route.py -x` green.

If `test_ack_then_kill_ends_working_with_ids` shows `state == "FAILED"` → the OP-1 bug is not
fixed; STOP and re-check Steps 3–5.

---

### Step 7 — Reason code registration + parity (Python + TS)

**7a.** `src/xenon/execution/preflight.py` — add to the `ReasonCode` enum after
`SUBPROCESS_ERROR = "SUBPROCESS_ERROR"` (line 61):

```python
    # OP-1 — broker outcome unknown after subprocess timeout/kill (no ack).
    ORDER_STATUS_UNCERTAIN = "ORDER_STATUS_UNCERTAIN"
```

**7b.** `scripts/tests/test_preflight_reason_codes.py` — add to `test_new_reason_codes_present`:

```python
    # OP-1 — ambiguous broker outcome
    assert "ORDER_STATUS_UNCERTAIN" in names
```

**7c.** `web/lib/orderReasonCodes.ts` — add to `ORDER_REASON_CODES` (after `SUBPROCESS_ERROR`):

```typescript
  // OP-1 — broker outcome unknown after place subprocess timeout/kill.
  ORDER_STATUS_UNCERTAIN: {
    severity: "warn",
    copy: "Broker outcome unknown — reconciling. Do NOT resubmit.",
  },
```

**7d.** `web/tests/order-reason-codes.test.ts` — add to the `PYTHON_REASON_CODES` array
(after `"SUBPROCESS_ERROR"`):

```typescript
  // OP-1 — ambiguous broker outcome
  "ORDER_STATUS_UNCERTAIN",
```

**Gate:**

```bash
uv run pytest scripts/tests/test_preflight_reason_codes.py -x
cd web && npm test -- order-reason-codes
```

---

### Step 8 — Make `UNCERTAIN` visible in the UI **and counted by preflight risk**

**8a-0. SAFETY-CRITICAL — count `UNCERTAIN` in preflight working reservations.**
`orders_store._ACTIVE_STATES` (`orders_store.py:851`, `("PENDING", "WORKING",
"PARTIALLY_FILLED")`) feeds `working_reservations_for(...)`, which `_run_preflight` uses for
naked-short/coverage aggregation (`server.py:~1784,~1803`). An `UNCERTAIN` order **may be live
at IB** — if it is not counted, a follow-up order can pass preflight while the ambiguous order
is actually resting, under-counting short exposure. Change:

```python
_ACTIVE_STATES = ("PENDING", "WORKING", "PARTIALLY_FILLED", "UNCERTAIN")
```

with a one-line comment: `# UNCERTAIN may be live at IB — must count toward coverage (OP-1)`.

**Regression test** — append to the Step-1 test file (or the preflight reservations test file
if one exists — grep `working_reservations_for` in `scripts/tests` and follow its seeding
style): seed a row, `mark_uncertain` it, call `orders_store.working_reservations_for(...)` for
that user/ticker/scope, and assert the UNCERTAIN row's quantity is included in the aggregate
exactly as a WORKING row's would be.

**8a.** `src/xenon/api/routes/orders.py`:

(i) Add `UNCERTAIN` to `ACTIVE_STATES` (line 22) so the row is returned by
`orders_payload_for_scope`:

```python
ACTIVE_STATES = {"PENDING", "WORKING", "PARTIALLY_FILLED", "UNCERTAIN"}
```

(ii) Add a display mapping in `_status_from_state` (line 98):

```python
def _status_from_state(state: str) -> str:
    return {
        "PENDING": "PendingSubmit",
        "WORKING": "Submitted",
        "PARTIALLY_FILLED": "PartiallyFilled",
        "UNCERTAIN": "Uncertain",
    }.get(state, state)
```

**8b.** `src/xenon/db/queries/orders.py` — add `UNCERTAIN` to the per-ticker
`working_orders_for` filter (line 211) so ticker-scoped open-order reads also surface it:

```python
        order_submissions.c.state.in_(["PENDING", "WORKING", "PARTIALLY_FILLED", "UNCERTAIN"]),
```

**8c. Backend test** — new `scripts/tests/test_orders_payload_uncertain_visible.py`
(`committed_db`): seed one `UNCERTAIN` row via `orders_store.reserve_attempt` +
`orders_store.mark_uncertain`, call `orders_payload_for_scope(scope)`, assert the row appears
in `open_orders` with `status == "Uncertain"`. Mirror the scope/seed style of
`scripts/tests/test_orders_list_pg.py` (read it first for the exact `AccountScope` construction
and seeding helpers). Do NOT hit the network.

**8d. Frontend — status label + badge (SHARED helper — multiple surfaces).** Raw status
strings render at **five** verified sites: `web/components/WorkspaceSections.tsx` ~1818
(`o.status`) and ~1933 (`o.order.status`), `web/components/ticker-detail/OrderTab.tsx` ~199,
`web/components/ticker-detail/BookTab.tsx` ~353, and `web/components/CancelOrderDialog.tsx`
~51. Create the helper ONCE in a new shared module `web/components/orderStatusBadge.tsx` and
import it at all five sites (do not copy-paste it per component):

```tsx
// web/components/orderStatusBadge.tsx
export function renderOrderStatus(status: string) {
  if (status === "Uncertain" || status === "UNCERTAIN") {
    return (
      <span
        className="status-uncertain"
        title="Broker outcome unknown — reconciling. Do NOT resubmit."
      >
        UNCERTAIN
      </span>
    );
  }
  return status;
}
```

Call sites — replace ONLY the raw status expression, nothing around it:

- `WorkspaceSections.tsx` ~1818: `o.status` (final branch of the ternary) →
  `renderOrderStatus(o.status)`; ~1933: `o.order.status` →
  `renderOrderStatus(o.order.status)`. Do not alter the `isPendingCancel` /
  `isPendingModify` branches.
- `ticker-detail/OrderTab.tsx` ~199 and `ticker-detail/BookTab.tsx` ~353: read the
  surrounding JSX first, then wrap the raw status expression the same way.
- `CancelOrderDialog.tsx` ~51 (`{order.status}` in the Status detail row) → wrap the same way.
  (A user should see the warning even in the cancel dialog — an UNCERTAIN order has no
  reliable ib ids, so a cancel attempt may 404; the title text explains why.)

> Read the two ternaries first to place `renderOrderStatus(...)` exactly in the else-branch —
> do not wrap the whole ternary.

**8e. Badge styling.** Add a `.status-uncertain` rule to the same stylesheet that already
defines `.status-cancelling` / `.status-modifying`. Find it:

```bash
grep -rn "status-cancelling" web/ | grep -v node_modules
```

Add, in that file, next to `.status-cancelling`, using existing design tokens only (no raw
hex — brand rule; reuse whatever warn/amber token the codebase already uses, e.g. the token
used by `LIMIT_OUT_OF_BAND` warn toasts — grep for a `warn` color token):

```css
.status-uncertain {
  color: var(--<warn-token-you-found>);
  font-weight: 600;
}
```

> STOP and report if you cannot find an existing warn/amber design token — do not introduce a
> raw hex value (brand rule, `web/CLAUDE.md`).

**8f. Frontend test** — `web/tests/order-status-uncertain.test.tsx` (or extend an existing
open-orders render test): render the open-orders table with one order whose `status ===
"Uncertain"`; assert the DOM shows text `UNCERTAIN` and the element carries the
`status-uncertain` class + the "Do NOT resubmit" title. Follow the render/setup pattern of an
existing `WorkspaceSections`/orders component test (grep `web/tests` for one that renders open
orders).

**Gate:**

```bash
uv run pytest scripts/tests/test_orders_payload_uncertain_visible.py -x
cd web && npm test -- order-status-uncertain
cd web && npx tsc --noEmit
cd web && npm run lint
```

---

### Step 9 — Docs + CHANGELOG + incident history

- `CHANGELOG.md` under `## [Unreleased]`:

  ```markdown
  ### Fixed

  - **Order placement no longer records a false terminal `FAILED` when the place
    subprocess times out or is killed after IB may have accepted the order (OP-1,
    Critical).** The place CLI now sets `order.orderRef = client_attempt_id`, emits
    an early `ack` line the moment IB acknowledges (`permId`), and waits
    event-driven instead of a blind 2s/5s sleep (OP-11). The parent persists that
    ack as `WORKING` with real ib ids even if the process later dies; a
    genuinely-ambiguous outcome (spawned, no ack, no result) becomes a new
    non-terminal `UNCERTAIN` state and returns HTTP 502 `ORDER_STATUS_UNCERTAIN`
    ("Do NOT resubmit"), preventing duplicate live orders on retry. `UNCERTAIN`
    rows are visible in the open-orders panel.
  ```

- `docs/reference/order-path-incident-history.md` — append the row in §11.
- `src/xenon/api/CLAUDE.md` — under the place-path notes, add a one-line note that the place
  path uses `run_place_subprocess` (staged) and that ack→WORKING / no-ack→UNCERTAIN.
  (Keep it short; do not restructure the file.)

---

## 6. Verification matrix (MANDATORY — exact commands + expected outcomes)

### Unit (Python)

| Command                                                                                               | Expected                                                                                                                                           |
| ----------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `uv run pytest scripts/tests/test_orders_submissions_store.py -x`                                     | pass — `mark_uncertain` transitions PENDING→UNCERTAIN, guard blocks WORKING                                                                        |
| `uv run pytest scripts/tests/test_ib_place_order.py scripts/tests/test_ib_place_order_contract.py -x` | pass — orderRef set, ack line emitted                                                                                                              |
| `uv run pytest scripts/tests/test_run_place_subprocess.py -x`                                         | pass — all 5 cases (success/ack-hang/timeout/garbage/reject)                                                                                       |
| `uv run pytest scripts/tests/test_orders_place_uncertain_route.py -x`                                 | pass — ack-kill→WORKING w/ ids `555/777`; no-ack→502 `ORDER_STATUS_UNCERTAIN` + row UNCERTAIN; garbage→502; 110→REJECTED/LIMIT_OFF_TICK; clean→200 |
| `uv run pytest scripts/tests/test_orders_payload_uncertain_visible.py -x`                             | pass — UNCERTAIN row in `open_orders`, `status == "Uncertain"`                                                                                     |
| `uv run pytest scripts/tests/test_preflight_reason_codes.py -x`                                       | pass — `ORDER_STATUS_UNCERTAIN in names`                                                                                                           |
| `uv run pytest scripts/tests/test_orders_place_no_regime_gate.py -x`                                  | pass — existing place-path regression still green                                                                                                  |
| the Step-8a-0 preflight-reservation regression (file per 8a-0)                                        | pass — an UNCERTAIN row's quantity counts in `working_reservations_for` like a WORKING row's                                                       |

### Unit (web)

| Command                                        | Expected                                                                          |
| ---------------------------------------------- | --------------------------------------------------------------------------------- |
| `cd web && npm test -- order-reason-codes`     | pass — Py↔TS parity incl. `ORDER_STATUS_UNCERTAIN` (missingInTs=[], extraInTs=[]) |
| `cd web && npm test -- order-status-uncertain` | pass — badge text `UNCERTAIN` + `status-uncertain` class + title                  |

### Typecheck / lint (web touched)

| `cd web && npx tsc --noEmit` | exit 0 |
| `cd web && npm run lint` | exit 0 |

### Scoped suite

| `uv run python scripts/infra/dev/run_pytest_affected.py` | all green |

### CI order-path guards (all three, expect exit 0)

```bash
uv run python scripts/checks/no_json_fallback_on_order_path.py
uv run python scripts/checks/no_json_write_on_order_path.py
uv run python scripts/checks/order_path_caller_allowlist.py
```

Expect each: exit 0. (No new JSON reads/writes; `run_place_subprocess` still invokes the
allowlisted `xenon-ib-place-order` via the same subprocess helper — the caller allowlist
guards the _module import_ of `ib_place_order`, which is unchanged.) If
`order_path_caller_allowlist.py` fails, STOP — you likely imported `ib_place_order` somewhere
new; you must not.

### E2E browser (UI-visible change) — seeded UNCERTAIN row (PAPER stack)

1. Start paper stack: `scripts/infra/dev.sh paper` (Next :3200, FastAPI :8421). Wait for
   `curl -s http://localhost:8421/health` → JSON with `"ib_gateway"` present.
2. Read the paper scope the stack is using:
   `curl -s http://localhost:8421/health | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('account'), d.get('trading_mode'))"`
   — note the `broker_account` value (the paper account id). It is also `XENON_BROKER_ACCOUNT`
   in the running `dev.sh` env.
3. Seed one UNCERTAIN row into `core_test` (LOCAL paper DB — `DATABASE_URL_PAPER`, i.e.
   `127.0.0.1/core_test`, per memory `two_core_test_dbs`). Replace `<ACCT>` with the value
   from step 2:
   ```bash
   psql "$DATABASE_URL_PAPER" -c "INSERT INTO xenon.order_submissions
     (submission_id, user_id, client_attempt_id, ticker, security_type, action, quantity,
      limit_price, state, reason_code, tif, submitted_at, broker, account_env, broker_account)
     VALUES ('uncertain-e2e-1','local','uncertain-e2e-1','AAPL','STK','BUY',1,210.00,
      'UNCERTAIN','ORDER_STATUS_UNCERTAIN','DAY', now(), 'IB','paper','<ACCT>');"
   ```
4. Load `http://localhost:3200`, navigate to the workspace open-orders panel (or the AAPL
   ticker Orders tab). Confirm a row for AAPL with a visible **UNCERTAIN** badge.
5. Screenshot to `output/playwright/uncertain-badge-2026-07-05.png`. On-screen assertion: the
   status cell text is `UNCERTAIN` and hovering shows "Broker outcome unknown — reconciling.
   Do NOT resubmit."
6. Cleanup: `psql "$DATABASE_URL_PAPER" -c "DELETE FROM xenon.order_submissions WHERE submission_id='uncertain-e2e-1';"`

> If the paper stack cannot start (IB Gateway 2FA / offline), the seeded-row browser check is
> **deferred, not skipped** — report it as pending and rely on the frontend Vitest
> (`order-status-uncertain`) as the mandatory automated proof that the badge renders. The
> offline route/unit tests above are the hard gate.

### Live paper SIGKILL-after-ack drill (OPTIONAL — paper only, IB port 4002)

Only if a paper IB Gateway is connected. This exercises the real CLI end-to-end.

1. `scripts/infra/dev.sh paper`; confirm `/health` shows `ib_gateway.port_listening: true`.
2. Place a **far-from-market limit** BUY (won't fill) on a liquid paper name (e.g. AAPL) via
   the UI so an order id exists.
3. To force the ack-then-kill path deterministically, temporarily set the route's place
   `timeout` lower than the ack settle by editing nothing in prod — instead, run the drill as
   a scripted subprocess kill: in a second shell, immediately after submit,
   `pkill -9 -f xenon-ib-place-order` within ~1s of the ack. (Racy — acceptable for a manual
   OPTIONAL drill.)
4. Assert in DB: `psql "$DATABASE_URL_PAPER" -c "SELECT state, ib_order_id, perm_id,
reason_code FROM xenon.order_submissions WHERE ticker='AAPL' ORDER BY submitted_at DESC
LIMIT 1;"` → **state = WORKING**, non-null `ib_order_id`/`perm_id`, `reason_code` NOT
   `SUBPROCESS_ERROR`.
5. Cancel the order in the UI to clean up. **Never leave a live paper order resting.**

This drill is OPTIONAL because it needs a connected paper gateway and a timing race; the
mandatory equivalent is the deterministic offline route test
`test_ack_then_kill_ends_working_with_ids` (Step 6).

### Negative direction (both directions covered)

- **Never-spawned still FAILED (retryable):** covered implicitly — if
  `_pool_has_any_connection()` is False and the gateway is down,
  `_run_ib_script_with_recovery` returns a non-ambiguous `ScriptResult(ok=False)` → the FAILED
  branch. Add an assertion in `test_orders_place_uncertain_route.py` if trivially seedable by
  monkeypatching `server._pool_has_any_connection` → `lambda: False` and
  `server.check_ib_gateway` → async returning `{"port_listening": False, "upstream_dead": True}`;
  expect **502** with row **FAILED / SUBPROCESS_ERROR** (NOT UNCERTAIN). Include this as a 6th
  test case (`test_gateway_down_stays_failed_not_uncertain`).
- **Guard direction:** `test_mark_uncertain_does_not_clobber_working` (Step 1) proves a WORKING
  row is never downgraded.

### Migration

None — `order_submissions.state` has no CHECK constraint at HEAD (verified `schema.py:576-632`).
No `uv run alembic` step. (When P2.2 adds the CHECK, `UNCERTAIN` must be in its allowed set.)

---

## 7. Tripwires / abort criteria (STOP and report)

- **If `test_ack_then_kill_ends_working_with_ids` shows `state == "FAILED"`** → the core OP-1
  fix is not working; do not proceed to the UI steps.
- **If any test in Step 1/2/3 PASSES before you implement** → the anchor is wrong (the symbol
  already exists); STOP and re-read the file.
- **If `grep -n "run_entry_point(entry, args, timeout=timeout)" src/xenon/api/server.py`
  returns anything other than 0 after Step 4** → you missed one of the two internal calls, or
  changed an unintended site; STOP.
- **If more than these files need edits, STOP and report** (expected edit set):
  `src/xenon/execution/orders_store.py`, `src/xenon/execution/ib_place_order.py`,
  `src/xenon/api/subprocess.py`, `src/xenon/api/server.py`, `src/xenon/api/routes/orders.py`,
  `src/xenon/db/queries/orders.py`, `src/xenon/execution/preflight.py`,
  `web/lib/orderReasonCodes.ts`, `web/components/orderStatusBadge.tsx` (new),
  `web/components/WorkspaceSections.tsx`, `web/components/ticker-detail/OrderTab.tsx`,
  `web/components/ticker-detail/BookTab.tsx`, `web/components/CancelOrderDialog.tsx`, one CSS/stylesheet
  file, `CHANGELOG.md`, `docs/reference/order-path-incident-history.md`,
  `src/xenon/api/CLAUDE.md`, plus the new/edited test files. If a required change lands
  anywhere else, stop and reassess.
- **If any step needs a live-IB call**, use **PAPER** only (`scripts/infra/dev.sh paper`, port
  4002). Never live money.
- **If you cannot find an existing warn/amber design token for `.status-uncertain`** → STOP;
  do not use raw hex (brand rule).
- **If the reason-code parity test fails** with `extraInTs`/`missingInTs` non-empty → you added
  the code to one side only; fix both `preflight.py` and the TS array + map.
- **Do NOT** add the generic `transition()` helper, the `state` CHECK constraint, or the
  reconciliation sweep — those are P2.2 / S3, out of scope. If tempted, STOP.

---

## 8. Rollback

- Nothing is persisted irreversibly (no migration). To revert:
  `git checkout master && git branch -D fix/order-uncertain-state-orderref`.
- If already merged and a regression appears, revert the PR; no down-migration needed. Any
  `UNCERTAIN` rows already written remain valid free-text states and simply won't be produced
  by the reverted code (they stay visible under the old `ACTIVE_STATES` only if that revert is
  partial — a full revert also drops `UNCERTAIN` from `ACTIVE_STATES`, hiding them again;
  resolve any stragglers manually via psql).

---

## 9. Incident-history row (append to `docs/reference/order-path-incident-history.md`)

Append as row **#24** (last existing row is #23), matching the 6-column table format
(`#`, `Date / PR`, `Issue`, `Root cause`, `Solution`, `Prevention`):

```
| 24  | 2026-07-05 (S2, OP-1/OP-11)                     | 15s place-subprocess timeout/SIGKILL after IB may have accepted was recorded as terminal `FAILED/SUBPROCESS_ERROR` with no ib ids; a retry under a new `client_attempt_id` could place a duplicate live order. Ack was also a blind 2–5s sleep (fixed latency, arbitrary `initialStatus`). | `run_entry_point` used `communicate()` and discarded all partial stdout on timeout, so an early broker ack was lost; the handler had only PENDING→WORKING/FAILED/REJECTED with no ambiguous state; the CLI never set `orderRef` and never emitted an early ack. | CLI sets `order.orderRef = client_attempt_id`, waits event-driven for `permId`, and prints a flushed `{"stage":"ack",...}` line; new `run_place_subprocess` streams stdout line-by-line so the ack survives SIGKILL; handler persists ack→`WORKING` (real ib ids) or, when spawned with no ack + no result, writes the new non-terminal `UNCERTAIN` state + 502 `ORDER_STATUS_UNCERTAIN` ("Do NOT resubmit"); never-spawned/gateway-down stays `FAILED` (retryable). `UNCERTAIN` added to `ACTIVE_STATES`, to `orders_store._ACTIVE_STATES` (so preflight coverage counts possibly-live orders), and to the open-orders UI via a shared `renderOrderStatus` badge (workspace tables, ticker OrderTab/BookTab, cancel dialog). | `test_run_place_subprocess.py` (ack-hang/timeout/garbage), `test_orders_place_uncertain_route.py` (ack-kill→WORKING, no-ack→UNCERTAIN, gateway-down→FAILED), `test_orders_submissions_store.py::mark_uncertain*`, reason-code parity. **Non-goal (S3/OP-3):** `orderRef` reconciliation sweep resolves UNCERTAIN rows — tracked separately. |
```

---

## 10. Repo invariants honored (self-check before PR)

- All Python via `uv run`; no bare python/pip. ✔
- Branch + PR; no `git push origin master`; no AI attribution trailer. ✔
- Order-path change diagnosed on PAPER only. ✔
- 502 error uses **top-level** `reason_code` in `JSONResponse` (not buried under `detail`). ✔
- `expected_states=` on the FAILED/REJECTED/UNCERTAIN writes this handler owns. ✔
- No new JSON read/write on the order path (guards run clean). ✔
- `client_id="auto"` unchanged in the CLI. ✔
- CLI stays synchronous; no `asyncio.to_thread` around ib_async sync calls. ✔
- Tests use a real ticker (AAPL) at a frozen real-ish limit; no network at runtime; fake CLI
  stub, not live IB. ✔
- `committed_db` marker on route/store tests that fork a subprocess or use `get_sync_engine`. ✔

```

```
