"""Tail-risk guard state and acks (spec 2026-11-08 §5.5).

``GET /risk/state`` — live guard state plus recent ``risk_events``.
``POST /risk/incidents/{key}/ack`` — ack an open incident. The ack is written
to ``risk_events`` first; with no DB write the incident keeps alerting (503).
Both need the internal token (no API key lists ``/risk``).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from xenon.api.guards import is_read_only, read_only_403

logger = logging.getLogger(__name__)

router = APIRouter(tags=["risk"])


def _guard():
    from xenon.api import server

    return getattr(server.app.state, "risk_guard", None)


def _refusal(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"reason_code": code, "detail": detail})


@router.get("/risk/state")
async def risk_state():
    from xenon.api import server
    from xenon.api.services.risk_guard.app import resolve_scope
    from xenon.db.engine import get_engine
    from xenon.db.queries.risk_guard import recent_events

    guard = _guard()
    state = guard.state() if guard is not None else {"enabled": False, "running": False}
    state["running"] = guard is not None
    events: list = []
    scope = resolve_scope(server.app.state)[0]
    if scope is not None:
        try:
            async with asyncio.timeout(5):
                events = await recent_events(get_engine(), scope, limit=20)
        except Exception as exc:  # noqa: BLE001 — the live state is still useful
            logger.warning("risk events read failed: %s", type(exc).__name__)
    return {**state, "events": events}


@router.post("/risk/incidents/{key}/ack")
async def risk_ack(key: str):
    if is_read_only():
        return read_only_403()
    guard = _guard()
    if guard is None:
        return _refusal(409, "RISK_GUARD_OFF", "risk guard is not running")
    result = await guard.ack(key)
    if result == "unknown":
        return _refusal(404, "RISK_INCIDENT_UNKNOWN", f"no open incident {key!r}")
    if result == "unavailable":
        return _refusal(503, "RISK_ACK_UNAVAILABLE", "ack not recorded (database); alerts continue")
    if result == "changed":
        return _refusal(
            409, "RISK_INCIDENT_CHANGED", f"incident {key!r} rose or was replaced during the ack; ack again"
        )
    return {"key": key, "acked": True}
