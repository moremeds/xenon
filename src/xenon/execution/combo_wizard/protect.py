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

from starlette.responses import JSONResponse, Response

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


def _protection_config(
    *,
    tp_enabled: bool,
    tp_target_price: Decimal | str | None,
    tp_ib_order_id: str | None,
    tp_client_attempt_id: str | None,
    alert_enabled: bool,
    alert_threshold: Decimal | str | None,
    alert_polarity: str | None,
    alert_virtual_id: str | None,
) -> dict[str, Any]:
    return {
        "tp_enabled": tp_enabled,
        "tp_target_price": str(tp_target_price) if tp_target_price is not None else None,
        "tp_ib_order_id": tp_ib_order_id,
        # order_submissions.client_attempt_id of the TP — the link a retry
        # uses to find out what reached IB.
        "tp_client_attempt_id": tp_client_attempt_id,
        "alert_enabled": alert_enabled,
        "alert_net_mid_threshold": str(alert_threshold) if alert_threshold is not None else None,
        "alert_polarity": alert_polarity,
        "alert_virtual_id": alert_virtual_id,
    }


def _upsert_protection(session_id: str, config: dict[str, Any]) -> None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        combo_wizard.upsert_protection(conn, session_id, config=config)


def _load_protection_config(session_id: str) -> dict[str, Any] | None:
    engine = get_sync_engine()
    with engine.connect() as conn:
        return combo_wizard.get_protection_config(conn, session_id)


def _claim_with_intent(session_id: str, intent: dict[str, Any]) -> bool:
    """CAS FILLED → PROTECTION_PENDING and store the protect intent (attempt
    id, target, alert threshold + polarity) in the SAME transaction, before
    any send: a claimed session always has the link a retry needs."""
    engine = get_sync_engine()
    with engine.begin() as conn:
        if combo_wizard.transition_session_state(conn, session_id, from_state="FILLED", to_state="PROTECTION_PENDING") != 1:
            return False
        combo_wizard.upsert_protection(conn, session_id, config=intent)
    return True


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


# Leg fields the TP keeps from the stored entry legs. Anything else (per-leg
# limitPrice, exchange, client extras) is entry-only and dropped.
_TP_LEG_KEYS = ("symbol", "expiry", "strike", "right", "action", "ratio", "conId", "con_id")


def _tp_place_body(session_id: str, session: dict, tp_target_price: Decimal) -> dict[str, Any]:
    """The take-profit as an /orders/place body, built from the session's
    stored payload (the entry body submit_combo placed).

    Allowlist, not a copy: only symbol, quantity and the legs' contract
    fields come from the entry. Entry-only or stale keys (orderRef, tif,
    outsideRth, account, replaces, acknowledge_limit_override, the entry's
    client_attempt_id / limitPrice / nonGuaranteed, per-leg limitPrice) never
    ride on the TP.

    SELL envelope closes the BUY-opened combo: IB reverses the legs, so the
    per-leg actions stay the stored structure (never flipped a second time —
    a double reverse is IB error 201). LMT at the SIGNED target (no abs()),
    GTC. The order owner places it on the reserved scope account."""
    payload = session.get("payload") or {}
    return {
        "type": "combo",
        "symbol": payload.get("symbol"),
        "action": "SELL",
        "quantity": payload.get("quantity"),
        "limitPrice": str(tp_target_price),
        "tif": "GTC",
        # Always a GUARANTEED combo (IB NonGuaranteed=0), whatever the entry
        # used. A non-guaranteed BAG may leg: IB can fill "SELL the long
        # call" before "BUY back the short call", leaving the short call
        # naked meanwhile. The pre-Gate-4 TP sent no NonGuaranteed tag (IB
        # default: guaranteed); the place path defaults to 1, so set it here.
        "nonGuaranteed": 0,
        "legs": [{k: leg[k] for k in _TP_LEG_KEYS if k in leg} for leg in payload.get("legs") or []],
        "client_attempt_id": f"wiz:{session_id}:tp:{uuid.uuid4().hex}",
    }


