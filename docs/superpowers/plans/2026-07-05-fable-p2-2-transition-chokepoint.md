# Plan: P2.2 — `transition()` state chokepoint + `order_submissions.state` CHECK constraint + `expected_states` at every terminal write

**Date:** 2026-07-05
**Branch:** `fix/order-state-transition-chokepoint`
**Findings:** OP-8 (terminal-state clobber races), OP-9 (no CHECK on `order_submissions.state`) — `docs/fable/03-findings-table.md` rows OP-8/OP-9; `docs/fable/10-roadmap.md` § P2.2
**Severity:** Medium
**Goal (one line):** Introduce one guarded `transition()` writer in `orders_store` (legal-edge set incl. the S2/S3 `UNCERTAIN` edges; the `register_from_snapshot` RESURRECT edges live in a separate private set so ordinary writers can never revive a terminal row), route every `order_submissions.state` writer through it so a guard-miss can never clobber a newer state and every transition writes an `order_events` row in the same transaction, and add a Postgres `CHECK` constraint pinning `state` to the known 9-value set.

**Prereqs (this plan lands AFTER all three merge):**

- **S2** — `docs/superpowers/plans/2026-07-05-fable-s2-uncertain-orderref.md`. Adds the `UNCERTAIN` state, the `orders_store.mark_uncertain(...)` writer (writes an `AMBIGUOUS_ACK` event in-txn), and `expected_states=` on the place handler's own FAILED/REJECTED/UNCERTAIN writes.
- **S3** — `docs/superpowers/plans/2026-07-05-fable-s3-poller-reconciliation-sweep.md`. Adds `orders_store.resolve_stuck_submission(...)` (guarded UPDATE, backfills ids/qty; the caller writes a separate `RECONCILE_SWEEP` event) and the `UNCERTAIN→WORKING/FILLED/FAILED` runtime edges.
- **S5** — `docs/superpowers/plans/2026-07-05-fable-s5-naked-short-audit-state.md`. Makes the naked-short audit call `mark_terminal(state="CANCELLED", reason_code="NAKED_SHORT_AUDIT")`. Adds no new state and no new writer.

None of S2/S3/S5 add an Alembic migration, so the Alembic head is unchanged when this plan runs (verify in Step 6).

This is a **live order-path change**. Any live check is **PAPER only** (`scripts/infra/dev.sh paper`, IB port 4002). Never test against live money.

---

## 1. Context — what exists today (verified at HEAD + against the S2/S3/S5 specs)

`order_submissions` is the single-leg order table. Its `state` column is free `Text` with **no CHECK constraint** (`src/xenon/db/schema.py:594` — `Column("state", Text, nullable=False)`), unlike `trades.state` which has `ck_trades_state` (`schema.py:120`). That is OP-9.

The state writers on `order_submissions` at HEAD (sync facade `src/xenon/execution/orders_store.py`), plus the two added by S2/S3:

| Writer                                                                          | State(s) written                                             | Guard today                                                                                         | Writes event in-txn?                  |
| ------------------------------------------------------------------------------- | ------------------------------------------------------------ | --------------------------------------------------------------------------------------------------- | ------------------------------------- |
| `reserve_attempt` (`orders_store.py:86`)                                        | INSERT `PENDING`                                             | n/a (INSERT)                                                                                        | no                                    |
| `mark_submitted` (`orders_store.py:485`)                                        | `WORKING`                                                    | **none — unconditional UPDATE**                                                                     | no (caller records `IB_ACK`)          |
| `mark_terminal` (`orders_store.py:529`)                                         | `FILLED\|REJECTED\|CANCELLED\|FAILED\|PARTIALLY_FILLED`      | optional `expected_states=` (default **None = unguarded**)                                          | no (callers record semantic events)   |
| `register_from_snapshot` (`orders_store.py:246`)                                | INSERT `WORKING`; RESURRECT terminal→`WORKING`; drift UPDATE | INSERT dedupe + inline `existing_state in {...}` branch; already writes `IB_MIRROR_*` events in-txn | yes (own)                             |
| `mark_uncertain` (S2)                                                           | `UNCERTAIN`                                                  | `expected_states=("PENDING",)`                                                                      | yes (`AMBIGUOUS_ACK`)                 |
| `resolve_stuck_submission` (S3)                                                 | `WORKING\|FILLED\|FAILED`                                    | required `expected_states=`                                                                         | no (caller records `RECONCILE_SWEEP`) |
| `single_leg_rehydrate._update_state_only` (`single_leg_rehydrate.py:631`)       | `WORKING`, `UNKNOWN`                                         | **none — raw UPDATE via `combo_wizard.update_order_state`**                                         | no (caller records `REHYDRATE_*`)     |
| `single_leg_rehydrate` line 610 → `mark_terminal(...)`                          | `FILLED\|CANCELLED\|FAILED\|PARTIALLY_FILLED`                | **no `expected_states` passed**                                                                     | no                                    |
| `ib_activity_mirror.sweep_disappeared_orders` (`ib_activity_mirror.py:253,266`) | `FILLED`, `CANCELLED`                                        | `expected_states=("WORKING","PARTIALLY_FILLED")` (already guarded)                                  | no (caller records)                   |

The full runtime state set observed in code is **9 values**: `PENDING`, `WORKING`, `PARTIALLY_FILLED`, `FILLED`, `REJECTED`, `CANCELLED`, `FAILED`, `UNKNOWN`, `UNCERTAIN`. `UNKNOWN` is real and load-bearing — `single_leg_rehydrate._reconcile_from_three_sources` returns `to_state="UNKNOWN"` when positions moved but no fill could be reconciled (`single_leg_rehydrate.py:188`), applied via `_update_state_only`. `UNCERTAIN` is added by S2. There is **no other value** written to `order_submissions.state` anywhere in `src/` (the combo-wizard `state` values — `planned/working/submitting/protected/ABORTED` — belong to the **separate** `wizard_combo_sessions`/`wizard_combo_attempts` tables, NOT `order_submissions`; leave them entirely alone).

`order_events` uses column **`kind`** (`schema.py:646`), not `event_type`. `record_event(submission_id, kind, detail)` inserts one row (`orders_store.py:713`).

### What the executor does NOT need to understand

- The place-order subprocess, ack streaming, combo wizard, relay, Futu, naked-short guard math, Clerk/auth. This change is confined to the `orders_store` writer facade, its `single_leg_rehydrate` caller, the schema, and one migration. No route handler changes. No web changes.
- `db/queries/orders.py`'s **async** `mark_terminal`/`mark_submitted` (`orders.py:79,100`) are a separate async CRUD layer imported **only** by `src/xenon/db/tests/test_orders.py` (grep-verified — no production caller). They are OUT OF SCOPE for the `transition()` migration; the new CHECK constraint still backstops them at the DB level. Do not touch them.

