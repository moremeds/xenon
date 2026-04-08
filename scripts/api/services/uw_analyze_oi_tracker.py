"""Daily OI tracker — surfaces notable strike-level open-interest deltas.

Wraps `UWClient.get_stock_oi_change` (which already returns per-strike OI
deltas vs the previous trading session) and applies our notability gates:

- |Δ OI| / prev_oi  >= 25%   (bypass when prev_oi == 0)
- |Δ OI| absolute   >= 1000  contracts
- |strike - spot| / spot <= 5%

Pure-ish: takes the raw oi-change rows + spot, returns OiChange dataclasses.
The fetch is a thin async wrapper to keep the rest of the cache code
agnostic about whether we hit UW or a fixture.

Spec: docs/superpowers/specs/2026-04-08-uw-analyze-overhaul-design.md
      §"Daily OI tracker"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Literal, Optional

logger = logging.getLogger("xenon.uw_analyze_oi")

# ── Tunables ────────────────────────────────────────────────────────────────
NOTABLE_PCT = 0.25  # 25%
NOTABLE_ABSOLUTE = 1000  # contracts
NEAR_SPOT_PCT = 0.05  # ±5%

OiSide = Literal["call", "put"]


@dataclass(frozen=True)
class OiChange:
    strike: float
    side: OiSide
    prev_oi: int
    curr_oi: int
    delta: int
    delta_pct: float
    label: str

    def to_dict(self) -> dict:
        return {
            "strike": self.strike,
            "side": self.side,
            "prev_oi": self.prev_oi,
            "curr_oi": self.curr_oi,
            "delta": self.delta,
            "delta_pct": self.delta_pct,
            "label": self.label,
        }


def _coerce_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _coerce_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _format_count(n: int) -> str:
    if abs(n) >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)


def _is_notable(prev_oi: int, curr_oi: int) -> tuple[bool, float]:
    """Return (notable, delta_pct). delta_pct is 0.0 when prev_oi == 0."""
    delta = curr_oi - prev_oi
    abs_delta = abs(delta)
    if abs_delta < NOTABLE_ABSOLUTE:
        return False, 0.0
    if prev_oi <= 0:
        # Zero-guard: can't compute %, so absolute alone decides.
        return True, 0.0
    pct = abs_delta / prev_oi
    if pct < NOTABLE_PCT:
        return False, pct
    # delta_pct keeps the sign of delta for the label.
    signed = (curr_oi - prev_oi) / prev_oi
    return True, signed


def diff_oi(rows: Iterable[dict], spot: Optional[float]) -> list[OiChange]:
    """Apply notability gates to a list of UW oi-change rows.

    Each row is expected to have at minimum:
        strike, call_oi (or call.curr_oi), put_oi (or put.curr_oi),
        prev_call_oi, prev_put_oi  (UW's exact field names vary by version)
    We tolerate either flat or nested shapes.
    """
    if spot is None or spot <= 0:
        return []  # Without spot we cannot apply the ±5% gate.

    out: list[OiChange] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        strike = _coerce_float(r.get("strike"))
        if strike is None:
            continue
        # ±5% spot gate
        if abs(strike - spot) / spot > NEAR_SPOT_PCT:
            continue

        for side in ("call", "put"):
            nested = r.get(side) if isinstance(r.get(side), dict) else None
            # Use explicit None checks instead of `or` chains so a literal 0
            # OI value isn't silently treated as missing.
            curr_candidates = [
                r.get(f"{side}_oi"),
                r.get(f"curr_{side}_oi"),
                nested.get("curr_oi") if nested else None,
            ]
            prev_candidates = [
                r.get(f"prev_{side}_oi"),
                nested.get("prev_oi") if nested else None,
            ]
            curr = next((c for c in curr_candidates if c is not None), None)
            prev = next((c for c in prev_candidates if c is not None), None)
            curr = _coerce_int(curr)
            prev = _coerce_int(prev)
            if curr is None or prev is None:
                continue
            notable, delta_pct = _is_notable(prev, curr)
            if not notable:
                continue
            delta = curr - prev
            sign = "+" if delta > 0 else ""
            label = (
                f"{sign}{_format_count(delta)} {side}s @ ${strike:g}"
                f"{f' ({sign}{int(delta_pct * 100)}%)' if delta_pct != 0 else ''}"
            )
            out.append(
                OiChange(
                    strike=strike,
                    side=side,  # type: ignore[arg-type]
                    prev_oi=prev,
                    curr_oi=curr,
                    delta=delta,
                    delta_pct=delta_pct,
                    label=label,
                )
            )
    # Strongest first.
    out.sort(key=lambda c: abs(c.delta), reverse=True)
    return out


async def fetch_and_diff(uw_client, ticker: str, spot: Optional[float]) -> list[OiChange]:
    """Async wrapper that hits UW and applies the notability gates.

    Kept thin so callers can swap in fixtures via `diff_oi` directly.
    """
    import asyncio

    def _call():
        return uw_client.get_stock_oi_change(ticker)

    try:
        resp = await asyncio.to_thread(_call)
    except Exception as exc:  # noqa: BLE001
        logger.debug("oi-change fetch failed for %s: %s", ticker, exc)
        return []
    rows = resp.get("data") if isinstance(resp, dict) else None
    if not isinstance(rows, list):
        return []
    return diff_oi(rows, spot)
