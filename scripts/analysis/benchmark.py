"""SPY + sector ETF benchmark loader."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from scripts.analysis.models import BenchmarkContext, BenchmarkSnapshot

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


def _to_pct(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v) * 100.0
    except (TypeError, ValueError):
        return None


def _to_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _load_snapshot(client, ticker: str) -> BenchmarkSnapshot:
    iv_rank = None
    gex_regime = None
    gex_flip = None
    price = None
    ok = True

    try:
        vol = client.get_volatility_stats(ticker) or {}
        iv_rank = _to_pct(vol.get("iv_rank"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("benchmark vol_stats failed for %s: %s", ticker, exc)
        ok = False

    try:
        gex = client.get_greek_exposure(ticker) or {}
        net = gex.get("net") or gex.get("net_gamma")
        if net is not None:
            net_f = _to_float(net) or 0.0
            if net_f > 0:
                gex_regime = "positive"
            elif net_f < 0:
                gex_regime = "negative"
            else:
                gex_regime = "mixed"
        gex_flip = _to_float(gex.get("flip") or gex.get("flip_point"))
        price = _to_float(gex.get("price") or gex.get("spot"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("benchmark gex failed for %s: %s", ticker, exc)
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