---

## 2. Drift from review

1. **Sketch §5 (`11-code-sketches.md`) uses `event_type=` on the events insert.** The real column is `kind`. Use `kind`.
2. **Sketch §5's `LEGAL` omits several real edges.** It lacks the `UNKNOWN` edges (rehydrate writes `UNKNOWN`), the `PENDING→{PARTIALLY_FILLED,FILLED,CANCELLED}` and `WORKING→{REJECTED,FAILED}` edges that rehydrate/place actually perform, and it only comments the RESURRECT edge. The real `LEGAL_TRANSITIONS` in Step 1 is the verified union of every edge current code performs — do not shrink it.
3. **Sketch §5 says "the single writer every state change must use".** Taken literally that means routing `register_from_snapshot`'s multi-query RESURRECT/INSERT transaction through `transition()`. That table function already does a guarded UPDATE + a same-transaction `IB_MIRROR_RESURRECT`/`IB_MIRROR_UPDATE` event — i.e. it is already transition-equivalent — and rewriting its three-branch body carries real regression risk for benefit that the CHECK constraint already provides. **Decision: leave `register_from_snapshot` inline; list its RESURRECT edge in `LEGAL_TRANSITIONS` for completeness.** This is a deliberate minimal-churn call, not a gap.
4. **`07-testing-review.md §7.4-3` recommends `hypothesis`.** `hypothesis` is **not** a project dependency and adding one risks `uv sync --frozen` CI churn. The property test is implemented as a deterministic exhaustive sweep over `ORDER_STATES × ORDER_STATES` plus a threaded concurrency race — same invariants, no new dep. Do not add `hypothesis`.
5. Cited line numbers in `03-findings-table.md` (`orders_store.py:536-551`, `ib_activity_mirror.py:259,272`) have drifted; anchors below use function names + snippets.

---

## 3. Goal / Non-goals

### Goal

1. Add `orders_store.transition(...)` — one guarded state-transition primitive with a `LEGAL_TRANSITIONS` edge set and an `ORDER_STATES` value set. It raises `ValueError` on an illegal edge (programming error), applies a guarded UPDATE, writes exactly one `order_events` row in the **same transaction** on success, and returns `False` (writing nothing) on a guard-miss race.
2. Route every `order_submissions.state` writer through `transition()`: `mark_submitted`, `mark_terminal`, `mark_uncertain` (S2), `resolve_stuck_submission` (S3), and `single_leg_rehydrate._update_state_only`. Preserve every public signature (zero-break shim style) — the bodies delegate. Add `expected_states=` to the one unguarded `mark_terminal` call in `single_leg_rehydrate`.
3. Add a Postgres `CHECK (state IN (…9…))` constraint (`ck_order_sub_state`) on `order_submissions.state` via a new Alembic migration, mirrored in `schema.py`. Include a pre-migration distinct-state audit and a defensive remap rule.
4. Add the property test (illegal edges rejected; every legal edge recorded with an event) + the concurrency race test (one winner) to `scripts/tests/test_orders_submissions_store.py`.

### Non-goals (explicitly NOT in this PR — one change, one PR)

- Re-plumbing `register_from_snapshot` through `transition()` (Drift §3).
- The async `db/queries/orders.py` writers (test-only; §1).
- Anything in S2/S3/S5 (assumed merged). Do not re-add `UNCERTAIN`, `mark_uncertain`, `resolve_stuck_submission`, or the S5 audit write — they already exist when this plan runs.
- P2.3 (modify persists price/qty), P2.4 (`_orders_place_from_body` decomposition), combo wizard state, any UI/route/relay/Futu change.

---

## 4. Key facts (verified)

- `orders_store.py` already imports at module top: `from datetime import datetime, timezone`; `from sqlalchemy import Text, cast, func, insert, literal, select, update`; `from xenon.db.engine import get_sync_engine`; `from xenon.db.schema import order_events, order_fills, order_submissions, regime_overrides`. `transition()` needs one new import: `from sqlalchemy.engine import Connection`.
- `_TERMINAL_STATES = {"REJECTED", "CANCELLED", "FAILED"}` (`orders_store.py:63`) is consumed by `reserve_attempt` idempotency logic. **Do not modify it** — it is unrelated to `transition()`'s state set.
- `mark_terminal` currently returns `result.rowcount` (0 or 1); callers use it as truthy (`if applied:`) and `sweep_disappeared_orders`/`resolve_stuck_submission` count on 0/1. Keep returning `int`.
- `mark_submitted` returns `None` today and updates `regime_overrides` in the same transaction after the state UPDATE. Preserve both.
- `record_event`/callers write these semantic `kind`s (membership-asserted by tests, never exact-count on the terminal paths — verified): `IB_ACK`, `IB_REJECT`, `AMBIGUOUS_ACK`, `RECONCILE_SWEEP`, `RECONCILED`, `TWS_CANCEL_MIRROR`, `REHYDRATE_RECONCILED`, `REHYDRATE_UNCERTAIN`. An **additional** generic transition event is therefore safe: `test_place_quote_gate.py:329` asserts `("PREFLIGHT_ACK_LIMIT",) in rows` (membership), `test_single_leg_rehydrate.py` asserts `any(k == "REHYDRATE_RECONCILED")` (membership). The only `assert len(events) == 1` sites are for `register_from_snapshot` drift (`test_orders_store_snapshot_drift.py:174`, untouched) and `record_fill`/wizard outbox (different tables).
- Alembic head at HEAD is `2026_06_22_positions_currency` (from `uv run alembic heads`). S2/S3/S5 add no migration → still the head when this runs. Migration API pattern (verified `eaec7f146df5_...py`): `op.create_check_constraint(name, table, condition, schema="xenon")` / `op.drop_constraint(name, table, schema="xenon", type_="check")`.
- Test home `scripts/tests/test_orders_submissions_store.py` is `pytestmark = pytest.mark.committed_db` (real committed transactions — required for the concurrency race) with a `db_path` fixture (legacy no-op env var) and a `threading.Barrier` concurrency precedent (`test_reserve_attempt_concurrent_only_one_winner`).

---

## 5. Steps (strictly ordered — TDD: failing test first, then implement, then green)

> All Python via `uv run …`. Do not edit `.env`. Do not commit until the user says so.

