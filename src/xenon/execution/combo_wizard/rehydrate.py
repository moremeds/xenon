"""Restart-safe reconcile for combo wizard sessions.

On boot, for every wizard session in ``SUBMITTING``, ``WORKING``,
``REPRICE_PENDING``, ``PROTECTION_PENDING``, or ``PROTECTED``:

1. Fetch IB open orders, executions, and positions.
2. Reconcile the combo order attempt against executions first. Because IB
   reports combo fills as **per-leg executions sharing one parent `permId`**,
   reconcile by grouping executions on the attempt's ``permId`` and checking
   that every leg reached the expected ratio — do not treat a single-leg
   execution row as a combo fill.
3. Re-register Risk Alert rows for protected sessions.
4. Retry unfinished protection attachment.
5. Log any disagreement as a session event.

See spec §13 and plan lines 416-422 for the authoritative rule.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from xenon.execution import orders_store
from xenon.execution.combo_wizard import protect as _protect_mod
from xenon.execution.single_leg_rehydrate import (  # noqa: F401 — shared pattern
    _index_open_orders,
)

logger = logging.getLogger(__name__)

REHYDRATABLE_STATES = {
    "SUBMITTING",
    "WORKING",
    "REPRICE_PENDING",
    "PROTECTION_PENDING",
    "PROTECTED",
    # Case variants written by earlier F5–F7 code paths.
    "submitting",
    "working",
    "reprice_pending",
    "protection_pending",
    "protected",
}


@dataclass
class WizardReconcileDecision:
    session_id: str
    from_state: str
    to_state: str
    detail: dict = field(default_factory=dict)


def _db_path(db_path: Path | str | None) -> Path:
    return orders_store._resolve_path(db_path)


def _connect(db_path: Path | str | None):
    return orders_store._connect_utc(_db_path(db_path))


def _list_rehydratable(db_path: Path | str | None) -> list[dict]:
    orders_store.init_store(_db_path(db_path))
    con = _connect(db_path)
    try:
        rows = con.execute(
            """
            SELECT session_id, ticker, state, structure_name, intent, payload_json,
                   current_attempt_id
              FROM wizard_sessions
             WHERE UPPER(state) IN ('SUBMITTING','WORKING','REPRICE_PENDING',
                                    'PROTECTION_PENDING','PROTECTED')
            """
        ).fetchall()
    finally:
        con.close()
    out: list[dict] = []
    for r in rows:
        payload = json.loads(r[5]) if r[5] else {}
        out.append(
            {
                "session_id": r[0],
                "ticker": r[1],
                "state": r[2],
                "structure_name": r[3],
                "intent": r[4],
                "payload": payload,
                "current_attempt_id": r[6],
            }
        )
    return out


def _load_latest_attempt(session_id: str, db_path: Path | str | None) -> dict[str, Any] | None:
    con = _connect(db_path)
    try:
        row = con.execute(
            """
            SELECT attempt_id, ib_order_id, perm_id, terminal_state, filled_qty
              FROM wizard_combo_attempts
             WHERE session_id = ?
             ORDER BY created_at DESC
             LIMIT 1
            """,
            [session_id],
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return None
    return {
        "attempt_id": row[0],
        "ib_order_id": row[1],
        "perm_id": row[2],
        "terminal_state": row[3],
        "filled_qty": int(row[4] or 0),
    }


def _set_session_state(session_id: str, state: str, db_path: Path | str | None) -> None:
    from datetime import datetime, timezone

    con = _connect(db_path)
    try:
        con.execute(
            "UPDATE wizard_sessions SET state=?, updated_at=? WHERE session_id=?",
            [state, datetime.now(timezone.utc), session_id],
        )
    finally:
        con.close()


def _record_event(session_id: str, kind: str, detail: dict, db_path: Path | str | None) -> None:
    import uuid
    from datetime import datetime, timezone

    con = _connect(db_path)
    try:
        con.execute(
            'INSERT INTO wizard_session_events (event_id, session_id, kind, detail, "at") VALUES (?, ?, ?, ?, ?)',
            [
                str(uuid.uuid4()),
                session_id,
                kind,
                json.dumps(detail, default=str),
                datetime.now(timezone.utc),
            ],
        )
    finally:
        con.close()


# ---------------------------------------------------------------------------
# BAG per-leg aggregation — the critical correctness rule.
# ---------------------------------------------------------------------------


def _aggregate_leg_fills(*, perm_id: str, executions: list[dict]) -> dict[int, int]:
    """Sum executed shares per conId for a given parent permId.

    Only executions whose perm_id matches are counted. Returns a dict
    {con_id: total_shares}.
    """
    by_con: dict[int, int] = {}
    for ex in executions or []:
        if not isinstance(ex, dict):
            # Normalize objects with attribute access.
            pid = getattr(ex, "perm_id", None) or getattr(ex, "permId", None)
            cid = getattr(ex, "con_id", None) or getattr(ex, "conId", None)
            sh = getattr(ex, "shares", 0)
        else:
            pid = ex.get("perm_id") or ex.get("permId")
            cid = ex.get("con_id") or ex.get("conId")
            sh = ex.get("shares") or ex.get("filled_qty") or 0
        if str(pid) != str(perm_id):
            continue
        if cid is None:
            continue
        by_con[int(cid)] = by_con.get(int(cid), 0) + int(sh or 0)
    return by_con


def _combo_fill_state(
    *,
    legs: list[dict],
    quantity: int,
    leg_shares_by_con: dict[int, int],
) -> str:
    """Classify combo fill state from per-leg shares.

    - FILLED iff every leg has shares >= ratio * quantity
    - PARTIALLY_FILLED iff any leg has shares > 0 but not all legs reached target
    - WORKING iff no leg has any shares yet
    """
    if not legs:
        return "WORKING"
    any_partial = False
    all_done = True
    for leg in legs:
        con_id = int(leg.get("conId") or leg.get("con_id") or 0)
        ratio = int(leg.get("ratio") or 1)
        target = ratio * max(1, int(quantity))
        got = int(leg_shares_by_con.get(con_id, 0))
        if got > 0:
            any_partial = True
        if got < target:
            all_done = False
    if all_done:
        return "FILLED"
    if any_partial:
        return "PARTIALLY_FILLED"
    return "WORKING"


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def rehydrate_combo_sessions(
    *,
    ib_client_factory: Callable[[], Any],
    db_path: Path | str | None = None,
) -> list[WizardReconcileDecision]:
    sessions = _list_rehydratable(db_path)
    if not sessions:
        return []

    ib = ib_client_factory()
    open_orders = ib.get_open_orders() or []
    executions = ib.get_executions() or []

    open_by_perm: dict[str, dict] = {}
    for oo in open_orders:
        if isinstance(oo, dict):
            pid = oo.get("perm_id") or oo.get("permId")
        else:
            pid = getattr(oo, "perm_id", None) or getattr(oo, "permId", None)
        if pid is not None:
            open_by_perm[str(pid)] = oo if isinstance(oo, dict) else {"perm_id": str(pid)}

    decisions: list[WizardReconcileDecision] = []

    for sess in sessions:
        sid = sess["session_id"]
        from_state = sess["state"]
        upper = from_state.upper()

        # PROTECTION_PENDING / PROTECTED: surface redrive signal — the daemon
        # will retry protection attach / re-register risk alerts.
        if upper in {"PROTECTION_PENDING", "PROTECTED"}:
            reason = "PROTECTION_RETRY_REQUIRED" if upper == "PROTECTION_PENDING" else "PROTECTED_REDRIVE"
            decision = WizardReconcileDecision(
                session_id=sid,
                from_state=from_state,
                to_state=upper,
                detail={
                    "reason_code": "PROTECTION_RETRY_REQUIRED"
                    if upper == "PROTECTION_PENDING"
                    else "PROTECTION_REDRIVE",
                    "note": reason,
                },
            )
            _record_event(sid, "REHYDRATE_RECONCILED", decision.detail, db_path)
            decisions.append(decision)
            continue

        attempt = _load_latest_attempt(sid, db_path)
        if attempt is None:
            decision = WizardReconcileDecision(
                session_id=sid,
                from_state=from_state,
                to_state="UNKNOWN",
                detail={"reason_code": "NO_ATTEMPT_ROW"},
            )
            _record_event(sid, "REHYDRATE_UNCERTAIN", decision.detail, db_path)
            decisions.append(decision)
            continue

        perm_id = attempt.get("perm_id") or ""
        legs = sess["payload"].get("legs", [])
        quantity = int(sess["payload"].get("quantity", 1))

        if perm_id and perm_id in open_by_perm:
            # Still live at IB.
            decision = WizardReconcileDecision(
                session_id=sid,
                from_state=from_state,
                to_state="WORKING",
                detail={"perm_id": perm_id, "sources": {"open_orders": True}},
            )
            _record_event(sid, "REHYDRATE_RECONCILED", decision.detail, db_path)
            if from_state.lower() != "working":
                _set_session_state(sid, "working", db_path)
            decisions.append(decision)
            continue

        # Not in open orders. Reconcile against execs with BAG per-leg aggregation.
        leg_shares = _aggregate_leg_fills(perm_id=perm_id, executions=executions)
        to_state = _combo_fill_state(legs=legs, quantity=quantity, leg_shares_by_con=leg_shares)

        detail = {
            "perm_id": perm_id,
            "leg_shares": leg_shares,
            "to_state": to_state,
            "sources": {
                "open_orders": False,
                "executions": bool(leg_shares),
            },
        }

        # Persist state change.
        db_state = {
            "FILLED": "filled",
            "PARTIALLY_FILLED": "partially_filled",
            "WORKING": "working",
        }[to_state]
        _set_session_state(sid, db_state, db_path)
        _record_event(sid, "REHYDRATE_RECONCILED", detail, db_path)

        decisions.append(
            WizardReconcileDecision(
                session_id=sid,
                from_state=from_state,
                to_state=to_state,
                detail=detail,
            )
        )

    return decisions
