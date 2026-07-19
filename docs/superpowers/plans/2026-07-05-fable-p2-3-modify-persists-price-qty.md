# P2.3 — Successful modify persists limit_price / quantity + MODIFY_APPLIED event

**Date:** 2026-07-05
**Proposed branch:** `fix/modify-persists-price-qty`
**Finding IDs:** OP-4 (High, "State machine") + its failure-mode twin ("External TWS modify … Xenon-authored rows skipped")
**Severity:** High
**One-line goal:** On a successful `/orders/modify`, write the new `limit_price` / `quantity` onto the Xenon-authored `order_submissions` row (sequence- and state-guarded) and record a `MODIFY_APPLIED` order-event, so Postgres — and therefore the UI, reports, and audits — stops showing the pre-modify price.

---

## 1. Context (what exists today, verified at HEAD)

- **The bug.** `POST /orders/modify` → `server.py::_orders_modify_from_body` advances the monotonic modify gate via `orders_store.apply_modify(order_id, seq, **scope)` (or `apply_modify_by_perm_id`) **before** spawning the `xenon-ib-order-manage` subprocess. Both writers update **only** `modify_sequence` + `updated_at` — they never touch `limit_price` or `quantity`. Verified:
  - `src/xenon/execution/orders_store.py::apply_modify` (`def apply_modify(` ~line 195): the `.values(modify_sequence=sequence, updated_at=…)` UPDATE has no price/qty columns.
  - `src/xenon/execution/orders_store.py::apply_modify_by_perm_id` (~line 431): resolves `ib_order_id` from `perm_id`, then the same sequence-only UPDATE.
  - `src/xenon/execution/ib_order_manage.py` has **zero** `orders_store` references (it only talks to IB), so the subprocess never writes the new price back either.
- **The UI read path is PG.** `src/xenon/api/routes/orders.py::orders_payload_for_scope` (~line 306) SELECTs `order_submissions` rows in `ACTIVE_STATES` and maps them through `_open_order` (~line 233) → `"limitPrice": _float_or_none(row.get("limit_price"))`. So the open-orders panel renders whatever `limit_price` is in Postgres. A modify that doesn't rewrite that column leaves the panel permanently stale until the order fills/cancels.
- **The mirror won't fix it either.** The IB→PG drift mirror (`orders_store.register_from_snapshot` ~line 282, called via `ib_activity_mirror` / `ib_orders.sync_open_orders_to_postgres`) **deliberately skips UUID-authored rows**: `if not existing_submission_id.startswith("snapshot-"): return {"action": "SKIPPED_UUID", "drift": None}` (~line 321). Only `snapshot-*` rows get drift-mirrored. So for a Xenon-placed order there is **no** background path that would eventually correct the price — the modify handler is the only place that knows the new value.
- **Sequence is intentionally not rolled back on subprocess failure.** In `_orders_modify_from_body`, after `apply_modify` succeeds, if `_run_ib_script_with_recovery` fails the comment reads: _"DB sequence is already advanced; don't roll back — prevents double-apply on a retry."_ Any post-success writer you add must tolerate a `modify_sequence` that was advanced even when the IB modify later failed (that path returns 503/4xx and never reaches the confirm write, which is exactly what we want).

**What the executor does NOT need to understand:** the IB subprocess internals (`ib_order_manage.py`), the naked-short guard, the combo/BAG wizard, the regime-override preflight gate, or the activity poller's fills/sweep logic. This change is confined to one new store writer, one call site in the modify handler, and their tests.

---

## 2. Drift from review

**None material.** OP-4's cited lines (`orders_store.py:195-243,322-323`) still map to the real functions (`apply_modify` at 195, `apply_modify_by_perm_id` at 431 — the fable table only listed the first, the `_by_perm_id` variant must be covered too; the `322-323` "SKIPPED_UUID" claim is confirmed at ~line 321). `MODIFY_APPLIED` and `confirm_modify` do **not** exist anywhere in the tree (grepped `src/ web/ scripts/` → no matches), so this is a clean addition.

---

## 3. Goal / Non-goals

### Goal

On a **successful** Xenon-authored modify, persist the request's new `limit_price` and/or `quantity` onto the matching `order_submissions` row, guarded so a stale/concurrent write cannot clobber a newer modify or a row that already left the working state, and emit a `MODIFY_APPLIED` order-event with old→new values.

### Non-goals (explicitly NOT in this PR — one change, one PR)

1. **External TWS modifies on UUID rows stay skipped (OP-4 twin).** This plan does **not** change the drift mirror's `SKIPPED_UUID` branch (`register_from_snapshot`). Rationale: the mirror runs on a 60s poll from an IB open-orders snapshot that carries **no** `modify_sequence`; letting it overwrite a UUID row's price would race against an in-flight Xenon modify and could regress a just-applied value or resurrect a superseded one. Xenon-authored modifies persisting **their own** request values (this PR) is a closed, sequence-safe loop; mirroring arbitrary TWS edits onto UUID rows is a separate, riskier policy decision left open in the failure-mode table.
2. No change to the modify **sequence gate** semantics, the subprocess-failure `applied_sequence` echo, or read-only 403 behavior.
3. No schema/migration change (`limit_price` and `quantity` columns already exist on `order_submissions`).
4. No frontend/TypeScript change (the panel already reads `limit_price` from PG; the fix is server-side only).