def _proves_tp_not_at_ib(status_code: int, reason_code: str | None) -> bool:
    """True only for place responses that prove IB holds no TP: a refusal
    before the send (4xx: Gate 4, preflight, read-only, attempt-id
    conflicts), an owner refusal that proves nothing was sent (503:
    ORDERS_HALTED / IB_BROKER_DISCONNECTED / IB_CONNECTION), or an explicit
    IB reject (502 IB_REJECT / LIMIT_OFF_TICK / account codes, row
    REJECTED). Everything else (PLACE_OUTCOME_UNKNOWN, a 200 duplicate,
    an unknown code) keeps the session claimed."""
    if reason_code == "PLACE_OUTCOME_UNKNOWN":
        return False
    if 400 <= status_code < 500 or status_code == 503:
        return True
    return status_code == 502 and reason_code in _IB_REJECT_CODES


# 502 codes `_place_result` writes for an explicit IB reject (row REJECTED).
_IB_REJECT_CODES = frozenset(
    {"IB_REJECT", "LIMIT_OFF_TICK", "ACCOUNT_UNRESOLVED", "ACCOUNT_NOT_MANAGED", "INVALID_ORDER_BODY"}
)


def _response_reason_code(response: Response) -> str | None:
    try:
        body = json.loads(bytes(response.body or b"{}"))
    except (ValueError, TypeError, AttributeError):
        return None
    return body.get("reason_code") if isinstance(body, dict) else None


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


# Sessions whose protect is running in THIS process. The API runs one
# uvicorn worker (docker/api.Dockerfile CMD, no --workers) with one order
# owner, so this set sees every in-flight protect. A retry for a session in
# it is refused: between the claim and the reservation insert no
# order_submissions row exists yet, and "no row" must not read as "nothing
# was sent". Check-and-add runs before the first await (atomic on the loop).
_IN_FLIGHT: set[str] = set()

# Reservation states that prove the TP is (or was) live at IB.
_TP_LIVE_STATES = frozenset({"WORKING", "PARTIALLY_FILLED", "FILLED"})
# Terminal states that prove no TP is live (orders_store._TERMINAL_STATES).
_TP_DEAD_STATES = frozenset({"FAILED", "REJECTED", "CANCELLED"})


