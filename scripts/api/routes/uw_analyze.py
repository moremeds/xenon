"""POST /uw-analyze — per-ticker UW signal analysis for the web UI.

Wraps `scripts.uw_analyze.run_analysis_with_data` and exposes both the
serialized AnalysisReport and a UI-facing display slice extracted from
TickerData (walls, gamma, IV rank, net premium, gex_by_strike rows).

Auth: covered globally by Clerk middleware in scripts/api/server.py.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from analysis.models import TickerData
from clients.uw_client import UWAPIError, UWNotFoundError
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from uw_analyze import run_analysis_with_data

logger = logging.getLogger("xenon.uw_analyze")
router = APIRouter()

# 30s in-process TTL cache to absorb double-clicks / rate-limit risk.
_CACHE_TTL_SECONDS = 30.0
_cache: dict[str, tuple[float, "UwAnalyzeResponse"]] = {}


# ── Request / response models ─────────────────────────────────────────────


class UwAnalyzeRequest(BaseModel):
    ticker: str


class GexStrikeRow(BaseModel):
    strike: float
    call_gamma: Optional[float] = None  # $M
    put_gamma: Optional[float] = None
    net_gamma: Optional[float] = None
    distance_pct: Optional[float] = None
    is_call_wall: bool = False
    is_put_wall: bool = False


class UwAnalyzeDisplay(BaseModel):
    """UI-facing slice extracted from TickerData. All Optional."""

    sector: Optional[str] = None
    iv_rank: Optional[float] = None
    iv: Optional[float] = None
    rv: Optional[float] = None
    call_wall_strike: Optional[float] = None
    put_wall_strike: Optional[float] = None
    gamma_per_1pct: Optional[float] = None
    net_call_premium: Optional[float] = None
    net_put_premium: Optional[float] = None
    short_volume_ratio: Optional[float] = None
    short_volume_trend: Optional[list[float]] = None
    term_structure_label: Optional[Literal["normal", "inverted"]] = None
    gex_flip: Optional[float] = None
    gex_by_strike: Optional[list[GexStrikeRow]] = None
    max_pain: Optional[float] = None


class UwAnalyzeResponse(BaseModel):
    report: dict[str, Any]
    display: UwAnalyzeDisplay
    generated_at: str


# ── Mappers ───────────────────────────────────────────────────────────────


def _coerce_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _build_gex_rows(td: TickerData) -> Optional[list[GexStrikeRow]]:
    raw = td.gex_by_strike
    if not raw or not isinstance(raw, dict):
        return None

    # ticker_data.py normalizes to: {"strikes": [{strike, gamma, call_gamma, put_gamma}, ...]}
    # Also tolerate the legacy/test shape: {strike_key: {call_gamma, put_gamma}}.
    raw_rows: list[dict] = []
    strikes_list = raw.get("strikes") if isinstance(raw, dict) else None
    if isinstance(strikes_list, list):
        for r in strikes_list:
            if isinstance(r, dict):
                raw_rows.append(r)
    else:
        for strike_key, payload in raw.items():
            if not isinstance(payload, dict):
                continue
            strike = _coerce_float(strike_key)
            if strike is None:
                continue
            raw_rows.append({"strike": strike, **payload})

    rows: list[GexStrikeRow] = []
    spot = td.price
    cw = td.call_wall_strike
    pw = td.put_wall_strike

    for r in raw_rows:
        strike = _coerce_float(r.get("strike"))
        if strike is None:
            continue
        call_g = _coerce_float(
            r.get("call_gamma") if r.get("call_gamma") is not None else r.get("call_gex") or r.get("calls")
        )
        put_g = _coerce_float(
            r.get("put_gamma") if r.get("put_gamma") is not None else r.get("put_gex") or r.get("puts")
        )
        net_g = _coerce_float(r.get("gamma"))
        if net_g is None:
            net_g = _coerce_float(r.get("net") or r.get("net_gamma"))
        if net_g is None and (call_g is not None or put_g is not None):
            net_g = (call_g or 0.0) + (put_g or 0.0)

        dist_pct: Optional[float] = None
        if spot:
            try:
                dist_pct = (strike - spot) / spot
            except ZeroDivisionError:
                dist_pct = None

        rows.append(
            GexStrikeRow(
                strike=strike,
                call_gamma=call_g,
                put_gamma=put_g,
                net_gamma=net_g,
                distance_pct=dist_pct,
                is_call_wall=(cw is not None and strike == cw),
                is_put_wall=(pw is not None and strike == pw),
            )
        )

    rows.sort(key=lambda r: r.strike, reverse=True)

    # Trim to ±10 strikes around spot when possible.
    if spot is not None and len(rows) > 21:
        # Find index closest to spot.
        idx = min(range(len(rows)), key=lambda i: abs(rows[i].strike - spot))
        lo = max(0, idx - 10)
        hi = min(len(rows), idx + 11)
        rows = rows[lo:hi]

    return rows or None


def _td_to_display(td: TickerData) -> UwAnalyzeDisplay:
    # Term structure label: prefer VRP-derived, fall back from raw term_structure.
    term_label: Optional[Literal["normal", "inverted"]] = None
    ts = td.term_structure
    if isinstance(ts, list) and len(ts) >= 1:
        try:
            front = float(ts[0].get("iv"))
            back = float(ts[-1].get("iv"))
            term_label = "inverted" if front > back else "normal"
        except (TypeError, ValueError, AttributeError):
            term_label = None

    gex_flip: Optional[float] = None
    if isinstance(td.gex, dict):
        gex_flip = _coerce_float(td.gex.get("flip") or td.gex.get("gex_flip"))

    return UwAnalyzeDisplay(
        sector=td.sector,
        iv_rank=td.iv_rank,
        iv=td.iv,
        rv=td.rv,
        call_wall_strike=td.call_wall_strike,
        put_wall_strike=td.put_wall_strike,
        gamma_per_1pct=td.gamma_per_1pct,
        net_call_premium=td.net_call_premium,
        net_put_premium=td.net_put_premium,
        short_volume_ratio=td.short_volume_ratio,
        short_volume_trend=list(td.short_volume_trend) if td.short_volume_trend else None,
        term_structure_label=term_label,
        gex_flip=gex_flip,
        gex_by_strike=_build_gex_rows(td),
        max_pain=td.max_pain,
    )


def _serialize_report(report) -> dict[str, Any]:
    """asdict() with datetime/date stringification."""

    def _coerce(o: Any) -> Any:
        if isinstance(o, dict):
            return {k: _coerce(v) for k, v in o.items()}
        if isinstance(o, list):
            return [_coerce(x) for x in o]
        # datetime / date
        if hasattr(o, "isoformat"):
            return o.isoformat()
        return o

    return _coerce(asdict(report))


# ── Route ────────────────────────────────────────────────────────────────


@router.post("/uw-analyze", response_model=UwAnalyzeResponse)
async def uw_analyze(req: UwAnalyzeRequest) -> UwAnalyzeResponse:
    raw_ticker = (req.ticker or "").strip().upper()
    if not raw_ticker or not raw_ticker.replace(".", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="invalid ticker")

    now = time.monotonic()
    cached = _cache.get(raw_ticker)
    if cached and (now - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    try:
        report, td = await asyncio.wait_for(
            asyncio.to_thread(run_analysis_with_data, raw_ticker),
            timeout=60.0,
        )
    except UWNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"ticker not found: {raw_ticker}") from exc
    except UWAPIError as exc:
        logger.warning("uw-analyze upstream error for %s: %s", raw_ticker, exc)
        raise HTTPException(status_code=502, detail="UW upstream failed") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="analysis timed out") from exc

    response = UwAnalyzeResponse(
        report=_serialize_report(report),
        display=_td_to_display(td),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    _cache[raw_ticker] = (now, response)
    return response