### Step 0 — Branch

```bash
cd /Users/chenxi/projects/xenon
git checkout -b fix/order-state-transition-chokepoint origin/master
```

STOP if the working tree is dirty with unrelated changes — report and wait. STOP if `orders_store.py` does **not** already contain `mark_uncertain` and `resolve_stuck_submission` — that means S2/S3 have not merged and this plan's prereqs are unmet; report and wait.

---

### Step 1 — `transition()` primitive + `LEGAL_TRANSITIONS` + `ORDER_STATES` (TDD)

**1a. Failing test.** Append to `scripts/tests/test_orders_submissions_store.py` (the file is already `committed_db`; reuse its imports and the `db_path` fixture). Add a direct-insert seeding helper and the property + concurrency tests:

```python
import threading  # already imported at top of file — do not duplicate

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import insert, select
from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_events, order_submissions
from xenon.execution import orders_store
from xenon.execution.orders_store import (
    LEGAL_TRANSITIONS,
    ORDER_STATES,
    transition,
)

_NOW = datetime(2026, 7, 5, 14, 30, tzinfo=timezone.utc)


def _seed_state(sid: str, state: str) -> None:
    """Insert one order_submissions row directly in an arbitrary (valid) state."""
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(order_submissions).values(
                submission_id=sid,
                user_id="local",
                client_attempt_id=f"ca-{sid}",
                ticker="AAPL",
                security_type="STK",
                action="BUY",
                quantity=1,
                limit_price=Decimal("210.0000"),
                state=state,
                tif="DAY",
                submitted_at=_NOW,
                updated_at=_NOW,
                modify_sequence=0,
                broker="IB",
                account_env="paper",
                broker_account="DU111",
            )
        )


def _events_of(sid: str) -> list[tuple[str, dict]]:
    engine = get_sync_engine()
    with engine.connect() as conn:
        return [
            (r[0], r[1])
            for r in conn.execute(
                select(order_events.c.kind, order_events.c.detail).where(
                    order_events.c.submission_id == sid
                )
            )
        ]


def _state_of(sid: str) -> str:
    engine = get_sync_engine()
    with engine.connect() as conn:
        return conn.execute(
            select(order_submissions.c.state).where(
                order_submissions.c.submission_id == sid
            )
        ).scalar()


def test_transition_illegal_edges_raise_value_error(db_path):
    """Every (from, to) NOT in LEGAL_TRANSITIONS (and not a self-edge) must
    raise ValueError before touching the DB (illegal transitions rejected)."""
    illegal = [
        (f, t)
        for f in ORDER_STATES
        for t in ORDER_STATES
        if f != t and (f, t) not in LEGAL_TRANSITIONS
    ]
    assert illegal, "expected some illegal edges to exist"
    for f, t in illegal:
        with pytest.raises(ValueError):
            transition("sid-illegal", from_states=(f,), to=t, event="X")


def test_transition_legal_edges_apply_and_record_event(db_path):
    """Every legal edge applies the state change and writes exactly one event
    keyed by `kind`, in the same transaction (append-only completeness)."""
    for i, (f, t) in enumerate(sorted(LEGAL_TRANSITIONS)):
        sid = f"sid-legal-{i}"
        _seed_state(sid, f)
        ok = transition(sid, from_states=(f,), to=t, event="TEST_EDGE")
        assert ok is True, (f, t)
        assert _state_of(sid) == t, (f, t)
        evs = [(k, d) for k, d in _events_of(sid) if k == "TEST_EDGE"]
        assert len(evs) == 1, (f, t, evs)
        assert evs[0][1]["from"] == [f]
        assert evs[0][1]["to"] == t


def test_transition_guard_miss_returns_false_no_event(db_path):
    """A row not in from_states → rowcount 0 → False and no event (never clobber)."""
    _seed_state("sid-guard", "FILLED")
    ok = transition("sid-guard", from_states=("PENDING",), to="WORKING", event="TEST_EDGE")
    assert ok is False
    assert _state_of("sid-guard") == "FILLED"
    assert _events_of("sid-guard") == []


def test_transition_concurrent_race_single_winner(db_path):
    """Two threads racing the same PENDING→WORKING transition: exactly one wins
    (rowcount 1 + one event), the other loses (False, no event)."""
    _seed_state("sid-race", "PENDING")
    results: list[bool] = []
    barrier = threading.Barrier(2)

    def _go():
        barrier.wait()
        results.append(
            transition("sid-race", from_states=("PENDING",), to="WORKING", event="TEST_RACE")
        )

    threads = [threading.Thread(target=_go) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert sum(1 for r in results if r) == 1, results
    assert _state_of("sid-race") == "WORKING"
    race_events = [k for k, _ in _events_of("sid-race") if k == "TEST_RACE"]
    assert len(race_events) == 1
```

Run it — MUST fail (`ImportError: cannot import name 'transition'`). If it passes/imports, STOP (S-something already added it — reconcile).

```bash
uv run pytest scripts/tests/test_orders_submissions_store.py -x -k "transition_" 2>&1 | tail -20
```

**1b. Implement.** In `src/xenon/execution/orders_store.py`:

(i) Add the import beside the existing SQLAlchemy import block (top of file):

```python
from sqlalchemy.engine import Connection
```

(ii) Add the state set, legal edges, predecessor helper, and `transition()` **immediately after** the `_TERMINAL_STATES = {...}` line (`orders_store.py:63`) and before `@dataclass class SubmissionRow`:

