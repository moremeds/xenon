"""Triple-source universe builder for trend scanner."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from scripts.scanner_lib.universe import dedup_and_normalize, load_tickers_from_json, union_sources

logger = logging.getLogger(__name__)


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        rows = payload.get("data")
        return rows if isinstance(rows, list) else []
    if isinstance(payload, list):
        return payload
    return []


def build_static_universe(
    *,
    sp500_path: Path | str,
    nasdaq100_path: Path | str,
) -> list[str]:
    """Load tickers from static index constituent files."""
    sp = load_tickers_from_json(Path(sp500_path))
    nq = load_tickers_from_json(Path(nasdaq100_path))
    return union_sources(sp, nq)


def build_uw_flow_universe(
    *,
    client: Any,
    min_premium: float = 100_000,
    lookback_days: int = 5,
) -> list[str]:
    """Extract tickers from recent UW flow alerts and dark pool activity."""
    tickers: list[str] = []
    newer_than = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date().isoformat()
    try:
        alerts = client.get_flow_alerts(min_premium=min_premium, newer_than=newer_than)
        tickers.extend(a["ticker"] for a in _extract_rows(alerts) if isinstance(a, dict) and "ticker" in a)
    except Exception:
        logger.warning("Failed to fetch UW flow alerts for universe", exc_info=True)

    try:
        recent_fn = getattr(client, "get_darkpool_recent", None)
        dp_rows: list[dict[str, Any]] = []
        if callable(recent_fn):
            dp_rows = _extract_rows(recent_fn(limit=250))
        if not dp_rows:
            dp_rows = _extract_rows(client.get_darkpool_flow())
        tickers.extend(d["ticker"] for d in dp_rows if isinstance(d, dict) and "ticker" in d)
    except Exception:
        logger.warning("Failed to fetch UW dark pool for universe", exc_info=True)

    return dedup_and_normalize(tickers)


def build_ib_scanner_universe(*, client: Any) -> list[str]:
    """Fetch tickers from IB market scanners (top gainers, most active)."""
    tickers: list[str] = []
    scanner_types = ["TOP_PERC_GAIN", "MOST_ACTIVE_USD"]
    for scan_type in scanner_types:
        try:
            results = client.run_scanner(scan_type=scan_type)
            tickers.extend(r["ticker"] for r in results if "ticker" in r)
        except Exception:
            logger.warning("IB scanner %s failed", scan_type, exc_info=True)

    return dedup_and_normalize(tickers)


def build_universe(
    cfg: Any,
    *,
    uw_client: Optional[Any] = None,
    ib_client: Optional[Any] = None,
) -> list[str]:
    """Build the full universe from all three sources."""
    static = build_static_universe(
        sp500_path=cfg.sp500_path,
        nasdaq100_path=cfg.nasdaq100_path,
    )

    uw: list[str] = []
    if uw_client is not None:
        uw = build_uw_flow_universe(
            client=uw_client,
            min_premium=cfg.uw_flow_min_premium,
            lookback_days=cfg.uw_flow_lookback_days,
        )

    ib: list[str] = []
    if ib_client is not None:
        ib = build_ib_scanner_universe(client=ib_client)

    universe = union_sources(static, uw, ib)
    logger.info(
        "Universe built: %d tickers (static=%d, uw=%d, ib=%d)",
        len(universe),
        len(static),
        len(uw),
        len(ib),
    )
    return universe
