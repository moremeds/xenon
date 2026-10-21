"""Market-data routes: option chain / expirations / greeks and market depth (IB subprocess CLIs)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from xenon.api.subprocess import run_entry_point
from xenon.clients.ib_client import DEFAULT_GATEWAY_PORT

router = APIRouter()


@router.get("/options/chain")
async def options_chain(symbol: str, expiry: Optional[str] = None):
    """Fetch options chain for a symbol."""
    from xenon.api import server

    # Pass the resolved gateway port (4002 paper / 4001 live) — the CLI
    # otherwise defaults to 4001, which fails in paper mode. Mirrors the
    # sync routes (see /sync, /orders/sync).
    args = ["--symbol", symbol.upper(), "--port", str(DEFAULT_GATEWAY_PORT)]
    if expiry:
        args.extend(["--expiry", expiry])
    result = await server._run_ib_script_with_recovery("xenon-ib-option-chain", args, timeout=15)
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    if result.data and result.data.get("error"):
        raise HTTPException(status_code=502, detail=result.data["error"])
    return result.data


@router.get("/options/expirations")
async def options_expirations(symbol: str):
    """List option expirations for a symbol."""
    result = await run_entry_point(
        "xenon-ib-option-chain",
        ["--symbol", symbol.upper(), "--port", str(DEFAULT_GATEWAY_PORT)],
        timeout=15,
    )
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    if result.data and result.data.get("error"):
        raise HTTPException(status_code=502, detail=result.data["error"])
    return {"symbol": result.data.get("symbol"), "expirations": result.data.get("expirations")}


@router.get("/market-depth")
async def market_depth(
    symbol: str,
    expiry: Optional[str] = None,
    strike: Optional[float] = None,
    right: Optional[str] = None,
    num_rows: int = Query(10, ge=1, le=20),
):
    """Point-in-time L2 order-book snapshot (subprocess; mirrors /options/chain).

    `symbol` alone fetches stock/index depth; pass the full option triplet
    (`expiry` + `strike` + `right`) for option depth. Returns the qualified
    `conId`. A no-entitlement contract is a 200 with ``entitled: false`` — not
    an error. ``entitled`` reflects the permission axis only; an empty book with
    no permission failure stays ``entitled: true`` with a ``note``.
    """
    from xenon.api import server

    # Normalize blank query strings to absent: an empty option field (e.g.
    # ?right=) is "omitted", not "present-but-invalid". Keeps the all-or-none
    # guard and the arg builder on the same presence predicate, so a blank field
    # yields the documented 422 instead of leaking through to a subprocess 502.
    expiry = (expiry or "").strip() or None
    right = (right or "").strip() or None

    # All-or-none option tuple — never silently degrade an option request to
    # stock depth on a partial tuple.
    opt = [expiry, strike, right]
    if any(v is not None for v in opt) and not all(v is not None for v in opt):
        raise HTTPException(status_code=422, detail="provide all of expiry/strike/right, or none")

    args = ["--symbol", symbol.upper(), "--port", str(DEFAULT_GATEWAY_PORT), "--num-rows", str(num_rows)]
    if expiry:
        args += ["--expiry", expiry]
    if strike is not None:
        args += ["--strike", str(strike)]
    if right:
        args += ["--right", right.upper()]
    result = await server._run_ib_script_with_recovery("xenon-ib-market-depth", args, timeout=15)
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    if result.data and result.data.get("error"):
        raise HTTPException(status_code=502, detail=result.data["error"])
    return result.data


@router.get("/options/greeks")
async def options_greeks(
    symbol: str,
    expiry: str,
    strike: float,
    right: str,
):
    """Broker-computed option greeks (IB modelGreeks) for one contract (subprocess).

    Greeks are option-only — the full triplet (``expiry`` + ``strike`` + ``right``)
    is required; FastAPI 422s a missing leg before any subprocess spawns. Returns
    the qualified ``conId``, bid/ask, and greeks (impliedVol/delta/gamma/vega/
    theta/undPrice). When IB delivers no greeks (illiquid contract, market closed)
    ``greeks`` is null with a ``note`` — still a 200, not an error.
    """
    from xenon.api import server

    expiry = (expiry or "").strip()
    right = (right or "").strip().upper()
    if not expiry:
        raise HTTPException(status_code=422, detail="expiry is required (YYYYMMDD)")
    if right not in ("C", "P"):
        raise HTTPException(status_code=422, detail="right must be C or P")

    args = [
        "--symbol",
        symbol.upper(),
        "--expiry",
        expiry,
        "--strike",
        str(strike),
        "--right",
        right,
        "--port",
        str(DEFAULT_GATEWAY_PORT),
    ]
    result = await server._run_ib_script_with_recovery("xenon-ib-option-greeks", args, timeout=20)
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    if result.data and result.data.get("error"):
        raise HTTPException(status_code=502, detail=result.data["error"])
    return result.data