```python
# ── Order state machine (OP-8 / OP-9) ──────────────────────────────────────
# The complete set of values `order_submissions.state` may hold. Mirrored by
# the `ck_order_sub_state` CHECK constraint (see schema.py + the migration).
ORDER_STATES: frozenset[str] = frozenset(
    {
        "PENDING",
        "WORKING",
        "PARTIALLY_FILLED",
        "FILLED",
        "REJECTED",
        "CANCELLED",
        "FAILED",
        "UNKNOWN",
        "UNCERTAIN",
    }
)

# Legal (from -> to) edges. This is the verified union of every transition the
# order code actually performs. Self-edges (from == to) are always legal
# (idempotent re-assert) and are NOT listed. An edge absent here is a
# programming error and makes transition() raise ValueError — that, plus the
# DB CHECK constraint, is what keeps illegal states out.
#
# UNCERTAIN edges: S2 performs ONLY PENDING -> UNCERTAIN (its mark_uncertain
# guard explicitly refuses to downgrade a WORKING row — see the S2 plan's
# test_mark_uncertain_does_not_clobber_working); S3 resolves
# UNCERTAIN -> WORKING/FILLED/FAILED. There is deliberately NO
# WORKING -> UNCERTAIN edge.
#
# RESURRECT edges (terminal/UNKNOWN -> WORKING) are kept in a SEPARATE private
# set below — they exist only for register_from_snapshot (IB still reports a
# row we had marked terminal as open), which applies them inline with its own
# guarded UPDATE + IB_MIRROR_RESURRECT event. Keeping them out of
# LEGAL_TRANSITIONS means _legal_from_states() can never hand a resurrection
# edge to an ordinary writer (OP-8: a mark_submitted/rehydrate write must not
# be able to clobber a newer terminal state back to WORKING).
LEGAL_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        # from PENDING
        ("PENDING", "WORKING"),
        ("PENDING", "PARTIALLY_FILLED"),
        ("PENDING", "FILLED"),
        ("PENDING", "REJECTED"),
        ("PENDING", "CANCELLED"),
        ("PENDING", "FAILED"),
        ("PENDING", "UNKNOWN"),
        ("PENDING", "UNCERTAIN"),
        # from WORKING
        ("WORKING", "PARTIALLY_FILLED"),
        ("WORKING", "FILLED"),
        ("WORKING", "CANCELLED"),
        ("WORKING", "REJECTED"),
        ("WORKING", "FAILED"),
        ("WORKING", "UNKNOWN"),
        # from PARTIALLY_FILLED
        ("PARTIALLY_FILLED", "FILLED"),
        ("PARTIALLY_FILLED", "CANCELLED"),
        ("PARTIALLY_FILLED", "UNKNOWN"),
        # from UNCERTAIN (S3 reconciliation sweep)
        ("UNCERTAIN", "WORKING"),
        ("UNCERTAIN", "FILLED"),
        ("UNCERTAIN", "FAILED"),
    }
)

# Private to register_from_snapshot's inline RESURRECT branch. NEVER merged
# into LEGAL_TRANSITIONS and NEVER reachable via _legal_from_states().
RESURRECT_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("CANCELLED", "WORKING"),
        ("FILLED", "WORKING"),
        ("REJECTED", "WORKING"),
        ("FAILED", "WORKING"),
        ("UNKNOWN", "WORKING"),
    }
)


def _legal_from_states(to_state: str) -> tuple[str, ...]:
    """All states from which `to_state` is a legal (or self) transition.

    Used when a caller does not pin `expected_states`: the guard defaults to
    every legal predecessor of the target, so the write still cannot land on
    an illegal predecessor (OP-8) but stays permissive for legitimate ones.
    """
    froms = {f for (f, t) in LEGAL_TRANSITIONS if t == to_state}
    froms.add(to_state)  # self-edge always legal (idempotent re-assert)
    return tuple(sorted(froms))
    # NOTE (OP-8): RESURRECT_TRANSITIONS is deliberately excluded, so
    # _legal_from_states("WORKING") == ("PENDING", "UNCERTAIN", "WORKING") —
    # an ordinary writer can never revive a terminal row. If the property test
    # finds real code performing an edge missing from LEGAL_TRANSITIONS
    # (e.g. rehydrate re-asserting WORKING on a PARTIALLY_FILLED row), add that
    # GENUINE edge explicitly with a justification comment — never by merging
    # the resurrection set.


def transition(
    submission_id: str,
    *,
    from_states: tuple[str, ...],
    to: str,
    event: str,
    detail: dict | None = None,
    conn: Connection | None = None,
    **cols,
) -> bool:
    """Single guarded state-transition chokepoint for `order_submissions` (OP-8/OP-9).

    Applies ``state = to`` only if the row is currently in one of
    ``from_states`` (optimistic-concurrency guard — a concurrent writer that
    already moved the row makes this a rowcount-0 no-op, so a newer state is
    never clobbered). On success it writes exactly one ``order_events`` row
    (``kind = event``) in the SAME transaction and returns ``True``; on a
    guard-miss it returns ``False`` and writes nothing.

    Raises ``ValueError`` if any ``(from_state, to)`` pair is not in
    ``LEGAL_TRANSITIONS`` (and not a self-edge) — an illegal edge is a
    programming error and must never reach the DB.

    Extra column writes (``ib_order_id``, ``perm_id``, ``filled_qty``,
    ``avg_fill_price``, ``reason_code``, ``placing_client_id`` …) pass through
    ``**cols``. ``state`` and ``updated_at`` are managed here and must not
    appear in ``cols``. Pass ``conn`` to enlist in an existing transaction
    (so the state change + event + any sibling writes commit atomically).
    """
    if to not in ORDER_STATES:
        raise ValueError(f"transition: unknown target state {to!r}")
    for f in from_states:
        if f == to:
            continue
        if (f, to) not in LEGAL_TRANSITIONS:
            raise ValueError(f"transition: illegal edge {f!r} -> {to!r}")
    for reserved in ("state", "updated_at"):
        if reserved in cols:
            raise ValueError(f"transition: {reserved!r} is managed by transition()")

    now = datetime.now(timezone.utc)
    values = {"state": to, "updated_at": now, **cols}

    def _run(c: Connection) -> bool:
        res = c.execute(
            update(order_submissions)
            .where(
                order_submissions.c.submission_id == submission_id,
                order_submissions.c.state.in_(from_states),
            )
            .values(**values)
        )
        if res.rowcount == 0:
            return False
        c.execute(
            insert(order_events).values(
                submission_id=submission_id,
                kind=event,
                detail={**(detail or {}), "from": list(from_states), "to": to},
            )
        )
        return True

    if conn is not None:
        return _run(conn)
    engine = get_sync_engine()
    with engine.begin() as owned:
        return _run(owned)
```

Run the Step-1a tests → all green:

```bash
uv run pytest scripts/tests/test_orders_submissions_store.py -x -k "transition_"
```

---

### Step 2 — Route `mark_submitted` / `mark_terminal` / `mark_uncertain` / `resolve_stuck_submission` through `transition()` (delegation, signatures unchanged)

No new test file here — the existing store/route/rehydrate/sweep suites are the regression net (Step 5). Each edit preserves the public signature and observable return.

**2a. `mark_submitted`** — replace the body of `def mark_submitted` (`orders_store.py:485`, currently ends by updating `order_submissions` then `regime_overrides`) with a version that delegates the state UPDATE to `transition()` on the same connection and keeps the `regime_overrides` update:

