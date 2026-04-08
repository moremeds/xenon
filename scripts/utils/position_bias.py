"""Per-position directional bias inference.

Maps a NormalizedPosition-shaped dict to one of six bias labels:

    bullish / bearish / neutral_vol / income / hedge / unknown

Source of truth is docs/trading/options-structures.json, which contains
a `bias` field per structure. A post-process override table remaps the
volatility / income / hedge categories that the json flattens into the
three base directional labels.

Canonicalization order (important — broker structure labels drift):

    1. IB: pos["raw"]["structure_type"]  e.g. "Short Put"
    2. Futu: pos["raw"]["normalized"]    derive from kind + right + side
    3. pos["structure"]                  last-resort text match
    4. IB: walk pos["raw"]["legs"]       derive from right + action
    5. "unknown" — never default to "bullish"
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Optional

Bias = Literal["bullish", "bearish", "neutral_vol", "income", "hedge", "unknown"]

_STRUCTURES_PATH = Path(__file__).resolve().parent.parent.parent / "docs" / "trading" / "options-structures.json"


def _load_structure_bias_table() -> dict[str, str]:
    """Build {name_or_alias_lowercased: json_bias} from options-structures.json."""
    try:
        raw = json.loads(_STRUCTURES_PATH.read_text())
    except FileNotFoundError:
        return {}
    table: dict[str, str] = {}
    for entry in raw:
        name = entry.get("name", "")
        bias = entry.get("bias", "")
        if not name or not bias:
            continue
        table[name.lower()] = bias
        for alias in entry.get("aliases", []) or []:
            table[str(alias).lower()] = bias
    return table


_STRUCTURE_BIAS_TABLE = _load_structure_bias_table()

# Override table: map json bias + specific structure names into our
# six-label enum. json only uses bullish/bearish/neutral/volatility_*;
# we need to distinguish hedge/income/neutral_vol for UI honesty.
_HEDGE_STRUCTURES = {
    "protective put",
    "collar",
}
_INCOME_STRUCTURES = {
    "iron condor",
    "short strangle",
    "short straddle",
    "short iron condor",
    # Note: "short put (cash-secured)" and "covered call" are income by
    # motive but strictly directional by exposure (long delta / short
    # delta respectively). Treat them as bullish/bearish so the
    # portfolio alignment view can still say "supports"/"against".
}
_NEUTRAL_VOL_STRUCTURES = {
    "long straddle",
    "long strangle",
}


def _map_json_bias(structure_key: str, json_bias: str) -> Bias:
    key = structure_key.lower()
    if key in _HEDGE_STRUCTURES:
        return "hedge"
    if key in _INCOME_STRUCTURES:
        return "income"
    if key in _NEUTRAL_VOL_STRUCTURES:
        return "neutral_vol"
    # json "volatility_long"/"volatility_short" not caught above: treat as neutral_vol
    if json_bias.startswith("volatility"):
        return "neutral_vol"
    if json_bias == "neutral":
        return "income"  # most "neutral" structures in the json are theta plays
    if json_bias == "bullish":
        return "bullish"
    if json_bias == "bearish":
        return "bearish"
    return "unknown"


_EQUITY_LABELS: dict[str, Bias] = {
    "long stock": "bullish",
    "stock": "bullish",
    "short stock": "bearish",
}

# Direct shortcuts that bypass the json catalog — used for structures whose
# catalog names don't match the IB structure_type exactly, or where we want
# to override the json classification with a more honest UI label.
_STRUCTURE_SHORTCUTS: dict[str, Bias] = {
    "collar": "hedge",
    "protective put": "hedge",
    "long straddle": "neutral_vol",
    "long strangle": "neutral_vol",
    "iron condor": "income",
    "short strangle": "income",
    "short straddle": "income",
    "short iron condor": "income",
}


def _lookup(name: Optional[str]) -> Optional[Bias]:
    if not name:
        return None
    key = name.strip().lower()
    if key in _EQUITY_LABELS:
        return _EQUITY_LABELS[key]
    if key in _STRUCTURE_SHORTCUTS:
        return _STRUCTURE_SHORTCUTS[key]
    json_bias = _STRUCTURE_BIAS_TABLE.get(key)
    if json_bias is None:
        return None
    return _map_json_bias(key, json_bias)


def _from_ib_legs(legs: list[dict]) -> Optional[Bias]:
    if not legs or len(legs) != 1:
        return None
    leg = legs[0]
    right = str(leg.get("type", "")).upper()
    direction = str(leg.get("direction", "")).upper()
    if right == "CALL":
        return "bullish" if direction == "LONG" else "bearish"
    if right == "PUT":
        return "bearish" if direction == "LONG" else "bullish"
    return None


def _from_futu_normalized(norm: dict, side: str) -> Optional[Bias]:
    kind = str(norm.get("kind", "")).upper()
    side = side.upper()
    if kind == "STK":
        return "bullish" if side == "LONG" else "bearish"
    if kind == "OPT":
        right = str(norm.get("right", "")).upper()
        if right == "C":
            return "bullish" if side == "LONG" else "bearish"
        if right == "P":
            return "bearish" if side == "LONG" else "bullish"
    return None


def position_bias(pos: dict) -> Bias:
    """Return the directional bias label for a single NormalizedPosition dict.

    See module docstring for the canonicalization order. Never returns
    "bullish" as a fallback — unknowns return "unknown" so the UI can
    surface them as non-directional instead of misclassifying.
    """
    raw = pos.get("raw", {}) or {}

    # 1. IB canonical: raw.structure_type
    hit = _lookup(raw.get("structure_type"))
    if hit is not None:
        return hit

    # 2. Futu canonical: raw.normalized (single-leg, no structure name)
    norm = raw.get("normalized")
    if isinstance(norm, dict):
        side = str(raw.get("position_side") or pos.get("direction") or "LONG")
        hit2 = _from_futu_normalized(norm, side)
        if hit2 is not None:
            return hit2

    # 3. pos.structure text match (may include trailing strike, so try stripped)
    structure = pos.get("structure")
    if isinstance(structure, str):
        # Try exact match first
        hit = _lookup(structure)
        if hit is not None:
            return hit
        # Try stripping trailing "$123.4" that the portfolio adapter attaches
        stripped = structure.split("$")[0].strip()
        hit = _lookup(stripped)
        if hit is not None:
            return hit

    # 4. IB single-leg fallback via raw.legs
    legs = raw.get("legs")
    if isinstance(legs, list):
        hit3 = _from_ib_legs(legs)
        if hit3 is not None:
            return hit3

    # 5. Unknown — never default to bullish
    return "unknown"