---

## 4. Key facts (verified against the working tree)

- **Table `xenon.order_submissions`** (`src/xenon/db/schema.py` ~line 576): `limit_price Numeric(12,4)`, `quantity Integer NOT NULL`, `state Text NOT NULL`, `modify_sequence Integer default 0`, plus scope columns `broker`, `account_env`, `broker_account`. PK `submission_id`.
- **`orders_store` imports already available** (no new imports needed): `from datetime import datetime, timezone`; `from decimal import Decimal`; `from sqlalchemy import … select, update`; `from xenon.db.engine import get_sync_engine`; `from xenon.db.schema import … order_submissions`.
- **`orders_store.record_event(submission_id: str, kind: str, detail: dict) -> None`** (~line 713) — INSERTs one `order_events` row. Use it for `MODIFY_APPLIED`.
- **Scope kwargs.** `server.py::_resolve_scope_kwargs()` (~line 2125) returns `{"broker": …, "account_env": …, "broker_account": …}`. `apply_modify` already accepts those as keyword-only `broker`/`account_env`/`broker_account` (each `str | None`). `confirm_modify` must accept the same shape.
- **Active/working states.** `routes/orders.py::ACTIVE_STATES = {"PENDING", "WORKING", "PARTIALLY_FILLED"}`. The correct guard for a price-confirm write is the narrower `("WORKING", "PARTIALLY_FILLED")` — a `PENDING` row has not been acknowledged by IB and should never receive a modify-confirm.
- **Existing terminal set.** `orders_store._TERMINAL_STATES = {"REJECTED", "CANCELLED", "FAILED"}` (~line 63).
- **Modify handler success point.** `server.py::_orders_modify_from_body` (~line 2515). The success tail is the block that calls `_record_manage_event(str(order_id or ""), "MODIFY", {… "http_status": 200 …}, perm_id=…, scope=_scope)` (~line 2637) followed by `return {**data, "applied_sequence": modify_sequence}` (~line 2650). `new_price = body.get("newPrice")` and `new_quantity = body.get("newQuantity")` are already in scope in this function (~lines 2525-2526), as are `modify_sequence` (int) and `_scope`.
- **Test-mode short-circuit.** `_orders_modify_from_body` returns early when `_is_test_mode()` (~line 2516) — so the confirm write only runs on the real (non-test-mode) path. The route tests below set `XENON_API_TEST_MODE=0`.
- **Read-only:** the modify route returns `read_only_403()` at entry (`if is_read_only(): return read_only_403()`, ~line 2509) **before** `_orders_modify_from_body`, so `confirm_modify` is unreachable under `XENON_READ_ONLY=1` — exactly like `apply_modify`, which also has no internal flag check. **Do not add a flag check inside `confirm_modify`**; it would be dead code and diverge from its sibling writer.

---

## 5. Steps (strictly ordered — TDD: red first, then implement, then green)

### Step 1 — RED: store-level tests for `confirm_modify`

Append to the **existing** file `scripts/tests/test_orders_store_modify_sequence.py` (reuse its `db_path`, `_seed_order`, `_fetch_scalar` fixtures — do NOT create a new file). Add these seven tests at the end of the file:

```python
def test_confirm_modify_writes_price_and_qty(db_path):
    from decimal import Decimal

    from xenon.execution.orders_store import apply_modify, confirm_modify

    _seed_order(db_path, user_id="u1", client_attempt_id="a1", ib_order_id="1")
    # Advance the gate exactly as the route does before the subprocess.
    assert apply_modify(order_id="1", sequence=1) == {"applied": True, "current_sequence": 1}

    out = confirm_modify(
        order_id="1",
        sequence=1,
        new_price=Decimal("499.00"),
        new_quantity=50,
    )
    assert out["applied"] is True
    assert out["old_limit_price"] == pytest.approx(500.15)
    assert out["new_limit_price"] == pytest.approx(499.00)
    assert out["old_quantity"] == 100
    assert out["new_quantity"] == 50

    lp = _fetch_scalar(
        "SELECT limit_price FROM xenon.order_submissions WHERE ib_order_id = :i",
        {"i": "1"},
    )
    qty = _fetch_scalar(
        "SELECT quantity FROM xenon.order_submissions WHERE ib_order_id = :i",
        {"i": "1"},
    )
    assert float(lp) == pytest.approx(499.00)
    assert int(qty) == 50


def test_confirm_modify_price_only_leaves_quantity(db_path):
    """M2: newQuantity absent (None) → only limit_price changes."""
    from decimal import Decimal

    from xenon.execution.orders_store import apply_modify, confirm_modify

    _seed_order(db_path, user_id="u1", client_attempt_id="a1", ib_order_id="1")
    assert apply_modify(order_id="1", sequence=1) == {"applied": True, "current_sequence": 1}

    out = confirm_modify(order_id="1", sequence=1, new_price=Decimal("501.00"))
    assert out["applied"] is True
    assert out["new_limit_price"] == pytest.approx(501.00)
    assert out["new_quantity"] == 100  # untouched, echoed from RETURNING

    qty = _fetch_scalar(
        "SELECT quantity FROM xenon.order_submissions WHERE ib_order_id = :i",
        {"i": "1"},
    )
    assert int(qty) == 100  # unchanged


def test_confirm_modify_quantity_only_leaves_price(db_path):
    """M2: newPrice absent (None) → only quantity changes."""
    from xenon.execution.orders_store import apply_modify, confirm_modify

    _seed_order(db_path, user_id="u1", client_attempt_id="a1", ib_order_id="1")
    assert apply_modify(order_id="1", sequence=1) == {"applied": True, "current_sequence": 1}

    out = confirm_modify(order_id="1", sequence=1, new_quantity=25)
    assert out["applied"] is True
    assert out["new_quantity"] == 25
    assert out["new_limit_price"] == pytest.approx(500.15)  # untouched

    lp = _fetch_scalar(
        "SELECT limit_price FROM xenon.order_submissions WHERE ib_order_id = :i",
        {"i": "1"},
    )
    assert float(lp) == pytest.approx(500.15)  # unchanged


def test_confirm_modify_both_none_is_noop(db_path):
    """M2: neither price nor quantity supplied → no write, applied False."""
    from xenon.execution.orders_store import apply_modify, confirm_modify

    _seed_order(db_path, user_id="u1", client_attempt_id="a1", ib_order_id="1")
    assert apply_modify(order_id="1", sequence=1) == {"applied": True, "current_sequence": 1}

    out = confirm_modify(order_id="1", sequence=1)
    assert out == {"applied": False, "submission_id": None}


def test_confirm_modify_loses_race_to_newer_reserve(db_path):
    """BLOCKER regression: interleaving — modify A reserves seq 1, modify B
    reserves seq 2 BEFORE A's confirm lands. A's confirm(sequence=1) must
    match zero rows (the atomic WHERE requires modify_sequence == 1, but the
    row is already at 2) and leave B's pending values untouched."""
    from decimal import Decimal

    from xenon.execution.orders_store import apply_modify, confirm_modify

    _seed_order(db_path, user_id="u1", client_attempt_id="a1", ib_order_id="1")
    # A reserves…
    assert apply_modify(order_id="1", sequence=1) == {"applied": True, "current_sequence": 1}
    # …B reserves before A's confirm arrives.
    assert apply_modify(order_id="1", sequence=2) == {"applied": True, "current_sequence": 2}

    # A's late confirm must be a no-op.
    out = confirm_modify(
        order_id="1",
        sequence=1,
        new_price=Decimal("1.00"),
        new_quantity=1,
    )
    assert out["applied"] is False

    lp = _fetch_scalar(
        "SELECT limit_price FROM xenon.order_submissions WHERE ib_order_id = :i",
        {"i": "1"},
    )
    qty = _fetch_scalar(
        "SELECT quantity FROM xenon.order_submissions WHERE ib_order_id = :i",
        {"i": "1"},
    )
    assert float(lp) == pytest.approx(500.15)  # A's stale price NOT written
    assert int(qty) == 100                       # unchanged

    # B's own confirm (sequence=2) still applies.
    out2 = confirm_modify(order_id="1", sequence=2, new_price=Decimal("502.00"))
    assert out2["applied"] is True
    assert out2["new_limit_price"] == pytest.approx(502.00)


def test_confirm_modify_by_perm_id(db_path):
    """M3: perm_id-keyed confirm (route path when orderId=0)."""
    from decimal import Decimal

    from xenon.execution.orders_store import (
        apply_modify_by_perm_id,
        confirm_modify,
        mark_submitted,
        reserve_attempt,
    )

    outcome = reserve_attempt("u1", "a-perm-c", _req())
    mark_submitted(
        submission_id=outcome.submission_id,
        ib_order_id="88",
        perm_id="P88",
        placing_client_id=1,
    )
    assert apply_modify_by_perm_id(perm_id="P88", sequence=1) == {
        "applied": True,
        "current_sequence": 1,
    }

    out = confirm_modify(perm_id="P88", sequence=1, new_price=Decimal("499.50"))
    assert out["applied"] is True
    assert out["submission_id"] == outcome.submission_id
    assert out["new_limit_price"] == pytest.approx(499.50)

    lp = _fetch_scalar(
        "SELECT limit_price FROM xenon.order_submissions WHERE perm_id = :p",
        {"p": "P88"},
    )
    assert float(lp) == pytest.approx(499.50)


def test_confirm_modify_wrong_state_is_noop(db_path):
    from decimal import Decimal

    from sqlalchemy import text as _text

    from xenon.db.engine import get_sync_engine
    from xenon.execution.orders_store import apply_modify, confirm_modify

    _seed_order(db_path, user_id="u1", client_attempt_id="a1", ib_order_id="1")
    assert apply_modify(order_id="1", sequence=1) == {"applied": True, "current_sequence": 1}

    # Row leaves the working set between the gate and the confirm.
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            _text("UPDATE xenon.order_submissions SET state='CANCELLED' WHERE ib_order_id='1'"),
        )

    out = confirm_modify(
        order_id="1",
        sequence=1,
        new_price=Decimal("1.00"),
        new_quantity=1,
    )
    assert out["applied"] is False

    lp = _fetch_scalar(
        "SELECT limit_price FROM xenon.order_submissions WHERE ib_order_id = :i",
        {"i": "1"},
    )
    assert float(lp) == pytest.approx(500.15)  # unchanged
```