```python
def mark_submitted(
    *,
    submission_id: str,
    ib_order_id: str,
    perm_id: str | None,
    placing_client_id: int | None,
) -> None:
    def _int_or_none(value: str | None) -> int | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None

    perm_id_int = _int_or_none(perm_id)
    ib_order_id_int = _int_or_none(ib_order_id)
    engine = get_sync_engine()
    with engine.begin() as conn:
        # Guard is every legal predecessor of WORKING (effectively unconditional,
        # matching prior behavior) but can never land WORKING on an illegal
        # predecessor, and now writes an audit event in the same transaction.
        transition(
            submission_id,
            from_states=_legal_from_states("WORKING"),
            to="WORKING",
            event="ORDER_WORKING",
            conn=conn,
            ib_order_id=str(ib_order_id),
            perm_id=str(perm_id) if perm_id is not None else None,
            placing_client_id=placing_client_id,
        )
        conn.execute(
            update(regime_overrides)
            .where(regime_overrides.c.submission_id == submission_id)
            .values(
                perm_id=perm_id_int,
                ib_order_id=ib_order_id_int,
            )
        )
```

**2b. `mark_terminal`** — replace the body of `def mark_terminal` (`orders_store.py:529`, currently builds a `stmt` and returns `result.rowcount`) with a delegation. **Keep the signature and the `int` return.** When `expected_states` is not given, default to the legal predecessors of the target (this is the OP-8 tightening: a `None` guard no longer means "clobber anything"):

```python
def mark_terminal(
    *,
    submission_id: str,
    state: Literal["FILLED", "REJECTED", "CANCELLED", "FAILED", "PARTIALLY_FILLED"],
    reason_code: str | None,
    filled_qty: int,
    avg_fill_price: Decimal | None,
    expected_states: tuple[str, ...] | None = None,
) -> int:
    """Transition a submission to a terminal (or PARTIALLY_FILLED) state.
    Returns the affected rowcount (0 = guard blocked the write).

    ``expected_states`` is the optimistic-concurrency guard. When omitted it
    defaults to every legal predecessor of ``state`` (``_legal_from_states``),
    so an already-terminal row is never clobbered (OP-8). Routes through
    ``transition()`` — the single guarded chokepoint — which also writes an
    ``ORDER_TERMINAL`` audit event in the same transaction.
    """
    from_states = expected_states if expected_states is not None else _legal_from_states(state)
    applied = transition(
        submission_id,
        from_states=from_states,
        to=state,
        event="ORDER_TERMINAL",
        detail={"reason_code": reason_code},
        reason_code=reason_code,
        filled_qty=filled_qty,
        avg_fill_price=avg_fill_price,
    )
    return 1 if applied else 0
```

**2c. `mark_uncertain` (added by S2)** — locate it (grep `def mark_uncertain`) and replace its body so it delegates to `transition()` instead of doing its own UPDATE + `AMBIGUOUS_ACK` insert. Keep the exact signature S2 defined:

```python
def mark_uncertain(
    *,
    submission_id: str,
    detail: dict,
    expected_states: tuple[str, ...] = ("PENDING",),
) -> int:
    """Transition a submission to the non-terminal ``UNCERTAIN`` state.

    Routes through ``transition()`` (the single guarded chokepoint), which
    writes the ``AMBIGUOUS_ACK`` event in the same transaction and guards
    against downgrading a row that already reached ``WORKING``.
    """
    applied = transition(
        submission_id,
        from_states=expected_states,
        to="UNCERTAIN",
        event="AMBIGUOUS_ACK",
        detail=detail,
        reason_code="ORDER_STATUS_UNCERTAIN",
    )
    return 1 if applied else 0
```

> STOP if S2's `mark_uncertain` signature differs from the above (different kwargs / default) — reconcile to the merged S2 signature before editing; only the body changes.

