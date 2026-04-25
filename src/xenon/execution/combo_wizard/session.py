from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from starlette.responses import Response

from xenon.execution import orders_store
from xenon.execution.combo_wizard import planner as combo_planner
from xenon.execution.combo_wizard.models import ComboLegQuote, ComboLegSpec

_REPRICEABLE_STATES = {"WORKING", "REPRICE_PENDING"}
_ABORTABLE_LIVE_STATES = {"WORKING", "REPRICE_PENDING"}


def _db_path() -> Path:
    return orders_store._resolve_path(None)


def _connect():
    return orders_store._connect_utc(_db_path())


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
    orders_store.init_store(_db_path())
    session_id = f"wiz-{uuid.uuid4().hex[:12]}"
    now = _now()
    con = _connect()
    try:
        con.execute(
            """
            INSERT INTO wizard_sessions (
                session_id, ticker, state, structure_name, intent, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [session_id, ticker, "planned", structure_name, intent, json.dumps(payload), now, now],
        )
    finally:
        con.close()
    return {"session_id": session_id}


def _load_session(session_id: str) -> dict[str, Any]:
    orders_store.init_store(_db_path())
    con = _connect()
    try:
        row = con.execute(
            """
            SELECT session_id, ticker, state, structure_name, intent, payload_json, current_attempt_id
              FROM wizard_sessions
             WHERE session_id = ?
            """,
            [session_id],
        ).fetchone()
    finally:
        con.close()
    if row is None:
        raise ValueError(f"Unknown wizard session {session_id}")
    payload_json = row[5]
    payload = json.loads(payload_json) if payload_json else {}
    return {
        "session_id": row[0],
        "ticker": row[1],
        "state": row[2],
        "structure_name": row[3],
        "intent": row[4],
        "payload": payload,
        "current_attempt_id": row[6],
    }


def _load_current_attempt(session_id: str) -> dict[str, Any]:
    con = _connect()
    try:
        row = con.execute(
            """
            SELECT attempt_id, ib_order_id, perm_id, modify_sequence
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
        raise ValueError(f"No combo attempt found for session {session_id}")
    return {
        "attempt_id": row[0],
        "ib_order_id": row[1],
        "perm_id": row[2],
        "modify_sequence": int(row[3] or 0),
    }


def _claim_session_for_submit(session_id: str, attempt_id: str) -> dict[str, Any]:
    orders_store.init_store(_db_path())
    now = _now()
    with orders_store._WRITE_LOCK:
        con = _connect()
        try:
            row = con.execute(
                """
                UPDATE wizard_sessions
                   SET state='submitting', current_attempt_id=?, updated_at=?
                 WHERE session_id=?
                   AND UPPER(state)='PLANNED'
                   AND current_attempt_id IS NULL
                 RETURNING session_id, ticker, state, structure_name, intent,
                           payload_json, current_attempt_id
                """,
                [attempt_id, now, session_id],
            ).fetchone()
            if row is not None:
                payload_json = row[5]
                return {
                    "session_id": row[0],
                    "ticker": row[1],
                    "state": row[2],
                    "structure_name": row[3],
                    "intent": row[4],
                    "payload": json.loads(payload_json) if payload_json else {},
                    "current_attempt_id": row[6],
                }

            current = con.execute(
                "SELECT state, current_attempt_id FROM wizard_sessions WHERE session_id=?",
                [session_id],
            ).fetchone()
        finally:
            con.close()

    if current is None:
        raise ValueError(f"Unknown wizard session {session_id}")
    state, current_attempt_id = current
    if current_attempt_id:
        raise ValueError(f"Wizard session {session_id} already has a submitted combo attempt")
    raise ValueError(f"Wizard session {session_id} cannot submit from state {state}")


def _release_submit_claim(session_id: str, attempt_id: str) -> None:
    con = _connect()
    try:
        con.execute(
            """
            UPDATE wizard_sessions
               SET state='planned', current_attempt_id=NULL, updated_at=?
             WHERE session_id=?
               AND current_attempt_id=?
               AND UPPER(state)='SUBMITTING'
            """,
            [_now(), session_id, attempt_id],
        )
    finally:
        con.close()


def _order_store_modify_sequence(current: dict[str, Any]) -> int:
    ib_order_id = str(current.get("ib_order_id") or "")
    perm_id = str(current.get("perm_id") or "")
    if not ib_order_id and not perm_id:
        return 0

    con = _connect()
    try:
        row = con.execute(
            """
            SELECT modify_sequence
              FROM orders_submissions
             WHERE (? != '' AND ib_order_id = ?)
                OR (? != '' AND perm_id = ?)
             ORDER BY updated_at DESC
             LIMIT 1
            """,
            [ib_order_id, ib_order_id, perm_id, perm_id],
        ).fetchone()
    finally:
        con.close()
    return int(row[0] or 0) if row is not None else 0


def _update_attempt_modify_sequence(attempt_id: str, sequence: int) -> None:
    con = _connect()
    try:
        con.execute(
            """
            UPDATE wizard_combo_attempts
               SET modify_sequence=?, updated_at=?
             WHERE attempt_id=?
            """,
            [sequence, _now(), attempt_id],
        )
    finally:
        con.close()


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
        quotes={
            contract_id: ComboLegQuote.model_validate(quote)
            for contract_id, quote in quotes.items()
        },
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
    con = _connect()
    try:
        con.execute(
            """
            INSERT INTO wizard_combo_attempts (
                attempt_id, session_id, client_attempt_id, ib_order_id, perm_id,
                intent, target_price, price_basis, ladder_step, submitted_at,
                terminal_state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                attempt_id,
                session_id,
                client_attempt_id,
                str(result.get("orderId") or ""),
                str(result.get("permId") or ""),
                session["intent"],
                str(Decimal(str(target_price or payload.get("limitPrice") or "0"))),
                str(request.get("price_basis") or "CUSTOM"),
                None,
                now,
                "WORKING",
                now,
                now,
            ],
        )
        row = con.execute(
            """
            UPDATE wizard_sessions
               SET state = 'working', updated_at = ?
             WHERE session_id = ?
               AND current_attempt_id = ?
               AND UPPER(state) = 'SUBMITTING'
             RETURNING state
            """,
            [now, session_id, attempt_id],
        ).fetchone()
    finally:
        con.close()

    if row is None:
        raise ValueError(f"Wizard session {session_id} submit finalization lost state claim")

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

    next_sequence = max(
        int(current.get("modify_sequence") or 0),
        _order_store_modify_sequence(current),
    ) + 1
    con = _connect()
    try:
        con.execute(
            """
            UPDATE wizard_combo_attempts
               SET modify_sequence=?, target_price=?, updated_at=?
             WHERE attempt_id=?
            """,
            [
                next_sequence,
                str(request["target_price"]),
                _now(),
                current["attempt_id"],
            ],
        )
    finally:
        con.close()
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


def list_sessions() -> list[dict[str, Any]]:
    orders_store.init_store(_db_path())
    con = _connect()
    try:
        rows = con.execute(
            """
            SELECT session_id, ticker, state, structure_name, intent, payload_json,
                   current_attempt_id, created_at, updated_at
              FROM wizard_sessions
             ORDER BY updated_at DESC
             LIMIT 50
            """
        ).fetchall()
    finally:
        con.close()
    return [_session_row_to_dict(row) for row in rows]


def _session_row_to_dict(row: Any) -> dict[str, Any]:
    payload = json.loads(row[5]) if row[5] else {}
    return {
        "session_id": row[0],
        "ticker": row[1],
        "state": row[2],
        "structure_name": row[3],
        "intent": row[4],
        "payload": payload,
        "current_attempt_id": row[6],
        "created_at": row[7].isoformat() if hasattr(row[7], "isoformat") else row[7],
        "updated_at": row[8].isoformat() if hasattr(row[8], "isoformat") else row[8],
    }


def get_session(session_id: str) -> dict[str, Any]:
    orders_store.init_store(_db_path())
    con = _connect()
    try:
        row = con.execute(
            """
            SELECT session_id, ticker, state, structure_name, intent, payload_json,
                   current_attempt_id, created_at, updated_at
              FROM wizard_sessions
             WHERE session_id=?
            """,
            [session_id],
        ).fetchone()
    finally:
        con.close()
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

    now = _now()
    con = _connect()
    try:
        con.execute(
            """
            UPDATE wizard_sessions
               SET state='ABORTED', updated_at=?
             WHERE session_id=?
               AND UPPER(state)=?
            """,
            [now, session_id, upper],
        )
        con.execute(
            'INSERT INTO wizard_session_events (event_id, session_id, kind, detail, "at") VALUES (?, ?, ?, ?, ?)',
            [str(uuid.uuid4()), session_id, "ABORTED", json.dumps({}), now],
        )
    finally:
        con.close()
    return get_session(session_id)
