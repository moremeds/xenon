"""Boot reconcile + reconnect-triggered reconcile. Spec §10.4."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, update

from xenon.db.queries.position_close_claims import mark_terminal
from xenon.db.queries.position_protection import cas_transition
from xenon.db.schema import position_close_claims, position_protection
from xenon.execution.account_scope import AccountScope
from xenon.execution.brackets.executor.native_liveness import (
    NativeOrderState,
    verify_native_order_live,
)

logger = logging.getLogger(__name__)


def boot_reconcile(*, engine, ib_client, scope: AccountScope) -> dict[str, Any]:
    if not getattr(ib_client, "connected", True):
        logger.info("boot_reconcile: IB not connected, deferring")
        return {"status": "deferred"}

    counts = {"claims_resolved": 0, "armed_rows_re_armed": 0, "armed_rows_canceled": 0}

    with engine.connect() as conn:
        inflight = conn.execute(
            select(position_close_claims).where(
                position_close_claims.c.broker == scope.broker,
                position_close_claims.c.account_env == scope.account_env,
                position_close_claims.c.broker_account == scope.broker_account,
                position_close_claims.c.status.in_(("PENDING", "SUBMITTED")),
            )
        ).all()

    for claim_row in inflight:
        claim = dict(claim_row._mapping)
        order_ref = claim["order_ref"]
        executions = (
            ib_client.find_executions_by_order_ref(order_ref)
            if hasattr(ib_client, "find_executions_by_order_ref")
            else []
        )
        open_orders = (
            ib_client.find_open_orders_by_order_ref(order_ref)
            if hasattr(ib_client, "find_open_orders_by_order_ref")
            else []
        )
        if executions:
            mark_terminal(engine, claim_id=claim["claim_id"], status="FILLED")
            cas_transition(
                engine,
                protection_id=claim["claimed_by_protection_id"],
                expected_state="TRIGGERED",
                new_state="CLOSED",
                reason="boot_reconcile_filled",
            )
            counts["claims_resolved"] += 1
        elif not open_orders and claim["status"] == "SUBMITTED":
            with engine.begin() as conn:
                conn.execute(
                    update(position_close_claims)
                    .where(position_close_claims.c.claim_id == claim["claim_id"])
                    .values(status="PENDING")
                )
            cas_transition(
                engine,
                protection_id=claim["claimed_by_protection_id"],
                expected_state="TRIGGERED",
                new_state="ARMED",
                reason="boot_reconcile_close_missing_retry",
            )

    with engine.connect() as conn:
        armed_rows = conn.execute(
            select(position_protection).where(
                position_protection.c.broker == scope.broker,
                position_protection.c.account_env == scope.account_env,
                position_protection.c.broker_account == scope.broker_account,
                position_protection.c.state == "ARMED",
                position_protection.c.native_order_perm_id.is_not(None),
            )
        ).all()

    for row in armed_rows:
        state = verify_native_order_live(ib_client=ib_client, perm_id=row.native_order_perm_id)
        if state == NativeOrderState.CANCELLED:
            if _position_present(ib_client, row.position_descriptor):
                cas_transition(
                    engine,
                    protection_id=row.protection_id,
                    expected_state="ARMED",
                    new_state="PENDING_ARM",
                    reason="boot_reconcile_native_cancelled",
                )
                counts["armed_rows_re_armed"] += 1
            else:
                cas_transition(
                    engine,
                    protection_id=row.protection_id,
                    expected_state="ARMED",
                    new_state="CANCELED",
                    reason="boot_reconcile_position_absent",
                )
                counts["armed_rows_canceled"] += 1
        elif state == NativeOrderState.FILLED:
            cas_transition(
                engine,
                protection_id=row.protection_id,
                expected_state="ARMED",
                new_state="CLOSED",
                reason="boot_reconcile_native_filled",
            )
            counts["claims_resolved"] += 1

    return counts


def _position_present(ib_client, descriptor: dict[str, Any]) -> bool:
    if not hasattr(ib_client, "positions"):
        return True
    try:
        positions = ib_client.positions()
    except Exception:  # noqa: BLE001
        return True
    if not isinstance(positions, list):
        return True
    legs = descriptor.get("legs") or []
    if not legs:
        return False
    for leg in legs:
        if not any(
            position.get("symbol") == leg.get("symbol")
            and (leg.get("con_id") is None or position.get("con_id") == leg.get("con_id"))
            and abs(int(position.get("qty", 0))) > 0
            for position in positions
        ):
            return False
    return True