`pytest` is already imported at the top of that file. Run them now — all seven MUST fail with `ImportError: cannot import name 'confirm_modify'` (or `AttributeError`). If any test **passes** before Step 2, STOP (see Tripwires).

### Step 2 — GREEN: implement `orders_store.confirm_modify`

Insert this new function in `src/xenon/execution/orders_store.py` **immediately after `apply_modify_by_perm_id`** (i.e. after the line `return {"applied": False, "current_sequence": int(cur[0]) if cur else -1}` that closes that function, ~line 482, before `def mark_submitted(`):

```python
_MODIFY_CONFIRM_STATES: tuple[str, ...] = ("WORKING", "PARTIALLY_FILLED")


def confirm_modify(
    *,
    order_id: str = "",
    perm_id: str = "",
    sequence: int,
    new_price: Decimal | None = None,
    new_quantity: int | None = None,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> dict:
    """Persist the *values* of a successful modify onto the submission row.

    Called by the modify route AFTER the IB subprocess confirms success.
    Split from ``apply_modify`` on purpose: ``apply_modify`` reserves the
    monotonic ``modify_sequence`` BEFORE the subprocess and is deliberately
    NOT rolled back on failure, so value-writes must be a separate, later
    step that only runs on success.

    Canonical key: takes the same ``order_id`` / ``perm_id`` pair the route
    already holds (mirroring ``apply_modify`` vs ``apply_modify_by_perm_id``;
    ``order_id`` wins when both are non-empty). The pre-SELECT resolves
    ``submission_id`` and captures OLD values purely for the caller's
    ``MODIFY_APPLIED`` event detail — it is NOT the write guard.

    The write guard is ATOMIC: one UPDATE whose WHERE clause requires
    ``modify_sequence == sequence`` (the exact counter this modify reserved
    via ``apply_modify``; any newer modify has bumped it higher) AND
    ``state IN ('WORKING','PARTIALLY_FILLED')``. Zero rows updated == lost
    race or departed working state → no-op, caller writes no event. Same
    optimistic-WHERE pattern as ``mark_terminal(expected_states=…)`` and
    ``apply_modify``'s own ``modify_sequence < sequence`` predicate. A stale
    pre-SELECT cannot leak a wrong write: the interleaving writer changed
    the sequence, so this UPDATE matches zero rows.

    Partial modifies: only supplied (non-None) columns are written. The
    route reads ``newPrice``/``newQuantity`` via ``body.get`` — an absent
    key and JSON ``null`` are indistinguishable and both arrive as ``None``,
    meaning "leave that column alone". Both None → no-op.

    Returns ``{"applied": bool, "submission_id": str | None,
    "old_limit_price"/"new_limit_price"/"old_quantity"/"new_quantity": …}``.
    """
    if order_id:
        id_cond = order_submissions.c.ib_order_id == str(order_id)
    elif perm_id:
        id_cond = order_submissions.c.perm_id == str(perm_id)
    else:
        return {"applied": False, "submission_id": None}

    values: dict = {"updated_at": datetime.now(timezone.utc)}
    if new_price is not None:
        values["limit_price"] = Decimal(str(new_price))
    if new_quantity is not None:
        values["quantity"] = int(new_quantity)
    if len(values) == 1:  # only updated_at → nothing to persist
        return {"applied": False, "submission_id": None}

    scope_conds: list = []
    if broker is not None:
        scope_conds.append(order_submissions.c.broker == broker)
    if account_env is not None:
        scope_conds.append(order_submissions.c.account_env == account_env)
    if broker_account is not None:
        scope_conds.append(order_submissions.c.broker_account == broker_account)

    engine = get_sync_engine()
    with engine.begin() as conn:
        # Event-detail read ONLY — the guard lives in the UPDATE below.
        row = conn.execute(
            select(
                order_submissions.c.submission_id,
                order_submissions.c.limit_price,
                order_submissions.c.quantity,
            ).where(id_cond, *scope_conds)
        ).first()
        if row is None:
            return {"applied": False, "submission_id": None}
        sid, old_lp, old_qty = row
        old_limit_price = float(old_lp) if old_lp is not None else None
        old_quantity = int(old_qty) if old_qty is not None else None

        result = conn.execute(
            update(order_submissions)
            .where(
                order_submissions.c.submission_id == sid,
                order_submissions.c.modify_sequence == int(sequence),
                order_submissions.c.state.in_(_MODIFY_CONFIRM_STATES),
            )
            .values(**values)
            .returning(
                order_submissions.c.limit_price,
                order_submissions.c.quantity,
            )
        )
        updated = result.first()
        if updated is None:
            # Lost the race (sequence moved) or row left the working set.
            return {
                "applied": False,
                "submission_id": sid,
                "old_limit_price": old_limit_price,
                "old_quantity": old_quantity,
            }
        return {
            "applied": True,
            "submission_id": sid,
            "old_limit_price": old_limit_price,
            "new_limit_price": float(updated[0]) if updated[0] is not None else None,
            "old_quantity": old_quantity,
            "new_quantity": int(updated[1]) if updated[1] is not None else None,
        }
```

