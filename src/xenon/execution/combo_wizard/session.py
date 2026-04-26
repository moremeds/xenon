from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from starlette.responses import Response

from xenon.db.engine import get_sync_engine
from xenon.db.queries import combo_wizard as cwq
from xenon.execution.combo_wizard import planner as combo_planner
from xenon.execution.combo_wizard.models import ComboLegQuote, ComboLegSpec

_REPRICEABLE_STATES = {"WORKING", "REPRICE_PENDING"}
_ABORTABLE_LIVE_STATES = {"WORKING", "REPRICE_PENDING"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _scope_kwargs() -> dict[str, str]:
    """Resolve broker account scope from FastAPI app.state.

    Wizard flows always run in-process inside the FastAPI app, so the
    global server module's app.state is the source of truth. Falls back to
    legacy_unknown if the lifespan didn't run (test mode without setup).
    """
    try:
        from xenon.api import server as server_mod

        state = server_mod.app.state
        mode = getattr(state, "trading_mode", None)
        account = getattr(state, "account", None)
        if mode and account:
            return {"broker": "IB", "account_env": mode, "broker_account": account}
    except Exception:
        pass
    return {"broker": "IB", "account_env": "legacy_unknown", "broker_account": "legacy_unknown"}


def _normalize_right(value: Any) -> str:
    right = str(value or "").upper()
    if right == "CALL":
        return "C"
    if right == "PUT":
        return "P"
    return right


def _normalize_expiry(value: Any) -> str:
    return str(value or "").replace("-", "")


def _same_decimal(left: Any, right: Decimal) -> bool:
    try:
        return Decimal(str(left)) == right
    except Exception:
        return False


def _prepare_order_payload(
    *,
    ticker: str,
    intent: str,
    plan_legs: list[ComboLegSpec],
    order_payload: dict[str, Any],
) -> dict[str, Any]:
    """Validate and enrich the submitted payload from the already-priced legs."""
    payload = dict(order_payload)
    expected_symbol = ticker.upper()
    payload_symbol = str(payload.get("symbol") or expected_symbol).upper()
    if payload_symbol != expected_symbol:
        raise ValueError("order_payload symbol does not match planned ticker")

    expected_action = "BUY" if intent.upper() == "OPEN" else "SELL"
    payload_action = str(payload.get("action") or expected_action).upper()
    if payload_action != expected_action:
        raise ValueError("order_payload action does not match planned intent")

    payload["symbol"] = expected_symbol
    payload["type"] = "combo"
    payload["action"] = expected_action

    raw_legs = list(payload.get("legs") or [])
    if len(raw_legs) != len(plan_legs):
        raise ValueError("order_payload legs must match planned legs")

    enriched_legs: list[dict[str, Any]] = []
    for index, (planned, raw_leg) in enumerate(zip(plan_legs, raw_legs, strict=True)):
        leg = dict(raw_leg)

        action = str(leg.get("action") or planned.action).upper()
        if action != planned.action:
            raise ValueError(f"order_payload leg {index} action does not match planned leg")

        right = _normalize_right(leg.get("right") or planned.right)
        if right != planned.right:
            raise ValueError(f"order_payload leg {index} right does not match planned leg")

        expiry = _normalize_expiry(leg.get("expiry") or planned.expiry)
        if expiry != _normalize_expiry(planned.expiry):
            raise ValueError(f"order_payload leg {index} expiry does not match planned leg")

        if leg.get("strike") is not None and not _same_decimal(leg.get("strike"), planned.strike):
            raise ValueError(f"order_payload leg {index} strike does not match planned leg")

        ratio = int(leg.get("ratio") or leg.get("quantity") or planned.quantity)
        if ratio != int(planned.quantity):
            raise ValueError(f"order_payload leg {index} ratio does not match planned leg")

        leg.update(
            {
                "symbol": expected_symbol,
                "expiry": expiry,
                "strike": float(planned.strike),
                "right": right,
                "action": action,
                "ratio": ratio,
            }
        )
        enriched_legs.append(leg)

    payload["legs"] = enriched_legs
    return payload


def create_session(
    *,
    ticker: str,
    intent: str,
    structure_name: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    session_id = f"wiz-{uuid.uuid4().hex[:12]}"
    now = _now()
    engine = get_sync_engine()
    with engine.begin() as conn:
        cwq.create_session(
            conn,
            session_id=session_id,
            ticker=ticker,
            state="planned",
            structure_name=structure_name,
            intent=intent,
            payload=payload,
            created_at=now,
            updated_at=now,
            **_scope_kwargs(),
        )
    return {"session_id": session_id}


def _load_session(session_id: str) -> dict[str, Any]:
    engine = get_sync_engine()
    with engine.connect() as conn:
        row = cwq.get_session(conn, session_id)
    if row is None:
        raise ValueError(f"Unknown wizard session {session_id}")
    return {
        "session_id": row["session_id"],
        "ticker": row["ticker"],
        "state": row["state"],
        "structure_name": row["structure_name"],
        "intent": row["intent"],
        "payload": row["payload"] or {},
        "current_attempt_id": row["current_attempt_id"],
    }


def _load_current_attempt(session_id: str) -> dict[str, Any]:
    engine = get_sync_engine()
    with engine.connect() as conn:
        row = cwq.get_latest_attempt(conn, session_id)
    if row is None:
        raise ValueError(f"No combo attempt found for session {session_id}")
    return {
        "attempt_id": row["attempt_id"],
        "ib_order_id": row["ib_order_id"],
        "perm_id": row["perm_id"],
        "modify_sequence": int(row.get("modify_sequence") or 0),
    }


def _claim_session_for_submit(session_id: str, attempt_id: str) -> dict[str, Any]:
    engine = get_sync_engine()
    with engine.begin() as conn:
        row = cwq.claim_session_for_submit(conn, session_id, attempt_id)
        if row is not None:
            return {
                "session_id": row["session_id"],
                "ticker": row["ticker"],
                "state": row["state"],
                "structure_name": row["structure_name"],
                "intent": row["intent"],
                "payload": row["payload"] or {},
                "current_attempt_id": row["current_attempt_id"],
                "broker": row["broker"],
                "account_env": row["account_env"],
                "broker_account": row["broker_account"],
            }

        current = cwq.get_session(conn, session_id)

    if current is None:
        raise ValueError(f"Unknown wizard session {session_id}")
    state = current["state"]
    current_attempt_id = current["current_attempt_id"]
    if current_attempt_id:
        raise ValueError(f"Wizard session {session_id} already has a submitted combo attempt")
    raise ValueError(f"Wizard session {session_id} cannot submit from state {state}")


def _release_submit_claim(session_id: str, attempt_id: str) -> None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        cwq.release_submit_claim(conn, session_id, attempt_id)


def _order_store_modify_sequence(current: dict[str, Any]) -> int:
    ib_order_id = str(current.get("ib_order_id") or "")
    perm_id = str(current.get("perm_id") or "")
    if not ib_order_id and not perm_id:
        return 0
    engine = get_sync_engine()
    with engine.connect() as conn:
        seq = cwq.get_order_modify_sequence(conn, ib_order_id=ib_order_id, perm_id=perm_id)
    return seq or 0


def _update_attempt_modify_sequence(attempt_id: str, sequence: int) -> None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        cwq.update_attempt(conn, attempt_id, modify_sequence=sequence)


def plan_session(
    *,
    ticker: str,
    intent: str,
    legs: list[dict[str, Any]],
    quotes: dict[str, dict[str, Any]],
    order_payload: dict[str, Any],
) -> dict[str, Any]:
    plan_legs = [ComboLegSpec.model_validate(leg) for leg in legs]
    plan = combo_planner.build_plan(
        ticker=ticker,
        legs=plan_legs,
        quotes={contract_id: ComboLegQuote.model_validate(quote) for contract_id, quote in quotes.items()},
    )
    payload = _prepare_order_payload(
        ticker=ticker,
        intent=intent,
        plan_legs=plan_legs,
        order_payload=order_payload,
    )
    session = create_session(
        ticker=ticker,
        intent=intent,
        structure_name=plan.structure_name,
        payload=payload,
    )
    return {
        "session_id": session["session_id"],
        "mode": plan.mode,
        "structure_name": plan.structure_name,
        "natural_price": str(plan.natural_price),
        "mid_price": str(plan.mid_price),
        "signed_natural_price": str(plan.signed_natural_price),
        "signed_mid_price": str(plan.signed_mid_price),
        "price_polarity": plan.price_polarity,
        "ladder_step": str(plan.ladder_step),
    }


async def submit_combo(session_id: str, request: dict[str, Any]) -> dict[str, Any]:
    attempt_id = uuid.uuid4().hex
    session = _claim_session_for_submit(session_id, attempt_id)

    # Scope-mismatch guard: a session planned in paper must not be submitted
    # while the server is in live mode (and vice versa). Both rows
    # (wizard_combo_attempts AND order_submissions) would otherwise carry
    # different scope, breaking audit and isolation.
    current_scope = _scope_kwargs()
    session_scope = {
        "broker": session.get("broker"),
        "account_env": session.get("account_env"),
        "broker_account": session.get("broker_account"),
    }
    # Allow legacy_unknown sessions to flow through under any scope
    # (pre-migration sessions don't have a meaningful scope).
    if session_scope["account_env"] not in (None, "legacy_unknown"):
        if session_scope != current_scope:
            _release_submit_claim(session_id, attempt_id)
            raise ValueError(
                f"Wizard session {session_id} scope mismatch: session={session_scope}, current={current_scope}"
            )

    client_attempt_id = f"wiz:{session_id}:combo:{attempt_id}"

    payload = dict(session["payload"])
    target_price = request.get("target_price")
    if target_price is not None:
        payload["limitPrice"] = str(target_price)
    payload["client_attempt_id"] = client_attempt_id

    from xenon.api import server as server_mod

    try:
        result = await server_mod._orders_place_from_body(payload)
    except Exception:
        _release_submit_claim(session_id, attempt_id)
        raise

    if isinstance(result, Response):
        _release_submit_claim(session_id, attempt_id)
        return result
    if not isinstance(result, dict):
        _release_submit_claim(session_id, attempt_id)
        raise ValueError("Order placement returned an invalid response")

    now = _now()
    price_basis = str(request.get("price_basis") or "CUSTOM")
    target = str(Decimal(str(target_price or payload.get("limitPrice") or "0")))

    engine = get_sync_engine()
    with engine.begin() as conn:
        cwq.create_attempt(
            conn,
            attempt_id=attempt_id,
            session_id=session_id,
            ticker=session["ticker"],
            structure_name=session["structure_name"],
            ib_order_id=str(result.get("orderId") or ""),
            perm_id=str(result.get("permId") or ""),
            limit_price=Decimal(target),
            state="WORKING",
            submitted_at=now,
            updated_at=now,
            combo_contract={
                "client_attempt_id": client_attempt_id,
                "intent": session["intent"],
                "price_basis": price_basis,
            },
            **_scope_kwargs(),
        )
        cwq.update_session(conn, session_id, state="working")

    return {
        **result,
        "attempt_id": attempt_id,
        "client_attempt_id": client_attempt_id,
    }


async def reprice_combo(session_id: str, request: dict[str, Any]) -> dict[str, Any]:
    session = _load_session(session_id)
    current = _load_current_attempt(session_id)
    state = str(session.get("state") or "")
    if state.upper() not in _REPRICEABLE_STATES:
        raise ValueError(f"Wizard session {session_id} cannot reprice from state {state}")

    from xenon.api import server as server_mod

    next_sequence = (
        max(
            int(current.get("modify_sequence") or 0),
            _order_store_modify_sequence(current),
        )
        + 1
    )
    engine = get_sync_engine()
    with engine.begin() as conn:
        cwq.update_attempt(
            conn,
            current["attempt_id"],
            modify_sequence=next_sequence,
            limit_price=Decimal(str(request["target_price"])),
        )
    try:
        return await server_mod._orders_modify_from_body(
            {
                "orderId": int(current["ib_order_id"] or 0),
                "permId": int(current["perm_id"] or 0),
                "newPrice": str(request["target_price"]),
                "modifySequence": next_sequence,
            }
        )
    except Exception as exc:
        detail = getattr(exc, "detail", None)
        current_sequence = None
        if isinstance(detail, dict):
            current_sequence = detail.get("applied") or detail.get("current_sequence")
        try:
            if current_sequence is not None:
                _update_attempt_modify_sequence(current["attempt_id"], int(current_sequence))
        finally:
            raise


def _session_row_to_dict(row: dict) -> dict[str, Any]:
    payload = row.get("payload") or {}
    created = row.get("created_at")
    updated = row.get("updated_at")
    return {
        "session_id": row["session_id"],
        "ticker": row["ticker"],
        "state": row["state"],
        "structure_name": row.get("structure_name"),
        "intent": row.get("intent"),
        "payload": payload,
        "current_attempt_id": row.get("current_attempt_id"),
        "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
        "updated_at": updated.isoformat() if hasattr(updated, "isoformat") else updated,
    }


def list_sessions() -> list[dict[str, Any]]:
    """List wizard sessions for the current broker account scope.

    Filters by app.state-resolved scope so a live-mode UI never shows
    paper sessions and vice versa. legacy_unknown sessions are surfaced
    only when no scope is resolvable (test mode without lifespan).
    """
    scope = _scope_kwargs()
    # When scope resolves to legacy_unknown (test mode), don't filter —
    # tests seed rows with default scope. In production lifespan
    # populates scope, so this filter is active.
    filter_kwargs: dict[str, str] = {}
    if scope.get("account_env") and scope["account_env"] != "legacy_unknown":
        filter_kwargs = scope
    engine = get_sync_engine()
    with engine.connect() as conn:
        rows = cwq.list_sessions(conn, limit=50, **filter_kwargs)
    return [_session_row_to_dict(row) for row in rows]


def get_session(session_id: str) -> dict[str, Any]:
    engine = get_sync_engine()
    with engine.connect() as conn:
        row = cwq.get_session(conn, session_id)
    if row is None:
        raise ValueError(f"Unknown wizard session {session_id}")
    return _session_row_to_dict(row)


async def abort_session(session_id: str) -> dict[str, Any] | Response:
    session = _load_session(session_id)
    state = str(session.get("state") or "")
    upper = state.upper()
    if upper == "SUBMITTING":
        raise ValueError(f"Wizard session {session_id} cannot abort from state {state}")
    if upper not in {"PLANNED", *_ABORTABLE_LIVE_STATES}:
        raise ValueError(f"Wizard session {session_id} cannot abort from state {state}")

    if upper in _ABORTABLE_LIVE_STATES:
        current = _load_current_attempt(session_id)
        from xenon.api import server as server_mod

        cancel_result = await server_mod._orders_cancel_from_body(
            {
                "orderId": int(current["ib_order_id"] or 0),
                "permId": int(current["perm_id"] or 0),
            }
        )
        if isinstance(cancel_result, Response):
            return cancel_result

    engine = get_sync_engine()
    with engine.begin() as conn:
        cwq.update_session(conn, session_id, state="ABORTED")
        cwq.record_event(conn, session_id=session_id, kind="ABORTED", detail={})
    return get_session(session_id)
