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

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from xenon.db.engine import get_sync_engine
from xenon.db.queries import combo_wizard
from xenon.execution.combo_wizard import protect as _protect_mod
from xenon.execution.trade_aggregator import aggregate_trade_from_fills
from xenon.execution.single_leg_rehydrate import (  # noqa: F401 — shared pattern
    _index_execution_records,
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


def _list_rehydratable(
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> list[dict]:
    engine = get_sync_engine()
    with engine.begin() as conn:
        rows = combo_wizard.list_rehydratable(
            conn,
            broker=broker,
            account_env=account_env,
            broker_account=broker_account,
        )
    # Ensure payload is always a dict (JSONB returns dict directly).
    for r in rows:
        if r.get("payload") is None:
            r["payload"] = {}
    return rows


def _load_latest_attempt(session_id: str) -> dict[str, Any] | None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        row = combo_wizard.get_latest_attempt(conn, session_id)
    if row is None:
        return None
    # Map Postgres column names to the dict keys used downstream.
    return {
        "attempt_id": row["attempt_id"],
        "ticker": row.get("ticker"),
        "structure_name": row.get("structure_name"),
        "ib_order_id": row.get("ib_order_id"),
        "perm_id": row.get("perm_id"),
        "terminal_state": row.get("state"),  # Postgres `state` → legacy `terminal_state`
        "filled_qty": int(row.get("filled_qty") or 0),
        "broker": row.get("broker", "IB"),
        "account_env": row.get("account_env", "legacy_unknown"),
        "broker_account": row.get("broker_account", "legacy_unknown"),
    }


def _set_session_state(session_id: str, state: str) -> None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        combo_wizard.update_session(conn, session_id, state=state)


def _set_attempt_state(attempt_id: str, state: str) -> None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        combo_wizard.update_attempt(conn, attempt_id, state=state)


def _record_event(session_id: str, kind: str, detail: dict) -> None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        combo_wizard.record_event(conn, session_id=session_id, kind=kind, detail=detail)


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
            execution = getattr(ex, "execution", ex)
            contract = getattr(ex, "contract", None)
            pid = (
                getattr(ex, "perm_id", None)
                or getattr(ex, "permId", None)
                or getattr(execution, "permId", None)
                or getattr(execution, "perm_id", None)
            )
            cid = (
                getattr(ex, "con_id", None)
                or getattr(ex, "conId", None)
                or getattr(contract, "conId", None)
                or getattr(contract, "con_id", None)
            )
            sh = getattr(ex, "shares", None)
            if sh is None:
                sh = getattr(execution, "shares", 0)
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


def _has_explicit_scope(row: dict) -> bool:
    return bool(row.get("account_env") != "legacy_unknown" and row.get("broker_account") != "legacy_unknown")


def _record_combo_fill_records(sess: dict, attempt: dict, records: list[dict]) -> None:
    if not records or not _has_explicit_scope(attempt):
        return
    for record in records:
        if not record.get("exec_id"):
            continue
        _orders_store_record_fill(
            exec_id=record["exec_id"],
            combo_attempt_id=attempt["attempt_id"],
            perm_id=str(attempt.get("perm_id") or record["perm_id"]),
            ib_order_id=str(attempt.get("ib_order_id") or record.get("ib_order_id") or "") or None,
            con_id=record.get("con_id"),
            ticker=record.get("ticker") or attempt.get("ticker") or sess["ticker"],
            side=record["side"],
            qty=record["qty"],
            price=record["price"],
            commission=record["commission"],
            filled_at=record["filled_at"],
            metadata={"source": "combo_wizard_rehydrate", "session_id": sess["session_id"]},
            broker=attempt.get("broker", "IB"),
            account_env=attempt["account_env"],
            broker_account=attempt["broker_account"],
        )
    aggregate_trade_from_fills(combo_attempt_id=attempt["attempt_id"])


def _orders_store_record_fill(**kwargs) -> bool:
    from xenon.execution.orders_store import record_fill

    return record_fill(submission_id=None, **kwargs)


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def rehydrate_combo_sessions(
    *,
    ib_client_factory: Callable[[], Any],
    db_path: Any = None,  # deprecated, ignored — kept for call-site compat
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> list[WizardReconcileDecision]:
    sessions = _list_rehydratable(
        broker=broker,
        account_env=account_env,
        broker_account=broker_account,
    )
    if not sessions:
        return []

    ib = ib_client_factory()
    open_orders = ib.get_open_orders() or []
    executions = ib.get_executions() or []
    execution_records = _index_execution_records(executions)

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
            _record_event(sid, "REHYDRATE_RECONCILED", decision.detail)
            decisions.append(decision)
            continue

        attempt = _load_latest_attempt(sid)
        if attempt is None:
            decision = WizardReconcileDecision(
                session_id=sid,
                from_state=from_state,
                to_state="UNKNOWN",
                detail={"reason_code": "NO_ATTEMPT_ROW"},
            )
            _record_event(sid, "REHYDRATE_UNCERTAIN", decision.detail)
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
            _record_event(sid, "REHYDRATE_RECONCILED", decision.detail)
            if from_state.lower() != "working":
                _set_session_state(sid, "working")
            decisions.append(decision)
            continue

        # Not in open orders. Reconcile against execs with BAG per-leg aggregation.
        leg_shares = _aggregate_leg_fills(perm_id=perm_id, executions=executions)
        to_state = _combo_fill_state(legs=legs, quantity=quantity, leg_shares_by_con=leg_shares)
        if to_state in {"FILLED", "PARTIALLY_FILLED"}:
            _record_combo_fill_records(sess, attempt, execution_records.get(str(perm_id), []))

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
        _set_session_state(sid, db_state)
        _set_attempt_state(attempt["attempt_id"], to_state)
        _record_event(sid, "REHYDRATE_RECONCILED", detail)

        decisions.append(
            WizardReconcileDecision(
                session_id=sid,
                from_state=from_state,
                to_state=to_state,
                detail=detail,
            )
        )

    return decisions