Re-run Step 1's seven tests — they MUST now pass.

**Accepted tradeoff (documented, not fixed here).** Because `apply_modify` reserves the sequence _before_ the subprocess and is never rolled back, this interleaving leaves PG stale on purpose: modify A succeeds at IB and reserves seq 1; modify B reserves seq 2 but then **fails** at IB; A's late confirm(sequence=1) no-ops because the row already sits at seq 2, and B's confirm never runs. Result: IB holds A's price while PG still shows the pre-A value. This is the consistent price of the "never clobber a possibly-newer write" rule — the safe failure mode is _stale_, never _wrong-direction_ (we never overwrite a newer modify with an older price). The drift is bounded: the next user modify/cancel, a fill, or the disappeared-order sweep converges the row; and the failed-B path already returns 5xx/4xx to the user, so the operator knows the modify chain broke. Do NOT "fix" this by relaxing the equality guard.

### Step 3 — RED: route-level tests

Append to the **existing** file `src/xenon/api/tests/test_orders_routes_failures.py` (reuse its `client`, `tmp_db`, `_patch_runner`, `_pg_engine`, `_seed_submission` helpers). Add four tests at the end:

```python
def test_modify_success_persists_price_and_qty(client, tmp_db, monkeypatch):
    """OP-4: a successful modify rewrites limit_price/quantity in PG and
    records a MODIFY_APPLIED event."""
    _seed_submission(tmp_db, ib_order_id="42")  # seeds limit 1.23, qty 1, seq 5

    async def fake_runner(entry, args, timeout=30):
        return ScriptResult(ok=True, data={"status": "ok", "message": "Modified"})

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fake_runner)

    resp = client.post(
        "/orders/modify",
        json={"orderId": 42, "permId": 0, "newPrice": 2.50,
              "newQuantity": 7, "modifySequence": 6},
    )
    assert resp.status_code == 200

    engine = _pg_engine()
    try:
        with engine.begin() as con:
            lp, qty = con.execute(
                text("SELECT limit_price, quantity FROM xenon.order_submissions "
                     "WHERE ib_order_id = '42'")
            ).one()
            events = con.execute(
                text("SELECT count(*) FROM xenon.order_events "
                     "WHERE kind = 'MODIFY_APPLIED'")
            ).scalar()
    finally:
        engine.dispose()

    assert float(lp) == 2.50
    assert int(qty) == 7
    assert events == 1


def test_modify_subprocess_failure_does_not_persist_price(client, tmp_db, monkeypatch):
    """OP-4: when the IB subprocess fails, the row keeps its pre-modify
    price/qty (confirm write never runs)."""
    _seed_submission(tmp_db, ib_order_id="42")  # limit 1.23, qty 1, seq 5

    async def fake_runner(entry, args, timeout=30):
        return ScriptResult(ok=False, data=None, error="IB gateway unreachable")

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fake_runner)

    resp = client.post(
        "/orders/modify",
        json={"orderId": 42, "permId": 0, "newPrice": 2.50,
              "newQuantity": 7, "modifySequence": 6},
    )
    assert resp.status_code == 503

    engine = _pg_engine()
    try:
        with engine.begin() as con:
            lp, qty = con.execute(
                text("SELECT limit_price, quantity FROM xenon.order_submissions "
                     "WHERE ib_order_id = '42'")
            ).one()
    finally:
        engine.dispose()

    assert float(lp) == 1.23   # unchanged
    assert int(qty) == 1       # unchanged


def test_modify_by_perm_id_persists_price_and_qty(client, tmp_db, monkeypatch):
    """M3/OP-4: the permId-only path (orderId=0) must also persist values +
    MODIFY_APPLIED. Reuses the same seed shape as
    test_modify_by_perm_id_with_known_row_advances_sequence."""
    engine = _pg_engine()
    try:
        with engine.begin() as con:
            con.execute(
                text(
                    """
            INSERT INTO xenon.order_submissions (
                submission_id, user_id, client_attempt_id, ticker, security_type,
                action, quantity, multiplier, limit_price, state, ib_order_id,
                perm_id, modify_sequence, submitted_at, updated_at,
                broker, account_env, broker_account
            ) VALUES ('sub-perm-op4', 'local', 'cid-perm-op4', 'AAPL', 'STK', 'BUY', 1, 100,
                      '1.23', 'WORKING', '99', '43', 0, NOW(), NOW(),
                      'IB', 'paper', 'DU0000000')
            """,
                ),
            )
    finally:
        engine.dispose()

    async def fake_runner(entry, args, timeout=30):
        return ScriptResult(ok=True, data={"status": "ok", "message": "Modified"})

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fake_runner)

    resp = client.post(
        "/orders/modify",
        json={"orderId": 0, "permId": 43, "newPrice": 3.75,
              "newQuantity": 4, "modifySequence": 1},
    )
    assert resp.status_code == 200

    engine = _pg_engine()
    try:
        with engine.begin() as con:
            lp, qty = con.execute(
                text("SELECT limit_price, quantity FROM xenon.order_submissions "
                     "WHERE perm_id = '43'")
            ).one()
            events = con.execute(
                text("SELECT count(*) FROM xenon.order_events "
                     "WHERE submission_id = 'sub-perm-op4' "
                     "AND kind = 'MODIFY_APPLIED'")
            ).scalar()
    finally:
        engine.dispose()

    assert float(lp) == 3.75
    assert int(qty) == 4
    assert events == 1


def test_modify_price_only_leaves_quantity(client, tmp_db, monkeypatch):
    """M2/OP-4: newQuantity omitted from the body → quantity column untouched."""
    _seed_submission(tmp_db, ib_order_id="42")  # limit 1.23, qty 1, seq 5

    async def fake_runner(entry, args, timeout=30):
        return ScriptResult(ok=True, data={"status": "ok", "message": "Modified"})

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fake_runner)

    resp = client.post(
        "/orders/modify",
        json={"orderId": 42, "permId": 0, "newPrice": 2.10, "modifySequence": 6},
    )
    assert resp.status_code == 200

    engine = _pg_engine()
    try:
        with engine.begin() as con:
            lp, qty = con.execute(
                text("SELECT limit_price, quantity FROM xenon.order_submissions "
                     "WHERE ib_order_id = '42'")
            ).one()
    finally:
        engine.dispose()

    assert float(lp) == 2.10
    assert int(qty) == 1  # unchanged — key was absent (None == omitted == no-op)
```

