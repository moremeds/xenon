from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from xenon.api.guards import require_mode_verified
from xenon.execution.combo_wizard import session as combo_session

router = APIRouter()


class PlanRequest(BaseModel):
    ticker: str
    intent: str
    legs: list[dict[str, Any]]
    quotes: dict[str, dict[str, Any]]
    order_payload: dict[str, Any]


class SubmitRequest(BaseModel):
    target_price: Decimal | None = None
    price_basis: str | None = None


class RepriceRequest(BaseModel):
    target_price: Decimal


class ProtectRequest(BaseModel):
    tp_target_price: Decimal
    alert_net_mid_threshold: Decimal
    polarity: str = "DEBIT"


@router.post("/wizard/plan")
async def wizard_plan(body: PlanRequest) -> dict[str, Any]:
    try:
        return combo_session.plan_session(
            ticker=body.ticker,
            intent=body.intent,
            legs=body.legs,
            quotes=body.quotes,
            order_payload=body.order_payload,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/wizard/sessions")
async def wizard_sessions() -> dict[str, Any]:
    return {"sessions": combo_session.list_sessions()}


@router.post("/wizard/sessions")
async def wizard_create_session(body: PlanRequest) -> dict[str, Any]:
    return await wizard_plan(body)


@router.get("/wizard/sessions/{session_id}")
async def wizard_get_session(session_id: str) -> dict[str, Any]:
    try:
        return combo_session.get_session(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/wizard/stream")
async def wizard_stream(session_id: str = Query(...)) -> Response:
    try:
        session = combo_session.get_session(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    payload = json.dumps(session, default=str, separators=(",", ":"))
    return Response(
        content=f"event: session\ndata: {payload}\n\n",
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@router.post(
    "/wizard/sessions/{session_id}/submit",
    dependencies=[Depends(require_mode_verified)],
)
async def wizard_submit(session_id: str, body: SubmitRequest) -> dict[str, Any]:
    try:
        return await combo_session.submit_combo(session_id, body.model_dump(mode="json", exclude_none=True))
    except ValueError as exc:
        status = 404 if "Unknown wizard session" in str(exc) else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.post(
    "/wizard/sessions/{session_id}/reprice",
    dependencies=[Depends(require_mode_verified)],
)
async def wizard_reprice(session_id: str, body: RepriceRequest) -> dict[str, Any]:
    try:
        return await combo_session.reprice_combo(session_id, body.model_dump(mode="json"))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/wizard/sessions/{session_id}/abort",
    dependencies=[Depends(require_mode_verified)],
)
async def wizard_abort(session_id: str) -> dict[str, Any]:
    try:
        return await combo_session.abort_session(session_id)
    except ValueError as exc:
        status = 404 if "Unknown wizard session" in str(exc) else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.post(
    "/wizard/sessions/{session_id}/protect",
    dependencies=[Depends(require_mode_verified)],
)
async def wizard_protect(session_id: str, body: ProtectRequest) -> dict[str, Any]:
    try:
        session = combo_session.get_session(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    state = str(session.get("state") or "")
    upper = state.upper()
    if upper == "PROTECTED":
        return {
            "state": "PROTECTED",
            "noop": True,
            "tp_attached": True,
            "alert_armed": True,
            "attempts": 0,
        }
    if upper != "FILLED":
        raise HTTPException(
            status_code=409,
            detail=f"Wizard session {session_id} cannot protect from state {state}",
        )

    from xenon.api import server as server_mod
    from xenon.execution.combo_wizard.ib_adapter import ComboWizardIbAdapter
    from xenon.execution.combo_wizard.protect import attach_protection

    if server_mod.ib_pool is None or not server_mod.ib_pool.is_connected("orders"):
        raise HTTPException(status_code=503, detail="IB orders pool is not connected")

    async with server_mod.ib_pool.acquire("orders") as ib_client:
        adapter = ComboWizardIbAdapter(ib_client)
        try:
            return await server_mod.ib_pool.run_sync(
                "orders",
                attach_protection,
                session_id,
                ib=adapter,
                tp_target_price=body.tp_target_price,
                alert_net_mid_threshold=body.alert_net_mid_threshold,
                polarity=body.polarity,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
