# Plan: S3 — Runtime reconciliation sweep for stuck PENDING / UNCERTAIN rows in the activity poller

**Date:** 2026-07-05
**Branch:** `fix/poller-reconciliation-sweep` (based on `origin/master` @ v0.8.1)
**Finding:** OP-3 (Recovery), Severity **High** — `docs/fable/03-findings-table.md` row OP-3;
`docs/fable/10-roadmap.md` § S3.
**Goal (one line):** Every poll tick, scan `PENDING`/`UNCERTAIN` `order_submissions` rows and
resolve them against IB open orders + recent executions matched by `orderRef ↔ client_attempt_id`,
so a hung/killed place subprocess no longer leaves a row stuck until the next FastAPI restart.

---

## 1. Context — what exists today (verified `file:function` citations)

- **The poller runs one tick via** `run_activity_poll_tick(...)` in
  `src/xenon/api/services/ib_activity_mirror.py` (verified: `def run_activity_poll_tick` at
  line 293). Each tick: `_fetch_open_orders(client)` →
  `_sync_open_orders_to_postgres(open_orders, scope=scope)` → `_safe_fills_tick(...)` (which
  internally calls `_fetch_ib_executions` + `_record_external_fills`) → then, **only when both
  feeds succeeded**, `sweep_disappeared_orders(open_orders, scope=scope)`.
- **`sweep_disappeared_orders`** (same file, `def sweep_disappeared_orders` at line 125) already
  handles the _opposite_ case: `WORKING`/`PARTIALLY_FILLED` rows that **vanished** from the
  open-order snapshot → `FILLED` (fills cover qty) or `CANCELLED` (missing two consecutive
  sweeps, `reason_code=TWS_CANCEL_MIRROR`). It **never scans `PENDING` or `UNCERTAIN`.** That is
  the OP-3 gap.
- **The only place a stuck `PENDING` row is ever resolved today** is boot rehydrate:
  `single_leg_rehydrate.rehydrate_on_boot` → `_reconcile_from_three_sources` (file
  `src/xenon/execution/single_leg_rehydrate.py`). A `PENDING` row with no `ib_order_id` older than
  `PENDING_TIMEOUT_SECONDS = 60` (line 31) is set `FAILED` with `reason_code="PENDING_TIMEOUT"`
  (lines 104-118). This runs **once, at process boot** — never on a running server.
- **`orderRef` is already surfaced** by `fetch_open_orders` in `src/xenon/execution/ib_orders.py`
  (verified: `"orderRef": getattr(order, "orderRef", None) or None` at line 180). So each
  open-order dict in the list the poller already holds carries `orderRef`.
- **Executions do NOT yet carry `orderRef`.** `fetch_ib_executions` in
  `src/xenon/execution/ib_reconcile.py` (verified: `def fetch_ib_executions` at line 105) emits
  `exec_id, perm_id, ib_order_id, con_id, shares, price, …` but **not** the execution's
  `orderRef`. This plan adds one field.
- **`order_submissions.state` is a free `Text` column with NO `CHECK` constraint** (verified:
  `Column("state", Text, nullable=False)` at `src/xenon/db/schema.py:594`; the only order-sub
  CheckConstraints are `ck_order_sub_broker_ib_only` and `ck_order_sub_account_env`). Therefore
  **`UNCERTAIN` is storable with no migration**, and a new event `kind` (`RECONCILE_SWEEP`) needs
  no migration either (`order_events.kind` is free `Text`, `schema.py:646`).
- **The writer facade** is `src/xenon/execution/orders_store.py`. Relevant existing helpers:
  - `mark_terminal(*, submission_id, state, reason_code, filled_qty, avg_fill_price, expected_states=None) -> int`
    — a guarded UPDATE (rowcount) that only fires if the row is still in `expected_states`. Its
    `state` Literal is `FILLED|REJECTED|CANCELLED|FAILED|PARTIALLY_FILLED` — **it cannot write
    `WORKING`** and **does not backfill `ib_order_id`/`perm_id`**.
  - `record_event(submission_id, kind, detail)` — inserts one `order_events` row
    (columns `submission_id`, `kind`, `detail`). **This is the event-write helper name** the sweep
    must use (there is no `event_type`/`transition` helper in the module at HEAD).
- **Poller wiring** (`src/xenon/api/server.py::_maybe_start_activity_poller`, line 252): skipped in
  test mode (`_is_test_mode()`), skipped when `XENON_IB_ACTIVITY_POLLER` is off, skipped entirely
  under `XENON_READ_ONLY=1` (the whole lifespan block that calls it is guarded — server.py:541,
  `"XENON_READ_ONLY=1 — skipping rehydrate, fills replay, and activity poller"`). **A new sweep
  added inside `run_activity_poll_tick` inherits all three skips for free** — no new env plumbing.
- **Place subprocess timeout is 15 s per run, but the recovery helper can retry** (verified:
  `_run_ib_script_with_recovery("xenon-ib-place-order", ["--json", order_json], timeout=15)` at
  server.py:2277; on failure the helper may restart the IB Gateway (local mode) and re-run the
  entry point once — server.py ~3164-3220). Worst-case wall time ≈ 15 s + gateway-restart wait
  - 15 s. This is why the runtime FAILED gate is 120 s (see §5 constants), not 60 s.

### What the executor does NOT need to understand

- The IB connection pool / role-pinning internals — the sweep is a pure function of two lists
  (`open_orders`, `executions`) the tick already fetches, plus DB reads/writes. It never touches
  ib_async directly.
- The BAG envelope-vs-leg accounting in `sweep_disappeared_orders` — the new sweep does not
  compute fill economics; it matches by `orderRef` and reads aggregate exec shares/price.
- Clerk/auth, the relay, or any frontend surface — this change has **no UI-visible output** and no
  route changes.

---

## 2. Dependency on S2 (READ THIS FIRST)

**Prereq: S2 (`docs/superpowers/plans/2026-07-05-fable-s2-uncertain-orderref.md`, not yet written
at authoring time).** This plan is written against the S2 spec in
`docs/fable/11-code-sketches.md §1`. S2 provides three things this sweep relies on **in
production**:

1. **`order.orderRef = client_attempt_id`** is set on every IB-side order in
   `src/xenon/execution/ib_place_order.py`, so IB echoes `orderRef` back on open orders and
   executions. This is the join key: `orderRef == order_submissions.client_attempt_id`.
2. **The `UNCERTAIN` state exists** — S2's place handler transitions a row to `UNCERTAIN` (not
   `FAILED`) when the place subprocess dies/times out after a possible broker acceptance. S3 is
   what _resolves_ those `UNCERTAIN` rows on the next tick.
3. **The ack stdout protocol** (early `{"stage":"ack",...}` line) — **not consumed by S3**; listed
   only for completeness. S3 reads DB state + IB lists, never subprocess stdout.

**S3 is designed to be independently mergeable and independently testable even if S2 lands later.**
Concretely:

- The new tests seed `UNCERTAIN`/`PENDING` rows **directly in the DB** and pass **hand-built**
  `open_orders`/`executions` lists carrying `orderRef` — they never invoke S2 code.
- If S3 merges before S2: no production row is ever in `UNCERTAIN` yet, and IB orders won't carry
  `orderRef` (so `orderRef`-matching finds nothing) — but the **PENDING-timeout branch still
  works** exactly as boot rehydrate does today (age-based, no `orderRef` needed). No regression.
- S3 adds its **own** guarded writer (`orders_store.resolve_stuck_submission`, §5 Step 2) rather
  than depending on S2 adding a generic `transition()` helper. If S2 also adds `transition()`,
  the two coexist harmlessly; do **not** refactor S3 onto it in this PR (one change = one PR).

**Tripwire:** if, when you start, `orders_store.py` already contains a function named
`resolve_stuck_submission`, STOP and report — S2 or another branch may have already added the
writer; reconcile before proceeding.