`text` is already imported in that file (`from sqlalchemy import create_engine, text`). Run all four — `test_modify_success_persists_price_and_qty`, `test_modify_by_perm_id_persists_price_and_qty`, and `test_modify_price_only_leaves_quantity` MUST fail (price stays 1.23 / event count 0) before Step 4; `test_modify_subprocess_failure_does_not_persist_price` will already pass (nothing writes price today) — that's fine, it's a regression guard for Step 4.

### Step 4 — GREEN: wire the confirm write into the modify handler

In `src/xenon/api/server.py::_orders_modify_from_body`, locate the success tail (~line 2637):

```python
    _record_manage_event(
        str(order_id or ""),
        "MODIFY",
        {
            "status": data.get("status"),
            "message": data.get("message"),
            "applied_sequence": modify_sequence,
            "http_status": 200,
        },
        perm_id=str(perm_id or ""),
        scope=_scope,
    )
    # Echo the applied sequence so the UI can anchor its per-order counter.
    return {**data, "applied_sequence": modify_sequence}
```

Insert the confirm-write block **between** the `_record_manage_event(...)` call and the `return` line, so the final tail reads:

```python
    _record_manage_event(
        str(order_id or ""),
        "MODIFY",
        {
            "status": data.get("status"),
            "message": data.get("message"),
            "applied_sequence": modify_sequence,
            "http_status": 200,
        },
        perm_id=str(perm_id or ""),
        scope=_scope,
    )

    # OP-4: the IB modify succeeded — persist the new price/qty onto the
    # submission row so PG (UI, reports, audits) reflects the modify. Guarded
    # by matching modify_sequence + WORKING/PARTIALLY_FILLED state inside
    # confirm_modify. Best-effort: a confirm-write failure must NOT turn a
    # successful broker modify into an HTTP error.
    try:
        confirm = orders_store.confirm_modify(
            order_id=str(order_id or ""),
            perm_id=str(perm_id or ""),
            sequence=modify_sequence,
            new_price=Decimal(str(new_price)) if new_price is not None else None,
            new_quantity=int(new_quantity) if new_quantity is not None else None,
            **_scope,
        )
        if confirm.get("applied") and confirm.get("submission_id"):
            orders_store.record_event(
                confirm["submission_id"],
                "MODIFY_APPLIED",
                {
                    "sequence": modify_sequence,
                    "old_limit_price": confirm.get("old_limit_price"),
                    "new_limit_price": confirm.get("new_limit_price"),
                    "old_quantity": confirm.get("old_quantity"),
                    "new_quantity": confirm.get("new_quantity"),
                },
            )
    except Exception:  # pragma: no cover — value persistence is best-effort
        logger.warning(
            "confirm_modify failed after successful IB modify (order=%s perm=%s seq=%s)",
            order_id,
            perm_id,
            modify_sequence,
            exc_info=True,
        )

    # Echo the applied sequence so the UI can anchor its per-order counter.
    return {**data, "applied_sequence": modify_sequence}
```

