"""Shared operator cancel flow for position protection rules."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.engine import Engine

from xenon.db.queries.position_close_claims import find_inflight_for_position, mark_terminal
from xenon.db.queries.position_protection import cas_transition, get_by_id
from xenon.execution.account_scope import AccountScope
from xenon.execution.brackets.executor.ib_executor import IBExecutor

ACTIVE_STATES = ("PENDING_ARM", "ARMED", "TRIGGERED")


@dataclass(frozen=True)
class CancelProtectionResult:
    status: str
    row: dict[str, Any] | None = None
    canceled_perm_ids: list[int] = field(default_factory=list)


def cancel_protection(
    engine: Engine,
    *,
    scope: AccountScope,
    protection_id: int,
    reason: str,
    force: bool = False,
) -> CancelProtectionResult:
    row = get_by_id(
        engine,
        protection_id=protection_id,
        broker=scope.broker,
        account_env=scope.account_env,
        broker_account=scope.broker_account,
    )
    if row is None:
        return CancelProtectionResult(status="not_found")
    if row["state"] not in ACTIVE_STATES and not force:
        return CancelProtectionResult(status="already_terminal", row=row)

    executor = IBExecutor()
    canceled_perm_ids: list[int] = []
    native_perm_id = row.get("native_order_perm_id")
    if native_perm_id is not None:
        executor.cancel(scope=scope, perm_id=int(native_perm_id))
        canceled_perm_ids.append(int(native_perm_id))

    claim = find_inflight_for_position(
        engine,
        broker=scope.broker,
        account_env=scope.account_env,
        broker_account=scope.broker_account,
        position_key=row["position_key"],
    )
    if claim is not None and claim["claimed_by_protection_id"] == protection_id:
        claim_perm_id = claim.get("broker_perm_id")
        if claim["status"] == "SUBMITTED" and claim_perm_id is not None and int(claim_perm_id) not in canceled_perm_ids:
            executor.cancel(scope=scope, perm_id=int(claim_perm_id))
            canceled_perm_ids.append(int(claim_perm_id))
        mark_terminal(
            engine,
            claim_id=claim["claim_id"],
            status="ABANDONED",
            last_error=reason,
        )

    if not cas_transition(
        engine,
        protection_id=protection_id,
        expected_state=row["state"],
        new_state="CANCELED",
        reason=reason,
        broker=scope.broker,
        account_env=scope.account_env,
        broker_account=scope.broker_account,
    ):
        return CancelProtectionResult(status="concurrent_state_change", row=row, canceled_perm_ids=canceled_perm_ids)
    return CancelProtectionResult(status="canceled", row=row, canceled_perm_ids=canceled_perm_ids)
