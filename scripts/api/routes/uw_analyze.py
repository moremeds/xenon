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

from api.services.uw_analyze_cache import UwAnalyzeCache, build_snapshot
from api.services.uw_analyze_candidates import (
    add_adhoc as _candidates_add_adhoc,
)
from api.services.uw_analyze_candidates import (
    seed_candidates,
)
from api.services.uw_analyze_diff import compute_changes
from api.services.uw_analyze_flow_tracker import FlowLog, capture_from_changes

logger = logging.getLogger("xenon.uw_analyze")
router = APIRouter()

# 30s in-process TTL cache to absorb double-clicks / rate-limit risk on the
# legacy single-ticker route. The portfolio routes use UwAnalyzeCache (5/30
# minute TTL with singleflight) instead.
_CACHE_TTL_SECONDS = 30.0
_cache: dict[str, tuple[float, "UwAnalyzeResponse"]] = {}

# Long-lived cache + flow log shared across requests for /portfolio and
# /refresh. Lazily instantiated so test discovery doesn't touch disk.
_portfolio_cache: Optional[UwAnalyzeCache] = None
_flow_log: Optional[FlowLog] = None


def get_portfolio_cache() -> UwAnalyzeCache:
    global _portfolio_cache
    if _portfolio_cache is None:
        _portfolio_cache = UwAnalyzeCache()
    return _portfolio_cache


def get_flow_log() -> FlowLog:
    global _flow_log
    if _flow_log is None:
        _flow_log = FlowLog()
    return _flow_log


def reset_state_for_tests() -> None:
    """Reset module-level singletons. Tests only."""
    global _portfolio_cache, _flow_log, _cache
    _portfolio_cache = None
    _flow_log = None
    _cache = {}


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


# ── Portfolio routes ────────────────────────────────────────────────────────


class RefreshRequest(BaseModel):
    tickers: Optional[list[str]] = None
    adhoc: bool = False


async def _runner(ticker: str) -> tuple[dict, dict, list[dict]]:
    """Adapt scripts.uw_analyze.run_analysis_with_data into the cache's
    `(report_dict, display_dict, flow_alerts)` contract."""
    report, td = await asyncio.wait_for(
        asyncio.to_thread(run_analysis_with_data, ticker),
        timeout=60.0,
    )
    return (
        _serialize_report(report),
        _td_to_display(td).model_dump(),
        list(td.flow_alerts or []),
    )


def _action_items_from(payload: list[dict]) -> list[dict]:
    items: list[dict] = []
    for row in payload:
        ticker = row.get("ticker")
        for ch in row.get("changes") or []:
            if ch.get("severity") in ("warn", "alert"):
                items.append(
                    {
                        "ticker": ticker,
                        "code": ch["code"],
                        "label": ch["label"],
                        "severity": ch["severity"],
                    }
                )
        for ev in row.get("unusual_flow_events") or []:
            if ev.get("status") == "anomaly":
                items.append(
                    {
                        "ticker": ticker,
                        "code": "FLOW_ANOMALY",
                        "label": f"{ticker} {ev.get('side', '').upper()} ${ev.get('strike')} {ev.get('expiry')} — {ev.get('anomaly_reason')}",
                        "severity": "alert",
                    }
                )
        # Surface the strongest OI delta per ticker.
        oi = row.get("oi_changes") or []
        if oi:
            top = oi[0]
            items.append(
                {
                    "ticker": ticker,
                    "code": "OI_DELTA",
                    "label": f"{ticker} — {top['label']}",
                    "severity": "warn",
                }
            )
    return items


@router.get("/uw-analyze/portfolio")
async def uw_analyze_portfolio() -> dict:
    """Return current snapshots + diffs for all portfolio + watchlist tickers.

    Cache is consulted first; only stale/missing entries trigger an upstream
    call. Concurrency is bounded by the cache's semaphore.
    """
    from utils.market_hours import is_market_open

    cache = get_portfolio_cache()
    flow_log = get_flow_log()
    candidates = seed_candidates()

    rows: list[dict] = []
    for ticker, sources in sorted(candidates.items()):
        try:
            entry, did_refresh = await cache.get_or_run(
                ticker,
                runner=_runner,
                force=False,
                sources=sources,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("uw-analyze portfolio: %s failed: %s", ticker, exc)
            continue

        # If the daily cron hasn't yet stamped oi_baseline (e.g. before 15:50 ET
        # or first boot), fetch on-demand so the dashboard surfaces deltas
        # immediately. Failures are non-fatal — empty oi_changes is fine.
        if not entry.get("oi_baseline"):
            try:
                from clients.uw_client import UWClient

                from api.services import uw_analyze_oi_tracker

                spot = (entry.get("current") or {}).get("derived", {}).get("spot")
                oi_changes_ondemand = await uw_analyze_oi_tracker.fetch_and_diff(UWClient(), ticker, spot)
                entry["oi_baseline"] = {
                    "data_date": datetime.now(timezone.utc).date().isoformat(),
                    "changes": [c.to_dict() for c in oi_changes_ondemand],
                }
            except Exception as exc:  # noqa: BLE001
                logger.debug("on-demand oi fetch failed for %s: %s", ticker, exc)

        snap = entry.get("current") or {}
        prev = entry.get("previous")
        # Read persisted materialized changes — never recompute on the GET
        # path. Flow event capture below runs only on fresh refreshes.
        change_dicts = entry.get("materialized_changes") or []

        if did_refresh and change_dicts:
            flow_alerts = snap.get("flow_alerts") or None
            underlying = (snap.get("derived") or {}).get("spot")
            if flow_alerts and underlying is not None:
                changes_objs = compute_changes(prev, snap)
                new_events = capture_from_changes(
                    ticker=ticker,
                    changes=changes_objs,
                    flow_alerts=flow_alerts,
                    underlying_price=underlying,
                )
                for ev in new_events:
                    flow_log.upsert(ev)

        oi_baseline = entry.get("oi_baseline") or {}
        oi_changes = oi_baseline.get("changes") or []
        unusual_events = [e.to_dict() for e in flow_log.for_ticker(ticker)]

        rows.append(
            {
                "ticker": ticker,
                "sources": list(sources),
                "snapshot": snap,
                "prev_ts": (prev or {}).get("ts") if isinstance(prev, dict) else None,
                "changes": change_dicts,
                "oi_changes": oi_changes,
                "unusual_flow_events": unusual_events,
            }
        )

    if any(r.get("changes") for r in rows):
        flow_log.save()

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "market_state": "open" if is_market_open() else "closed",
        "ttl_seconds": cache._ttl(),
        "tickers": rows,
        "action_items": _action_items_from(rows),
    }


@router.post("/uw-analyze/refresh")
async def uw_analyze_refresh(req: RefreshRequest) -> dict:
    """Force re-run for the given tickers (or all candidates)."""
    cache = get_portfolio_cache()
    targets: list[str]
    if req.tickers:
        targets = [t.strip().upper() for t in req.tickers if t and t.strip()]
        if req.adhoc:
            for t in targets:
                _candidates_add_adhoc(t)
    else:
        targets = sorted(seed_candidates().keys())

    refreshed = 0
    failed: list[dict] = []
    for ticker in targets:
        try:
            await cache.get_or_run(
                ticker,
                runner=_runner,
                force=True,
                sources=["adhoc"] if req.adhoc else (),
            )  # returns (entry, did_refresh); unused here
            refreshed += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("refresh failed for %s: %s", ticker, exc)
            failed.append({"ticker": ticker, "error": str(exc)})

    return {"refreshed": refreshed, "failed": failed}
