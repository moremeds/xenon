"""Broker-agnostic portfolio normalization for downstream analysis.

Loads positions from either the IB portfolio (`data/portfolio.json`) or the Futu
portfolio (`data/futu_portfolio.json`) and converts them into a single
`NormalizedPosition` shape so analysis scripts (e.g. `flow_analysis.py`) can stay
source-unaware.

Unsupported instruments (non-US, HK.*, etc.) are filtered out and counted in
`LoadResult.skipped_unsupported`.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal

Account = Literal["ib", "futu"]

SCRIPT_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = SCRIPT_DIR.parent
IB_PORTFOLIO = PROJECT_DIR / "data" / "portfolio.json"
FUTU_PORTFOLIO = PROJECT_DIR / "data" / "futu_portfolio.json"


@dataclass
class NormalizedPosition:
    ticker: str
    direction: str  # "LONG" or "SHORT"
    structure: str
    qty: float
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LoadResult:
    positions: List[NormalizedPosition]
    skipped_unsupported: int = 0


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        from utils.atomic_io import verified_load  # type: ignore
        return verified_load(str(path))
    except Exception:
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return {}


def _normalize_ib(rows: List[dict]) -> LoadResult:
    out: List[NormalizedPosition] = []
    for row in rows:
        ticker = row.get("ticker") or row.get("symbol")
        if not ticker:
            continue
        out.append(NormalizedPosition(
            ticker=str(ticker).upper(),
            direction=str(row.get("direction", "LONG")).upper(),
            structure=str(row.get("structure", "Unknown")),
            qty=float(row.get("contracts") or row.get("position") or 0),
            raw=row,
        ))
    return LoadResult(positions=out, skipped_unsupported=0)


def _is_us_futu_row(row: dict) -> bool:
    code = str(row.get("futu_code", ""))
    if code.startswith("US."):
        return True
    norm = row.get("normalized") or {}
    if str(norm.get("currency", "")).upper() == "USD":
        return True
    return False


def _futu_structure_label(norm: dict) -> str:
    kind = str(norm.get("kind", "")).upper()
    if kind == "STK":
        return "Stock"
    if kind == "OPT":
        right = str(norm.get("right", "")).upper()
        strike = norm.get("strike")
        side = "Call" if right == "C" else "Put"
        return f"{side} ${strike}" if strike is not None else side
    return kind or "Unknown"


def _normalize_futu(rows: List[dict]) -> LoadResult:
    out: List[NormalizedPosition] = []
    skipped = 0
    for row in rows:
        if not _is_us_futu_row(row):
            skipped += 1
            continue
        norm = row.get("normalized") or {}
        ticker = norm.get("symbol") or row.get("symbol")
        if not ticker:
            skipped += 1
            continue
        side = str(row.get("position_side") or "").upper()
        if side not in ("LONG", "SHORT"):
            qty = float(row.get("quantity") or 0)
            side = "SHORT" if qty < 0 else "LONG"
        out.append(NormalizedPosition(
            ticker=str(ticker).upper(),
            direction=side,
            structure=_futu_structure_label(norm),
            qty=float(row.get("quantity") or 0),
            raw=row,
        ))
    return LoadResult(positions=out, skipped_unsupported=skipped)


def load_normalized_positions(account: Account) -> LoadResult:
    """Load and normalize positions for the given account.

    Returns an empty `LoadResult` if the source file is missing.
    Raises `ValueError` for unknown account values.
    """
    if account == "ib":
        data = _read_json(IB_PORTFOLIO)
        rows = data.get("positions", []) if isinstance(data, dict) else []
        return _normalize_ib(rows)
    if account == "futu":
        data = _read_json(FUTU_PORTFOLIO)
        rows = data.get("positions", []) if isinstance(data, dict) else []
        return _normalize_futu(rows)
    raise ValueError(f"Unknown account: {account!r} (expected 'ib' or 'futu')")


def group_by_ticker(positions: List[NormalizedPosition]) -> Dict[str, List[NormalizedPosition]]:
    """Group positions by ticker so callers can fetch flow data once per symbol."""
    grouped: Dict[str, List[NormalizedPosition]] = defaultdict(list)
    for p in positions:
        grouped[p.ticker].append(p)
    return dict(grouped)
