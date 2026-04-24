from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

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


@router.post("/wizard/plan")
async def wizard_plan(body: PlanRequest) -> dict[str, Any]:
    return combo_session.plan_session(
        ticker=body.ticker,
        intent=body.intent,
        legs=body.legs,
        quotes=body.quotes,
        order_payload=body.order_payload,
    )


@router.post("/wizard/sessions/{session_id}/submit")
async def wizard_submit(session_id: str, body: SubmitRequest) -> dict[str, Any]:
    return await combo_session.submit_combo(session_id, body.model_dump(mode="json", exclude_none=True))


@router.post("/wizard/sessions/{session_id}/reprice")
async def wizard_reprice(session_id: str, body: RepriceRequest) -> dict[str, Any]:
    return await combo_session.reprice_combo(session_id, body.model_dump(mode="json"))
