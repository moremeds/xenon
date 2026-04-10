"""Universe loading and merging utilities for scanners."""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_tickers_from_json(path: Path) -> list[str]:
    """Load tickers from a JSON file. Supports list of strings or list of {ticker: str} dicts."""
    if not path.exists():
        logger.warning("Universe file not found: %s", path)
        return []
    try:
        data = json.loads(path.read_text())
        tickers: list[str] = []
        for item in data:
            if isinstance(item, str):
                tickers.append(item)
            elif isinstance(item, dict) and "ticker" in item:
                tickers.append(item["ticker"])
        return dedup_and_normalize(tickers)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load universe from %s: %s", path, e)
        return []


def dedup_and_normalize(tickers: list[str]) -> list[str]:
    """Uppercase, deduplicate, and sort tickers."""
    seen: set[str] = set()
    result: list[str] = []
    for t in tickers:
        upper = t.upper().strip()
        if upper and upper not in seen:
            seen.add(upper)
            result.append(upper)
    return sorted(result)


def union_sources(*sources: list[str]) -> list[str]:
    """Merge multiple ticker lists into a deduplicated, sorted union."""
    combined: list[str] = []
    for source in sources:
        combined.extend(source)
    return dedup_and_normalize(combined)
