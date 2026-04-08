"""Candidate seeding for UW Analyze portfolio view.

Resolves the union of:
- portfolio underlyings (data/portfolio.json::positions[].ticker)
- watchlist tickers   (data/watchlist.json::tickers[].ticker)
- in-memory ad-hoc set (per-process; cleared on restart)

Returns `dict[str, list[Source]]` where Source ∈ {"portfolio", "watchlist", "adhoc"}.
A ticker present in multiple buckets is tagged with all of them.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Iterable

_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

logger = logging.getLogger("xenon.uw_analyze_candidates")

_DATA = _SCRIPTS.parent / "data"
PORTFOLIO_PATH = _DATA / "portfolio.json"
WATCHLIST_PATH = _DATA / "watchlist.json"

# Process-local ad-hoc set; survives across requests but not restarts.
_adhoc: set[str] = set()


def add_adhoc(ticker: str) -> None:
    if ticker:
        _adhoc.add(ticker.upper())


def clear_adhoc() -> None:
    _adhoc.clear()


def adhoc_set() -> set[str]:
    return set(_adhoc)


def _read_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to read %s: %s", path, exc)
        return None


def portfolio_tickers(path: Path = PORTFOLIO_PATH) -> set[str]:
    data = _read_json(path)
    if not isinstance(data, dict):
        return set()
    out: set[str] = set()
    for pos in data.get("positions") or []:
        if not isinstance(pos, dict):
            continue
        t = pos.get("ticker") or pos.get("symbol")
        if isinstance(t, str) and t:
            out.add(t.upper())
    return out


def watchlist_tickers(path: Path = WATCHLIST_PATH) -> set[str]:
    data = _read_json(path)
    if not isinstance(data, dict):
        return set()
    out: set[str] = set()
    for row in data.get("tickers") or []:
        if not isinstance(row, dict):
            continue
        t = row.get("ticker")
        if isinstance(t, str) and t:
            out.add(t.upper())
    return out


def seed_candidates(
    *,
    portfolio_path: Path | None = None,
    watchlist_path: Path | None = None,
    extra_adhoc: Iterable[str] = (),
) -> dict[str, list[str]]:
    """Build the ticker → sources[] map.

    Paths default to the module-level `PORTFOLIO_PATH` / `WATCHLIST_PATH`
    attributes resolved AT CALL TIME — tests rebinding these module
    attributes will be picked up correctly.
    """
    port = portfolio_tickers(portfolio_path or PORTFOLIO_PATH)
    watch = watchlist_tickers(watchlist_path or WATCHLIST_PATH)
    adhoc = set(_adhoc) | {t.upper() for t in extra_adhoc if t}

    out: dict[str, set[str]] = {}
    for t in port:
        out.setdefault(t, set()).add("portfolio")
    for t in watch:
        out.setdefault(t, set()).add("watchlist")
    for t in adhoc:
        out.setdefault(t, set()).add("adhoc")

    return {t: sorted(s) for t, s in out.items()}
