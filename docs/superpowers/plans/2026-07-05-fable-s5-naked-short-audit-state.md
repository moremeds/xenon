# S5 — Naked-short audit writes order state (OP-5)

- **Date:** 2026-07-05
- **Branch:** `fix/naked-short-audit-writes-state`
- **Finding IDs:** OP-5 (High). Recurrence of incident-history bug class #16(a) / #20.
- **Goal (one line):** After the naked-short audit cancels a violating order at IB, transition its `order_submissions` row to `CANCELLED` with `reason_code="NAKED_SHORT_AUDIT"` and append an `order_events` row — so the row never lingers `WORKING` and never gets mislabeled `TWS_CANCEL_MIRROR` by the activity-mirror sweep.

---

## Context (what exists today)

- `src/xenon/execution/naked_short_audit.py::cancel_violations(client, violations)` (lines 245–292 at HEAD) is the ONLY place both audit call sites route their cancels through. It finds the live `Trade` (by `permId`, falling back to `orderId`), calls `client.cancel_order(trade.order)`, sleeps 1s, increments a counter — and **never touches Postgres**. The `order_submissions` row therefore stays `WORKING`.
- Two callers invoke `cancel_violations`:
  1. `naked_short_audit.py::main()` — the `xenon-naked-short-audit` CLI (default IB path at line ~419; file-supplied-orders path at line ~386).
  2. `src/xenon/execution/ib_sync.py` post-sync audit block (line ~1459), inside `if args.sync:` → `if not args.skip_audit:`.
     Fixing `cancel_violations` itself covers **both** callers. Do not duplicate the fix into `ib_sync.py`.
