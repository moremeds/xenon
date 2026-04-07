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


def fetch_ticker_data(ticker: str, client) -> TickerData:
    """Fetch all per-ticker inputs and return a TickerData.

    Silent degradation: any individual endpoint failure leaves that field None.
    """
    ticker = ticker.upper()
    fetched_at = datetime.now()

    # Volatility stats (drives iv_percentile normalization)
    iv = rv = iv_percentile = None
    try:
        vol = client.get_volatility_stats(ticker) or {}
        iv = _to_float_times_100(vol.get("iv"))
        rv = _to_float_times_100(vol.get("rv"))
        iv_percentile = _to_float_times_100(vol.get("iv_rank"))
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

    # GEX
    gex = gex_by_strike = None
    price = None
    try:
        gex_resp = client.get_greek_exposure(ticker) or {}
        gex = gex_resp if gex_resp else None
        if isinstance(gex_resp, dict):
            price = _to_float(gex_resp.get("price") or gex_resp.get("spot"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("greek_exposure failed for %s: %s", ticker, exc)

    try:
        gbs = client.get_greek_exposure_by_strike(ticker) or {}
        gex_by_strike = gbs if gbs else None
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
        net_premium=None,
        pcr=pcr,
        darkpool=darkpool,
        oi_changes=oi_changes,
        short_interest=short_interest,
        earnings_date=earnings_date,
        earnings_within_14d=earnings_within_14d,
    )
