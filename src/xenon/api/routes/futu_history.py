"""FastAPI router for Futu history surfaces.

Per src/xenon/api/CLAUDE.md: routers stay thin, services own the logic.
Every route is scope-gated via xenon.api.guards.get_futu_account_scope
(C29) — get_account_scope reads the IB scope by default, so /futu/*
routes would return empty data in production without this override.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from xenon.api.guards import get_futu_account_scope
from xenon.api.services.futu_history import (
    MIN_HISTORICAL_DATE,
    SinceOutOfRange,
    SinceUnparseable,
    compute_performance_stats,
    futu_history_sync,
    get_equity_curve,
    get_reconciliation_summary,
    list_trades,
    parse_since,
)
from xenon.execution.account_scope import AccountScope
from xenon.utils.futu_api_stats import FutuApiStats

router = APIRouter(tags=["futu_history"])


class SyncBody(BaseModel):
    since: str = "auto"
    rematch: bool = False


def _validate_date(s: str, name: str) -> date:
    """C33: include the upper bound and emit a structured error envelope."""
    try:
        d = date.fromisoformat(s)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"reason_code": "date_unparseable", "param": name, "value": s},
        ) from exc
    today_et = datetime.now(ZoneInfo("America/New_York")).date()
    if d < MIN_HISTORICAL_DATE or d > today_et:
        raise HTTPException(
            status_code=422,
            detail={
                "reason_code": "date_out_of_range",
                "param": name,
                "value": s,
                "min_date": MIN_HISTORICAL_DATE.isoformat(),
                "max_date": today_et.isoformat(),
            },
        )
    return d


def _validate_range(start: date, end: date) -> None:
    """C33: reject from > to (was silently empty before)."""
    if end < start:
        raise HTTPException(
            status_code=422,
            detail={
                "reason_code": "date_range_inverted",
                "from": start.isoformat(),
                "to": end.isoformat(),
            },
        )


@router.post("/futu/history/sync")
def post_sync(
    body: SyncBody,
    scope: AccountScope = Depends(get_futu_account_scope),
) -> dict[str, Any]:
    try:
        since = parse_since(body.since)
    except SinceUnparseable as exc:
        raise HTTPException(
            status_code=422,
            detail={"reason_code": exc.reason_code, "bad_value": exc.bad_value},
        ) from exc
    except SinceOutOfRange as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "reason_code": exc.reason_code,
                "bad_value": exc.bad_value,
                "min_date": MIN_HISTORICAL_DATE.isoformat(),
            },
        ) from exc
    return futu_history_sync(scope=scope, since=since, rematch=body.rematch)


@router.get("/futu-stats")
def get_futu_stats() -> dict:
    """C11: observability sidebar route. Mirrors /uw-stats — no auth dep."""
    return FutuApiStats.instance().snapshot()


@router.get("/futu/equity-curve")
def get_equity_curve_route(
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
    scope: AccountScope = Depends(get_futu_account_scope),
) -> list[dict]:
    start = _validate_date(from_, "from")
    end = _validate_date(to, "to")
    _validate_range(start, end)
    return get_equity_curve(scope, start, end)


@router.get("/futu/reconciliation")
def get_reconciliation_route(
    scope: AccountScope = Depends(get_futu_account_scope),
) -> dict:
    return get_reconciliation_summary(scope)


@router.get("/futu/trades")
def get_trades_route(
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
    ticker: str | None = None,
    action: str | None = None,
    sort: str = "filled_at",
    direction: str = "desc",
    limit: int = 50,
    cursor: str | None = None,
    scope: AccountScope = Depends(get_futu_account_scope),
) -> dict:
    start = _validate_date(from_, "from")
    end = _validate_date(to, "to")
    _validate_range(start, end)
    if sort not in {"filled_at", "realized_pnl", "ticker", "fees"}:
        raise HTTPException(
            status_code=422,
            detail={"reason_code": "sort_invalid", "value": sort},
        )
    return list_trades(
        scope,
        start,
        end,
        ticker=ticker,
        action=action,
        sort=sort,
        direction=direction,
        limit=limit,
        cursor=cursor,
    )


@router.get("/futu/performance")
def get_performance_route(
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
    scope: AccountScope = Depends(get_futu_account_scope),
) -> dict:
    start = _validate_date(from_, "from")
    end = _validate_date(to, "to")
    _validate_range(start, end)
    return compute_performance_stats(scope, start, end)
