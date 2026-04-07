"""TickerData aggregator.

Fetches all per-ticker data a scan/analyze run needs, with one single
normalization step for iv_rank (raw 0..1 float -> 0..100 percentile).

All downstream code uses TickerData.iv_percentile and never touches raw iv_rank.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from scripts.analysis.models import TickerData
from scripts.analysis.gex import (
    extract_call_wall,
    extract_put_wall,
    compute_gamma_per_1pct,
)

logger = logging.getLogger(__name__)


def _to_float_times_100(v) -> Optional[float]:
    """Parse a raw UW fraction (string or float, 0..1 scale) to 0..100."""
    if v is None or v == "":
        return None
    try:
        return round(float(v) * 100.0, 10)
    except (TypeError, ValueError):
        return None


def _to_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_next_earnings(raw: dict) -> tuple[Optional[date], bool]:
    """From UW get_earnings_by_ticker response, derive the next earnings date.

    UW returns a list of earnings events (historical + upcoming). We treat any
    event with a date >= today as "upcoming" and take the soonest.
    Returns (date_or_None, within_14d_bool). If unknown, conservative True.
    """
    events = raw.get("data") or []
    if not isinstance(events, list) or not events:
        return None, True  # conservative

    today = datetime.now().date()
    upcoming: list[date] = []
    for ev in events:
        raw_date = ev.get("report_date") or ev.get("date")
        if not raw_date:
            continue
        try:
            d = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if d >= today:
            upcoming.append(d)

    if not upcoming:
        return None, True  # nothing upcoming - default conservative

    next_date = min(upcoming)
    return next_date, (next_date - today) <= timedelta(days=14)


def _deep_enrichment(ticker: str, client, *, gex_strikes: list, fallback_price: Optional[float]) -> dict:
    """Run the 6 deep-mode enrichment endpoints + 3 in-process GEX computations.

    Each endpoint is wrapped in its own try/except so any single failure
    degrades just that field. Shapes verified by probe on 2026-04-08 — see
    docs/superpowers/plans/notes/2026-04-08-stock-state-probe.md.
    """
    out: dict = {}

    # 1. Live price + sector ──────────────────────────────────────────────
    price = fallback_price
    try:
        state = (client.get_stock_state(ticker) or {}).get("data") or {}
        if state.get("close") is not None:
            price = _to_float(state.get("close"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("stock_state failed for %s: %s", ticker, exc)
    out["price"] = price

    try:
        info = (client.get_stock_info(ticker) or {}).get("data") or {}
        out["sector"] = info.get("sector") or info.get("gics_sector")
    except Exception as exc:  # noqa: BLE001
        logger.debug("stock_info failed for %s: %s", ticker, exc)

    # 2. Options volume → net_call_premium / net_put_premium ─────────────
    try:
        ov = client.get_options_volume(ticker) or {}
        rows = ov.get("data") if isinstance(ov, dict) else None
        if isinstance(rows, list) and rows:
            latest = rows[-1] if isinstance(rows[-1], dict) else rows[0]
            out["net_call_premium"] = _to_float(latest.get("net_call_premium"))
            out["net_put_premium"] = _to_float(latest.get("net_put_premium"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("options_volume failed for %s: %s", ticker, exc)

    # 3. Net premium ticks → augments net_premium dict ───────────────────
    try:
        npt = client.get_net_prem_ticks(ticker) or {}
        ticks = npt.get("data") if isinstance(npt, dict) else None
        if isinstance(ticks, list) and ticks:
            agg_call = sum(
                (_to_float(t.get("net_call_premium")) or 0.0) for t in ticks if isinstance(t, dict)
            )
            agg_put = sum(
                (_to_float(t.get("net_put_premium")) or 0.0) for t in ticks if isinstance(t, dict)
            )
            out["net_premium_dict"] = {
                "net_call_premium": agg_call,
                "net_put_premium": agg_put,
                "net_total_premium": agg_call + agg_put,
                "tick_count": len(ticks),
            }
    except Exception as exc:  # noqa: BLE001
        logger.debug("net_prem_ticks failed for %s: %s", ticker, exc)

    # 4. Short volume ratio (note: payload key is "si", not "data") ──────
    try:
        sv = client.get_short_volume_ratio(ticker) or {}
        series = sv.get("si") if isinstance(sv, dict) else None
        if isinstance(series, list) and series:
            # Sort newest-first by market_date for deterministic trend
            sorted_rows = sorted(
                (r for r in series if isinstance(r, dict) and r.get("market_date")),
                key=lambda r: r["market_date"],
                reverse=True,
            )
            if sorted_rows:
                latest_ratio = _to_float(sorted_rows[0].get("short_volume_ratio"))
                out["short_volume_ratio"] = latest_ratio
                trend: list[float] = []
                for r in sorted_rows[:3]:
                    v = _to_float(r.get("short_volume_ratio"))
                    if v is not None:
                        trend.append(v)
                if trend:
                    out["short_volume_trend"] = trend
    except Exception as exc:  # noqa: BLE001
        logger.debug("short_volume_ratio failed for %s: %s", ticker, exc)

    # 5. IV rank series → latest iv_rank + 52w IV lo/hi (RV not exposed) ─
    try:
        ivr = client.get_iv_rank(ticker) or {}
        series = ivr.get("data") if isinstance(ivr, dict) else None
        if isinstance(series, list) and series:
            latest = series[-1] if isinstance(series[-1], dict) else None
            if latest:
                out["iv_rank"] = _to_float(latest.get("iv_rank_1y"))
            iv_vals = [
                _to_float(r.get("volatility"))
                for r in series
                if isinstance(r, dict)
            ]
            iv_vals = [v for v in iv_vals if v is not None]
            if iv_vals:
                out["iv_52w_low"] = round(min(iv_vals) * 100.0, 4)
                out["iv_52w_high"] = round(max(iv_vals) * 100.0, 4)
    except Exception as exc:  # noqa: BLE001
        logger.debug("iv_rank failed for %s: %s", ticker, exc)

    # 6. In-process GEX wall + intensity + flip (no extra HTTP) ─────────
    if gex_strikes:
        cw = extract_call_wall(gex_strikes)
        if cw:
            out["call_wall_strike"] = cw["strike"]
            out["call_wall_gamma"] = cw["gamma"]
        pw = extract_put_wall(gex_strikes)
        if pw:
            out["put_wall_strike"] = pw["strike"]
            out["put_wall_gamma"] = pw["gamma"]
        gp = compute_gamma_per_1pct(gex_strikes, price)
        if gp is not None:
            out["gamma_per_1pct"] = gp
        # Flip detection: filter to ±20% of authoritative price first.
        # Far-OTM strikes have noisy gamma signs that produce a meaningless
        # "first sign change at $4.75" for high-priced names like NVDA.
        from scripts.analysis.gex import detect_flip_point
        if price and price > 0:
            band_lo, band_hi = price * 0.8, price * 1.2
            flip_input = [
                s for s in gex_strikes
                if band_lo <= float(s.get("strike", 0)) <= band_hi
            ]
        else:
            flip_input = gex_strikes
        flip = detect_flip_point(flip_input)
        if flip is not None:
            out["gex_flip"] = flip

    return out


def fetch_ticker_data(ticker: str, client, *, deep: bool = False) -> TickerData:
    """Fetch all per-ticker inputs and return a TickerData.

    Silent degradation: any individual endpoint failure leaves that field None.

    Args:
        deep: When True, also fetch the 6 enrichment endpoints used by
            ``run_analysis()``. Default False keeps the scan path lean —
            ``uw_scan.py`` runs across the watchlist with a thread pool, so
            adding 6 HTTP calls per ticker would significantly slow it down.
    """
    ticker = ticker.upper()
    fetched_at = datetime.now()

    # Volatility stats (drives iv_percentile normalization).
    # Real shape (probed 2026-04-08): {"data": {"iv": "0.37", "rv": "0.34",
    # "iv_rank": "16.0252", ...}}. Note: iv/rv are 0..1 fractions but iv_rank
    # is already a 0..100 percentile — do NOT multiply.
    iv = rv = iv_percentile = None
    try:
        vol_raw = client.get_volatility_stats(ticker) or {}
        vol = vol_raw.get("data") if isinstance(vol_raw.get("data"), dict) else vol_raw
        iv = _to_float_times_100(vol.get("iv"))
        rv = _to_float_times_100(vol.get("rv"))
        # iv_rank is already 0..100; treat values <= 1 as legacy 0..1 scale.
        raw_rank = _to_float(vol.get("iv_rank"))
        if raw_rank is not None:
            iv_percentile = round(raw_rank * 100.0, 4) if raw_rank <= 1.0 else round(raw_rank, 4)
    except Exception as exc:  # noqa: BLE001
        logger.debug("volatility_stats failed for %s: %s", ticker, exc)

    # Term structure
    term_structure = None
    try:
        ts = client.get_volatility_term_structure(ticker) or {}
        ts_data = ts.get("data") if isinstance(ts, dict) else None
        if isinstance(ts_data, list) and ts_data:
            term_structure = ts_data
    except Exception as exc:  # noqa: BLE001
        logger.debug("term_structure failed for %s: %s", ticker, exc)

    # GEX — both endpoints return {"data": [...rows...]} with separate
    # call/put gamma fields. Normalize into the shapes downstream code reads:
    #   td.gex          = {"net": float, "flip": float, "price": float|None}
    #   td.gex_by_strike = {"strikes": [{"strike": float, "gamma": float}, ...]}
    gex = gex_by_strike = None
    price = None
    try:
        gex_resp = client.get_greek_exposure(ticker) or {}
        rows = gex_resp.get("data") if isinstance(gex_resp, dict) else None
        if isinstance(rows, list) and rows:
            latest = rows[-1] if isinstance(rows[-1], dict) else None
            if latest:
                cg = _to_float(latest.get("call_gamma")) or 0.0
                pg = _to_float(latest.get("put_gamma")) or 0.0
                gex = {"net": cg + pg, "raw": latest}
    except Exception as exc:  # noqa: BLE001
        logger.debug("greek_exposure failed for %s: %s", ticker, exc)

    try:
        gbs_resp = client.get_greek_exposure_by_strike(ticker) or {}
        gbs_rows = gbs_resp.get("data") if isinstance(gbs_resp, dict) else None
        if isinstance(gbs_rows, list) and gbs_rows:
            # Keep only latest date's strikes (multi-date payload arrives sorted ASC).
            latest_date = None
            for r in reversed(gbs_rows):
                if isinstance(r, dict) and r.get("date"):
                    latest_date = r["date"]
                    break
            normalized: list[dict] = []
            for r in gbs_rows:
                if not isinstance(r, dict):
                    continue
                if latest_date and r.get("date") != latest_date:
                    continue
                strike = _to_float(r.get("strike"))
                cg = _to_float(r.get("call_gex")) or 0.0
                pg = _to_float(r.get("put_gex")) or 0.0
                if strike is None:
                    continue
                normalized.append({"strike": strike, "gamma": cg + pg})
            if normalized:
                gex_by_strike = {"strikes": normalized}
                # Compute and stash flip into the gex envelope so the regime
                # classifier can use it without re-importing detect_flip_point.
                # Flip is computed later in _deep_enrichment once we have
                # the authoritative live price (so we can filter to ±20%
                # band and avoid noisy deep-OTM sign changes).
                flip = None
                if gex is None:
                    gex = {}
                if flip is not None:
                    gex["flip"] = flip
    except Exception as exc:  # noqa: BLE001
        logger.debug("greek_exposure_by_strike failed for %s: %s", ticker, exc)

    # Flow alerts
    flow_alerts = None
    try:
        fa = client.get_flow_alerts(ticker=ticker, limit=50) or {}
        data = fa.get("data") if isinstance(fa, dict) else None
        flow_alerts = data if isinstance(data, list) else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("flow_alerts failed for %s: %s", ticker, exc)

    # Dark pool - trailing 5 calendar days, concatenate into a single {"data": [...]}.
    darkpool = None
    try:
        all_trades: list[dict] = []
        for days_ago in range(5):
            date_str = (datetime.now().date() - timedelta(days=days_ago)).isoformat()
            try:
                dp = client.get_darkpool_flow(ticker, date=date_str) or {}
            except Exception as exc:  # noqa: BLE001
                logger.debug("darkpool_flow %s %s failed: %s", ticker, date_str, exc)
                continue
            rows = dp.get("data") if isinstance(dp, dict) else None
            if isinstance(rows, list):
                all_trades.extend(rows)
        if all_trades:
            darkpool = {"data": all_trades}
    except Exception as exc:  # noqa: BLE001
        logger.debug("darkpool window failed for %s: %s", ticker, exc)

    # Earnings
    earnings_date, earnings_within_14d = None, True
    try:
        er = client.get_earnings_by_ticker(ticker) or {}
        earnings_date, earnings_within_14d = _parse_next_earnings(er)
    except Exception as exc:  # noqa: BLE001
        logger.debug("earnings_by_ticker failed for %s: %s", ticker, exc)

    # Short data - optional
    short_interest = None
    try:
        sd = client.get_short_data(ticker) or {}
        short_interest = sd if sd else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("short_data failed for %s: %s", ticker, exc)

    # Historical skew - informational only for v1
    rr_skew_25d = None
    try:
        rr = client.get_historical_risk_reversal_skew(ticker) or {}
        items = rr.get("data") if isinstance(rr, dict) else None
        if isinstance(items, list) and items:
            latest = items[-1]
            if isinstance(latest, dict):
                rr_skew_25d = _to_float(latest.get("skew_25") or latest.get("value"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("rr_skew failed for %s: %s", ticker, exc)

    # VRP history - conditional on endpoint availability (Task 0 probe result).
    vrp_history = None
    fetch_vrp = getattr(client, "get_variance_risk_premium", None)
    if callable(fetch_vrp):
        try:
            resp = fetch_vrp(ticker, timespan="1y") or {}
            rows = resp.get("data") if isinstance(resp, dict) else None
            if isinstance(rows, list) and rows:
                parsed: list[float] = []
                for row in rows:
                    v = row.get("vrp") or row.get("value")
                    if v is None:
                        continue
                    try:
                        parsed.append(float(v))
                    except (TypeError, ValueError):
                        continue
                if parsed:
                    vrp_history = parsed
        except Exception as exc:  # noqa: BLE001
            logger.debug("variance_risk_premium failed for %s: %s", ticker, exc)

    # OI changes - v1 does not use historical OI (Short Squeeze / OI Buildup deferred).
    oi_changes = None

    # PCR - derived from flow_alerts call/put counts (no extra fetch).
    pcr: Optional[float] = None
    if flow_alerts:
        calls = sum(1 for a in flow_alerts if str(a.get("option_type", "")).lower() == "call"
                    or a.get("is_call") is True)
        puts = sum(1 for a in flow_alerts if str(a.get("option_type", "")).lower() == "put"
                   or a.get("is_put") is True)
        if calls > 0:
            pcr = puts / calls

    # Deep enrichment (only when explicitly requested) ────────────────────
    enrich: dict = {}
    if deep:
        gex_strikes = []
        if isinstance(gex_by_strike, dict):
            raw_strikes = gex_by_strike.get("strikes")
            if isinstance(raw_strikes, list):
                gex_strikes = raw_strikes
        enrich = _deep_enrichment(
            ticker, client, gex_strikes=gex_strikes, fallback_price=price
        )
        # Authoritative live price wins over GEX-derived fallback
        if enrich.get("price") is not None:
            price = enrich["price"]
        # Patch flip into the gex envelope so the regime classifier can find it
        if enrich.get("gex_flip") is not None:
            if gex is None:
                gex = {}
            else:
                gex = dict(gex)  # avoid mutating the dict we already constructed
            gex["flip"] = enrich["gex_flip"]

    return TickerData(
        ticker=ticker,
        price=price,
        fetched_at=fetched_at,
        gex=gex,
        gex_by_strike=gex_by_strike,
        iv=iv,
        rv=rv,
        iv_percentile=iv_percentile,
        term_structure=term_structure,
        rr_skew_25d=rr_skew_25d,
        vrp_history=vrp_history,
        flow_alerts=flow_alerts,
        net_premium=enrich.get("net_premium_dict"),
        pcr=pcr,
        darkpool=darkpool,
        oi_changes=oi_changes,
        short_interest=short_interest,
        earnings_date=earnings_date,
        earnings_within_14d=earnings_within_14d,
        iv_rank=enrich.get("iv_rank"),
        iv_52w_low=enrich.get("iv_52w_low"),
        iv_52w_high=enrich.get("iv_52w_high"),
        rv_52w_low=enrich.get("rv_52w_low"),
        rv_52w_high=enrich.get("rv_52w_high"),
        net_call_premium=enrich.get("net_call_premium"),
        net_put_premium=enrich.get("net_put_premium"),
        short_volume_ratio=enrich.get("short_volume_ratio"),
        short_volume_trend=enrich.get("short_volume_trend"),
        call_wall_strike=enrich.get("call_wall_strike"),
        call_wall_gamma=enrich.get("call_wall_gamma"),
        put_wall_strike=enrich.get("put_wall_strike"),
        put_wall_gamma=enrich.get("put_wall_gamma"),
        gamma_per_1pct=enrich.get("gamma_per_1pct"),
        sector=enrich.get("sector"),
    )
