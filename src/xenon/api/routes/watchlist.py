"""Operator-scoped watchlist endpoints backed by Postgres.

xenon is a single-operator terminal: the Next→FastAPI hop is unauthenticated
localhost, so these routes resolve the operator ``user_id`` to the constant
``"local"`` server-side (mirroring the order path at ``server.py:2298``). No
auth dependency, and NOT gated by read-only mode — a watchlist is a user
preference, not order/portfolio data.
"""

import asyncio

from fastapi import APIRouter
from pydantic import BaseModel

from xenon.db.queries import watchlist as watchlist_q

router = APIRouter(tags=["watchlist"])

_OPERATOR_USER_ID = "local"  # single-operator terminal; mirrors order-path user_id


class WatchlistAddBody(BaseModel):
    symbol: str
    sector: str | None = None


@router.get("/watchlist")
async def get_watchlist():
    rows = await asyncio.to_thread(watchlist_q.list_for_user, _OPERATOR_USER_ID)
    return {"watchlist": rows}


@router.post("/watchlist")
async def add_watchlist(body: WatchlistAddBody):
    await asyncio.to_thread(watchlist_q.add, _OPERATOR_USER_ID, body.symbol, body.sector)
    return {"ok": True}


@router.delete("/watchlist/{symbol}")
async def delete_watchlist(symbol: str):
    await asyncio.to_thread(watchlist_q.remove, _OPERATOR_USER_ID, symbol)
    return {"ok": True}
