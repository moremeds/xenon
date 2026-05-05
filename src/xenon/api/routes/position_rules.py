"""FastAPI routes for /position-rules."""
from __future__ import annotations

import json
import os
import subprocess
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from xenon.api.guards import get_account_scope
from xenon.api.services.position_rules_cancel import cancel_protection
from xenon.api.services.position_rules_health import compute_health
from xenon.db.engine import get_sync_engine
from xenon.db.queries.position_protection import list_active_rows
from xenon.execution.account_scope import AccountScope

router = APIRouter(prefix="/position-rules", tags=["position-rules"])


def _json_safe_rows(rows: list[dict]) -> list[dict]:
    for row in rows:
        for key, value in list(row.items()):
            if hasattr(value, "isoformat"):
                row[key] = value.isoformat()
    return rows


def _live_auth_error(request: Request, scope: AccountScope) -> JSONResponse | None:
    if scope.account_env != "live":
        return None
    if not (os.environ.get("CLERK_JWKS_URL") and os.environ.get("CLERK_ISSUER")):
        return JSONResponse(
            status_code=503,
            content={
                "reason_code": "live_trading_auth_unconfigured",
                "message": "Clerk auth must be configured for live mutating endpoints.",
            },
        )
    if getattr(request.state, "user", None) is None:
        return JSONResponse(
            status_code=401,
            content={
                "reason_code": "live_trading_auth_unauthenticated",
                "message": "Authenticated user required for live mode.",
            },
        )
    return None


@router.get("")
@router.get("/")
def list_rules(scope: Annotated[AccountScope, Depends(get_account_scope)]):
    rows = list_active_rows(
        get_sync_engine(),
        broker=scope.broker,
        account_env=scope.account_env,
        broker_account=scope.broker_account,
    )
    return _json_safe_rows(rows)


@router.get("/health")
def health(scope: Annotated[AccountScope, Depends(get_account_scope)]):
    return compute_health(engine=get_sync_engine(), scope=scope)


@router.post("/{protection_id}/cancel")
def cancel(
    protection_id: int,
    request: Request,
    scope: Annotated[AccountScope, Depends(get_account_scope)],
):
    auth_error = _live_auth_error(request, scope)
    if auth_error is not None:
        return auth_error

    engine = get_sync_engine()
    try:
        result = cancel_protection(
            engine,
            scope=scope,
            protection_id=protection_id,
            reason="operator_cancel_api",
        )
    except RuntimeError as exc:
        return JSONResponse(
            status_code=500,
            content={"reason_code": "broker_cancel_failed", "message": str(exc)},
        )
    if result.status == "not_found":
        raise HTTPException(status_code=404, detail="protection not found")
    if result.status == "already_terminal":
        return JSONResponse(
            status_code=409,
            content={"reason_code": "already_terminal", "state": result.row["state"] if result.row else None},
        )
    if result.status == "concurrent_state_change":
        return JSONResponse(status_code=409, content={"reason_code": "concurrent_state_change"})
    return {"protection_id": protection_id, "state": "CANCELED"}


@router.post("/sweep")
async def sweep(request: Request, scope: Annotated[AccountScope, Depends(get_account_scope)]):
    body = await request.json() if request.headers.get("content-length") not in (None, "0") else {}
    apply_mode = bool(body.get("apply"))
    if apply_mode:
        auth_error = _live_auth_error(request, scope)
        if auth_error is not None:
            return auth_error

    cmd = ["xenon-position-rules", "sweep"]
    if apply_mode:
        cmd.append("--apply")
    env = {
        **os.environ,
        "XENON_BROKER": scope.broker,
        "XENON_TRADING_MODE": scope.account_env,
        "XENON_BROKER_ACCOUNT": scope.broker_account,
    }
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
    if result.returncode != 0:
        return JSONResponse(status_code=500, content={"reason_code": "sweep_failed", "stderr": result.stderr})
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return JSONResponse(
            status_code=500,
            content={"reason_code": "sweep_unparseable", "stdout": result.stdout[:500]},
        )