---

## 3. Drift from review

- **Fable OP-3 cites `single_leg_rehydrate.py:31,104-118` and `ib_activity_mirror.py:293-344`.**
  Verified accurate at HEAD: `PENDING_TIMEOUT_SECONDS = 60` is line 31; the PENDING-timeout branch
  is lines 104-118; `run_activity_poll_tick` spans 293-344. No drift.
- **Fable remediation hint says "match via `orderRef`".** Confirmed feasible: `orderRef` is already
  on open orders (ib_orders.py:180) but **must be added to executions** (`fetch_ib_executions` does
  not surface it). Step 1 adds it. This is the one extra edit the fable row did not spell out.
- **No `state` CHECK constraint** exists, so — unlike the fable §5 sketch which appends a
  `CHECK (state IN (...))` — **S3 needs no migration.** (Adding that CHECK is S2/§5's concern, out
  of scope here.)

---

## 4. Goal / Non-goals

### Goal

1. Add `sweep_stuck_pending_uncertain(open_orders, executions, *, scope, now=None, grace=None)` to
   `ib_activity_mirror.py` implementing the decision table in §5 Step 3.
2. Surface the execution `orderRef` as `order_ref` in `fetch_ib_executions` (Step 1).
3. Add `orders_store.resolve_stuck_submission(...)` — one guarded UPDATE that transitions a stuck
   row to `WORKING`/`FILLED`/`FAILED` with id/qty backfill and an `expected_states` guard (Step 2).
4. Wire the new sweep into `run_activity_poll_tick` under the same "both feeds succeeded" guard,
   fetching executions **once** and passing them to both `_record_external_fills` and the sweep.

### Non-goals (explicitly NOT in this PR — one change, one PR)

- **S2** itself (setting `orderRef` in `ib_place_order`, the `UNCERTAIN` write in the place
  handler, the ack line). S3 assumes S2 or is inert without it (§2).
- **S4** (protecting the post-ack persist, OP-2) — separate roadmap item.
- **S5** (naked-short audit → state sync, OP-5).
- Adding a `CHECK (state IN (...))` constraint or a generic `transition()` writer (§5 sketch).
- Any change to `sweep_disappeared_orders` (the WORKING-disappearance sweep) — left untouched.
- Fill-row insertion for FILLED resolutions: the sweep backfills `perm_id` on the submission and
  runs **before** `record_external_fills` in the tick (§5 tick-order requirement), so the
  existing fill writer links the fill to the recovered row **in the same tick**; the sweep itself
  does not insert fills.

---

## 5. Key facts (verified against the working tree)

| Fact                               | Value                                                                                                | Verified at                              |
| ---------------------------------- | ---------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| Poller tick entry                  | `run_activity_poll_tick(*, ib_client_factory, scope, lookback_days=7)`                               | `ib_activity_mirror.py:293`              |
| Existing WORKING sweep             | `sweep_disappeared_orders(open_orders, *, scope, grace=None)`                                        | `ib_activity_mirror.py:125`              |
| Open-order dict fields             | `orderId`, `permId`, `orderRef`, `contract`, `totalQuantity`, …                                      | `ib_orders.py:162-182`                   |
| Exec dict fields (pre-change)      | `exec_id`, `perm_id`, `ib_order_id`, `shares`, `price`, … (no `order_ref`)                           | `ib_reconcile.py:118-139`                |
| PENDING timeout constant           | `PENDING_TIMEOUT_SECONDS = 60`                                                                       | `single_leg_rehydrate.py:31`             |
| submitted_at→epoch helper          | `_submitted_at_epoch(val)` (handles tz-aware + naive)                                                | `single_leg_rehydrate.py:56-69`          |
| Guarded terminal writer            | `mark_terminal(..., expected_states=...) -> int` (rowcount)                                          | `orders_store.py:529-562`                |
| Event writer                       | `record_event(submission_id, kind, detail)`                                                          | `orders_store.py:713-726`                |
| `order_submissions.state`          | free `Text`, **no CHECK**                                                                            | `schema.py:594`                          |
| `order_events.kind`                | free `Text`                                                                                          | `schema.py:646`                          |
| Scoped columns                     | `broker`, `account_env`, `broker_account` on every row                                               | `schema.py:602-604`                      |
| Join key                           | `order_submissions.client_attempt_id` (Text) ⇔ IB `orderRef`                                         | `schema.py:581`                          |
| Place subprocess timeout           | 15 s, single run (no retry)                                                                          | `server.py:2277`, `:3164`                |
| Poller skip conditions (inherited) | test mode, `XENON_IB_ACTIVITY_POLLER=0`, `XENON_READ_ONLY=1`                                         | `server.py:252-270,541`                  |
| Test fixture style for sweeps      | seed via `get_sync_engine()`, call sweep directly, **no `committed_db` marker** (Phase-2 shared txn) | `scripts/tests/test_tws_cancel_sweep.py` |

**Constants introduced by this plan** (module scope in `ib_activity_mirror.py`):

- `STUCK_PENDING_FAIL_AGE_S = 120` — a `PENDING` row absent from both feeds is only `FAILED`
  (`PENDING_TIMEOUT`) once older than this AND lacking an `ib_order_id`.
  **Justification (honest):** `_run_ib_script_with_recovery` is NOT single-shot — on failure it
  can restart the IB Gateway (local/non-docker mode) and re-run the entry point once
  (`server.py` ~3164-3220). Worst-case wall time for a live place attempt is therefore
  ~15 s (first run) + gateway-restart wait + 15 s (retry) — comfortably under 120 s with margin.
  The boot-rehydrate constant (`PENDING_TIMEOUT_SECONDS = 60`) is NOT reused for the runtime
  failure branch because at boot no subprocess can be alive (fresh process), while at runtime one
  can. **Matching transitions (→WORKING/→FILLED) carry no age gate at all** — writing
  WORKING+ids for an order IB itself reports is benign even against a still-live subprocess
  (`mark_submitted` writes the same values; `expected_states` guards the rest).
- `RECONCILE_SWEEP_KIND = "RECONCILE_SWEEP"` — the `order_events.kind` written on every applied
  transition.
- `_STUCK_SWEEP_GRACE: set[str] = set()` — module-level, mirrors `_SWEEP_GRACE`; holds
  `UNCERTAIN` submission_ids that were absent on the current sweep, so an `UNCERTAIN` row is only
  `FAILED` after being absent **two consecutive sweeps** (matches the existing one-tick-grace
  pattern). `PENDING` rows do **not** use grace — their 120 s fail-age is itself the grace.

**Tick-order requirement (load-bearing — fixes two review blockers):** the stuck sweep MUST run
**before** `_sync_open_orders_to_postgres` and **before** `_record_external_fills` in the tick:

1. `register_from_snapshot` matches existing rows by `perm_id` only and inserts a
   `snapshot-<permId>` row otherwise. A stuck UUID row has **no** `perm_id` yet — if the snapshot
   sync ran first, it would insert a **duplicate** row for the same broker order. Sweep-first
   backfills `perm_id` onto the UUID row, so the sync then takes its `SKIPPED_UUID`/update path.
2. `record_external_fills` resolves `order_fills.submission_id` by `perm_id` **at insert** and
   never backfills it on the `exec_id` conflict path. Sweep-first means the fill insert resolves
   to the recovered UUID row in the **same tick**; run after, the fill row would stay orphaned
   (`submission_id=NULL`) forever.

---

## 6. Decision table (the exact contract for `sweep_stuck_pending_uncertain`)

Scan rows where `state IN ('PENDING','UNCERTAIN')` for the given scope. Build two indexes:

- `open_by_ref = {str(oo["orderRef"]): oo for oo in open_orders if oo.get("orderRef")}`
- `exec_by_ref = {ref: {"shares", "value", "perm_id", "ib_order_id"}}` aggregated over executions
  that carry `order_ref` (sum shares; `value = Σ shares*price`; keep last non-null `perm_id`/`ib_order_id`).