**2d. `resolve_stuck_submission` (added by S3)** — locate it (grep `def resolve_stuck_submission`) and replace its UPDATE body with a `transition()` delegation. Keep S3's exact signature and `int` return:

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

    Routes through ``transition()``; backfills ids/qty when provided. Returns
    the affected rowcount (0 = a concurrent writer already moved the row).
    """
    cols: dict = {"reason_code": reason_code}
    if ib_order_id is not None:
        cols["ib_order_id"] = str(ib_order_id)
    if perm_id is not None:
        cols["perm_id"] = str(perm_id)
    if filled_qty is not None:
        cols["filled_qty"] = filled_qty
    if avg_fill_price is not None:
        cols["avg_fill_price"] = avg_fill_price
    applied = transition(
        submission_id,
        from_states=expected_states,
        to=to_state,
        event="STUCK_RESOLVED",
        detail={"reason_code": reason_code},
        **cols,
    )
    return 1 if applied else 0
```

> STOP if S3's `resolve_stuck_submission` signature differs — reconcile to the merged S3 signature; only the body changes. If the merged S3 `expected_states` for a `FAILED` resolution can include a value that makes `(f, "FAILED")` illegal (only `PENDING`/`UNCERTAIN`→`FAILED` are legal), that is a real bug to surface — but per S3's decision table the only `FAILED` guards are `("PENDING",)` and `("UNCERTAIN",)`, both legal, so no change is needed.

**Gate:**

```bash
uv run pytest scripts/tests/test_orders_submissions_store.py -x
```

---

### Step 3 — `single_leg_rehydrate`: guard the two remaining unguarded writes (TDD)

**3a. Failing test.** Append to `scripts/tests/test_single_leg_rehydrate.py` a test that a WORKING/UNKNOWN state shift is now guarded and records an event. Read the top of that file first for its seeding/`db_path` helpers and mirror them. Minimal case targeting `_update_state_only`:

```python
def test_update_state_only_is_guarded_and_records_event(db_path):
    """_update_state_only now routes through transition(): it applies only from
    a legal predecessor and writes a REHYDRATE_STATE event."""
    from xenon.execution import single_leg_rehydrate as slr
    from xenon.execution import orders_store

    sid = _seed_working_row(db_path)  # helper already used elsewhere in this file
    slr._update_state_only(sid, "UNKNOWN")

    engine = orders_store.get_sync_engine()
    from sqlalchemy import select
    from xenon.db.schema import order_submissions, order_events
    with engine.connect() as conn:
        state = conn.execute(
            select(order_submissions.c.state).where(order_submissions.c.submission_id == sid)
        ).scalar()
        kinds = [
            r[0]
            for r in conn.execute(
                select(order_events.c.kind).where(order_events.c.submission_id == sid)
            )
        ]
    assert state == "UNKNOWN"
    assert "REHYDRATE_STATE" in kinds
```

> If `_seed_working_row` does not exist under that name, grep this file for its existing WORKING-row seeding helper and use it; do not invent a new production helper.

Run → MUST fail (`REHYDRATE_STATE` not emitted today — `_update_state_only` uses `combo_wizard.update_order_state`, which writes no event). If it passes, STOP.

**3b. Implement.** In `src/xenon/execution/single_leg_rehydrate.py`:

(i) Replace `_update_state_only` (`single_leg_rehydrate.py:631`):

```python
def _update_state_only(submission_id: str, state: str) -> None:
    # Route through the single guarded chokepoint. from_states defaults to every
    # legal predecessor of `state`, so a concurrent terminal write is never
    # clobbered, and the shift is recorded as a REHYDRATE_STATE event in-txn.
    orders_store.transition(
        submission_id,
        from_states=orders_store._legal_from_states(state),
        to=state,
        event="REHYDRATE_STATE",
    )
```

(ii) Add `expected_states` to the unguarded `mark_terminal` call at `single_leg_rehydrate.py:610`. The row being reconciled here is `WORKING`, `PARTIALLY_FILLED`, or a `PENDING` row that carries an `ib_order_id` (it reached the broker). Pass exactly those as the guard:

```python
                orders_store.mark_terminal(
                    submission_id=row["submission_id"],
                    state=decision.to_state,
                    reason_code=decision.reason_code,
                    filled_qty=decision.filled_qty
                    if decision.filled_qty is not None
                    else int(row.get("filled_qty") or 0),
                    avg_fill_price=decision.avg_fill_price,
                    expected_states=("PENDING", "WORKING", "PARTIALLY_FILLED"),
                )
```

> Note the PENDING-timeout `mark_terminal` path in this file (`to_state="FAILED"`, `reason_code="PENDING_TIMEOUT"`) goes through the same `mark_terminal` at line 610 only if it reaches that branch; the young-PENDING/`PENDING`-no-`ib_order_id` timeout is handled by the reconcile-decision branch that returns `FAILED` — it too flows through this guarded call. `("PENDING", …)` is a legal predecessor of `FAILED`, so no separate change is needed.

**Gate:**

```bash
uv run pytest scripts/tests/test_single_leg_rehydrate.py -x
```

---

### Step 4 — `schema.py`: add the CHECK constraint to the table definition

Add the constraint to the `order_submissions` `Table(...)` in `src/xenon/db/schema.py`, immediately after the existing `CheckConstraint("account_env IN (...)", name="ck_order_sub_account_env")` block (so `schema.py` matches the migration and any future autogenerate diff is clean):

```python
    CheckConstraint(
        "state IN ('PENDING','WORKING','PARTIALLY_FILLED','FILLED',"
        "'REJECTED','CANCELLED','FAILED','UNKNOWN','UNCERTAIN')",
        name="ck_order_sub_state",
    ),
```

No test for this alone — the migration (Step 6) and the full suite validate it.

---

### Step 5 — Regression sweep of every affected caller

Run the writer-adjacent suites and confirm green (these exercise `mark_terminal`/`mark_submitted`/`mark_uncertain`/`resolve_stuck_submission`/rehydrate/sweeps through the new chokepoint):

```bash
uv run pytest \
  scripts/tests/test_orders_submissions_store.py \
  scripts/tests/test_single_leg_rehydrate.py \
  scripts/tests/test_tws_cancel_sweep.py \
  scripts/tests/test_orders_store_snapshot_drift.py \
  scripts/tests/test_stuck_sweep.py \
  scripts/tests/test_resolve_stuck_submission.py \
  scripts/tests/test_naked_short_audit.py \
  scripts/tests/test_orders_place_uncertain_route.py \
  src/xenon/db/tests/test_orders.py \
  -x
```

> `test_stuck_sweep.py` / `test_resolve_stuck_submission.py` exist only after S3 merges; `test_orders_place_uncertain_route.py` only after S2. If a file is absent, its prereq did not merge — STOP and report. Any FAILURE here means a delegation changed observable behavior — read the failure; if a test asserted the OLD clobber-anything behavior of `mark_terminal(expected_states=None)`, that test encodes the OP-8 bug and must be updated to the guarded expectation (note it in the PR), but a failure anywhere else means the delegation is wrong — fix the code, not the test.

---

### Step 6 — Alembic migration: `ck_order_sub_state` (with pre-audit + defensive remap)

**6a. Confirm the head** (must be unchanged by S2/S3/S5):

```bash
uv run alembic heads
```

Expect a single head. Record its revision id as `<HEAD>` (expected `2026_06_22_positions_currency`). If there are multiple heads, STOP — a prereq unexpectedly added a migration; reconcile the `down_revision` before continuing.

**6b. Pre-migration data audit** (dev DB `core_test`, via the paper URL — read-only SELECT):

```bash
psql "$DATABASE_URL_TEST" -c "SELECT state, count(*) FROM xenon.order_submissions GROUP BY state ORDER BY state;"
```

Record the output. **Decision rule for unexpected values:** the 9 allowed states are `PENDING, WORKING, PARTIALLY_FILLED, FILLED, REJECTED, CANCELLED, FAILED, UNKNOWN, UNCERTAIN`. Any row whose `state` is NOT one of these is remapped to `UNKNOWN` (the literal "we don't know" state) by the migration's `upgrade()` before the CHECK is added — this guarantees the constraint applies and preserves the row as auditable rather than dropping it. If the audit shows an unexpected value, note it in the PR description (it is a data-integrity signal worth a human glance) but do NOT hand-edit rows — the migration handles it deterministically.

**6c. Create the migration.** New file `src/xenon/db/migrations/versions/2026_07_05_order_sub_state_check.py`:

```python
"""add ck_order_sub_state CHECK constraint on order_submissions.state (OP-9)

Pins order_submissions.state to the known 9-value order state machine. Any
legacy row outside the set is remapped to 'UNKNOWN' before the constraint is
added, so the migration always applies.

Revision ID: 2026_07_05_order_sub_state_check
Revises: 2026_06_22_positions_currency
Create Date: 2026-07-05

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2026_07_05_order_sub_state_check"
down_revision: Union[str, Sequence[str], None] = "2026_06_22_positions_currency"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALLOWED = (
    "'PENDING','WORKING','PARTIALLY_FILLED','FILLED',"
    "'REJECTED','CANCELLED','FAILED','UNKNOWN','UNCERTAIN'"
)