`Decimal` is **already imported** in `server.py` (verified: `from decimal import Decimal` at line 21) — do not add an import. `orders_store` and `logger` are already imported/defined in `server.py` (used throughout this handler).

Note on optional fields: the handler reads `new_price = body.get("newPrice")` / `new_quantity = body.get("newQuantity")` — an **absent key and JSON `null` are indistinguishable** (both `None`) and both mean "don't touch that column". The `if … is not None else None` pass-through above preserves exactly that, and `confirm_modify` no-ops entirely when both are `None`.

Re-run Step 3's tests — all four MUST now pass.

---

## 6. Verification matrix (run every applicable row; expected outcomes are literal)

| #   | Command                                                                   | Expected                                                                                                                                                                                                                                   |
| --- | ------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1   | `uv run pytest scripts/tests/test_orders_store_modify_sequence.py -xvs`   | All tests pass, incl. the 7 new `test_confirm_modify_*` (writes_price_and_qty, price_only_leaves_quantity, quantity_only_leaves_price, both_none_is_noop, loses_race_to_newer_reserve, wrong_state_is_noop, by_perm_id). Exit 0.           |
| 2   | `uv run pytest "src/xenon/api/tests/test_orders_routes_failures.py" -xvs` | All tests pass, incl. the 4 new: `test_modify_success_persists_price_and_qty`, `test_modify_subprocess_failure_does_not_persist_price`, `test_modify_by_perm_id_persists_price_and_qty`, `test_modify_price_only_leaves_quantity`. Exit 0. |
| 3   | `uv run python scripts/checks/no_json_fallback_on_order_path.py`          | Exit 0 (no new JSON reads).                                                                                                                                                                                                                |
| 4   | `uv run python scripts/checks/no_json_write_on_order_path.py`             | Exit 0 (confirm write is to Postgres, not `data/*.json`).                                                                                                                                                                                  |
| 5   | `uv run python scripts/checks/order_path_caller_allowlist.py`             | Exit 0 (no new `ib_place_order` caller).                                                                                                                                                                                                   |
| 6   | `uv run python scripts/infra/dev/run_pytest_affected.py`                  | Green; picks up both changed test files. Exit 0.                                                                                                                                                                                           |
| 7   | `cd web && npm test`                                                      | **Not required** — no web/TS change in this PR. State this in the PR body; skip only because zero web files were touched.                                                                                                                  |

**Migration checks:** none — no schema change (`limit_price`, `quantity` already exist). Do not run `alembic revision`.

### Live paper probe (PAPER ONLY — `scripts/infra/dev.sh paper`, IB port 4002, Next :3200, FastAPI :8421)

Only if a paper IB session is available. This is the browser verification for the UI-visible change (open-orders panel reads `limit_price` from PG — `routes/orders.py:311/233`).

1. `scripts/infra/dev.sh paper` — wait for `curl http://localhost:8421/health` to report `"ib_gateway": {"port_listening": true}` and `mode_verified: true`.
2. In the browser (chrome-cdp) at `http://localhost:3200`, place a **limit** order on a liquid paper symbol that will rest (e.g. AAPL BUY 1 @ a limit well below the ask so it stays WORKING). Confirm it appears in the open-orders panel at the placed limit price.
3. Modify that order's limit price to a new value via the UI. Wait for the success toast.
4. Refresh the open-orders panel. **Assert:** the displayed limit price equals the NEW value (not the original). Screenshot to `output/playwright/modify-persists-price-2026-07-05.png`.
5. Confirm the event landed:
   ```bash
   psql "$DATABASE_URL_PAPER" -c "SELECT kind, detail->>'old_limit_price', detail->>'new_limit_price' FROM xenon.order_events WHERE kind='MODIFY_APPLIED' ORDER BY 1 DESC LIMIT 1;"
   ```
   Expected: one `MODIFY_APPLIED` row with `new_limit_price` = the value you set in step 3. (`$DATABASE_URL_PAPER` is the LOCAL `127.0.0.1/core_test` that `dev.sh paper` writes — not the remote `DATABASE_URL_TEST`.)
6. Cancel the resting order to clean up.

If no paper IB session is available, SKIP steps 1-6 and rely on rows 1-2 (route test already asserts the PG write end-to-end through the handler with a patched runner). Report the skip explicitly.

---

## 7. Tripwires / abort criteria (STOP and report)