Let `caid = row["client_attempt_id"]`, `age = now - epoch(submitted_at)`. Matching rows (1–2)
have **no age gate** — they only assert what IB itself reports (benign vs a live subprocess).
The FAILED branches are the guarded ones. `empty_snapshot = not open_orders and not executions`
(the RAW lists, mirroring `sweep_disappeared_orders`'s raw `not open_orders` guard — NOT the
ref-filtered maps, which would treat a snapshot of ref-less orders as empty).

| #   | Row state   | Condition                                                                       | Action                                                                                                                            | `expected_states`         | Event detail `resolution`              |
| --- | ----------- | ------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- | ------------------------- | -------------------------------------- |
| 1   | either      | `caid in open_by_ref`                                                           | → `WORKING`; backfill `perm_id=str(oo["permId"])`, `ib_order_id=str(oo["orderId"])`, `reason_code=None`; discard from grace       | `("PENDING","UNCERTAIN")` | `WORKING` / `matched:"open_order"`     |
| 2   | either      | else `caid in exec_by_ref`                                                      | → `FILLED`; backfill `perm_id`, `ib_order_id`, `filled_qty=int(round(shares))`, `avg_fill_price=value/shares`; discard from grace | `("PENDING","UNCERTAIN")` | `FILLED` / `matched:"execution"`       |
| 3   | `PENDING`   | else `not row["ib_order_id"]` **and** `age > STUCK_PENDING_FAIL_AGE_S` (120 s)  | → `FAILED`, `reason_code="PENDING_TIMEOUT"`; discard from grace                                                                   | `("PENDING",)`            | `FAILED` / `reason:"PENDING_TIMEOUT"`  |
| 4   | `PENDING`   | else (young, or has `ib_order_id` — it reached the broker; other sweeps own it) | **hold** (no write)                                                                                                               | —                         | —                                      |
| 5   | `UNCERTAIN` | else, **and** `empty_snapshot` (raw lists)                                      | **skip** (post-reconnect stale read — never trust absence); log warning                                                           | —                         | —                                      |
| 6   | `UNCERTAIN` | else `sid in grace` (absent last sweep too)                                     | → `FAILED`, `reason_code="UNCERTAIN_ABSENT"`; discard from grace                                                                  | `("UNCERTAIN",)`          | `FAILED` / `reason:"UNCERTAIN_ABSENT"` |
| 7   | `UNCERTAIN` | else (absent, first time)                                                       | **hold**; add `sid` (submission_id — the grace set is keyed by submission_id, not caid) to `missing_now`                          | —                         | —                                      |

The boot-rehydrate parity note: this matches `single_leg_rehydrate`'s PENDING-timeout semantics,
which also require `not ib_order_id` (`single_leg_rehydrate.py:103-106`) — a PENDING row WITH an
`ib_order_id` reached the broker and must be reconciled through IB sources, never age-failed.

After the loop: `grace.clear(); grace.update(missing_now)` (exactly the `UNCERTAIN` rows absent
this sweep — reappeared/resolved ids were discarded in-loop). Return
`{"working": w, "filled": f, "failed": x, "graced": g, "skipped": <str|absent>}`.

**Invariants honored:**

- Every write goes through `resolve_stuck_submission` with a narrow `expected_states` guard → a
  concurrent fill/user-cancel that already moved the row makes the UPDATE a rowcount-0 no-op
  (**never clobber a newer terminal state**).
- Event + counter are emitted **only when the write applied** (rowcount > 0), so a lost race emits
  nothing.
- Idempotent: once a row leaves `PENDING`/`UNCERTAIN` it is no longer selected, so re-running the
  sweep is a no-op.
- Empty-snapshot safety for `UNCERTAIN→FAILED` (row 6) mirrors the `sweep_disappeared_orders`
  empty-snapshot guard. **`PENDING→FAILED` (row 4) is intentionally NOT empty-guarded**: a `PENDING`
  row with no `ib_order_id` older than 60 s means the place never acked (never reached the broker),
  identical to boot rehydrate's unconditional `PENDING_TIMEOUT`.

---

## 7. Steps (strict order, TDD — failing test first, then implement, then green)

> All Python via `uv run …`. Never bare python/pip.

### Step 0 — branch

```bash
git checkout -b fix/poller-reconciliation-sweep origin/master
```

---

### Step 1 — surface `order_ref` on executions (TDD)

**1a. Failing test.** Append to `scripts/tests/test_ib_reconcile.py`:

```python
def test_fetch_ib_executions_surfaces_order_ref():
    """The sweep matches executions to submissions by orderRef == client_attempt_id,
    so fetch_ib_executions must carry the execution's orderRef through."""
    from types import SimpleNamespace

    from xenon.execution.ib_reconcile import fetch_ib_executions

    execution = SimpleNamespace(
        execId="0001", permId=777, orderId=555, orderRef="ca-abc-123",
        time="2026-07-05T14:30:00+00:00", side="BOT", shares=2.0, price=1.25,
        exchange="SMART",
    )
    contract = SimpleNamespace(conId=42, symbol="QQQ", secType="STK",
                               strike=0.0, lastTradeDateOrContractMonth="", right="")
    fill = SimpleNamespace(execution=execution, contract=contract,
                           commissionReport=SimpleNamespace(execId="0001", commission=1.0,
                                                            realizedPNL=None))

    class _Client:
        def get_fills(self):
            return [fill]

    rows = fetch_ib_executions(_Client())
    assert rows[0]["order_ref"] == "ca-abc-123"
```

Run it — MUST FAIL with `KeyError: 'order_ref'`:

```bash
uv run pytest scripts/tests/test_ib_reconcile.py::test_fetch_ib_executions_surfaces_order_ref -xvs
```

**Tripwire:** if it PASSES before the change, STOP — the field already exists; re-check HEAD.

**1b. Implement.** In `src/xenon/execution/ib_reconcile.py::fetch_ib_executions`, inside the dict
appended to `executions` (the block starting `"exec_id": getattr(e, "execId", None),`), add one
line right after the `"ib_order_id"` entry:

```python
                "exec_id": getattr(e, "execId", None),
                "perm_id": getattr(e, "permId", None),
                "ib_order_id": getattr(e, "orderId", None),
                "order_ref": getattr(e, "orderRef", None) or None,   # NEW (OP-3: sweep join key)
                "con_id": getattr(c, "conId", None),
```

Green: rerun the test above → 1 passed.

---

### Step 2 — add the guarded stuck-row writer to `orders_store` (TDD)

**2a. Failing test.** Create `scripts/tests/test_resolve_stuck_submission.py`:

```python
"""orders_store.resolve_stuck_submission — guarded transition used by the OP-3 poller sweep."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import insert, select

from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_submissions
from xenon.execution import orders_store

NOW = datetime(2026, 7, 5, 14, 30, tzinfo=timezone.utc)


def _seed(sid: str, state: str) -> None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(order_submissions).values(
                submission_id=sid, user_id="u1", client_attempt_id=f"ca-{sid}",
                ticker="QQQ", security_type="STK", action="BUY", quantity=2,
                limit_price=Decimal("700.00"), state=state, tif="DAY",
                submitted_at=NOW, updated_at=NOW, modify_sequence=0,
                broker="IB", account_env="paper", broker_account="DU1234567",
            )
        )


def _state(sid: str):
    engine = get_sync_engine()
    with engine.connect() as conn:
        return conn.execute(
            select(order_submissions.c.state, order_submissions.c.perm_id,
                   order_submissions.c.ib_order_id)
            .where(order_submissions.c.submission_id == sid)
        ).one()


def test_resolve_to_working_backfills_ids():
    _seed("s1", "UNCERTAIN")
    rc = orders_store.resolve_stuck_submission(
        submission_id="s1", to_state="WORKING",
        expected_states=("PENDING", "UNCERTAIN"),
        ib_order_id="555", perm_id="777",
    )
    assert rc == 1
    st, perm, ib = _state("s1")
    assert (st, perm, ib) == ("WORKING", "777", "555")


def test_expected_states_guard_is_a_noop_on_mismatch():
    _seed("s2", "FILLED")  # already terminal
    rc = orders_store.resolve_stuck_submission(
        submission_id="s2", to_state="FAILED",
        expected_states=("UNCERTAIN",), reason_code="UNCERTAIN_ABSENT",
    )
    assert rc == 0
    assert _state("s2")[0] == "FILLED"  # untouched — never clobber a newer terminal state
```

