"""Ticker universe loader for uw-scan."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Optional

Mode = Literal["watchlist", "targeted"]


def load_universe(
    *,
    mode: Mode,
    tickers: Optional[list[str]] = None,
    watchlist_path: str = "data/watchlist.json",
) -> list[str]:
    if mode == "targeted":
        if not tickers:
            return []
        seen: set[str] = set()
        result: list[str] = []
        for t in tickers:
            up = t.upper()
            if up not in seen:
                seen.add(up)
                result.append(up)
        return result

    if mode == "watchlist":
        path = Path(watchlist_path)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            return []
        raw_tickers = data.get("tickers", [])
        out: list[str] = []
        seen = set()
        for row in raw_tickers:
            if isinstance(row, dict) and row.get("ticker"):
                up = str(row["ticker"]).upper()
            elif isinstance(row, str):
                up = row.upper()
            else:
                continue
            if up not in seen:
                seen.add(up)
                out.append(up)
        return out

    raise ValueError(f"unsupported mode: {mode}")
