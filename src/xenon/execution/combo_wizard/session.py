from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from xenon.execution import orders_store
from xenon.execution.combo_wizard import planner as combo_planner
from xenon.execution.combo_wizard.models import ComboLegQuote, ComboLegSpec


def _db_path() -> Path:
    return orders_store._resolve_path(None)


def _connect():
    return orders_store._connect_utc(_db_path())


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
            SELECT attempt_id, ib_order_id, perm_id
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
    return {"attempt_id": row[0], "ib_order_id": row[1], "perm_id": row[2]}


def plan_session(
    *,
    ticker: str,
    intent: str,
    legs: list[dict[str, Any]],
    quotes: dict[str, dict[str, Any]],
    order_payload: dict[str, Any],
) -> dict[str, Any]:
    plan = combo_planner.build_plan(
        ticker=ticker,
        legs=[ComboLegSpec.model_validate(leg) for leg in legs],
        quotes={
            contract_id: ComboLegQuote.model_validate(quote)
            for contract_id, quote in quotes.items()
        },
    )
    session = create_session(
        ticker=ticker,
        intent=intent,
        structure_name=plan.structure_name,
        payload=order_payload,
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
    session = _load_session(session_id)
    attempt_id = uuid.uuid4().hex
    client_attempt_id = f"wiz:{session_id}:combo:{attempt_id}"

    payload = dict(session["payload"])
    target_price = request.get("target_price")
    if target_price is not None:
        payload["limitPrice"] = str(target_price)
    payload["client_attempt_id"] = client_attempt_id

    from xenon.api import server as server_mod

    result = await server_mod._orders_place_from_body(payload)

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
        con.execute(
            """
            UPDATE wizard_sessions
               SET state = 'working', current_attempt_id = ?, updated_at = ?
             WHERE session_id = ?
            """,
            [attempt_id, now, session_id],
        )
    finally:
        con.close()

    return {
        **result,
        "attempt_id": attempt_id,
        "client_attempt_id": client_attempt_id,
    }


async def reprice_combo(session_id: str, request: dict[str, Any]) -> dict[str, Any]:
    current = _load_current_attempt(session_id)
    from xenon.api import server as server_mod

    return await server_mod._orders_modify_from_body(
        {
            "orderId": int(current["ib_order_id"] or 0),
            "permId": int(current["perm_id"] or 0),
            "newPrice": str(request["target_price"]),
            "modifySequence": 1,
        }
    )
