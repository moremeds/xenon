"""uw-scan universe loading — delegates to scanner_lib for core utilities."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal, Optional

from scanners._shared.universe import dedup_and_normalize

logger = logging.getLogger(__name__)

Mode = Literal["watchlist", "targeted"]


def load_universe(
    *,
    mode: Mode,
    tickers: Optional[list[str]] = None,
    watchlist_path: str = "data/watchlist.json",
) -> list[str]:
    """Load scan universe. 'targeted' uses explicit list; 'watchlist' reads JSON file."""
    if mode == "targeted":
        return dedup_and_normalize(tickers or [])
    elif mode == "watchlist":
        path = Path(watchlist_path)
        if not path.exists():
            logger.warning("Watchlist not found: %s", path)
            return []
        try:
            raw = json.loads(path.read_text())
            data = raw.get("tickers", []) if isinstance(raw, dict) else raw
            tickers_list: list[str] = []
            for item in data:
                if isinstance(item, str):
                    tickers_list.append(item)
                elif isinstance(item, dict) and "ticker" in item:
                    tickers_list.append(item["ticker"])
            return dedup_and_normalize(tickers_list)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load watchlist: %s", e)
            return []
    else:
        raise ValueError(f"Unsupported mode: {mode}")