Run → MUST FAIL with `AttributeError: module 'xenon.execution.orders_store' has no attribute 'resolve_stuck_submission'`:

```bash
uv run pytest scripts/tests/test_resolve_stuck_submission.py -xvs
```

**2b. Implement.** In `src/xenon/execution/orders_store.py`, add this function immediately after
`mark_terminal` (i.e. after its `return result.rowcount` at line 562). It reuses the module's
existing imports (`update`, `datetime`, `timezone`, `Decimal`, `order_submissions`,
`get_sync_engine`, `Literal`):

```python
def resolve_stuck_submission(
    *,
    submission_id: str,
    to_state: Literal["WORKING", "FILLED", "FAILED"],
    expected_states: tuple[str, ...],
    ib_order_id: str | None = None,
    perm_id: str | None = None,
    filled_qty: int | None = None,
    avg_fill_price: Decimal | None = None,
    reason_code: str | None = None,
) -> int:
    """Guarded transition for the runtime reconciliation sweep (OP-3).

    Sets ``state=to_state`` only if the row is still in one of ``expected_states``
    (optimistic-concurrency guard — never clobber a newer terminal state written
    by a concurrent fill event or user cancel). Backfills ``ib_order_id`` /
    ``perm_id`` / fill columns when provided (a stuck row lost them when its place
    subprocess died before ``mark_submitted``). Returns the affected rowcount.
    """
    now = datetime.now(timezone.utc)
    values: dict = {"state": to_state, "reason_code": reason_code, "updated_at": now}
    if ib_order_id is not None:
        values["ib_order_id"] = str(ib_order_id)
    if perm_id is not None:
        values["perm_id"] = str(perm_id)
    if filled_qty is not None:
        values["filled_qty"] = filled_qty
    if avg_fill_price is not None:
        values["avg_fill_price"] = avg_fill_price
    engine = get_sync_engine()
    with engine.begin() as conn:
        result = conn.execute(
            update(order_submissions)
            .where(
                order_submissions.c.submission_id == submission_id,
                order_submissions.c.state.in_(expected_states),
            )
            .values(**values)
        )
    return result.rowcount
```

Green: rerun Step 2a test → 2 passed.

---

### Step 3 — the sweep function (TDD)

**3a. Failing test.** Create `scripts/tests/test_stuck_sweep.py`:

```python
"""OP-3: runtime sweep resolves stuck PENDING/UNCERTAIN rows in the poll tick.

Fidelity: seeds real rows via get_sync_engine (Phase-2 shared txn — no committed_db
marker, mirroring test_tws_cancel_sweep.py) and passes hand-built open_orders /
executions lists carrying orderRef. Does NOT invoke S2 code.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import insert, select

from xenon.api.services.ib_activity_mirror import sweep_stuck_pending_uncertain
from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_events, order_submissions
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DU1234567")
NOW_DT = datetime(2026, 7, 5, 14, 30, tzinfo=timezone.utc)
NOW = NOW_DT.timestamp()


def _seed(sid: str, state: str, *, caid: str, age_s: int, ib_order_id: str | None = None) -> None:
    submitted = NOW_DT - timedelta(seconds=age_s)
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(order_submissions).values(
                submission_id=sid, user_id="u1", client_attempt_id=caid,
                ticker="QQQ", security_type="STK", action="BUY", quantity=2,
                limit_price=Decimal("700.00"), state=state, tif="DAY",
                ib_order_id=ib_order_id,
                submitted_at=submitted, updated_at=submitted, modify_sequence=0,
                broker="IB", account_env="paper", broker_account="DU1234567",
            )
        )


def _row(sid: str):
    engine = get_sync_engine()
    with engine.connect() as conn:
        return conn.execute(
            select(order_submissions.c.state, order_submissions.c.perm_id,
                   order_submissions.c.ib_order_id, order_submissions.c.filled_qty,
                   order_submissions.c.reason_code)
            .where(order_submissions.c.submission_id == sid)
        ).one()


def _events(sid: str) -> list[tuple[str, dict]]:
    engine = get_sync_engine()
    with engine.connect() as conn:
        return [
            (r[0], r[1])
            for r in conn.execute(
                select(order_events.c.kind, order_events.c.detail)
                .where(order_events.c.submission_id == sid)
            )
        ]


def _oo(order_ref: str, *, order_id: int, perm_id: int) -> dict:
    return {"orderId": order_id, "permId": perm_id, "orderRef": order_ref,
            "contract": {"secType": "STK", "symbol": "QQQ"}}


# 7.4-2: UNCERTAIN + open order carrying orderRef -> WORKING with ids attached
def test_uncertain_resolves_to_working_with_ids():
    _seed("u1", "UNCERTAIN", caid="ca-u1", age_s=5)
    res = sweep_stuck_pending_uncertain(
        [_oo("ca-u1", order_id=555, perm_id=777)], [], scope=SCOPE, now=NOW)
    assert res["working"] == 1
    st, perm, ib, _fq, _rc = _row("u1")
    assert (st, perm, ib) == ("WORKING", "777", "555")
    kinds = [k for k, _ in _events("u1")]
    assert "RECONCILE_SWEEP" in kinds


# UNCERTAIN + matching execution -> FILLED with qty/avg
def test_uncertain_resolves_to_filled_from_execution():
    _seed("u2", "UNCERTAIN", caid="ca-u2", age_s=5)
    execs = [{"order_ref": "ca-u2", "perm_id": "778", "ib_order_id": "556",
              "shares": 2.0, "price": 700.5}]
    res = sweep_stuck_pending_uncertain([], execs, scope=SCOPE, now=NOW)
    assert res["filled"] == 1
    st, perm, ib, fq, _rc = _row("u2")
    assert st == "FILLED" and perm == "778" and int(fq) == 2


# PENDING older than 120s, absent, NO ib_order_id -> FAILED PENDING_TIMEOUT
def test_pending_timeout_fails():
    _seed("p1", "PENDING", caid="ca-p1", age_s=180)
    res = sweep_stuck_pending_uncertain([], [], scope=SCOPE, now=NOW)
    assert res["failed"] == 1
    st, _p, _i, _fq, rc = _row("p1")
    assert st == "FAILED" and rc == "PENDING_TIMEOUT"


# PENDING younger than the 120s fail-age -> left alone (recovery helper may
# still be running: 15s run + gateway restart + 15s retry)
def test_pending_young_is_left_alone():
    _seed("p2", "PENDING", caid="ca-p2", age_s=60)
    res = sweep_stuck_pending_uncertain([], [], scope=SCOPE, now=NOW)
    assert res == {"working": 0, "filled": 0, "failed": 0, "graced": 0}
    assert _row("p2")[0] == "PENDING"


# PENDING WITH an ib_order_id is NEVER age-failed (it reached the broker;
# rows 1-2 or the disappeared-orders sweep own its resolution) — mirrors
# single_leg_rehydrate's `not ib_order_id` condition.
def test_pending_with_ib_order_id_is_never_age_failed():
    _seed("p3", "PENDING", caid="ca-p3", age_s=600, ib_order_id="7010")
    res = sweep_stuck_pending_uncertain([], [], scope=SCOPE, now=NOW)
    assert res["failed"] == 0
    assert _row("p3")[0] == "PENDING"


# UNCERTAIN absent needs TWO sweeps (grace) before FAILED
def test_uncertain_absent_needs_two_sweeps():
    _seed("u3", "UNCERTAIN", caid="ca-u3", age_s=5)
    grace: set[str] = set()
    live = [_oo("ca-other", order_id=1, perm_id=1)]  # non-empty snapshot, no match
    r1 = sweep_stuck_pending_uncertain(live, [], scope=SCOPE, now=NOW, grace=grace)
    assert r1["graced"] == 1 and _row("u3")[0] == "UNCERTAIN"
    r2 = sweep_stuck_pending_uncertain(live, [], scope=SCOPE, now=NOW, grace=grace)
    assert r2["failed"] == 1
    st, _p, _i, _fq, rc = _row("u3")
    assert st == "FAILED" and rc == "UNCERTAIN_ABSENT"


# Empty snapshot (both feeds empty) must NOT fail an UNCERTAIN row (stale post-reconnect read)
def test_empty_snapshot_skips_uncertain_fail():
    _seed("u4", "UNCERTAIN", caid="ca-u4", age_s=5)
    grace: set[str] = set()
    # even on a second pass with the id already in grace, empty snapshot skips
    grace.add("u4")
    res = sweep_stuck_pending_uncertain([], [], scope=SCOPE, now=NOW, grace=grace)
    assert res.get("skipped") == "empty_snapshot"
    assert _row("u4")[0] == "UNCERTAIN"
```

