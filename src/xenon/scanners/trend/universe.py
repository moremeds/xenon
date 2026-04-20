"""Universe loader: read the authoritative ticker list from the R2 mirror.

Replaces the old SP500 + Nasdaq100 + UW flow alerts construction. The
external producer maintains meta/universe.json with 2,308 tickers, each
carrying symbol, marketCap, dollar_volume, turnover_rate, tier, sector,
and per-ticker timeframes list.
"""

from __future__ import annotations

import json
from pathlib import Path


def load_universe_from_mirror(mirror_dir: Path | str) -> list[dict]:
    """Return the tickers array verbatim from meta/universe.json in the mirror."""
    path = Path(mirror_dir) / "meta" / "universe.json"
    if not path.exists():
        raise FileNotFoundError(f"universe file missing: {path} — run apex_sync first")
    payload = json.loads(path.read_text())
    return payload.get("tickers") or []