def upgrade() -> None:
    # Defensive remap: any state outside the known set becomes UNKNOWN so the
    # CHECK can be added without failing on legacy/oddity rows.
    op.execute(
        f"UPDATE xenon.order_submissions SET state = 'UNKNOWN' "
        f"WHERE state NOT IN ({_ALLOWED})"
    )
    op.create_check_constraint(
        "ck_order_sub_state",
        "order_submissions",
        f"state IN ({_ALLOWED})",
        schema="xenon",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_order_sub_state", "order_submissions", schema="xenon", type_="check"
    )
```

> If Step 6a reported a head other than `2026_06_22_positions_currency`, set both the `Revises:` docstring line and `down_revision` to that head. Do not run `alembic revision --autogenerate` (it would also try to diff unrelated drift); write this file by hand as above.

**6d. Apply + assert the constraint on core_test:**

```bash
uv run alembic upgrade head
psql "$DATABASE_URL_TEST" -c "\d+ xenon.order_submissions" | grep -A2 "ck_order_sub_state"
```

Expect the `\d+` output to contain a line like:
`"ck_order_sub_state" CHECK (state = ANY (ARRAY['PENDING'::text, 'WORKING'::text, … 'UNCERTAIN'::text]))`

**6e. Prove the constraint bites** (a bad state is rejected):

```bash
psql "$DATABASE_URL_TEST" -c "INSERT INTO xenon.order_submissions (submission_id,user_id,client_attempt_id,ticker,security_type,action,quantity,state,tif,submitted_at,updated_at,broker,account_env,broker_account) VALUES ('ck-probe','x','x','AAPL','STK','BUY',1,'BOGUS','DAY',now(),now(),'IB','paper','DU111');"
```

Expect: `ERROR: new row for relation "order_submissions" violates check constraint "ck_order_sub_state"`. (No cleanup needed — the row was rejected.)

**6f. Prove downgrade works, then re-upgrade:**

```bash
uv run alembic downgrade -1
psql "$DATABASE_URL_TEST" -c "\d+ xenon.order_submissions" | grep "ck_order_sub_state" || echo "constraint gone (expected)"
uv run alembic upgrade head
```

Expect `constraint gone (expected)` after downgrade, and the constraint present again after the re-upgrade.

---

### Step 7 — Docs + CHANGELOG + incident history

- `CHANGELOG.md` under `## [Unreleased]` → `### Changed` (or `### Fixed`):

  ```markdown
  - **Order state changes now flow through a single guarded `transition()`
    chokepoint (OP-8) and `order_submissions.state` is pinned by a Postgres
    CHECK constraint to the 9-value state machine (OP-9).** Every terminal /
    working / uncertain / rehydrate / reconciliation-sweep write is now an
    optimistic-guarded transition that records an `order_events` row in the
    same transaction and cannot clobber a newer state; illegal edges raise.
  ```

