"""GET /regime + GET /regime/overrides — read-only regime surface.

Both routes scope by the request's resolved AccountScope. The override
listing is the audit trail for `regime_overrides` rows — Phase 3 will
INSERT into this table from the order route; Phase 2 only exposes the
listing so the UI can render audit history before the gate is wired.
"""

from __future__ import annotations

from dataclasses import asdict

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Response

from xenon.api.guards import get_account_scope
from xenon.api.services.regime_state import RegimeState, get_regime_state
from xenon.db.engine import get_engine
from xenon.db.schema import regime_overrides
from xenon.execution.account_scope import AccountScope

router = APIRouter()


@router.get("/regime/state")
async def get_regime(
    response: Response,
    state: RegimeState = Depends(get_regime_state),
):
    response.headers["Cache-Control"] = "private, max-age=30"
    payload = asdict(state)
    payload.pop("raw", None)
    if payload.get("vcg_scanned_at") is not None:
        payload["vcg_scanned_at"] = payload["vcg_scanned_at"].isoformat()
    if payload.get("cri_scanned_at") is not None:
        payload["cri_scanned_at"] = payload["cri_scanned_at"].isoformat()
    return payload


@router.get("/regime/overrides")
async def list_regime_overrides(
    limit: int = Query(50, ge=1, le=200),
    scope: AccountScope = Depends(get_account_scope),
):
    # Codex-review CODEX-4: filter on the full AccountScope tuple including
    # broker. The audit table stores the broker column lowercase ("ib"/"futu");
    # AccountScope.broker is uppercase ("IB"/"FUTU"). Match on lowercase to
    # align with the existing insert convention.
    engine = get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(
            sa.select(regime_overrides)
            .where(
                regime_overrides.c.broker == scope.broker.lower(),
                regime_overrides.c.account_env == scope.account_env,
                regime_overrides.c.broker_account == scope.broker_account,
            )
            .order_by(regime_overrides.c.ts.desc())
            .limit(limit)
        )
        rows = result.mappings().all()

    items = []
    for row in rows:
        item = dict(row)
        ts = item.get("ts")
        if ts is not None:
            item["ts"] = ts.isoformat()
        items.append(item)
    return {"items": items, "limit": limit}