Run → MUST FAIL with `ImportError: cannot import name 'sweep_stuck_pending_uncertain'`:

```bash
uv run pytest scripts/tests/test_stuck_sweep.py -xvs
```

**3b. Implement.** In `src/xenon/api/services/ib_activity_mirror.py`:

(i) Add module-scope constants + grace set, immediately after the existing
`_SWEEP_GRACE: set[str] = set()` block (after line 122):

```python
# --- OP-3 stuck-row sweep (PENDING/UNCERTAIN) ------------------------------
# Minimum age before an absent PENDING row (with no ib_order_id) is FAILED.
# _run_ib_script_with_recovery is NOT single-shot: on failure it can restart
# the IB Gateway (local mode) and re-run the place entry point once, so a live
# attempt's worst-case wall time is ~15s + restart wait + 15s. 120s covers that
# with margin. Matching transitions (→WORKING/→FILLED) carry no age gate —
# they only assert what IB itself reports, which is benign even against a
# still-live subprocess. Boot rehydrate keeps its own 60s constant (no
# subprocess can be alive at boot).
STUCK_PENDING_FAIL_AGE_S = 120

# UNCERTAIN submission_ids absent on the previous sweep. Two-sweep grace before
# FAILED (an order placed just before the tick could be absent for one read).
# PENDING rows do NOT use grace — their 120s fail-age is itself the grace.
# Module-level on purpose, same rationale as _SWEEP_GRACE (one scope per
# process today).
_STUCK_SWEEP_GRACE: set[str] = set()

RECONCILE_SWEEP_KIND = "RECONCILE_SWEEP"
```

(ii) Add the sweep function immediately after `sweep_disappeared_orders` (after its `return`
at line 290, before `def run_activity_poll_tick`):

```python
def sweep_stuck_pending_uncertain(
    open_orders: list[dict],
    executions: list[dict],
    *,
    scope: AccountScope,
    now: float | None = None,
    grace: set[str] | None = None,
) -> dict:
    """Resolve PENDING/UNCERTAIN rows stuck by a hung/killed place subprocess (OP-3).

    Matches IB open orders / recent executions by ``orderRef == client_attempt_id``:
      - present in open orders   -> WORKING (backfill perm_id/ib_order_id)
      - else present in an exec  -> FILLED  (backfill ids + qty/avg)
      - else PENDING age>60s     -> FAILED (PENDING_TIMEOUT)
      - else UNCERTAIN absent 2x -> FAILED (UNCERTAIN_ABSENT, one-tick grace)
    Every applied transition is guarded (expected_states) so a concurrent fill /
    user cancel is never clobbered, and writes a RECONCILE_SWEEP order_event.
    Idempotent and race-safe; see the plan's §6 decision table.
    """
    import time as _time
    from decimal import Decimal

    from sqlalchemy import select

    from xenon.db.engine import get_sync_engine
    from xenon.db.schema import order_submissions
    from xenon.execution import orders_store
    from xenon.execution.single_leg_rehydrate import _submitted_at_epoch

    now_ts = _time.time() if now is None else now
    tracked = _STUCK_SWEEP_GRACE if grace is None else grace

    open_by_ref: dict[str, dict] = {
        str(oo.get("orderRef")): oo for oo in (open_orders or []) if oo.get("orderRef")
    }
    exec_by_ref: dict[str, dict] = {}
    for ex in executions or []:
        ref = ex.get("order_ref")
        if not ref:
            continue
        shares = float(ex.get("shares") or 0)
        price = float(ex.get("price") or 0)
        agg = exec_by_ref.setdefault(
            str(ref), {"shares": 0.0, "value": 0.0, "perm_id": None, "ib_order_id": None}
        )
        agg["shares"] += shares
        agg["value"] += shares * price
        if ex.get("perm_id") is not None:
            agg["perm_id"] = str(ex.get("perm_id"))
        if ex.get("ib_order_id") is not None:
            agg["ib_order_id"] = str(ex.get("ib_order_id"))

    engine = get_sync_engine()
    with engine.connect() as conn:
        rows = (
            conn.execute(
                select(
                    order_submissions.c.submission_id,
                    order_submissions.c.client_attempt_id,
                    order_submissions.c.state,
                    order_submissions.c.submitted_at,
                    order_submissions.c.ib_order_id,
                ).where(
                    order_submissions.c.state.in_(("PENDING", "UNCERTAIN")),
                    order_submissions.c.broker == scope.broker,
                    order_submissions.c.account_env == scope.account_env,
                    order_submissions.c.broker_account == scope.broker_account,
                )
            )
            .mappings()
            .all()
        )

    # Raw lists, NOT the ref-filtered maps — a snapshot of ref-less orders is
    # still a real snapshot (mirrors sweep_disappeared_orders' raw guard).
    empty_snapshot = not open_orders and not executions
    working = filled = failed = graced = 0
    missing_now: set[str] = set()

    for row in rows:
        sid = row["submission_id"]
        caid = row["client_attempt_id"]
        state = row["state"]
        age_s = now_ts - _submitted_at_epoch(row["submitted_at"])

        oo = open_by_ref.get(str(caid)) if caid else None
        ex = exec_by_ref.get(str(caid)) if caid else None

        # Row 1: present in open orders -> WORKING (no age gate — benign).
        if oo is not None:
            applied = orders_store.resolve_stuck_submission(
                submission_id=sid,
                to_state="WORKING",
                expected_states=("PENDING", "UNCERTAIN"),
                ib_order_id=str(oo.get("orderId")) if oo.get("orderId") is not None else None,
                perm_id=str(oo.get("permId")) if oo.get("permId") is not None else None,
                reason_code=None,
            )
            tracked.discard(sid)
            if applied:
                orders_store.record_event(
                    sid, RECONCILE_SWEEP_KIND,
                    {"resolution": "WORKING", "matched": "open_order",
                     "from": state, "perm_id": str(oo.get("permId")),
                     "ib_order_id": str(oo.get("orderId"))},
                )
                working += 1
            continue

        # Row 2: present in an execution -> FILLED (no age gate — benign).
        if ex is not None and ex["shares"] > 0:
            shares_dec = Decimal(str(ex["shares"]))
            avg = Decimal(str(ex["value"])) / shares_dec
            applied = orders_store.resolve_stuck_submission(
                submission_id=sid,
                to_state="FILLED",
                expected_states=("PENDING", "UNCERTAIN"),
                ib_order_id=ex["ib_order_id"],
                perm_id=ex["perm_id"],
                filled_qty=int(round(ex["shares"])),
                avg_fill_price=avg,
                reason_code=None,
            )
            tracked.discard(sid)
            if applied:
                orders_store.record_event(
                    sid, RECONCILE_SWEEP_KIND,
                    {"resolution": "FILLED", "matched": "execution",
                     "from": state, "filled_qty": int(round(ex["shares"])),
                     "avg_fill_price": str(avg)},
                )
                filled += 1
            continue

        # Absent from both feeds.
        if state == "PENDING":
            # Row 3: FAILED only when the row never reached the broker
            # (no ib_order_id — mirrors single_leg_rehydrate:103) AND it is
            # older than the recovery helper's worst case (120s).
            if not row["ib_order_id"] and age_s > STUCK_PENDING_FAIL_AGE_S:
                applied = orders_store.resolve_stuck_submission(
                    submission_id=sid, to_state="FAILED",
                    expected_states=("PENDING",), reason_code="PENDING_TIMEOUT",
                )
                tracked.discard(sid)
                if applied:
                    orders_store.record_event(
                        sid, RECONCILE_SWEEP_KIND,
                        {"resolution": "FAILED", "reason": "PENDING_TIMEOUT",
                         "from": state, "age_seconds": round(age_s, 2)},
                    )
                    failed += 1
            # Row 4: young, or has ib_order_id (reached the broker — rows 1-2 /
            # the disappeared-orders sweep own it) -> hold (no write).
            continue

        # state == "UNCERTAIN", absent.
        # Row 5: empty snapshot -> never trust absence (post-reconnect stale read).
        if empty_snapshot:
            logger.warning(
                "stuck_sweep: empty snapshot with UNCERTAIN row %s — skipping fail", sid
            )
            continue
        # Row 6: absent two consecutive sweeps -> FAILED.
        if sid in tracked:
            applied = orders_store.resolve_stuck_submission(
                submission_id=sid, to_state="FAILED",
                expected_states=("UNCERTAIN",), reason_code="UNCERTAIN_ABSENT",
            )
            tracked.discard(sid)
            if applied:
                orders_store.record_event(
                    sid, RECONCILE_SWEEP_KIND,
                    {"resolution": "FAILED", "reason": "UNCERTAIN_ABSENT", "from": state},
                )
                failed += 1
            continue
        # Row 7: first absence -> hold in grace.
        missing_now.add(sid)
        graced += 1

    tracked.clear()
    tracked.update(missing_now)
    result = {"working": working, "filled": filled, "failed": failed, "graced": graced}
    if empty_snapshot and any(r["state"] == "UNCERTAIN" for r in rows):
        result["skipped"] = "empty_snapshot"
    return result
```

