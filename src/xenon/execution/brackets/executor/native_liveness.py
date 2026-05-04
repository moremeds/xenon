"""Per-tick native-order liveness probe. Spec §8, §10.3."""
from __future__ import annotations

from enum import StrEnum


class NativeOrderState(StrEnum):
    LIVE = "LIVE"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    INACTIVE = "INACTIVE"
    UNKNOWN = "UNKNOWN"


_IB_TO_NATIVE = {
    "Submitted": NativeOrderState.LIVE,
    "PreSubmitted": NativeOrderState.LIVE,
    "Working": NativeOrderState.LIVE,
    "Filled": NativeOrderState.FILLED,
    "Cancelled": NativeOrderState.CANCELLED,
    "ApiCancelled": NativeOrderState.CANCELLED,
    "Inactive": NativeOrderState.INACTIVE,
}


def verify_native_order_live(*, ib_client, perm_id: int) -> NativeOrderState:
    try:
        state = ib_client.get_order_state(perm_id=perm_id)
    except Exception:  # noqa: BLE001
        return NativeOrderState.UNKNOWN
    if state is None:
        return NativeOrderState.UNKNOWN
    return _IB_TO_NATIVE.get(state.get("status"), NativeOrderState.UNKNOWN)
