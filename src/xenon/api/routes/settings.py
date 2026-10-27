"""Runtime settings admin routes: GET / PUT / DELETE /admin/settings.

Internal token only (Next → API via xenonFetch): an `X-API-Key` header is
403 even next to a valid internal token, and dev-open or no credential is
401. Writes are refused under `XENON_READ_ONLY=1` (403 READ_ONLY_MODE) and
append an audit row in the same transaction. Errors carry a top-level
`reason_code`. Registry and rules: `xenon.runtime_settings`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from xenon import runtime_settings
from xenon.api.guards import require_writable

router = APIRouter()

INTERNAL_TOKEN_REQUIRED = "INTERNAL_TOKEN_REQUIRED"
API_KEY_FORBIDDEN = "API_KEY_FORBIDDEN"

_STATUS = {
    runtime_settings.UNKNOWN_KEY: 404,
    runtime_settings.INVALID_VALUE: 400,
    runtime_settings.WIDENS_ENV: 403,
    runtime_settings.CONFIRM_REQUIRED: 428,
}


def _refuse(status: int, reason_code: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, "reason_code": reason_code})


def is_settings_path(path: str) -> bool:
    return path == "/admin/settings" or path.startswith("/admin/settings/")


def auth_refusal(request: Request, identity: dict | None) -> JSONResponse | None:
    """Settings-specific refusal at the auth middleware (before the generic
    401): any X-API-Key header → 403 API_KEY_FORBIDDEN (D7), else anything
    but the internal token → 401 INTERNAL_TOKEN_REQUIRED. Grants nothing."""
    if request.headers.get("X-API-Key"):
        return _refuse(403, API_KEY_FORBIDDEN, "X-API-Key is not accepted on /admin/settings")
    if not (identity or {}).get("internal"):
        return _refuse(401, INTERNAL_TOKEN_REQUIRED, "/admin/settings needs X-Internal-Token")
    return None


def _internal_actor(request: Request) -> tuple[str | None, JSONResponse | None]:
    if request.headers.get("X-API-Key"):
        return None, _refuse(403, API_KEY_FORBIDDEN, "X-API-Key is not accepted on /admin/settings")
    identity = getattr(request.state, "user", None) or {}
    if not identity.get("internal"):
        return None, _refuse(401, INTERNAL_TOKEN_REQUIRED, "/admin/settings needs X-Internal-Token")
    return str(identity.get("sub") or "internal"), None


def _setting_error(exc: runtime_settings.SettingError) -> JSONResponse:
    return _refuse(_STATUS.get(exc.reason_code, 400), exc.reason_code, exc.detail)


@router.get("/admin/settings")
async def get_settings(request: Request):
    _, denial = _internal_actor(request)
    if denial is not None:
        return denial
    rows, live = await asyncio.gather(asyncio.to_thread(runtime_settings.describe), asyncio.to_thread(_relay_live))
    for row in rows:
        if row["owner"] == "relay" and row["key"] in live:
            row["live_value"] = live[row["key"]]
    return {"settings": rows}


def _relay_live() -> dict:
    """The relay's effective values (its /status `runtime_settings.effective`):
    the api cannot see the relay's env. Short timeout; any failure = {} and
    the UI shows no live value. The status token stays server-side."""
    from xenon.api.routes import system

    try:
        status = system._fetch_realtime_status_json(system._resolve_realtime_status_url(), timeout=0.5)
        effective = (status.get("runtime_settings") or {}).get("effective") or {}
        return effective if isinstance(effective, dict) else {}
    except Exception:  # noqa: BLE001 — relay down / 401 / bad body: no live values
        return {}


@router.put("/admin/settings/{key}", dependencies=[Depends(require_writable)])
async def put_setting(key: str, request: Request):
    actor, denial = _internal_actor(request)
    if denial is not None:
        return denial
    try:
        body: Any = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or "value" not in body:
        return _refuse(400, runtime_settings.INVALID_VALUE, "body must be a JSON object with `value`")
    note, confirm = body.get("note"), body.get("confirm")
    if (note is not None and not isinstance(note, str)) or (confirm is not None and not isinstance(confirm, str)):
        return _refuse(400, runtime_settings.INVALID_VALUE, "`note` and `confirm` must be strings")
    try:
        return await asyncio.to_thread(
            runtime_settings.put, key, body["value"], actor=actor, note=note, confirm=confirm
        )
    except runtime_settings.SettingError as exc:
        return _setting_error(exc)


@router.delete("/admin/settings/{key}", dependencies=[Depends(require_writable)])
async def delete_setting(key: str, request: Request, note: str | None = None, confirm: str | None = None):
    actor, denial = _internal_actor(request)
    if denial is not None:
        return denial
    try:
        return await asyncio.to_thread(runtime_settings.delete, key, actor=actor, note=note, confirm=confirm)
    except runtime_settings.SettingError as exc:
        return _setting_error(exc)