> Note on `test_empty_snapshot_skips_uncertain_fail`: the `result["skipped"] = "empty_snapshot"`
> key is set whenever the snapshot was empty **and** at least one UNCERTAIN row was scanned, so
> the test's assertion `res.get("skipped") == "empty_snapshot"` holds while the row stays
> UNCERTAIN. `test_pending_young_is_left_alone` asserts the exact dict **without** `skipped`
> (its snapshot is empty but it seeds a PENDING row, not UNCERTAIN — so the `any(... UNCERTAIN)`
> guard is False and no `skipped` key is added). Keep both behaviors.

Green: rerun Step 3a → all pass.

---

### Step 4 — wire the sweep into the tick + fetch executions once (TDD)

**4a. Failing/adjust test.** Add to `scripts/tests/test_ib_activity_mirror.py`:

```python
def test_run_activity_poll_tick_runs_stuck_sweep(monkeypatch, scope):
    """The tick must invoke the stuck-row sweep when both feeds succeed, passing
    the same open_orders + executions it fetched."""
    from xenon.api.services import ib_activity_mirror

    monkeypatch.setattr(
        ib_activity_mirror, "_fetch_open_orders",
        lambda c: [{"orderId": 1, "permId": 9, "orderRef": "ca-x",
                    "contract": {"secType": "STK", "symbol": "QQQ"}}],
    )
    monkeypatch.setattr(
        ib_activity_mirror, "_fetch_ib_executions",
        lambda c, lookback_days=7: [{"exec_id": "e1", "order_ref": "ca-y",
                                     "perm_id": "9", "shares": 1.0, "price": 2.0}],
    )
    call_order: list[str] = []
    captured = {}

    def fake_sync(oo, *, scope):
        call_order.append("sync")
        return {"registered": 0, "updated": 0, "skipped": 0, "open_count": 1}

    def fake_record(ex, *, scope):
        call_order.append("fills")
        return {"inserted": 0, "replayed": 0,
                "affected_legacy_ids": [], "affected_submission_ids": []}

    def fake_stuck(open_orders, executions, *, scope):
        call_order.append("stuck")
        captured["oo"] = open_orders
        captured["ex"] = executions
        return {"working": 0, "filled": 0, "failed": 0, "graced": 0}

    def fake_disappeared(oo, *, scope):
        call_order.append("disappeared")
        return {"filled": 0, "cancelled": 0, "graced": 0}

    monkeypatch.setattr(ib_activity_mirror, "_sync_open_orders_to_postgres", fake_sync)
    monkeypatch.setattr(ib_activity_mirror, "_record_external_fills", fake_record)
    monkeypatch.setattr(ib_activity_mirror, "sweep_stuck_pending_uncertain", fake_stuck)
    monkeypatch.setattr(ib_activity_mirror, "sweep_disappeared_orders", fake_disappeared)

    result = ib_activity_mirror.run_activity_poll_tick(
        ib_client_factory=lambda: object(), scope=scope)

    assert captured["oo"][0]["orderRef"] == "ca-x"
    assert captured["ex"][0]["order_ref"] == "ca-y"
    assert result["stuck_sweep"] == {"working": 0, "filled": 0, "failed": 0, "graced": 0}
    # ORDER IS LOAD-BEARING: the stuck sweep must backfill perm_id/ib_order_id
    # BEFORE the snapshot sync (else register_from_snapshot inserts a duplicate
    # snapshot-<permId> row) and BEFORE fill recording (else order_fills rows
    # insert with submission_id=NULL and are never backfilled).
    assert call_order == ["stuck", "sync", "fills", "disappeared"]
```

