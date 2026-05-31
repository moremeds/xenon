"""Close-claim helpers. Spec §5.6."""
from __future__ import annotations

from typing import Any

_ORDER_REF_PREFIX = "xenon-pr-"


def derive_order_ref(*, claim_id: int) -> str:
    return f"{_ORDER_REF_PREFIX}{claim_id}"


def parse_order_ref_claim_id(order_ref: str) -> int:
    if not order_ref.startswith(_ORDER_REF_PREFIX):
        raise ValueError(f"order_ref {order_ref!r} does not have prefix {_ORDER_REF_PREFIX!r}")
    suffix = order_ref[len(_ORDER_REF_PREFIX) :]
    try:
        return int(suffix)
    except ValueError as e:
        raise ValueError(f"order_ref {order_ref!r} has non-integer suffix") from e


def should_skip_resubmit(
    *,
    order_ref: str,
    open_orders: list[dict[str, Any]],
    executions: list[dict[str, Any]],
) -> tuple[bool, int | None]:
    """Return (skip, perm_id) for deterministic orderRef retry idempotency."""
    for order in open_orders:
        if order.get("orderRef") == order_ref:
            return True, order.get("permId")

    for execution in executions:
        if execution.get("orderRef") == order_ref:
            return True, execution.get("permId")

    return False, None
