"""GET /performance — broker-aware, scope-keyed, market-aware-TTL cached.

Spec: docs/superpowers/specs/2026-05-31-performance-rebuild-design.md (v3.1)
Service: xenon.api.services.performance.compute (via perf_cache.cached_compute)
Dep: xenon.api.guards.get_performance_scope (broker-aware)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from xenon.api.guards import get_performance_scope
from xenon.api.services.futu_nav_persistence import NavAccountEnvConflict
from xenon.api.services.perf_cache import cached_compute
from xenon.api.services.performance_periods import (
    SUPPORTED_PERIODS,
    InvalidPeriodError,
)
from xenon.execution.account_scope import AccountScope

router = APIRouter()


@router.get("/performance")
async def get_performance(
    request: Request,
    scope: AccountScope = Depends(get_performance_scope),
    period: str = Query(
        "YTD",
        description=f"Window. One of {SUPPORTED_PERIODS} (case-insensitive).",
    ),
):
    """Return the performance payload for the resolved broker scope + window."""
    engine = getattr(request.app.state, "db_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="db engine not initialized")
    ib_pool = getattr(request.app.state, "ib_pool", None)
    try:
        return await cached_compute(engine, scope, ib_pool=ib_pool, period=period)
    except InvalidPeriodError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except NavAccountEnvConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
