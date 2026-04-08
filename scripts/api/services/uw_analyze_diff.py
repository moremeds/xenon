"""Pure diff engine for UW Analyze portfolio snapshots.

`compute_changes(prev, curr)` returns a list of `Change` objects describing
meaningful state transitions between two snapshots of the same ticker.

Design notes:
- Pure function. No I/O, no dependencies on cache/route layers.
- All rules are zero/null-guarded — a missing input means "skip the rule",
  never an exception or a synthetic baseline.
- Threshold values are constants at the top so tests can monkeypatch.

Spec: docs/superpowers/specs/2026-04-08-uw-analyze-overhaul-design.md
      §"Change-detection thresholds (Standard set)" + §"Zero / null guards"
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional

# ── Thresholds ──────────────────────────────────────────────────────────────
MAX_PAIN_SHIFT_FRAC = 0.02  # |Δ max_pain| / spot
IV_RANK_JUMP_PTS = 10.0  # |Δ iv_rank|
UNUSUAL_PREMIUM_USD = 5_000_000.0  # net call/put premium delta

ChangeCode = Literal[
    "GEX_FLIP_SIGN",
    "MAX_PAIN_SHIFT",
    "IV_RANK_JUMP",
    "UNUSUAL_CALL_SWEEP",
    "UNUSUAL_PUT_SWEEP",
]
Severity = Literal["info", "warn", "alert"]


@dataclass(frozen=True)
class Change:
    code: ChangeCode
    label: str
    prev: Any
    curr: Any
    severity: Severity

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "label": self.label,
            "prev": self.prev,
            "curr": self.curr,
            "severity": self.severity,
        }


def _g(snapshot: Optional[dict], key: str) -> Any:
    """Safe nested fetch into snapshot.derived."""
    if not snapshot:
        return None
    derived = snapshot.get("derived") if isinstance(snapshot, dict) else None
    if not isinstance(derived, dict):
        return None
    return derived.get(key)


def compute_changes(prev: Optional[dict], curr: Optional[dict]) -> list[Change]:
    """Return Changes for the prev → curr transition.

    Both inputs are UwSnapshot dicts (or None for first observation).
    Returns [] if `curr` is None or `prev` is None (no diff possible).
    """
    if not curr or not prev:
        return []

    changes: list[Change] = []

    # ── GEX_FLIP_SIGN ────────────────────────────────────────────────
    p_sign = _g(prev, "gex_sign")
    c_sign = _g(curr, "gex_sign")
    if p_sign in ("POSITIVE", "NEGATIVE") and c_sign in ("POSITIVE", "NEGATIVE") and p_sign != c_sign:
        changes.append(
            Change(
                code="GEX_FLIP_SIGN",
                label=f"GEX flipped {p_sign.lower()} → {c_sign.lower()}",
                prev=p_sign,
                curr=c_sign,
                severity="alert",
            )
        )

    # ── MAX_PAIN_SHIFT ───────────────────────────────────────────────
    p_mp = _g(prev, "max_pain")
    c_mp = _g(curr, "max_pain")
    c_spot = _g(curr, "spot")
    if (
        isinstance(p_mp, (int, float))
        and isinstance(c_mp, (int, float))
        and isinstance(c_spot, (int, float))
        and c_spot != 0
    ):
        delta = c_mp - p_mp
        frac = abs(delta) / abs(c_spot)
        if frac >= MAX_PAIN_SHIFT_FRAC:
            sign = "+" if delta > 0 else "-"
            changes.append(
                Change(
                    code="MAX_PAIN_SHIFT",
                    label=f"Max pain shifted {p_mp:.2f} → {c_mp:.2f} ({sign}{frac * 100:.1f}%)",
                    prev=p_mp,
                    curr=c_mp,
                    severity="warn",
                )
            )

    # ── IV_RANK_JUMP ─────────────────────────────────────────────────
    p_ivr = _g(prev, "iv_rank")
    c_ivr = _g(curr, "iv_rank")
    if isinstance(p_ivr, (int, float)) and isinstance(c_ivr, (int, float)):
        delta = c_ivr - p_ivr
        if abs(delta) >= IV_RANK_JUMP_PTS:
            sign = "+" if delta > 0 else ""
            changes.append(
                Change(
                    code="IV_RANK_JUMP",
                    label=f"IV rank {p_ivr:.0f} → {c_ivr:.0f} ({sign}{delta:.0f}pts)",
                    prev=p_ivr,
                    curr=c_ivr,
                    severity="warn",
                )
            )

    # ── UNUSUAL_CALL_SWEEP ───────────────────────────────────────────
    p_ncp = _g(prev, "net_call_premium")
    c_ncp = _g(curr, "net_call_premium")
    if isinstance(p_ncp, (int, float)) and isinstance(c_ncp, (int, float)):
        delta = c_ncp - p_ncp
        if delta >= UNUSUAL_PREMIUM_USD:
            changes.append(
                Change(
                    code="UNUSUAL_CALL_SWEEP",
                    label=f"Net call premium +${delta / 1e6:.1f}M",
                    prev=p_ncp,
                    curr=c_ncp,
                    severity="alert",
                )
            )

    # ── UNUSUAL_PUT_SWEEP ────────────────────────────────────────────
    p_npp = _g(prev, "net_put_premium")
    c_npp = _g(curr, "net_put_premium")
    if isinstance(p_npp, (int, float)) and isinstance(c_npp, (int, float)):
        delta = c_npp - p_npp
        if delta <= -UNUSUAL_PREMIUM_USD:
            changes.append(
                Change(
                    code="UNUSUAL_PUT_SWEEP",
                    label=f"Net put premium -${abs(delta) / 1e6:.1f}M",
                    prev=p_npp,
                    curr=c_npp,
                    severity="alert",
                )
            )

    return changes