- The activity-mirror sweep `src/xenon/api/services/ib_activity_mirror.py::sweep_disappeared_orders` runs on the 60s poller. It selects rows still `state.in_(("WORKING","PARTIALLY_FILLED"))`, and after two consecutive missing sweeps marks them `CANCELLED` with `reason_code="TWS_CANCEL_MIRROR"` (constant `TWS_CANCEL_REASON`, line 115) + a `record_event(sid, TWS_CANCEL_REASON, …)`. Because the audit leaves the row `WORKING`, this sweep currently steals provenance from the audit. After this fix the row is already terminal, so the sweep's `SELECT` won't return it → no double event, no clobber.
- `orders_store` already exposes everything needed: `mark_terminal(...)`, `record_event(...)`, and `load_submission_for_modify(...)` (fetches the full row by `perm_id` or `order_id`, scope-filtered — used both to resolve the `submission_id` and to preserve the row's existing `filled_qty`/`avg_fill_price`). No new store functions, no schema change, no migration.

**What the executor does NOT need to understand:** the naked-short _detection_ math (`find_naked_short_violations` and its helpers), the IB connection/`acquire_owner` plumbing in `main()`, the combo/BAG guardrails, or the frontend. This change is a single provenance write bolted onto the existing cancel loop.

---

## Drift from review

- The fable finding cites `naked_short_audit.py:285` — at HEAD the `client.cancel_order(trade.order)` call is at **line 285** inside `cancel_violations` (lines 245–292). Still accurate. Anchor to the function name + snippet, not the bare line.
- The fable finding cites `ib_activity_mirror.py:115,266-273` — at HEAD `TWS_CANCEL_REASON = "TWS_CANCEL_MIRROR"` is line **115** and the sweep's `mark_terminal(... reason_code=TWS_CANCEL_REASON ...)` is lines **266–273**. Accurate. The module lives at `src/xenon/api/services/ib_activity_mirror.py` (not `src/xenon/execution/`).
- The brief's expectation that the test "likely needs the `committed_db` marker" is **wrong for this change**: the new test drives `cancel_violations` **in-process** with a `MagicMock` client (no subprocess fork, no second physical DB connection). The closest analog, `scripts/tests/test_tws_cancel_sweep.py`, seeds rows and calls the sweep in-process with **no** `committed_db` marker and passes under the Phase-2 shared-transaction fixture. Follow that pattern — do **not** add `committed_db`.

---

## Goal / Non-goals

**Goal:** `cancel_violations` maps each cancelled IB order back to its `order_submissions` row and writes `CANCELLED` + `reason_code="NAKED_SHORT_AUDIT"` + an `order_events` row, guarded by `expected_states=("WORKING","PARTIALLY_FILLED")`. Best-effort: an unmatched order (externally placed, no snapshot row yet) or an unresolvable scope logs and skips — never crashes, never blocks the IB cancel.

**Non-goals (explicitly NOT in this PR — one change, one PR):**

- OP-6 (combo replace server-side move), OP-7 (subprocess semaphore), OP-8 (pass `expected_states` at _other_ terminal call sites), OP-9 (`state` CHECK constraint), CX-1 (dedup naked-short math across TS/preflight/audit). None of these are touched.
- Do **not** add a `reason_code` to the IB `cancel_order` call itself, add read-only guards to unrelated paths, or refactor `main()`.
- Do **not** change the sweep in `ib_activity_mirror.py`. The regression test only _asserts_ the sweep now no-ops on the already-terminal row; the sweep code is already correct.

---

## Key facts (verified against HEAD)

- `orders_store.mark_terminal` signature (all keyword-only), returns affected rowcount:
  ```python
  def mark_terminal(*, submission_id: str,
                    state: Literal["FILLED","REJECTED","CANCELLED","FAILED","PARTIALLY_FILLED"],
                    reason_code: str | None, filled_qty: int,
                    avg_fill_price: Decimal | None,
                    expected_states: tuple[str, ...] | None = None) -> int
  ```
  `expected_states` filters the UPDATE with `state.in_(expected_states)`; a lost race → rowcount `0`, row untouched.
- **`mark_terminal` UNCONDITIONALLY overwrites `filled_qty` and `avg_fill_price`** in the same UPDATE (verified in the function body). The sweep's own CANCELLED branch therefore passes the computed fill quantity through (`ib_activity_mirror.py` `filled_qty=int(fill_qty)`). This plan must do the same — pass the row's CURRENT `filled_qty`/`avg_fill_price` through, never hardcode `0`/`None`, or an audit cancel of a `PARTIALLY_FILLED` order erases its recorded fills.
- `orders_store.record_event(submission_id: str, kind: str, detail: dict) -> None` — **positional** args.
- `orders_store.load_submission_for_modify(*, order_id: str = "", perm_id: str = "", broker: str = "IB", account_env: str = "legacy_unknown", broker_account: str = "legacy_unknown") -> dict | None` — returns the full row dict (`submission_id`, `filled_qty`, `avg_fill_price`, …) matching `ib_order_id`/`perm_id` under the scope, or `None`. Prefers `order_id` when both are passed, so call it **twice**: first with only `perm_id`, then (if `None`) with only `order_id` — mirroring the sweep's identity precedence.
- `xenon.execution.account_scope.AccountScope` is a frozen dataclass: `.broker`, `.account_env`, `.broker_account`. `resolve_from_env()` builds it from `XENON_BROKER` / `XENON_BROKER_ACCOUNT` / `XENON_TRADING_MODE`; it **raises `ValueError`** if `XENON_BROKER_ACCOUNT` is unset.
- Scope env coverage (verified): `ib_sync.py` sets `os.environ["XENON_BROKER_ACCOUNT"]` from `managedAccounts()[0]` (line 1301) before the post-sync audit, and the CLI `main()`'s **default IB path** resolves via `resolve_from_env()` (line ~352). **Exception:** the forensic `--portfolio` file-supplied path skips that resolution — if the operator runs it with `XENON_BROKER_ACCOUNT` unset, `resolve_from_env()` inside the helper raises and the helper **logs-and-skips the state write** (the IB cancel still happens). That is accepted best-effort behavior for a manual debugging path; do not add extra plumbing for it.
- `naked_short_audit.py` has **no `import os`** at module top; the helper imports it function-locally (see Step 2a) — do not add a module-level import.
- **Read-only invariant:** `src/xenon/CLAUDE.md` requires new persistence code in the execution package to honor `XENON_READ_ONLY=1`. House pattern is an inline `os.environ.get("XENON_READ_ONLY") == "1"` check (see `ib_sync.py:1092,1130`) — the helper short-circuits before any DB write.
- Each violation dict carries `order_id` (= IB `orderId`) and `perm_id` (= IB `permId`) — see `find_naked_short_violations`. Identity precedence must mirror the sweep: **perm_id first, ib_order_id fallback** (the sweep documents why: permId=0 race for fresh orders, orderId=0 for non-originating BAG fetches).
- Module already has `logger = logging.getLogger(__name__)` at top of `naked_short_audit.py`.
- The `order_submissions.state` column has no CHECK constraint (OP-9 open), so `"CANCELLED"` writes freely — no migration needed.

---

## Steps (TDD — failing test first, then implement, then green)

### Step 1 — Add the failing state-sync test (red)

**File:** `scripts/tests/test_naked_short_audit.py`

Append these imports near the top (after the existing `from xenon.execution.naked_short_audit import cancel_violations, find_naked_short_violations` line):

```python
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import insert, select

from xenon.api.services.ib_activity_mirror import sweep_disappeared_orders
from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_events, order_submissions
from xenon.execution.account_scope import AccountScope
```

Append this test class at the end of the file:

```python
# ===========================================================================
# Test: cancel_violations writes order_submissions state (OP-5)
# ===========================================================================

_AUDIT_SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DU0000000")
_AUDIT_NOW = datetime(2026, 7, 5, 14, 30, tzinfo=timezone.utc)


def _seed_working_short_call(
    perm_id: str,
    ib_order_id: str,
    *,
    quantity: int = 1,
    state: str = "WORKING",
    filled_qty: int = 0,
    avg_fill_price: Decimal | None = None,
) -> str:
    """Seed a WORKING/PARTIALLY_FILLED snapshot row for a naked short-call order
    (real ticker, frozen price)."""
    submission_id = f"snapshot-{perm_id}"
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(order_submissions).values(
                submission_id=submission_id,
                user_id="snapshot",
                client_attempt_id=f"ca-{perm_id}",
                state=state,
                filled_qty=filled_qty,
                avg_fill_price=avg_fill_price,
                ticker="AAPL",
                security_type="OPT",
                action="SELL",
                right="C",
                strike=Decimal("250.00"),
                expiry=date(2026, 9, 18),  # order_submissions.expiry is a Date column
                quantity=quantity,
                limit_price=Decimal("3.10"),
                tif="GTC",
                perm_id=perm_id,
                ib_order_id=ib_order_id,
                submitted_at=_AUDIT_NOW,
                updated_at=_AUDIT_NOW,
                modify_sequence=0,
                broker=_AUDIT_SCOPE.broker,
                account_env=_AUDIT_SCOPE.account_env,
                broker_account=_AUDIT_SCOPE.broker_account,
            )
        )
    return submission_id


def _row_state(submission_id: str) -> tuple[str, str | None]:
    engine = get_sync_engine()
    with engine.connect() as conn:
        row = conn.execute(
            select(order_submissions.c.state, order_submissions.c.reason_code).where(
                order_submissions.c.submission_id == submission_id
            )
        ).first()
    return (row[0], row[1]) if row else ("<missing>", None)


def _event_kinds(submission_id: str) -> list[str]:
    engine = get_sync_engine()
    with engine.connect() as conn:
        return [
            r[0]
            for r in conn.execute(
                select(order_events.c.kind).where(order_events.c.submission_id == submission_id)
            ).all()
        ]


def _mock_trade(perm_id: int, order_id: int):
    from unittest.mock import MagicMock

    trade = MagicMock()
    trade.order.permId = perm_id
    trade.order.orderId = order_id
    return trade


class TestCancelWritesState:
    def test_audit_cancel_marks_row_cancelled_with_reason(self):
        sid = _seed_working_short_call("900001", "5001")
        client = MagicMock()
        client.get_open_orders.return_value = [_mock_trade(900001, 5001)]

        violations = [{"order_id": 5001, "perm_id": 900001, "reason": "naked short call", "symbol": "AAPL"}]
        cancelled = cancel_violations(client, violations, scope=_AUDIT_SCOPE)

        assert cancelled == 1
        assert client.cancel_order.call_count == 1
        assert _row_state(sid) == ("CANCELLED", "NAKED_SHORT_AUDIT")
        assert "NAKED_SHORT_AUDIT" in _event_kinds(sid)

    def test_no_matching_row_skips_without_crash(self):
        # Externally placed order: cancelled at IB, but no order_submissions row.
        client = MagicMock()
        client.get_open_orders.return_value = [_mock_trade(900777, 5777)]
        violations = [{"order_id": 5777, "perm_id": 900777, "reason": "naked", "symbol": "AAPL"}]

        cancelled = cancel_violations(client, violations, scope=_AUDIT_SCOPE)

        assert cancelled == 1  # IB cancel still counted
        assert _row_state("snapshot-900777") == ("<missing>", None)

    def test_lost_race_leaves_row_untouched(self):
        # Poller already flipped the row FILLED between read and audit write.
        sid = _seed_working_short_call("900002", "5002")
        engine = get_sync_engine()
        from xenon.execution import orders_store

        orders_store.mark_terminal(
            submission_id=sid, state="FILLED", reason_code=None,
            filled_qty=1, avg_fill_price=Decimal("3.10"),
            expected_states=("WORKING", "PARTIALLY_FILLED"),
        )
        client = MagicMock()
        client.get_open_orders.return_value = [_mock_trade(900002, 5002)]
        violations = [{"order_id": 5002, "perm_id": 900002, "reason": "naked", "symbol": "AAPL"}]

        cancel_violations(client, violations, scope=_AUDIT_SCOPE)

        # expected_states guard: FILLED must NOT be clobbered to CANCELLED.
        assert _row_state(sid) == ("FILLED", None)
        assert "NAKED_SHORT_AUDIT" not in _event_kinds(sid)

    def test_partial_fill_preserved_on_audit_cancel(self):
        # mark_terminal unconditionally rewrites filled_qty/avg_fill_price —
        # the audit write must pass the row's current values through, never 0/None.
        sid = _seed_working_short_call(
            "900004", "5004", quantity=2,
            state="PARTIALLY_FILLED", filled_qty=1, avg_fill_price=Decimal("3.10"),
        )
        client = MagicMock()
        client.get_open_orders.return_value = [_mock_trade(900004, 5004)]

        cancel_violations(
            client,
            [{"order_id": 5004, "perm_id": 900004, "reason": "naked", "symbol": "AAPL"}],
            scope=_AUDIT_SCOPE,
        )

        assert _row_state(sid) == ("CANCELLED", "NAKED_SHORT_AUDIT")
        engine = get_sync_engine()
        with engine.connect() as conn:
            row = conn.execute(
                select(order_submissions.c.filled_qty, order_submissions.c.avg_fill_price).where(
                    order_submissions.c.submission_id == sid
                )
            ).one()
        assert row[0] == 1
        assert row[1] == Decimal("3.1000")  # Numeric(12,4) round-trip

    def test_read_only_mode_skips_state_write(self, monkeypatch):
        # dev.sh live sessions (XENON_READ_ONLY=1) must never write order
        # state; the IB cancel itself still proceeds and is counted.
        monkeypatch.setenv("XENON_READ_ONLY", "1")
        sid = _seed_working_short_call("900005", "5005")
        client = MagicMock()
        client.get_open_orders.return_value = [_mock_trade(900005, 5005)]

        cancelled = cancel_violations(
            client,
            [{"order_id": 5005, "perm_id": 900005, "reason": "naked", "symbol": "AAPL"}],
            scope=_AUDIT_SCOPE,
        )

        assert cancelled == 1
        assert _row_state(sid) == ("WORKING", None)  # untouched
        assert _event_kinds(sid) == []

    def test_sweep_no_ops_on_already_terminal_audit_row(self):
        # Regression for OP-5 ↔ sweep interaction: after the audit marks the row
        # CANCELLED, the TWS_CANCEL_MIRROR sweep must not touch it or add an event.
        sid = _seed_working_short_call("900003", "5003")
        client = MagicMock()
        client.get_open_orders.return_value = [_mock_trade(900003, 5003)]
        cancel_violations(
            client,
            [{"order_id": 5003, "perm_id": 900003, "reason": "naked", "symbol": "AAPL"}],
            scope=_AUDIT_SCOPE,
        )

        # A later poll tick: the order is gone from IB; some *other* order is open
        # (non-empty snapshot so the empty-snapshot guard doesn't short-circuit).
        # Thread an explicit grace set across the two ticks (matches
        # test_tws_cancel_sweep.py) — do NOT rely on the module-level
        # _SWEEP_GRACE, which leaks state across tests.
        grace: set[str] = set()
        result = sweep_disappeared_orders(
            [{"permId": 999999, "orderId": 6000}], scope=_AUDIT_SCOPE, grace=grace
        )
        result = sweep_disappeared_orders(
            [{"permId": 999999, "orderId": 6000}], scope=_AUDIT_SCOPE, grace=grace
        )

        assert result["cancelled"] == 0
        assert _row_state(sid) == ("CANCELLED", "NAKED_SHORT_AUDIT")
        assert _event_kinds(sid).count("TWS_CANCEL_MIRROR") == 0
```

**Tripwire:** Run `test_audit_cancel_marks_row_cancelled_with_reason` now — it MUST fail (`cancel_violations` has no `scope` kwarg yet → `TypeError`, or the row stays `WORKING`). If it passes before Step 2, STOP — the anchor is wrong.

### Step 2 — Add the state-write helper + `scope` param (implement, green)

**File:** `src/xenon/execution/naked_short_audit.py`

**2a.** Insert a new private helper immediately **above** `def cancel_violations`. Anchor: the line `def cancel_violations(client, violations: list) -> int:`.

```python
def _record_audit_cancel_state(order_id, perm_id, symbol: str, scope) -> None:
    """Best-effort: transition the cancelled order's order_submissions row to
    CANCELLED(reason=NAKED_SHORT_AUDIT) + append an order_events row.

    Called AFTER the IB cancel succeeds. Never raises: the IB cancel is the
    safety-critical action; this write is provenance only. An externally placed
    order (no snapshot row), an unresolvable scope, or a lost optimistic-lock
    race all log-and-return rather than crash the audit loop.
    """
    import os

    from xenon.execution import orders_store
    from xenon.execution.account_scope import resolve_from_env

    # Read-only sessions (dev.sh live) must never write order state — same
    # inline env check ib_sync's writers use (_save_portfolio_to_postgres,
    # _append_nav_snapshot). The prod Docker stack runs without the flag,
    # so prod provenance is unaffected.
    if os.environ.get("XENON_READ_ONLY") == "1":
        logger.info("audit state-write skipped for %s: XENON_READ_ONLY=1", symbol)
        return

    try:
        if scope is None:
            scope = resolve_from_env()
    except Exception as exc:  # noqa: BLE001 — provenance write must not abort the cancel loop
        logger.warning("audit state-write: could not resolve account scope (%s); skipping for %s", exc, symbol)
        return

    try:
        # Resolve the row: perm_id first, ib_order_id fallback (the sweep's
        # identity precedence — permId=0 race / orderId=0 BAG fetch).
        # load_submission_for_modify prefers order_id when both are passed,
        # so call it once per key.
        row = None
        if perm_id and int(perm_id) > 0:
            row = orders_store.load_submission_for_modify(
                perm_id=str(perm_id),
                broker=scope.broker,
                account_env=scope.account_env,
                broker_account=scope.broker_account,
            )
        if row is None and order_id and int(order_id) > 0:
            row = orders_store.load_submission_for_modify(
                order_id=str(order_id),
                broker=scope.broker,
                account_env=scope.account_env,
                broker_account=scope.broker_account,
            )
        if row is None:
            logger.info(
                "audit state-write: no order_submissions row for %s (orderId=%s permId=%s) — "
                "externally placed; skipping state write",
                symbol, order_id, perm_id,
            )
            return
        submission_id = row["submission_id"]

        # mark_terminal unconditionally rewrites filled_qty/avg_fill_price:
        # pass the row's current values through so an audit cancel of a
        # PARTIALLY_FILLED order never erases its recorded fills.
        applied = orders_store.mark_terminal(
            submission_id=submission_id,
            state="CANCELLED",
            reason_code="NAKED_SHORT_AUDIT",
            filled_qty=int(row["filled_qty"] or 0),
            avg_fill_price=row["avg_fill_price"],
            expected_states=("WORKING", "PARTIALLY_FILLED"),
        )
        if applied:
            orders_store.record_event(
                submission_id,
                "NAKED_SHORT_AUDIT",
                {"source": "naked_short_audit", "symbol": symbol, "order_id": order_id, "perm_id": perm_id},
            )
            logger.info("audit state-write: %s → CANCELLED (NAKED_SHORT_AUDIT)", submission_id)
        else:
            # Poller/user won the race and already transitioned the row. Fine —
            # never clobber a newer terminal state.
            logger.info(
                "audit state-write: %s not in WORKING/PARTIALLY_FILLED (race lost); leaving as-is",
                submission_id,
            )
    except Exception:  # noqa: BLE001 — provenance write must not abort the cancel loop
        logger.warning("audit state-write failed for %s (orderId=%s); IB cancel already done", symbol, order_id, exc_info=True)
```

**2b.** Change the `cancel_violations` signature. Anchor:

```python
def cancel_violations(client, violations: list) -> int:
```

→

```python
def cancel_violations(client, violations: list, *, scope=None) -> int:
```

And update its docstring `Args:` block to add:

```
        scope: AccountScope for the state write. When None, resolved from
            XENON_TRADING_MODE / XENON_BROKER_ACCOUNT env (set by ib_sync and
            the CLI's default IB path; the forensic --portfolio path may lack
            it, in which case the state write is skipped best-effort).
```

**2c.** Insert the state write right after the successful IB cancel. Anchor the existing three lines inside the `try:` block:

```python
            client.cancel_order(trade.order)
            client.sleep(1)
            cancelled += 1
```

→

```python
            client.cancel_order(trade.order)
            client.sleep(1)
            cancelled += 1
            _record_audit_cancel_state(order_id, perm_id, symbol, scope)
```

No other edits. The two callers (`main()` and `ib_sync.py`) need **no change** — they call `cancel_violations(client, violations)` and `scope=None` resolves from env, which both already populate.

### Step 3 — Green + regression

Run the new test class (all six cases) — all MUST pass. Then run the full existing `test_naked_short_audit.py` to confirm `TestCancelViolations.test_cancel_calls_client` still passes. It calls `cancel_violations(client, violations)` with no scope and no seeded rows; depending on the test env either `resolve_from_env()` raises (env unset → helper logs-and-returns) or the scope resolves and the row lookup misses (helper logs-and-skips) — **both paths are silent no-ops by design**, so the returned count stays `2` either way.

---

## Verification matrix

| #   | Check                                 | Exact command                                                                                                                          | Expected                                                            |
| --- | ------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| 1   | New test class red before Step 2      | `uv run pytest scripts/tests/test_naked_short_audit.py::TestCancelWritesState::test_audit_cancel_marks_row_cancelled_with_reason -xvs` | FAILS (TypeError: unexpected kwarg `scope`, or state stays WORKING) |
| 2   | New test class green after Step 2     | `uv run pytest scripts/tests/test_naked_short_audit.py::TestCancelWritesState -xvs`                                                    | 6 passed (incl. `test_read_only_mode_skips_state_write`)            |
| 3   | Existing audit tests still pass       | `uv run pytest scripts/tests/test_naked_short_audit.py -x`                                                                             | all passed (incl. `TestCancelViolations`)                           |
| 4   | Sweep regression untouched            | `uv run pytest scripts/tests/test_tws_cancel_sweep.py -x`                                                                              | all passed                                                          |
| 5   | orders_store terminal-guard tests     | `uv run pytest scripts/tests/test_record_fill.py -x`                                                                                   | all passed                                                          |
| 6   | Scoped affected suite                 | `uv run python scripts/infra/dev/run_pytest_affected.py`                                                                               | exit 0                                                              |
| 7   | CI guard: no JSON read on order path  | `uv run python scripts/checks/no_json_fallback_on_order_path.py`                                                                       | exit 0                                                              |
| 8   | CI guard: no JSON write on order path | `uv run python scripts/checks/no_json_write_on_order_path.py`                                                                          | exit 0                                                              |
| 9   | CI guard: place-CLI caller allowlist  | `uv run python scripts/checks/order_path_caller_allowlist.py`                                                                          | exit 0                                                              |

**Not applicable (state why):** No web/frontend change → no Vitest, `tsc`, `npm run lint`, or Playwright/E2E. No schema change → no Alembic/migration checks. No route change → no FastAPI TestClient / `fastapiHarness` case. No relay change. No live-IB step required — the fix is exercised entirely by in-process unit tests with a `MagicMock` client (repo policy: order-path diagnosis is paper-only, but this change needs no broker at all).

---

## Tripwires / abort criteria

- **If test #1 passes before Step 2 is applied → STOP.** The anchor is wrong or `cancel_violations` already has the write; re-read the file at HEAD.
- **If `cancel_violations` at HEAD already imports/calls `orders_store` or `mark_terminal` → STOP and report** — OP-5 may already be partially fixed; adapt rather than double-write.
- **If more than 3 files need edits** (`naked_short_audit.py` + `test_naked_short_audit.py` + the `docs/reference/order-path-incident-history.md` row append) → STOP. This change must not spill into `ib_sync.py`, `server.py`, `orders_store.py`, or `ib_activity_mirror.py`. If you believe it must, report why before proceeding.
- **If any step seems to need a live or paper IB connection → STOP.** There is none; if a test tries to open a socket, the design is wrong.
- **If `resolve_from_env()` raising in the existing `test_cancel_calls_client` turns into a hard failure** (not caught) → the `try/except` in `_record_audit_cancel_state` is misplaced; the scope-resolution guard must wrap `resolve_from_env()`.
- **Do NOT** add a `committed_db` marker; if the new test only passes with it, the test is forking a subprocess it shouldn't — re-check it drives `cancel_violations` in-process.

---

## Rollback

Code + test + one doc row (incident history), no migration, no data write outside test transactions. Revert with `git checkout -- src/xenon/execution/naked_short_audit.py scripts/tests/test_naked_short_audit.py docs/reference/order-path-incident-history.md` or discard the branch (`git branch -D fix/naked-short-audit-writes-state`). No downgrade path needed.

---

## Incident-history row (append to `docs/reference/order-path-incident-history.md`)

Append as row **#24** at the end of the incident table (after row #23):

```
| 24  | 2026-07-05 fix/naked-short-audit-writes-state | Post-`ib_sync` naked-short audit cancelled violating orders at IB but never wrote Postgres: the `order_submissions` row stayed `WORKING` for up to two poll ticks, then the activity-mirror sweep mislabeled the cancel as `TWS_CANCEL_MIRROR` — wrong provenance + a stale open-order row in the UI in between. | `naked_short_audit.py::cancel_violations` cancelled directly via `client.cancel_order(trade.order)` with no `mark_terminal` / `order_events` write — a recurrence of the #16(a) class (cancel path that skips the state machine). The `TWS_CANCEL_MIRROR` sweep (`ib_activity_mirror.py::sweep_disappeared_orders`) then owned the terminal transition by default because the row was still `WORKING`. | Added `_record_audit_cancel_state()` in `naked_short_audit.py`, called right after the successful IB cancel inside `cancel_violations`: resolves the full row via `load_submission_for_modify` by `perm_id` (fallback `ib_order_id`, mirroring the sweep's identity precedence), then `mark_terminal(state="CANCELLED", reason_code="NAKED_SHORT_AUDIT", expected_states=("WORKING","PARTIALLY_FILLED"))` — passing the row's current `filled_qty`/`avg_fill_price` through, since `mark_terminal` rewrites both — + `record_event(sid, "NAKED_SHORT_AUDIT", …)`. Best-effort and non-raising: unmatched (externally placed) orders and lost optimistic-lock races log-and-skip; the IB cancel is never blocked. Scope resolves from `scope=` param or `resolve_from_env()` (both callers set it). No schema change. | `scripts/tests/test_naked_short_audit.py::TestCancelWritesState` (6 cases): row → CANCELLED+NAKED_SHORT_AUDIT+event; unmatched external order skips without crash; lost race (row already FILLED) not clobbered; partial-fill `filled_qty`/`avg_fill_price` preserved through the audit cancel; `XENON_READ_ONLY=1` skips the state write; and the sweep-interaction regression — after the audit marks the row terminal, `sweep_disappeared_orders` returns `cancelled=0` and adds no `TWS_CANCEL_MIRROR` event. **Watch pattern:** any broker-side mutation (cancel/modify/replace) must move the `order_submissions` state machine in the same operation — never leave the DB write to a downstream reconciliation sweep, which will attribute the wrong provenance. |
```

---

## Repo invariants honored (restated)

- All Python via `uv run …`. No bare python/pip.
- Branch + PR; never push master; no AI-attribution commit trailer.
- `expected_states=("WORKING","PARTIALLY_FILLED")` on the terminal write — never clobber a newer terminal state (a fill or user cancel that won the race).
- No new JSON read/write on the order path (CI guards #7–#9 confirm).
- Tests use a real ticker at a frozen price (AAPL, seeded fixture; no network, no `FOO`/round-number placeholders).
- No `committed_db` marker — in-process test under the Phase-2 shared transaction (matches `test_tws_cancel_sweep.py`).
- `XENON_READ_ONLY=1` sessions never write order state — helper short-circuits before any DB call (inline env check, house pattern from `ib_sync.py`), covered by `test_read_only_mode_skips_state_write`.