Run → MUST FAIL (`KeyError: 'stuck_sweep'` and/or `AttributeError` on the monkeypatch target
if the symbol isn't referenced yet):

```bash
uv run pytest scripts/tests/test_ib_activity_mirror.py::test_run_activity_poll_tick_runs_stuck_sweep -xvs
```

**4b. Implement.** In `run_activity_poll_tick` (lines 293-344), replace the fills + sweep tail.

Delete the now-unused `_safe_fills_tick` helper (lines 101-112) — it is referenced only by the
tick, which we are rewriting. (Grep-verified: no other caller in `src/` or `scripts/`.)

Replace the block from `if open_orders is None:` (the `oo_result` branch, line ~319) through the
end of the function (`return {...}`, line 344) with the code below. **The ordering is
load-bearing** (see §5 "Tick-order requirement"): the stuck sweep runs BEFORE the snapshot sync
and BEFORE fill recording, so the ids it backfills prevent `register_from_snapshot` from
inserting a duplicate `snapshot-<permId>` row and let `record_external_fills` resolve
`order_fills.submission_id` in the same tick.

```python
    try:
        executions = _fetch_ib_executions(client, lookback_days=lookback_days)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ib_activity_mirror tick: fetch_ib_executions failed: %s", exc)
        executions = None

    # OP-3 stuck sweep FIRST — before the snapshot sync and fill recording —
    # so that ids backfilled onto stuck UUID rows (a) stop
    # register_from_snapshot from inserting a duplicate snapshot-<permId> row
    # and (b) let record_external_fills resolve submission_id in this tick.
    # Runs only when BOTH fetches succeeded (never trust a partial view).
    if open_orders is not None and executions is not None:
        try:
            stuck_result = sweep_stuck_pending_uncertain(open_orders, executions, scope=scope)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ib_activity_mirror tick: stuck sweep failed: %s", exc)
            stuck_result = {"error": str(exc)}
    else:
        stuck_result = {"skipped": True}

    if open_orders is None:
        oo_result: dict = {"error": "fetch_open_orders failed"}
    else:
        try:
            oo_result = _sync_open_orders_to_postgres(open_orders, scope=scope)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ib_activity_mirror tick: sync_open_orders_to_postgres failed: %s", exc)
            oo_result = {"error": str(exc)}

    if executions is None:
        fills_result: dict = {"error": "fetch_ib_executions failed"}
    else:
        try:
            fills_result = _record_external_fills(executions, scope=scope)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ib_activity_mirror tick: record_external_fills failed: %s", exc)
            fills_result = {"error": str(exc)}

    # Disappearance sweep only when BOTH feeds succeeded this tick — a failed
    # open-order fetch would mass-cancel; missing fills data would misclassify
    # mid-tick fills.
    if open_orders is not None and "error" not in fills_result:
        try:
            sweep_result = sweep_disappeared_orders(open_orders, scope=scope)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ib_activity_mirror tick: cancel sweep failed: %s", exc)
            sweep_result = {"error": str(exc)}
    else:
        sweep_result = {"skipped": True}

    return {
        "open_orders": oo_result,
        "fills": fills_result,
        "cancel_sweep": sweep_result,
        "stuck_sweep": stuck_result,
    }
```

**NOTE for the executor:** the existing `if open_orders is None: oo_result ...` /
`oo_result = _sync_open_orders_to_postgres(...)` branch MOVES below the stuck sweep — delete the
original occurrence (it currently sits directly after `_fetch_open_orders`) so the sync is not
run twice. The `try: open_orders = _fetch_open_orders(client) ...` block itself stays where it
is, directly after the client factory.

(iii) Optional but recommended — surface the stuck-sweep counters in the loop's tick log.
In `activity_poller_loop`, after the existing `sweep = result.get("cancel_sweep") or {}`
(line 390), add:

```python
            stuck = result.get("stuck_sweep") or {}
```

and extend the `logger.info(...)` format string + args with
`" stuck[w=%s f=%s x=%s g=%s]"` and
`stuck.get("working"), stuck.get("filled"), stuck.get("failed"), stuck.get("graced")`.
(Log-only; no test asserts the exact string — keep it simple.)

Green: rerun Step 4a test **and** the pre-existing tick tests (they must stay green because the
new sweep is monkeypatched or DB-empty):

```bash
uv run pytest scripts/tests/test_ib_activity_mirror.py -xvs
```

**4c. The load-bearing integration test (REAL pipeline — proves the two review blockers are
closed).** Append to `scripts/tests/test_stuck_sweep.py`. Only the two IB fetches are stubbed;
`sweep_stuck_pending_uncertain`, `_sync_open_orders_to_postgres`, and `_record_external_fills`
all run for real against the test DB.

```python
def test_full_tick_recovers_stuck_row_without_duplicate_or_orphan_fill(monkeypatch):
    """BLOCKER regression (review): with the sweep ordered FIRST in the tick,
    (a) the snapshot sync must NOT insert a duplicate snapshot-<permId> row for
    a recovered UUID row, and (b) the fill row must link to the recovered
    submission in the SAME tick (record_external_fills resolves by perm_id at
    insert and never backfills)."""
    from sqlalchemy import func, select

    from xenon.api.services import ib_activity_mirror
    from xenon.db.schema import order_fills

    # Stuck UUID row: reserved, subprocess died before mark_submitted — no ids.
    _seed("uuid-int1", "PENDING", caid="ca-int1", age_s=30)
    # A second stuck row whose order FILLED at the broker.
    _seed("uuid-int2", "PENDING", caid="ca-int2", age_s=30)

    open_orders = [{
        "orderId": 7001, "permId": 999001, "orderRef": "ca-int1",
        "action": "BUY", "totalQuantity": 2, "limitPrice": 700.0, "tif": "DAY",
        "contract": {"secType": "STK", "symbol": "QQQ", "conId": 320227571,
                     "strike": 0.0, "right": "", "expiry": None},
    }]
    executions = [{
        "exec_id": "0000e1a2.686a1", "perm_id": 999002, "ib_order_id": 7002,
        "order_ref": "ca-int2", "con_id": 320227571, "time": NOW_DT.isoformat(),
        "symbol": "QQQ", "sec_type": "STK", "side": "BOT", "shares": 2.0,
        "price": 700.5, "exchange": "SMART", "commission": 1.0,
        "realized_pnl": 0, "commission_ready": True, "currency": "USD",
        "strike": None, "expiry": None, "right": None,
    }]
    monkeypatch.setattr(ib_activity_mirror, "_fetch_open_orders", lambda c: open_orders)
    monkeypatch.setattr(
        ib_activity_mirror, "_fetch_ib_executions",
        lambda c, lookback_days=7: executions,
    )

    result = ib_activity_mirror.run_activity_poll_tick(
        ib_client_factory=lambda: object(), scope=SCOPE)

    assert result["stuck_sweep"]["working"] == 1
    assert result["stuck_sweep"]["filled"] == 1

    engine = get_sync_engine()
    with engine.connect() as conn:
        # (a) No duplicate snapshot rows for either recovered order.
        dup_count = conn.execute(
            select(func.count()).select_from(order_submissions).where(
                order_submissions.c.submission_id.in_(
                    ("snapshot-999001", "snapshot-999002")
                )
            )
        ).scalar()
        assert dup_count == 0, "sweep-first ordering failed: duplicate snapshot row inserted"
        # Recovered rows carry the backfilled ids.
        st1 = conn.execute(
            select(order_submissions.c.state, order_submissions.c.perm_id).where(
                order_submissions.c.submission_id == "uuid-int1")
        ).one()
        assert tuple(st1) == ("WORKING", "999001")
        # (b) The fill row linked to the recovered submission in the SAME tick.
        fill_sub = conn.execute(
            select(order_fills.c.submission_id).where(
                order_fills.c.exec_id == "0000e1a2.686a1")
        ).scalar()
        assert fill_sub == "uuid-int2", "fill inserted before sweep backfilled perm_id (orphaned)"
```

> If `_sync_open_orders_to_postgres` rejects the open-order dict shape (KeyError on a field not
> listed here), READ `src/xenon/execution/ib_orders.py::sync_open_orders_to_postgres` and extend
> the dict with the missing field — do NOT stub the sync out; the whole point of this test is
> that the real writers run.

---

### Step 5 — full local verification (see §8 matrix).

---

## 8. Verification matrix (exhaustive)

Run each; the literal expected outcome is stated.

| #   | Command                                                                                                                                  | Expected                                                                                                                 |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| V1  | `uv run pytest scripts/tests/test_ib_reconcile.py::test_fetch_ib_executions_surfaces_order_ref -xvs`                                     | `1 passed`                                                                                                               |
| V2  | `uv run pytest scripts/tests/test_resolve_stuck_submission.py -xvs`                                                                      | `2 passed`                                                                                                               |
| V3  | `uv run pytest scripts/tests/test_stuck_sweep.py -xvs`                                                                                   | `8 passed` (6 sweep cases + pending-with-ib_order_id + the full-tick integration test)                                   |
| V4  | `uv run pytest scripts/tests/test_ib_activity_mirror.py -xvs`                                                                            | all pass incl. `test_run_activity_poll_tick_runs_stuck_sweep`; **no** existing test regresses                            |
| V5  | `uv run pytest scripts/tests/test_tws_cancel_sweep.py -xvs`                                                                              | all pass (disappearance sweep untouched)                                                                                 |
| V6  | `uv run pytest scripts/tests/test_single_leg_rehydrate.py scripts/tests/test_record_external_fills_commission_lag.py -xvs`               | all pass (`PENDING_TIMEOUT_SECONDS`/`_submitted_at_epoch` reuse + exec-field addition didn't break rehydrate/commission) |
| V7  | `uv run python scripts/infra/dev/run_pytest_affected.py`                                                                                 | exit 0                                                                                                                   |
| V8  | `uv run python scripts/checks/no_json_fallback_on_order_path.py`                                                                         | exit 0                                                                                                                   |
| V9  | `uv run python scripts/checks/no_json_write_on_order_path.py`                                                                            | exit 0                                                                                                                   |
| V10 | `uv run python scripts/checks/order_path_caller_allowlist.py`                                                                            | exit 0                                                                                                                   |
| V11 | `uv run ruff check src/xenon/api/services/ib_activity_mirror.py src/xenon/execution/orders_store.py src/xenon/execution/ib_reconcile.py` | `All checks passed!` (or no findings)                                                                                    |

**Web / typecheck / lint:** **N/A** — no `web/` file touched. Do NOT run `npm`/`tsc`/Playwright.

**E2E browser:** **N/A** — this change has **no UI-visible surface** (a background poller writing
state that the portfolio/orders UI already renders from `order_submissions`). Per the brief, E2E is
mandatory only for UI-visible changes; state a one-line justification in the PR body and skip.

**CI guard rationale:** the sweep writes only to Postgres via `orders_store` (no `data/*.json`
read/write on an order path), so V8–V10 pass unchanged. The `orders_store` addition imports nothing
new.

**Live probe (PAPER ONLY — optional, do not gate the PR on it):** if a paper stack is up
(`scripts/infra/dev.sh paper`, IB port 4002), you can observe the sweep by seeding a synthetic
stuck row and watching one tick resolve it:

```bash
# Confirm the poller is alive and scope resolved:
curl -s http://localhost:8421/health | uv run python -m json.tool | grep -A2 order_submissions
# Seed an UNCERTAIN row + watch it flip WORKING/FAILED within <= 2 ticks:
psql "$DATABASE_URL_PAPER" -c "SELECT submission_id,state,perm_id FROM xenon.order_submissions WHERE state IN ('PENDING','UNCERTAIN') ORDER BY submitted_at DESC LIMIT 5;"
```

Never run this against live IB (port 4001) — order-path checks are PAPER-only per repo policy.

**Migration checks:** **N/A** — no schema change (`state`/`kind` are free `Text`, no CHECK).

---

## 9. Tripwires / abort criteria (STOP and report)

1. **If any Step's "failing test" PASSES before you make its change** → the anchor is wrong or the
   feature already exists. STOP; re-read HEAD.
2. **If `orders_store.py` already defines `resolve_stuck_submission`** at start → STOP (S2/another
   branch beat you to it); reconcile before adding a duplicate.
3. **If `fetch_ib_executions` already emits `order_ref`** → skip Step 1's code edit (keep the test).
4. **If a `CHECK` constraint on `order_submissions.state` appears** (grep
   `ck_order_sub.*state` in `schema.py` / migrations) → STOP; `UNCERTAIN`/writes may be rejected —
   that means S2 added the CHECK and you must ensure it lists `UNCERTAIN` (do NOT add a migration
   here; coordinate with the S2 branch).
5. **If more than these 8 files need edits** — `ib_reconcile.py`, `orders_store.py`,
   `ib_activity_mirror.py`, the 4 new/edited test files (`test_ib_reconcile.py`,
   `test_resolve_stuck_submission.py`, `test_stuck_sweep.py`, `test_ib_activity_mirror.py`), and
   the `docs/reference/order-path-incident-history.md` row append (§11) — STOP
   and report; the scope has grown beyond the plan.
6. **If any existing test in `test_ib_activity_mirror.py` regresses** and the fix isn't an obvious
   monkeypatch-target adjustment → STOP; the tick refactor changed observable behavior it
   shouldn't have.
7. **If a live step would require live IB (port 4001)** → use PAPER (`dev.sh paper`, port 4002) or
   skip. Never live money.
8. **Do NOT commit.** Leave the branch for review (`/review-cycle` or `/codex-review` before merge,
   per repo policy). Open a PR — never `git push origin master`.

---

## 10. Rollback

- Pre-merge: `git checkout master && git branch -D fix/poller-reconciliation-sweep`.
- No migration → nothing to down-revision.
- Post-merge hot-disable without a revert: set `XENON_IB_ACTIVITY_POLLER=0` on the affected process
  to stop the whole poller (both sweeps); or revert the single commit — the sweep is purely
  additive (new function + one tick-tail rewrite), so a `git revert` restores the prior tick
  behavior cleanly. The new `orders_store.resolve_stuck_submission` and the `order_ref` exec field
  are inert if unused.

---

## 11. Incident-history row (append to `docs/reference/order-path-incident-history.md`)

Append as row **24** (bump if a higher row exists at implement time):

```
| 24  | 2026-07-05 fix/poller-reconciliation-sweep | A place subprocess that hung or was killed after (possibly) reaching the broker left its `order_submissions` row stuck in `PENDING` (or S2's `UNCERTAIN`) until the next FastAPI restart — the 60s `PENDING_TIMEOUT` ran only at boot rehydrate; the activity poller never scanned `PENDING`/`UNCERTAIN`. Hung subprocess ⇒ row stuck for the whole process lifetime; the live order re-imported only as a disconnected `snapshot-*` row (OP-3, High). | No runtime reconciliation: `sweep_disappeared_orders` handled only `WORKING`/`PARTIALLY_FILLED` disappearance; nothing resolved forward-stuck `PENDING`/`UNCERTAIN`. `fetch_ib_executions` also didn't surface `orderRef`, so a stuck row (whose `perm_id` was never persisted) couldn't be matched to its fill/open-order. | Added `sweep_stuck_pending_uncertain` to `ib_activity_mirror.py`, run each tick when both feeds succeed and ordered FIRST — before `sync_open_orders_to_postgres` and `record_external_fills` — so backfilled ids prevent a duplicate `snapshot-<permId>` row and let the fill writer resolve `submission_id` in the same tick. Match IB open orders / executions by `orderRef == client_attempt_id` → `WORKING` (backfill ids) / `FILLED` (backfill ids+qty/avg) / `FAILED` (`PENDING_TIMEOUT` only when no `ib_order_id` AND age>120s — the recovery helper can gateway-restart and retry, so 60s is not safe at runtime; or `UNCERTAIN_ABSENT` after two absent sweeps). Guards: matching transitions carry no age gate (benign vs a live subprocess); empty-snapshot skip (raw lists) for `UNCERTAIN→FAILED` (never trust a post-reconnect stale read); `orderRef` added to `fetch_ib_executions`; new guarded writer `orders_store.resolve_stuck_submission` (`expected_states` — never clobber a newer terminal state); every applied transition emits a `RECONCILE_SWEEP` order_event. Prereq S2 (sets `order.orderRef`+writes `UNCERTAIN`); inert-but-safe if merged first. No migration (`state`/`kind` are free Text). | `scripts/tests/test_stuck_sweep.py` (8 cases: WORKING/FILLED/PENDING_TIMEOUT/young-hold/ib_order_id-never-age-failed/two-sweep-grace/empty-snapshot/full-tick integration proving no duplicate row + same-tick fill linkage), `test_resolve_stuck_submission.py` (2), `test_ib_reconcile.py::test_fetch_ib_executions_surfaces_order_ref`, `test_ib_activity_mirror.py::test_run_activity_poll_tick_runs_stuck_sweep`. **Watch pattern:** a boot-only reconciliation leaves a long-running server blind to mid-session stuck rows — pair every boot-time recovery with a poller-tick equivalent. |
```