- `docs/reference/order-path-incident-history.md` — append the row from §9 (next sequential number after S2/S3/S5's rows).
- `src/xenon/CLAUDE.md` § Order Execution Modules — add one line under the `orders_store.py` bullet: "state changes go through the single `transition()` chokepoint (`LEGAL_TRANSITIONS`); `order_submissions.state` is CHECK-pinned to the 9-value set." Keep it short; do not restructure.

---

## 6. Verification matrix (MANDATORY — exact commands + expected outcomes)

### Unit (Python)

| Command                                                                                                     | Expected                                                                                                                                                               |
| ----------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `uv run pytest scripts/tests/test_orders_submissions_store.py -x -k "transition_"`                          | pass — illegal edges raise `ValueError`; every legal edge applies + writes 1 event with `from/to`; guard-miss → `False` + no event; concurrent race → exactly 1 winner |
| `uv run pytest scripts/tests/test_orders_submissions_store.py -x`                                           | pass — existing reserve/mark tests still green through the delegated writers                                                                                           |
| `uv run pytest scripts/tests/test_single_leg_rehydrate.py -x`                                               | pass — `_update_state_only` guarded + emits `REHYDRATE_STATE`; guarded `mark_terminal`                                                                                 |
| `uv run pytest scripts/tests/test_tws_cancel_sweep.py scripts/tests/test_orders_store_snapshot_drift.py -x` | pass — disappeared-orders sweep + snapshot drift unchanged (drift still `len(events)==1`, register_from_snapshot untouched)                                            |
| `uv run pytest scripts/tests/test_stuck_sweep.py scripts/tests/test_resolve_stuck_submission.py -x`         | pass — S3 sweep still resolves via the delegated `resolve_stuck_submission`                                                                                            |
| `uv run pytest scripts/tests/test_orders_place_uncertain_route.py -x`                                       | pass — S2 UNCERTAIN place path still green through delegated `mark_uncertain`                                                                                          |
| `uv run pytest scripts/tests/test_naked_short_audit.py src/xenon/db/tests/test_orders.py -x`                | pass — S5 audit CANCELLED write + async CRUD layer under the new CHECK                                                                                                 |
| `uv run python scripts/infra/dev/run_pytest_affected.py`                                                    | all green                                                                                                                                                              |

### CI order-path guards (all three, expect exit 0)

```bash
uv run python scripts/checks/no_json_fallback_on_order_path.py
uv run python scripts/checks/no_json_write_on_order_path.py
uv run python scripts/checks/order_path_caller_allowlist.py
```

Expect each exit 0. (No JSON reads/writes added; no new import of `ib_place_order`; only `orders_store` + `single_leg_rehydrate` + `schema.py` + a migration change.)

### Migration (core_test)

| Command                                                                | Expected                                                             |
| ---------------------------------------------------------------------- | -------------------------------------------------------------------- |
| `uv run alembic heads` (Step 6a)                                       | single head `2026_06_22_positions_currency`                          |
| `psql "$DATABASE_URL_TEST" -c "SELECT state,count(*) …"` (6b)          | distinct states ⊆ the 9 (record output; remap rule covers any stray) |
| `uv run alembic upgrade head` (6d)                                     | applies clean, no error                                              |
| `psql … "\d+ xenon.order_submissions" \| grep ck_order_sub_state` (6d) | shows `CHECK (state = ANY (ARRAY['PENDING'… 'UNCERTAIN']))`          |
| bad-state INSERT (6e)                                                  | `ERROR … violates check constraint "ck_order_sub_state"`             |
| `uv run alembic downgrade -1` then re-`upgrade head` (6f)              | constraint gone after downgrade, present after re-upgrade            |

### Web / typecheck / lint / E2E

**N/A — justified.** No file under `web/` is touched: this change is confined to `orders_store.py`, `single_leg_rehydrate.py`, `schema.py`, one migration, and Python tests. No route response shape, reason code, or rendered field changes (the extra `order_events` rows are audit-only and not surfaced in any UI read path). Therefore no Vitest, no `tsc`, no `npm run lint`, no Playwright/chrome-cdp step applies. (If the executor finds itself editing any `web/` file, that is out of scope — STOP per §7.)

### Live paper (OPTIONAL — paper only, IB port 4002)

Not required: the state machine is fully exercised by the committed-DB unit tests and the migration is validated on `core_test` above. If a paper stack is already up, a spot check: place a far-from-market paper limit, cancel it, and confirm `psql "$DATABASE_URL_PAPER" -c "SELECT kind FROM xenon.order_events WHERE submission_id=(SELECT submission_id FROM xenon.order_submissions ORDER BY submitted_at DESC LIMIT 1) ORDER BY at;"` shows both the semantic event (`IB_ACK`) and the new `ORDER_WORKING`/`ORDER_TERMINAL` transition event. Never leave a live paper order resting.

---

## 7. Tripwires / abort criteria (STOP and report)

- **If `orders_store.py` lacks `mark_uncertain` or `resolve_stuck_submission` at Step 0** → S2/S3 not merged; prereqs unmet. STOP.
- **If any Step-1a test passes before you implement `transition()`** → the symbol already exists (another branch added it); STOP and reconcile.
- **If the S2/S3 signatures of `mark_uncertain` / `resolve_stuck_submission` differ from §5.2c/5.2d** → reconcile to the merged signature; only the body may change. Do not alter their public parameters.
- **If a Step-5 regression fails anywhere other than a test that literally asserted the old `mark_terminal(expected_states=None)` clobber-anything behavior** → the delegation changed behavior incorrectly; fix the code, not the test.
- **If `uv run alembic heads` shows a head other than `2026_06_22_positions_currency`** → a prereq added a migration; update `down_revision`/`Revises:` to the actual head and re-verify, or STOP if you cannot resolve a single linear head.
- **If more than these files need edits, STOP and report.** Expected edit set: `src/xenon/execution/orders_store.py`, `src/xenon/execution/single_leg_rehydrate.py`, `src/xenon/db/schema.py`, `src/xenon/db/migrations/versions/2026_07_05_order_sub_state_check.py` (new), `scripts/tests/test_orders_submissions_store.py`, `scripts/tests/test_single_leg_rehydrate.py`, `CHANGELOG.md`, `docs/reference/order-path-incident-history.md`, `src/xenon/CLAUDE.md`. Any edit outside this set (especially `web/`, route handlers, `register_from_snapshot`, or the async `db/queries/orders.py`) is out of scope — STOP.
- **If any live-IB step is required**, use PAPER only (`scripts/infra/dev.sh paper`, port 4002). Never live money.
- **Do NOT** add `hypothesis`, re-plumb `register_from_snapshot`, or add a new state value. If tempted, STOP.

---

## 8. Rollback

- Pre-merge: `git checkout master && git branch -D fix/order-state-transition-chokepoint`.
- Migration: `uv run alembic downgrade -1` drops `ck_order_sub_state` cleanly (proven in Step 6f). The defensive `UPDATE … SET state='UNKNOWN'` in `upgrade()` is **not** reversed by `downgrade()` — but it only ever touches rows that were already outside the 9-value set (none expected on a clean DB), and `UNKNOWN` is a valid, already-in-use state, so leaving them as `UNKNOWN` is safe. If the audit (6b) showed zero stray states (the expected case), the remap was a no-op and there is nothing to reverse.
- Post-merge regression: revert the PR; then `uv run alembic downgrade -1` on any DB that already applied the migration.

---

## 9. Incident-history row (append to `docs/reference/order-path-incident-history.md`)

Append as the next sequential row after S2/S3/S5's rows (they add rows #24–#26; this is likely #27 — use whatever the next number is at execution time), matching the 6-column format (`#`, `Date / PR`, `Issue`, `Root cause`, `Solution`, `Prevention`):

```
| 27  | 2026-07-05 (P2.2, OP-8/OP-9) | `order_submissions.state` was free text with no CHECK, and terminal writes were only optimistically guarded on one path (the TWS-cancel sweep) — place-reject, boot-rehydrate, `_update_state_only`, and `mark_submitted` could clobber a newer terminal state, and any arbitrary string was a storable state. | `mark_terminal`'s `expected_states` guard defaulted to `None` (unconditional UPDATE); `mark_submitted` and `single_leg_rehydrate._update_state_only` did raw unguarded UPDATEs; no `transition()` chokepoint existed and no DB constraint pinned the state set. | Added a single `orders_store.transition()` chokepoint with a `LEGAL_TRANSITIONS` edge set (incl. the S2 `PENDING→UNCERTAIN` and S3 reconciliation edges; RESURRECT edges kept in a separate private set unreachable by ordinary writers) that guards the UPDATE, writes an `order_events` row in the same transaction, returns `False` on a guard-miss, and raises on an illegal edge. Routed `mark_submitted`/`mark_terminal`/`mark_uncertain`/`resolve_stuck_submission`/`_update_state_only` through it (signatures unchanged; `mark_terminal`'s `None` guard now defaults to legal predecessors) and guarded the rehydrate `mark_terminal` call. Added `ck_order_sub_state CHECK (state IN (…9…))` (migration `2026_07_05_order_sub_state_check`, defensive remap of stray states to `UNKNOWN`, mirrored in `schema.py`). | Property test (exhaustive over `ORDER_STATES²`: illegal edges raise, legal edges apply + record one event) + threaded concurrency race (single winner) in `test_orders_submissions_store.py`; migration up/down + bad-state-INSERT rejection verified on core_test. `register_from_snapshot` left inline by design (already guarded + same-txn event). |
```

---

## 10. Repo invariants honored (self-check before PR)

- All Python via `uv run`; no bare python/pip. ✔
- Branch + PR; no `git push origin master`; no AI attribution trailer. ✔
- Order-path change; any live check PAPER only. ✔
- `expected_states=` now enforced at every terminal write via the chokepoint; never clobber a newer terminal state. ✔
- No new JSON read/write on the order path (guards run clean). ✔
- No new dependency (`hypothesis` deliberately avoided). ✔
- Migration applies + downgrades on core_test; `schema.py` mirrors the constraint. ✔
- No `web/` change → web verification correctly N/A. ✔
- Tests use a real ticker (AAPL) at a frozen limit; committed_db for the concurrency race; no network at runtime. ✔

```

```