def _outcome_unknown_409(session_id: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "reason_code": "PLACE_OUTCOME_UNKNOWN",
            "detail": f"Take-profit for wizard session {session_id}: {detail} — check working orders. Do not resubmit.",
        },
    )


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
    owner. The session is claimed FILLED → PROTECTION_PENDING first, with
    the intent (attempt id, target, alert threshold + polarity) stored in
    the same transaction, so two protects cannot place two TPs.

    Re-entrant for PROTECTION_PENDING: a retry looks up the stored attempt's
    reservation row (by client_attempt_id + scope):
      - WORKING / PARTIALLY_FILLED / FILLED → bind that TP, arm the Risk
        Alert with the STORED threshold + polarity, PROTECTED; no send. A
        different requested target is ignored (reported in the response).
      - FAILED / REJECTED / CANCELLED, or no row → nothing is live: release
        to FILLED and run the normal flow with this request's values (at
        most one new send).
      - PENDING / UNKNOWN / anything else, or a protect still running →
        409 PLACE_OUTCOME_UNKNOWN, claim kept, no send.

    Returns:
        - dict with ``state`` (PROTECTED / PROTECTION_PENDING), ``attempts``,
          ``tp_attached``, ``alert_armed``, ``tp_order_id``,
          ``alert_virtual_id``, ``client_attempt_id``, ``bound`` (True when a
          retry bound an existing TP); ``noop`` when already PROTECTED.
        - a Response with a top-level ``reason_code``: the ``order_entry.place``
          refusal (session back to FILLED only when it proves no TP is at
          IB), or the 409 above.

    Raises:
        ValueError: unknown session, scope mismatch, or a state other than
            FILLED / PROTECTION_PENDING / PROTECTED.
    """
    if is_read_only():
        return read_only_403()

    session = _load_session(session_id)
    if session["state"].upper() == "PROTECTED":
        return _PROTECTED_NOOP.copy()
    if session_id in _IN_FLIGHT:
        return _outcome_unknown_409(session_id, "a protect for this session is still running")
    _IN_FLIGHT.add(session_id)
    try:
        return await _protect(session_id, session, tp_target_price, alert_net_mid_threshold, polarity)
    finally:
        _IN_FLIGHT.discard(session_id)


async def _protect(
    session_id: str,
    session: dict[str, Any],
    tp_target_price: Decimal,
    alert_net_mid_threshold: Decimal,
    polarity: str,
) -> dict[str, Any] | Response:
    if session["state"].upper() == "PROTECTION_PENDING":
        resumed = _resume_pending(session_id, requested_target=tp_target_price)
        if resumed is not None:
            return resumed
        # Proven: no TP is live. Released to FILLED — continue normally.

    body = _tp_place_body(session_id, session, tp_target_price)
    cid = body["client_attempt_id"]
    intent = _protection_config(
        tp_enabled=False,
        tp_target_price=tp_target_price,
        tp_ib_order_id=None,
        tp_client_attempt_id=cid,
        alert_enabled=False,
        alert_threshold=alert_net_mid_threshold,
        alert_polarity=polarity,
        alert_virtual_id=None,
    )
    if not _claim_with_intent(session_id, intent):
        current = _load_session(session_id)
        if current["state"].upper() == "PROTECTED":
            return _PROTECTED_NOOP.copy()
        raise ValueError(f"Wizard session {session_id} cannot protect from state {current['state']}")

    from xenon.api.services import order_entry

    try:
        result = await order_entry.place(body)
    except BaseException as exc:
        # Fail safe: a raise does not prove nothing was sent (a request
        # cancelled after the owner job started re-raises from
        # OrderOwner.run while the worker may still send). Keep the claim;
        # a retry resolves it from the reservation row.
        try:
            _record_event(
                session_id,
                "PROTECTION_PENDING",
                {"reason": "PLACE_RAISED", "error": repr(exc), "client_attempt_id": cid},
            )
        except Exception:  # noqa: BLE001 — never mask the original error
            logger.exception("protect: could not record PLACE_RAISED for %s", session_id)
        raise

    if isinstance(result, Response):
        reason_code = _response_reason_code(result)
        detail = {"reason_code": reason_code, "status": result.status_code, "client_attempt_id": cid}
        if not _proves_tp_not_at_ib(result.status_code, reason_code):
            # PLACE_OUTCOME_UNKNOWN or anything unexpected: IB may hold the TP.
            _record_event(session_id, "PROTECTION_PENDING", detail)
            return result
        _transition(session_id, "PROTECTION_PENDING", "FILLED")
        _record_event(session_id, "PROTECTION_TP_REFUSED", detail)
        return result
    if not isinstance(result, dict):
        _record_event(session_id, "PROTECTION_PENDING", {"reason": "PLACE_INVALID_RESPONSE", "client_attempt_id": cid})
        raise ValueError("Order placement returned an invalid response")

    tp_order_id = str(result.get("permId") or result.get("orderId") or "")
    _record_event(
        session_id,
        "PROTECTION_TP_ATTACHED",
        {"tp_order_id": tp_order_id, "client_attempt_id": cid, "target_price": str(tp_target_price)},  # signed
    )
    return _arm_and_finish(session_id, intent, tp_order_id=tp_order_id, bound=False)


def _resume_pending(session_id: str, *, requested_target: Decimal) -> dict[str, Any] | Response | None:
    """Resolve a PROTECTION_PENDING session from its stored attempt.

    Returns the bind result or a 409; None when no TP is live and the
    session was released to FILLED (the caller then places a new TP)."""
    from xenon.api.services import order_entry
    from xenon.execution import orders_store

    config = _load_protection_config(session_id) or {}
    cid = config.get("tp_client_attempt_id")
    if not cid:
        # Claimed without a stored attempt (only possible for a session
        # claimed by an older build): what reached IB cannot be proven.
        return _outcome_unknown_409(session_id, "no stored take-profit attempt to check")

    # order_entry.place reserves as user "local" in the resolved scope.
    row = orders_store.lookup_by_attempt("local", cid, **order_entry._resolve_scope_kwargs())
    state = str(row.state).upper() if row is not None else None

    if state in _TP_LIVE_STATES:
        tp_order_id = str(row.perm_id or row.ib_order_id or "")
        detail: dict[str, Any] = {"client_attempt_id": cid, "row_state": state, "tp_order_id": tp_order_id}
        stored_target = config.get("tp_target_price")
        price_note = None
        if stored_target is not None and Decimal(stored_target) != requested_target:
            price_note = {"tp_target_price": stored_target, "requested_tp_target_price": str(requested_target)}
            detail.update(price_note)
        _record_event(session_id, "PROTECTION_TP_BOUND", detail)
        out = _arm_and_finish(session_id, config, tp_order_id=tp_order_id, bound=True)
        if price_note:
            out.update(price_note)
        return out

    if state is None or state in _TP_DEAD_STATES:
        if not _transition(session_id, "PROTECTION_PENDING", "FILLED"):
            current = _load_session(session_id)
            raise ValueError(f"Wizard session {session_id} cannot protect from state {current['state']}")
        _record_event(
            session_id, "PROTECTION_RELEASED", {"client_attempt_id": cid, "row_state": state or "NO_RESERVATION"}
        )
        return None

    _record_event(session_id, "PROTECTION_PENDING", {"reason": "TP_UNRESOLVED", "client_attempt_id": cid, "row_state": state})
    return _outcome_unknown_409(session_id, f"its order is {state}")


def _arm_and_finish(session_id: str, intent: dict[str, Any], *, tp_order_id: str, bound: bool) -> dict[str, Any]:
    """Arm the Risk Alert with the intent's threshold + polarity, persist the
    protection config, set the final state. No broker call."""
    threshold = Decimal(str(intent["alert_net_mid_threshold"]))
    polarity = str(intent.get("alert_polarity") or "DEBIT")
    alert_armed = False
    alert_virtual_id: str | None = None
    try:
        alert_virtual_id = _register_risk_alert(session_id, threshold=threshold, polarity=polarity)
        alert_armed = True
        _record_event(
            session_id,
            "PROTECTION_RISK_ALERT_ARMED",
            {"threshold": str(threshold), "polarity": polarity, "alert_virtual_id": alert_virtual_id},  # signed
        )
    except Exception as exc:  # noqa: BLE001
        _record_event(session_id, "PROTECTION_RISK_ALERT_FAILED", {"error": str(exc)})

    _upsert_protection(
        session_id,
        _protection_config(
            tp_enabled=True,
            tp_target_price=intent.get("tp_target_price"),
            tp_ib_order_id=tp_order_id,
            tp_client_attempt_id=intent.get("tp_client_attempt_id"),
            alert_enabled=alert_armed,
            alert_threshold=threshold,
            alert_polarity=polarity,
            alert_virtual_id=alert_virtual_id,
        ),
    )

    # A TP alone is not the assisted-exit contract: without the alert the
    # session stays PROTECTION_PENDING (the Risk Alert task polls only
    # alert_enabled rows); a retry binds the live TP and arms the alert.
    final_state = "PROTECTED" if alert_armed else "PROTECTION_PENDING"
    _set_state(session_id, final_state)

    return {
        "state": final_state,
        "attempts": 0 if bound else 1,
        "tp_attached": True,
        "alert_armed": alert_armed,
        "tp_order_id": tp_order_id,
        "alert_virtual_id": alert_virtual_id,
        "client_attempt_id": intent.get("tp_client_attempt_id"),
        "bound": bound,
        "tp_target_price": intent.get("tp_target_price"),
    }
