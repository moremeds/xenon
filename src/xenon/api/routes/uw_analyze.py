"""POST /uw-analyze — per-ticker UW signal analysis for the web UI.

Wraps `xenon.scanners.uw.analyze.run_analysis_with_data` and exposes both the
serialized AnalysisReport and a UI-facing display slice extracted from
TickerData (walls, gamma, IV rank, net premium, gex_by_strike rows).

Auth: covered globally by Clerk middleware in src/xenon/api/server.py.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from xenon.analysis.dark_pool_summary import summarize_dark_pool
from xenon.analysis.models import TickerData
from xenon.analysis.options_flow_summary import summarize_options_flow
from xenon.api.services.uw_analyze_cache import UwAnalyzeCache, build_snapshot
from xenon.api.services.uw_analyze_candidates import (
    add_adhoc as _candidates_add_adhoc,
)
from xenon.api.services.uw_analyze_candidates import (
    seed_candidates,
)
from xenon.api.services.uw_analyze_flow_tracker import FlowLog, capture_from_changes
from xenon.clients.uw_client import UWAPIError, UWNotFoundError
from xenon.fetchers.fetch_flow import analyze_darkpool
from xenon.scanners.uw.analyze import run_analysis_with_data

logger = logging.getLogger("xenon.uw_analyze")
router = APIRouter()

# Long-lived cache + flow log shared across requests for /portfolio and
# /refresh. Lazily instantiated so test discovery doesn't touch disk.
_portfolio_cache: Optional[UwAnalyzeCache] = None
_flow_log: Optional[FlowLog] = None
# Shared UWClient for on-demand OI refresh. Reused across tickers within
# a single /portfolio request and across requests — avoids allocating N
# client/session objects per GET, which was a major contributor to
# memory churn on the first warm-cache call after restart.
_uw_client_singleton: Optional[Any] = None
# Bounds on-demand OI fan-out inside /uw-analyze/portfolio. Without this,
# 35 portfolio tickers whose oi_baseline is not today-stamped would all
# fire `fetch_and_diff` (full option-chain fetch) in parallel, saturating
# the UW client and Python threadpool. Mirrors the upstream runner
# semaphore value in UwAnalyzeCache._semaphore (=3).
_ON_DEMAND_OI_SEM = asyncio.Semaphore(3)


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


def _shared_uw_client():
    """Return the process-wide shared UWClient, constructing on first use."""
    global _uw_client_singleton
    if _uw_client_singleton is None:
        from xenon.clients.uw_client import UWClient

        _uw_client_singleton = UWClient()
    return _uw_client_singleton


def reset_state_for_tests() -> None:
    """Reset module-level singletons. Tests only.

    Also recreates ``_ON_DEMAND_OI_SEM``. An `asyncio.Semaphore` captures
    the running event loop on first `acquire()`; suites that build a
    fresh event loop per test (FastAPI `TestClient` does this) must
    start from a fresh semaphore or risk a "bound to a different event
    loop" RuntimeError under contention.
    """
    global _portfolio_cache, _flow_log, _uw_client_singleton, _ON_DEMAND_OI_SEM
    _portfolio_cache = None
    _flow_log = None
    _uw_client_singleton = None
    _ON_DEMAND_OI_SEM = asyncio.Semaphore(3)


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
    # Term structure label: compare front-month vs back-month IV.
    #
    # UW's /stock/{t}/volatility/term-structure returns rows with a
    # "volatility" field (string, e.g. "0.2866"), sorted by dte. Earlier
    # versions of this code read `.get("iv")` which does not exist in
    # the response — that made `term_structure_label` always None for
    # every ticker and the UI term card perpetually empty.
    term_label: Optional[Literal["normal", "inverted"]] = None
    ts = td.term_structure
    if isinstance(ts, list) and len(ts) >= 2:
        try:
            front = float(ts[0].get("volatility"))
            back = float(ts[-1].get("volatility"))
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

    cache = get_portfolio_cache()
    try:
        # POST /uw-analyze is explicit user action — bypass the closed-market
        # gate so the user can analyze any ticker at any time.
        entry, _ = await cache.get_or_run(raw_ticker, runner=_runner, force=False, user_initiated=True)
    except UWNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"ticker not found: {raw_ticker}") from exc
    except UWAPIError as exc:
        logger.warning("uw-analyze upstream error for %s: %s", raw_ticker, exc)
        raise HTTPException(status_code=502, detail="UW upstream failed") from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="analysis timed out") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    snap = entry.get("current") or {}
    display_dict = snap.get("display") or {}
    return UwAnalyzeResponse(
        report=snap.get("report") or {},
        display=UwAnalyzeDisplay(**display_dict),
        generated_at=snap.get("ts") or datetime.now(timezone.utc).isoformat(),
    )


# ── Portfolio routes ────────────────────────────────────────────────────────


class RefreshRequest(BaseModel):
    tickers: Optional[list[str]] = None
    adhoc: bool = False


def _compute_dark_pool_summary(td: TickerData) -> Optional[dict]:
    """Build a coarse flow_data dict from TickerData.darkpool and score it.

    uw-analyze concatenates ~5 calendar days of dark-pool prints into a
    single list (see analysis/ticker_data.py); it does not break them up
    per day. For /flow-analysis alignment purposes we only need direction,
    strength, buy_ratio, and the options-conflict verdict — the
    sustained-day bonuses are OK to leave at zero for this path.
    """
    dp = td.darkpool if isinstance(td.darkpool, dict) else None
    trades = (dp or {}).get("data") or []
    aggregate = analyze_darkpool(trades)
    # Synthesize a single-day "daily" so summarize_dark_pool's recent-day
    # checks degenerate cleanly.
    daily = [
        {
            "flow_direction": aggregate.get("flow_direction"),
            "flow_strength": aggregate.get("flow_strength", 0),
            "num_prints": aggregate.get("num_prints", 0),
        }
    ]
    options_flow = summarize_options_flow(list(td.flow_alerts or []))
    flow_data = {
        "dark_pool": {"aggregate": aggregate, "daily": daily},
        "options_flow": options_flow,
    }
    return summarize_dark_pool(flow_data)


async def _runner(ticker: str) -> tuple[dict, dict, list[dict], Optional[dict], Optional[dict]]:
    """Adapt scripts.uw_analyze.run_analysis_with_data into the cache's
    ``(report_dict, display_dict, flow_alerts, dark_pool_summary, options_flow_summary)``
    contract. The last two elements feed the /flow-analysis portfolio
    classifier so it can answer "does the flow support this position?"
    without triggering a second UW fetch.
    """
    report, td = await asyncio.wait_for(
        asyncio.to_thread(run_analysis_with_data, ticker),
        timeout=60.0,
    )
    dark_pool_summary = _compute_dark_pool_summary(td)
    options_flow_summary = summarize_options_flow(list(td.flow_alerts or []))
    return (
        _serialize_report(report),
        _td_to_display(td).model_dump(),
        list(td.flow_alerts or []),
        dark_pool_summary,
        options_flow_summary,
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


async def _process_ticker(
    ticker: str,
    sources,
    cache: "UwAnalyzeCache",
    flow_log: "FlowLog",
    *,
    user_initiated: bool = False,
) -> Optional[dict]:
    """Process a single ticker: cache lookup/scan + OI baseline + flow capture.

    Returns a row dict with an internal ``_oi_refreshed`` flag (stripped by
    the caller before sending to the client).

    ``user_initiated=True`` bypasses the closed-market gate on both the
    primary cache.get_or_run call AND the on-demand OI refresh below.
    """
    try:
        entry, did_refresh = await cache.get_or_run(
            ticker,
            runner=_runner,
            force=False,
            user_initiated=user_initiated,
            sources=sources,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("uw-analyze portfolio: %s failed: %s", ticker, exc)
        return None

    # On-demand OI fetch when the daily cron hasn't yet stamped oi_baseline,
    # OR when the stamped baseline is from an earlier trading day.
    #
    # CLOSED-MARKET GATE: this block hits UW directly (not via get_or_run),
    # so we must gate it independently. Without this check, the midnight ET
    # rollover makes every ticker's baseline stale and the next automatic
    # /portfolio poll fires fetch_and_diff for all ~70 tickers — blowing
    # through the daily UW budget on every Saturday morning. Plan §2a +
    # Codex tribunal issue C1 in silly-humming-tide.md.
    #
    # IMPORTANT: this block must NOT call `cache._persist()`. The caller
    # batches a single persist after all tickers complete based on the
    # `_oi_refreshed` flag on each row.
    from xenon.api.services.uw_analyze_daily_job import now_et_date

    today_iso = now_et_date().isoformat()
    baseline = entry.get("oi_baseline") or {}
    oi_refreshed = False
    oi_stale = not baseline or baseline.get("data_date") != today_iso
    # Skip the OI refresh outside market hours unless this is an explicit
    # user action. The cache entry's existing oi_baseline (even if stale) is
    # served unchanged — the user can click refresh to pull fresh OI at any
    # time, and the daily cron will restamp baselines during open hours.
    if oi_stale and (cache._market_open_fn() or user_initiated):
        try:
            from xenon.api.services import uw_analyze_oi_tracker

            spot = (entry.get("current") or {}).get("derived", {}).get("spot")
            async with _ON_DEMAND_OI_SEM:
                oi_changes_ondemand = await uw_analyze_oi_tracker.fetch_and_diff(_shared_uw_client(), ticker, spot)
            entry["oi_baseline"] = {
                "data_date": today_iso,
                "changes": [c.to_dict() for c in oi_changes_ondemand],
            }
            oi_refreshed = True
        except Exception as exc:  # noqa: BLE001
            logger.debug("on-demand oi fetch failed for %s: %s", ticker, exc)

    snap = entry.get("current") or {}
    prev = entry.get("previous")
    change_dicts = entry.get("materialized_changes") or []

    if did_refresh and change_dicts:
        flow_alerts = snap.get("flow_alerts") or None
        underlying = (snap.get("derived") or {}).get("spot")
        if flow_alerts and underlying is not None:
            new_events = capture_from_changes(
                ticker=ticker,
                changes=change_dicts,
                flow_alerts=flow_alerts,
                underlying_price=underlying,
            )
            for ev in new_events:
                flow_log.upsert(ev)

    oi_baseline = entry.get("oi_baseline") or {}
    # Per-row stale fields (plan §3). `has_snapshot=False` signals scaffold
    # render; `served_stale=True` signals a dimmer "cached" badge on the tile.
    has_snapshot = bool(snap)
    snap_ts = snap.get("ts") if isinstance(snap, dict) else None
    served_stale = bool(entry.get("served_stale"))
    return {
        "ticker": ticker,
        "sources": list(sources),
        "snapshot": snap,
        "prev_ts": (prev or {}).get("ts") if isinstance(prev, dict) else None,
        "changes": change_dicts,
        "oi_changes": oi_baseline.get("changes") or [],
        "unusual_flow_events": [e.to_dict() for e in flow_log.for_ticker(ticker)],
        "has_snapshot": has_snapshot,
        "served_stale": served_stale,
        "snapshot_ts": snap_ts,
        "_oi_refreshed": oi_refreshed,
    }


async def _finalize_rows(rows: list[dict], cache, flow_log) -> None:
    """Post-process: strip internal flags, batched persist, save flow log."""
    any_oi_refreshed = False
    for r in rows:
        if r.pop("_oi_refreshed", False):
            any_oi_refreshed = True
    if any_oi_refreshed:
        try:
            await cache._persist()
        except Exception as exc:  # noqa: BLE001
            logger.warning("uw-analyze portfolio: batched persist failed: %s", exc)
    if any(r.get("changes") for r in rows):
        flow_log.save()


@router.get("/uw-analyze/portfolio")
async def uw_analyze_portfolio(request: Request, cached: bool = False, user_initiated: bool = False):
    """Return current snapshots + diffs for all portfolio + watchlist tickers.

    Modes:
    - ``?cached=true``: instant JSON from in-memory cache — no analysis triggered.
      Used by the frontend for the initial paint so tiles aren't blank.
    - ``Accept: text/event-stream``: SSE stream — tickers emitted as they complete.
    - default: JSON envelope (backward-compatible for tests/curl).

    ``?user_initiated=1`` marks the request as an explicit user action and
    bypasses the closed-market gate — both in the cache and in the OI fetch
    path. Set by the frontend when chaining a refetch after
    ``POST /uw-analyze/refresh`` so the follow-up read also refreshes the
    OI baseline. See silly-humming-tide.md plan §2a.
    """
    if cached:
        return await _portfolio_cached()

    want_sse = "text/event-stream" in (request.headers.get("accept") or "")

    if want_sse:
        return StreamingResponse(
            _portfolio_sse(request, user_initiated=user_initiated),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return await _portfolio_json(user_initiated=user_initiated)


async def _portfolio_cached() -> dict:
    """Instant cache-only response — no analysis triggered.

    Returns whatever is in the in-memory cache right now, formatted as the
    same ``{tickers, action_items, ...}`` envelope the frontend expects.
    Used for the initial paint so tiles show data from the last session
    instead of blank scaffolds while the SSE stream catches up.

    ``fetched_at`` is the newest ``snapshot_ts`` across all rows — NOT
    ``datetime.now()`` — so the UI can accurately show how old the
    displayed data is. Plan §3 / Codex issue #8 in silly-humming-tide.md.
    """
    cache = get_portfolio_cache()
    flow_log = get_flow_log()
    entries = cache.all_entries()

    # CRITICAL: read market state through the cache's injected function
    # (not directly from utils.market_hours.is_market_open) so test clocks
    # and the gate agree on a single source of truth. Fix #8 in
    # silly-humming-tide.md review.
    market_open = cache._market_open_fn()
    gated = not market_open

    rows: list[dict] = []
    latest_snapshot_ts: Optional[str] = None
    for ticker, entry in entries.items():
        snap = entry.get("current") or {}
        snap_ts = snap.get("ts") if isinstance(snap, dict) else None
        if isinstance(snap_ts, str) and (latest_snapshot_ts is None or snap_ts > latest_snapshot_ts):
            latest_snapshot_ts = snap_ts
        prev = entry.get("previous")
        oi_baseline = entry.get("oi_baseline") or {}
        has_snapshot = bool(snap)
        # served_stale: entry exists but is past TTL. Only meaningful during
        # closed hours (auto-refresh is paused so stale data is expected).
        stale_now = has_snapshot and not cache._is_fresh(entry)
        rows.append(
            {
                "ticker": ticker,
                "sources": list(entry.get("sources") or []),
                "snapshot": snap,
                "prev_ts": (prev or {}).get("ts") if isinstance(prev, dict) else None,
                "changes": entry.get("materialized_changes") or [],
                "oi_changes": oi_baseline.get("changes") or [],
                "unusual_flow_events": [e.to_dict() for e in flow_log.for_ticker(ticker)],
                "has_snapshot": has_snapshot,
                "served_stale": stale_now and gated,
                "snapshot_ts": snap_ts,
            }
        )

    return {
        "fetched_at": latest_snapshot_ts,
        "response_generated_at": datetime.now(timezone.utc).isoformat(),
        "market_state": "open" if market_open else "closed",
        "ttl_seconds": cache._ttl(),
        "closed_market_paused": gated,
        "tickers": rows,
        "action_items": _action_items_from(rows),
    }


async def _portfolio_json(*, user_initiated: bool = False) -> dict:
    """Original JSON response path — unchanged for tests + backward compat.

    Surfaces the same per-row and top-level stale fields as
    ``_portfolio_cached`` so the frontend contract is consistent across the
    two code paths.
    """
    cache = get_portfolio_cache()
    flow_log = get_flow_log()
    candidates = seed_candidates()

    results = await asyncio.gather(
        *[_process_ticker(t, s, cache, flow_log, user_initiated=user_initiated) for t, s in sorted(candidates.items())]
    )
    rows = [r for r in results if r is not None]
    await _finalize_rows(rows, cache, flow_log)

    # Single source of truth for market state — see Fix #8 note above.
    market_open = cache._market_open_fn()
    gated = not market_open
    latest_snapshot_ts: Optional[str] = None
    for r in rows:
        ts = r.get("snapshot_ts")
        if isinstance(ts, str) and (latest_snapshot_ts is None or ts > latest_snapshot_ts):
            latest_snapshot_ts = ts

    return {
        "fetched_at": latest_snapshot_ts,
        "response_generated_at": datetime.now(timezone.utc).isoformat(),
        "market_state": "open" if market_open else "closed",
        "ttl_seconds": cache._ttl(),
        "closed_market_paused": gated,
        "tickers": rows,
        "action_items": _action_items_from(rows),
    }


async def _portfolio_sse(request: Request, *, user_initiated: bool = False):
    """SSE generator — yields tickers as they complete for incremental rendering.

    Events:
    - ``event: meta`` — ``{fetched_at, response_generated_at, market_state,
      ttl_seconds, closed_market_paused}``
    - ``data: {ticker row}`` — one per ticker (default "message" event)
      (includes ``has_snapshot``, ``served_stale``, ``snapshot_ts``)
    - ``event: done`` — ``{action_items: [...]}``

    ``user_initiated=True`` propagates into every ``_process_ticker`` call
    so both the cache gate AND the OI-fetch gate are bypassed. Used by the
    frontend's follow-up GET after a refresh-button click.
    """
    cache = get_portfolio_cache()
    flow_log = get_flow_log()
    candidates = seed_candidates()

    # Single source of truth for market state — see Fix #8 note above.
    market_open = cache._market_open_fn()

    # Metadata first — frontend needs this before any tickers.
    # `fetched_at` here reflects the response-generation time, NOT the
    # freshest snapshot timestamp, because at SSE meta-emission time we
    # don't yet know what rows will arrive. Frontend prefers the per-row
    # `snapshot_ts` when displaying row-level staleness (see
    # useUwPortfolio.ts fetchPortfolio onTicker handler).
    _now = datetime.now(timezone.utc).isoformat()
    meta = {
        "fetched_at": _now,
        "response_generated_at": _now,
        "market_state": "open" if market_open else "closed",
        "ttl_seconds": cache._ttl(),
        "closed_market_paused": not market_open,
    }
    yield f"event: meta\ndata: {_json.dumps(meta)}\n\n"

    # Fan out all tickers through the same _process_ticker (OI + flow logic
    # preserved). Results enqueued as they complete — cached entries finish
    # near-instantly, fresh scans trickle in.
    queue: asyncio.Queue[Optional[dict]] = asyncio.Queue()

    async def _enqueue(ticker: str, sources) -> None:
        try:
            row = await _process_ticker(ticker, sources, cache, flow_log, user_initiated=user_initiated)
            await queue.put(row)
        except Exception:  # noqa: BLE001
            await queue.put(None)

    tasks = [asyncio.create_task(_enqueue(t, s)) for t, s in sorted(candidates.items())]

    all_rows: list[dict] = []
    try:
        for _ in range(len(tasks)):
            # Check for client disconnect between tickers.
            if await request.is_disconnected():
                break
            row = await queue.get()
            if row is not None:
                all_rows.append(row)
                # Strip internal flag before sending to client.
                row_copy = {k: v for k, v in row.items() if k != "_oi_refreshed"}
                yield f"data: {_json.dumps(row_copy)}\n\n"
    finally:
        # Cancel outstanding tasks on disconnect or error.
        for t in tasks:
            if not t.done():
                t.cancel()
        # Suppress CancelledError from tasks we just cancelled.
        await asyncio.gather(*tasks, return_exceptions=True)

    # Post-process: batched persist + flow log save.
    await _finalize_rows(all_rows, cache, flow_log)

    # Final event: action items summary.
    done_payload = {"action_items": _action_items_from(all_rows)}
    yield f"event: done\ndata: {_json.dumps(done_payload)}\n\n"


@router.post("/uw-analyze/refresh")
async def uw_analyze_refresh(req: RefreshRequest) -> dict:
    """Force re-run for the given tickers (or all candidates)."""
    cache = get_portfolio_cache()
    flow_log = get_flow_log()
    targets: list[str]
    if req.tickers:
        targets = [t.strip().upper() for t in req.tickers if t and t.strip()]
        if req.adhoc:
            for t in targets:
                _candidates_add_adhoc(t)
    else:
        targets = sorted(seed_candidates().keys())

    captured_any = {"flag": False}

    async def _refresh_one(ticker: str) -> Optional[dict]:
        try:
            # POST /uw-analyze/refresh is the refresh-button path — explicit
            # user action. user_initiated=True bypasses the closed-market gate
            # so the button works during overnight/weekend hours.
            entry, did_refresh = await cache.get_or_run(
                ticker,
                runner=_runner,
                force=True,
                user_initiated=True,
                sources=["adhoc"] if req.adhoc else (),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("refresh failed for %s: %s", ticker, exc)
            return {"ticker": ticker, "error": str(exc)}

        if did_refresh:
            snap = entry.get("current") or {}
            change_dicts = entry.get("materialized_changes") or []
            if change_dicts:
                flow_alerts = snap.get("flow_alerts") or None
                underlying = (snap.get("derived") or {}).get("spot")
                if flow_alerts and underlying is not None:
                    new_events = capture_from_changes(
                        ticker=ticker,
                        changes=change_dicts,
                        flow_alerts=flow_alerts,
                        underlying_price=underlying,
                    )
                    for ev in new_events:
                        flow_log.upsert(ev)
                        captured_any["flag"] = True
        return None

    results = await asyncio.gather(*[_refresh_one(t) for t in targets])
    failed = [r for r in results if r is not None]
    refreshed = len(targets) - len(failed)

    if captured_any["flag"]:
        flow_log.save()

    return {"refreshed": refreshed, "failed": failed}
