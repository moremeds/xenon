"""Combo wizard post-fill protection pipeline.

For V1 defined-risk spreads, after a combo FILLED we:

1. Place a broker-side combo take-profit (GTC SELL BAG) through
   ``order_entry.place`` — the same path as every other order, so Gate 4
   evaluates coverage and reserves an ``order_submissions`` row before the
   order owner sends it.
2. Arm a Risk Alert (assisted-exit) row — NOT a stop-loss per spec §9.2.

No retry loop: a refused TP returns the place response (top-level
``reason_code``) and the session goes back to FILLED, so the operator can
protect again. An uncertain place (PLACE_OUTCOME_UNKNOWN) keeps the session
PROTECTION_PENDING; the activity poller resolves the reserved row.

IMPORTANT: signed combo pricing is preserved end-to-end. Do NOT abs() any
combo price in this module — CREDIT spreads have negative net prices.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from starlette.responses import Response

from xenon.api.guards import is_read_only, read_only_403
from xenon.db.engine import get_sync_engine
from xenon.db.queries import combo_wizard
from xenon.execution.combo_wizard.session import _assert_session_scope

logger = logging.getLogger(__name__)

_REHYDRATABLE = {"SUBMITTING", "WORKING", "REPRICE_PENDING", "PROTECTION_PENDING", "PROTECTED"}


def risk_alert_popup_copy() -> str:
    """Spec §9.2 — the popup must say Risk Alert → Assisted Exit, not stop-loss.

    Any UI surface that shows this message should call this helper so the
    wording stays centralized and the copy rule is grep-able.
    """
    return "Risk Alert triggered — Assisted Exit required. Confirm to close this combo; the system will not auto-exit."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_session(session_id: str) -> dict[str, Any]:
    engine = get_sync_engine()
    with engine.begin() as conn:
        row = combo_wizard.get_session(conn, session_id)
    if row is None:
        raise ValueError(f"Unknown wizard session {session_id}")
    _assert_session_scope(session_id, row)
    # JSONB `payload` is returned as a dict directly — no json.loads needed.
    if row.get("payload") is None:
        row["payload"] = {}
    return row


def _set_state(session_id: str, state: str) -> None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        combo_wizard.update_session(conn, session_id, state=state)


def _transition(session_id: str, from_state: str, to_state: str) -> bool:
    engine = get_sync_engine()
    with engine.begin() as conn:
        return combo_wizard.transition_session_state(conn, session_id, from_state=from_state, to_state=to_state) == 1


_PROTECTED_NOOP: dict[str, Any] = {
    "state": "PROTECTED",
    "noop": True,
    "tp_attached": True,
    "alert_armed": True,
    "attempts": 0,
}


def _record_event(session_id: str, kind: str, detail: dict) -> None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        combo_wizard.record_event(conn, session_id=session_id, kind=kind, detail=detail)


def _upsert_protection(
    *,
    session_id: str,
    tp_enabled: bool,
    tp_target_price: Decimal | None,
    tp_ib_order_id: str | None,
    alert_enabled: bool,
    alert_threshold: Decimal | None,
    alert_virtual_id: str | None,
) -> None:
    config = {
        "tp_enabled": tp_enabled,
        "tp_target_price": str(tp_target_price) if tp_target_price is not None else None,
        "tp_ib_order_id": tp_ib_order_id,
        "alert_enabled": alert_enabled,
        "alert_net_mid_threshold": str(alert_threshold) if alert_threshold is not None else None,
        "alert_virtual_id": alert_virtual_id,
    }
    engine = get_sync_engine()
    with engine.begin() as conn:
        combo_wizard.upsert_protection(conn, session_id, config=config)


# ---------------------------------------------------------------------------
# Risk Alert (app-side only — no broker order).
# ---------------------------------------------------------------------------


def _register_risk_alert(session_id: str, *, threshold: Decimal, polarity: str) -> str:
    """Persist a ``RISK_ALERT_REGISTERED`` event keyed by a virtual id. The
    Wizard Risk Alert task (``api/services/wizard_risk_alert``) drives the
    crossing check. Risk Alert → Assisted Exit, NOT a stop-loss."""
    virtual_id = f"ra-{uuid.uuid4().hex[:12]}"
    _record_event(
        session_id,
        "RISK_ALERT_REGISTERED",
        {"virtual_id": virtual_id, "threshold": str(threshold), "polarity": polarity},  # signed
    )
    return virtual_id


def _tp_place_body(session_id: str, session: dict, tp_target_price: Decimal) -> dict[str, Any]:
    """The take-profit as an /orders/place body, built from the session's
    stored payload the same way submit_combo builds the entry.

    SELL envelope closes the BUY-opened combo: IB reverses the legs, so the
    per-leg actions stay the stored structure (never flipped a second time —
    a double reverse is IB error 201). LMT at the SIGNED target (no abs()),
    GTC. The order owner places it on the reserved scope account."""
    body = dict(session.get("payload") or {})
    body.update(
        {
            "type": "combo",
            "action": "SELL",
            "limitPrice": str(tp_target_price),
            "tif": "GTC",
            "client_attempt_id": f"wiz:{session_id}:tp:{uuid.uuid4().hex}",
        }
    )
    return body


def _response_reason_code(response: Response) -> str | None:
    try:
        body = json.loads(bytes(response.body or b"{}"))
    except (ValueError, TypeError, AttributeError):
        return None
    return body.get("reason_code") if isinstance(body, dict) else None


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


async def attach_protection(
    session_id: str,
    *,
    tp_target_price: Decimal,
    alert_net_mid_threshold: Decimal,
    polarity: str = "DEBIT",
) -> dict[str, Any] | Response:
    """Attach combo TP + Risk Alert after a filled wizard session.

    The TP goes through ``order_entry.place`` like every other order:
    ``ensure_order_allowed`` → ``gate4.reserve_with_coverage`` (coverage +
    an ``order_submissions`` reservation in one locked txn) → the order
    owner. The session is claimed FILLED → PROTECTION_PENDING first, so two
    concurrent protects cannot place two TPs.

    Args:
        session_id: wizard session id.
        tp_target_price: SIGNED combo target (negative for CREDIT closes).
        alert_net_mid_threshold: SIGNED net-mid threshold for Risk Alert.
        polarity: "DEBIT" | "CREDIT" | "FLAT".

    Returns:
        - dict with ``state`` (PROTECTED / PROTECTION_PENDING), ``attempts``,
          ``tp_attached``, ``alert_armed``, ``tp_order_id``,
          ``alert_virtual_id``, ``submission_id`` when the TP was placed;
          ``noop`` when the session was already PROTECTED.
        - the ``order_entry.place`` Response (top-level ``reason_code``)
          when the TP was not placed. A refusal (Gate 4, halt, broker down,
          IB reject) returns the session to FILLED; PLACE_OUTCOME_UNKNOWN
          keeps it PROTECTION_PENDING (IB may hold the TP — a second protect
          must not place a duplicate).

    Raises:
        ValueError: unknown session, scope mismatch, or a state other than
            FILLED / PROTECTED.
    """
    if is_read_only():
        return read_only_403()

    session = _load_session(session_id)
    if session["state"].upper() == "PROTECTED":
        return _PROTECTED_NOOP.copy()
    if not _transition(session_id, "FILLED", "PROTECTION_PENDING"):
        current = _load_session(session_id)
        if current["state"].upper() == "PROTECTED":
            return _PROTECTED_NOOP.copy()
        raise ValueError(f"Wizard session {session_id} cannot protect from state {current['state']}")

    from xenon.api.services import order_entry

    body = _tp_place_body(session_id, session, tp_target_price)
    try:
        result = await order_entry.place(body)
    except Exception:
        _transition(session_id, "PROTECTION_PENDING", "FILLED")
        raise

    if isinstance(result, Response):
        reason_code = _response_reason_code(result)
        detail = {"reason_code": reason_code, "status": result.status_code, "client_attempt_id": body["client_attempt_id"]}
        if reason_code == "PLACE_OUTCOME_UNKNOWN":
            _record_event(session_id, "PROTECTION_PENDING", detail)
            return result
        _transition(session_id, "PROTECTION_PENDING", "FILLED")
        _record_event(session_id, "PROTECTION_TP_REFUSED", detail)
        return result
    if not isinstance(result, dict):
        _transition(session_id, "PROTECTION_PENDING", "FILLED")
        raise ValueError("Order placement returned an invalid response")

    tp_order_id = str(result.get("permId") or result.get("orderId") or "")
    submission_id = result.get("submission_id")
    _record_event(
        session_id,
        "PROTECTION_TP_ATTACHED",
        {
            "tp_order_id": tp_order_id,
            "submission_id": submission_id,
            "client_attempt_id": body["client_attempt_id"],
            "target_price": str(tp_target_price),  # signed
        },
    )

    # --- Arm the Risk Alert ---------------------------------------------------
    alert_armed = False
    alert_virtual_id: str | None = None
    try:
        alert_virtual_id = _register_risk_alert(session_id, threshold=alert_net_mid_threshold, polarity=polarity)
        alert_armed = True
        _record_event(
            session_id,
            "PROTECTION_RISK_ALERT_ARMED",
            {
                "threshold": str(alert_net_mid_threshold),  # signed
                "polarity": polarity,
                "alert_virtual_id": alert_virtual_id,
            },
        )
    except Exception as exc:  # noqa: BLE001
        _record_event(session_id, "PROTECTION_RISK_ALERT_FAILED", {"error": str(exc)})

    _upsert_protection(
        session_id=session_id,
        tp_enabled=True,
        tp_target_price=tp_target_price,
        tp_ib_order_id=tp_order_id,
        alert_enabled=alert_armed,
        alert_threshold=alert_net_mid_threshold,
        alert_virtual_id=alert_virtual_id,
    )

    # A TP alone is not the assisted-exit contract: without the alert the
    # session stays PROTECTION_PENDING (the Risk Alert task polls only
    # alert_enabled rows).
    final_state = "PROTECTED" if alert_armed else "PROTECTION_PENDING"
    _set_state(session_id, final_state)

    return {
        "state": final_state,
        "attempts": 1,
        "tp_attached": True,
        "alert_armed": alert_armed,
        "tp_order_id": tp_order_id,
        "alert_virtual_id": alert_virtual_id,
        "submission_id": submission_id,
    }
