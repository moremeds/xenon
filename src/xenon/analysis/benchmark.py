"""SPY + sector ETF benchmark loader."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from xenon.analysis.models import BenchmarkContext, BenchmarkSnapshot

logger = logging.getLogger(__name__)


SECTOR_ETF_MAP: dict[str, str] = {
    "Technology": "XLK",
    "Financial Services": "XLF",
    "Financials": "XLF",
    "Healthcare": "XLV",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Communication Services": "XLC",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Basic Materials": "XLB",
    "Real Estate": "XLRE",
    "Utilities": "XLU",
}


def _to_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _load_snapshot(client, ticker: str) -> BenchmarkSnapshot:
    """Load IV rank + GEX regime + flip + price snapshot for a benchmark ticker.

    Real UW shapes (probed 2026-04-08, see ticker_data.py for cross-reference):
      get_volatility_stats   → {"data": {"iv_rank": "16.0252", ...}}     (already 0..100)
      get_greek_exposure     → {"data": [{"call_gamma": ..., "put_gamma": ...}, ...]}
      get_greek_exposure_by_strike → {"data": [{"strike", "call_gex", "put_gex"}, ...]}
      get_stock_state        → {"data": {"close": "656.635", ...}}
    """
    iv_rank = None
    gex_regime = None
    gex_flip = None
    price = None
    ok = True

    # Live price
    try:
        state = (client.get_stock_state(ticker) or {}).get("data") or {}
        if state.get("close") is not None:
            price = _to_float(state.get("close"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("benchmark stock_state failed for %s: %s", ticker, exc)
        ok = False

    # IV rank — payload is nested under "data"; iv_rank is already 0..100
    # (treat values <= 1 as legacy 0..1 fraction).
    try:
        vol_raw = client.get_volatility_stats(ticker) or {}
        vol = vol_raw.get("data") if isinstance(vol_raw.get("data"), dict) else vol_raw
        raw_rank = _to_float(vol.get("iv_rank"))
        if raw_rank is not None:
            iv_rank = round(raw_rank * 100.0, 4) if raw_rank <= 1.0 else round(raw_rank, 4)
    except Exception as exc:  # noqa: BLE001
        logger.debug("benchmark vol_stats failed for %s: %s", ticker, exc)
        ok = False

    # GEX net regime — sum call_gamma + put_gamma from latest data row
    try:
        gex_resp = client.get_greek_exposure(ticker) or {}
        rows = gex_resp.get("data") if isinstance(gex_resp, dict) else None
        if isinstance(rows, list) and rows:
            latest = rows[-1] if isinstance(rows[-1], dict) else None
            if latest:
                cg = _to_float(latest.get("call_gamma")) or 0.0
                pg = _to_float(latest.get("put_gamma")) or 0.0
                net_f = cg + pg
                if net_f > 0:
                    gex_regime = "positive"
                elif net_f < 0:
                    gex_regime = "negative"
                else:
                    gex_regime = "mixed"
    except Exception as exc:  # noqa: BLE001
        logger.debug("benchmark gex failed for %s: %s", ticker, exc)
        ok = False

    # GEX flip — compute from strikes (no top-level flip field on this endpoint)
    try:
        gbs_resp = client.get_greek_exposure_by_strike(ticker) or {}
        gbs_rows = gbs_resp.get("data") if isinstance(gbs_resp, dict) else None
        if isinstance(gbs_rows, list) and gbs_rows:
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
                if strike is None:
                    continue
                cg = _to_float(r.get("call_gex")) or 0.0
                pg = _to_float(r.get("put_gex")) or 0.0
                normalized.append({"strike": strike, "gamma": cg + pg})
            if normalized:
                from xenon.analysis.gex import detect_flip_point
                if price and price > 0:
                    band_lo, band_hi = price * 0.8, price * 1.2
                    flip_input = [s for s in normalized if band_lo <= s["strike"] <= band_hi]
                else:
                    flip_input = normalized
                gex_flip = detect_flip_point(flip_input)
    except Exception as exc:  # noqa: BLE001
        logger.debug("benchmark gex_by_strike failed for %s: %s", ticker, exc)
        ok = False

    freshness = "live" if ok else "unavailable"
    return BenchmarkSnapshot(
        ticker=ticker,
        iv_rank=iv_rank,
        gex_regime=gex_regime,
        gex_flip=gex_flip,
        price=price,
        data_date=datetime.now().strftime("%Y-%m-%d"),
        freshness=freshness,
    )


def load_benchmark_context(client, *, ticker_sector: Optional[str] = None) -> BenchmarkContext:
    """Load SPY + optional sector ETF snapshots."""
    spy = _load_snapshot(client, "SPY")
    sector_etf: Optional[BenchmarkSnapshot] = None
    if ticker_sector and ticker_sector in SECTOR_ETF_MAP:
        etf_symbol = SECTOR_ETF_MAP[ticker_sector]
        sector_etf = _load_snapshot(client, etf_symbol)
    return BenchmarkContext(spy=spy, sector_etf=sector_etf)
