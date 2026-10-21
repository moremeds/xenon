"""Order-entry HTTP routes: /orders/quote, /orders/place, /orders/cancel, /orders/replace, /orders/modify.

Thin handlers: read-only refusal, body decode, then the core in services/order_entry.py.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request

from xenon.api.guards import is_read_only, read_only_403
from xenon.api.services import order_entry
from xenon.execution import quote_tokens
from xenon.execution.quote_tokens import QuotePayload

router = APIRouter()


# ---------------------------------------------------------------------------
# Phase 3: IB order operations — HTTP handlers (cores: services/order_entry.py)
# ---------------------------------------------------------------------------


@router.get("/orders/quote")
async def orders_quote(ticker: str, con_id: int):
    secret = os.environ.get("XENON_QUOTE_TOKEN_SECRET")
    if not secret:
        raise HTTPException(status_code=500, detail="quote secret not configured")
    snap = await order_entry._fetch_quote_snapshot(ticker, con_id)
    import time as _time

    payload = QuotePayload(
        con_id=con_id,
        ticker=ticker.upper(),
        bid=snap["bid"],
        ask=snap["ask"],
        bid_size=snap["bid_size"],
        ask_size=snap["ask_size"],
        ts_server_ms=int(_time.time() * 1000),
    )
    token = quote_tokens.mint(payload, secret)
    return {
        "token": token,
        "bid": str(payload.bid),
        "ask": str(payload.ask),
        "bid_size": payload.bid_size,
        "ask_size": payload.ask_size,
        "ts_server_ms": payload.ts_server_ms,
    }


@router.post("/orders/place")
async def orders_place(request: Request):
    """Place an order via IB (on-demand connection, client_id=26)."""
    if is_read_only():
        return read_only_403()
    body = await request.json()
    return await order_entry.place(body)


@router.post("/orders/cancel")
async def orders_cancel(request: Request):
    """Cancel an open order through the single order owner (PR-10).

    IB scopes cancelOrder by clientId, so only orders placed by this API's
    ``orders`` connection are cancelled here; any other placing clientId →
    409 ORDER_NOT_OWNED (cancel it in TWS / IBKR Mobile). Reply mapping:
      classification=connection          → 503 IB_CONNECTION
      classification=broker_disconnected → 503 IB_BROKER_DISCONNECTED
      classification=orders_halted       → 503 ORDERS_HALTED
      classification=ownership           → 409 ORDER_NOT_OWNED
      classification=ib_reject           → 400 (or 404 for 10147/10148) IB_REJECT
    The full upstream payload (code + message) is preserved in detail.
    """
    if is_read_only():
        return read_only_403()
    body = await request.json()
    return await order_entry.cancel(body)


@router.post("/orders/replace")
async def orders_replace(request: Request):
    """Combo replacement: claim → cancel the original(s) → place the successor,
    owned by ONE process (PR-07 fix3, OP-1). IB has no atomic restructure of
    a BAG's legs, so cancel+place is unavoidable; the claim makes it safe:
    a second replacement of the same target is refused before any cancel."""
    if is_read_only():
        return read_only_403()
    body = await request.json()
    return await order_entry.replace(body)


@router.post("/orders/modify")
async def orders_modify(request: Request):
    """Modify an open order through the single order owner (PR-10).

    Only orders placed by this API's ``orders`` connection are modified
    (409 ORDER_NOT_OWNED otherwise). The worker settles the Gate 4 amend.

    F5.4: modifySequence monotonic gate runs BEFORE the owner is called.
      - missing modifySequence → 400 MODIFY_SEQUENCE_REQUIRED
      - unknown ib_order_id     → 404 ORDER_NOT_FOUND
      - stale sequence          → 409 MODIFY_STALE (detail.applied=<current>)
    Owner replies are classified the same as cancel.
    """
    if is_read_only():
        return read_only_403()
    body = await request.json()
    return await order_entry.modify(body)