- **STOP** if any `test_confirm_modify_*` test **passes before Step 2** — `confirm_modify` already exists or the anchor is wrong; re-read `orders_store.py`.
- **STOP** if `test_modify_success_persists_price_and_qty` **passes before Step 4** — something already persists the price; the finding may be stale, re-verify against HEAD.
- **STOP** if the change requires editing more than these four files: `src/xenon/execution/orders_store.py`, `src/xenon/api/server.py`, `scripts/tests/test_orders_store_modify_sequence.py`, `src/xenon/api/tests/test_orders_routes_failures.py` (plus possibly one import line and the incident-history doc). If you find yourself editing `ib_order_manage.py`, `ib_activity_mirror.py`, or `register_from_snapshot`, you are drifting into the non-goal — STOP.
- **STOP** and use PAPER (`dev.sh paper`, port 4002) if any step seems to require live IB. Never point at live money (port 4001) for an order-path change.
- **STOP** if the existing `test_modify_by_perm_id_with_known_row_advances_sequence` starts failing. It should still pass: it modifies `newPrice: 1.50` on a row seeded at 1.23 — after this change the row's `limit_price` becomes 1.50, but that test only asserts `status`/`applied_sequence`, not price. If it fails for another reason, investigate before proceeding.
- **STOP** if Postgres is unreachable and the store/route tests SKIP rather than run — these tests need PG; a skip is not a pass. (`psql -h 100.66.147.98 -U xenon_dev core_test -c "SELECT 1"` to check.)

---

## 8. Rollback

Pure code + tests, no migration. To revert: `git checkout master -- src/xenon/execution/orders_store.py src/xenon/api/server.py scripts/tests/test_orders_store_modify_sequence.py src/xenon/api/tests/test_orders_routes_failures.py docs/reference/order-path-incident-history.md`, or discard the branch entirely (`git branch -D fix/modify-persists-price-qty`). No DB state to unwind — `confirm_modify` only ever writes columns that were already writable; existing rows are untouched until the next modify.

---

## 9. Incident-history row (append to `docs/reference/order-path-incident-history.md`)

Add as row **#24** to the `## Incident table` (columns: `#` | `Date / PR` | `Issue` | `Root cause` | `Solution` | `Prevention`). Fill the PR number/branch on open:

```
| 24  | 2026-07-05 fix/modify-persists-price-qty | Successful `/orders/modify` left `order_submissions.limit_price`/`quantity` frozen at pre-modify values for Xenon-authored (UUID) rows; the open-orders panel, reports, and audits (which read `limit_price` from PG via `routes/orders.py::orders_payload_for_scope`) permanently showed the old price. The drift mirror skips UUID rows (`SKIPPED_UUID`) and `ib_order_manage.py` never writes back, so no path corrected it. | `apply_modify`/`apply_modify_by_perm_id` advance only `modify_sequence`; they run pre-subprocess and are intentionally not rolled back on failure, so they never carried the new price/qty. No post-success writer existed. | Added `orders_store.confirm_modify(...)` — a post-success writer that rewrites `limit_price`/`quantity` guarded by `modify_sequence == <reserved seq>` AND `state IN (WORKING, PARTIALLY_FILLED)`, and records a `MODIFY_APPLIED` order-event (old→new). The guard is a single atomic UPDATE (conditions in the WHERE clause) so a confirm racing a newer reserve matches zero rows instead of writing a stale price. Wired into `_orders_modify_from_body` after the IB subprocess confirms success (best-effort — never fails a good broker modify). External TWS modifies on UUID rows remain a known gap (mirror unchanged) — snapshots carry no `modify_sequence`, so mirroring them onto UUID rows would race in-flight Xenon modifies. Accepted tradeoff: modify-B-reserved-then-failed leaves PG at pre-A values (stale, never wrong-direction) until the next modify/fill/sweep. | Store tests `test_confirm_modify_writes_price_and_qty` / `_price_only_leaves_quantity` / `_quantity_only_leaves_price` / `_both_none_is_noop` / `_loses_race_to_newer_reserve` / `_wrong_state_is_noop` / `_by_perm_id` (`scripts/tests/test_orders_store_modify_sequence.py`); route tests `test_modify_success_persists_price_and_qty` / `test_modify_subprocess_failure_does_not_persist_price` / `test_modify_by_perm_id_persists_price_and_qty` / `test_modify_price_only_leaves_quantity` (`src/xenon/api/tests/test_orders_routes_failures.py`). |
```

---

## 10. Repo invariants honored (do not violate)

- All Python via `uv run …`.
- No new JSON read/write on the order path (CI guards 3-5) — confirm write targets Postgres.
- State guard `state IN ('WORKING','PARTIALLY_FILLED')` + sequence-equality guard live **in the UPDATE's WHERE clause** (atomic — never SELECT-then-UPDATE for the guard), so a confirm racing a newer reserve matches zero rows; mirrors the "never clobber newer terminal states" principle and `mark_terminal(expected_states=…)`.
- Branch + PR; never push master; no AI attribution trailer in the commit.
- `XENON_READ_ONLY=1` unaffected — the route 403s before reaching `confirm_modify`.
- No change to combo/BAG action derivation, naked-short guard, or `ComboLeg.action` semantics.
- One change, one PR — the OP-4 twin (mirror UUID skip) is an explicit non-goal.
