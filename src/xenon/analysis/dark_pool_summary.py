"""Shared dark-pool flow summarizer.

Consumes a `flow_data` dict shaped like `scripts/fetch_flow.fetch_flow()` output
and returns scored signal metrics. Extracted from `scripts/scanner.analyze_signal`
so that both /flow-analysis and /uw-analyze pipelines can share the same
scoring path (and so the options-conflict penalty stops silently drifting).

Bug fixes applied during extraction (see docs/plans/flickering-puzzling-sifakis.md):
  - Read `options_flow["bias"]` (fetch_flow emits `bias`, not `combined_bias`)
  - Extend bias_map to cover all 5 states that fetch_flow emits
    (STRONGLY_BULLISH/BULLISH/BEARISH/STRONGLY_BEARISH). Previously only
    the LEAN_* variants were mapped, so strongest conflicts never fired.
"""

from __future__ import annotations

# Map options-flow bias (five-state) -> expected dark-pool direction.
# If the expected direction disagrees with observed dark-pool direction,
# apply a -25 score penalty. Tests: scripts/tests/test_dark_pool_summary.py
_BIAS_TO_EXPECTED_DP_DIRECTION = {
    "STRONGLY_BULLISH": "ACCUMULATION",
    "BULLISH": "ACCUMULATION",
    "STRONGLY_BEARISH": "DISTRIBUTION",
    "BEARISH": "DISTRIBUTION",
}


def summarize_dark_pool(flow_data: dict) -> dict:
    """Extract key scored metrics from a fetch_flow() payload.

    Returns a dict with:
        score, signal (STRONG|MODERATE|WEAK|NONE|ERROR),
        direction, strength, buy_ratio, options_conflict,
        num_prints, sustained_days, recent_direction, recent_strength
    """
    if "error" in flow_data:
        return {"score": -1, "signal": "ERROR", "error": flow_data["error"]}

    dp = flow_data.get("dark_pool", {}) or {}
    agg = dp.get("aggregate", {}) or {}
    daily = dp.get("daily", []) or []

    direction = agg.get("flow_direction", "UNKNOWN")
    strength = agg.get("flow_strength", 0) or 0
    buy_ratio = agg.get("dp_buy_ratio")
    num_prints = agg.get("num_prints", 0) or 0

    # Sustained direction: how many consecutive days after the most recent
    # agree with the most recent day's direction. `sustained` counts the
    # *additional* days beyond the most recent one (same semantic as
    # the original scanner.analyze_signal).
    sustained = 0
    if daily:
        current_dir = daily[0].get("flow_direction")
        for d in daily[1:]:
            if d.get("flow_direction") == current_dir and current_dir in (
                "ACCUMULATION",
                "DISTRIBUTION",
            ):
                sustained += 1
            else:
                break

    recent_dir = daily[0].get("flow_direction") if daily else "UNKNOWN"
    recent_strength = daily[0].get("flow_strength", 0) if daily else 0

    # Base score from aggregate strength (0..100)
    score = float(strength)

    # Sustained-direction bonuses (two-tier)
    if sustained >= 2:
        score += 20
    if sustained >= 4:
        score += 20

    # Recent-day confirmation bonus
    if recent_dir == direction and recent_strength > 50:
        score += 15

    # Recent-day contradiction penalty
    if recent_dir != direction and recent_dir in ("ACCUMULATION", "DISTRIBUTION"):
        score -= 30

    # Low-print-count statistical-reliability penalties
    if num_prints < 50:
        score -= 20
    elif num_prints < 100:
        score -= 10

    # Options-flow conflict penalty — BUG FIX CONSOLIDATION POINT.
    # Previously the field name mismatch + incomplete enum map meant this
    # branch was effectively dead. The shared summarizer is the single
    # source of truth so the drift cannot recur.
    options_conflict = False
    options_flow = flow_data.get("options_flow", {}) or {}
    bias = options_flow.get("bias", "NO_DATA")
    expected_dp = _BIAS_TO_EXPECTED_DP_DIRECTION.get(bias)
    if expected_dp and expected_dp != direction:
        options_conflict = True
        score -= 25

    # Signal bucket classification
    if score >= 60 and direction in ("ACCUMULATION", "DISTRIBUTION"):
        signal = "STRONG"
    elif score >= 40 and direction in ("ACCUMULATION", "DISTRIBUTION"):
        signal = "MODERATE"
    elif direction in ("ACCUMULATION", "DISTRIBUTION"):
        signal = "WEAK"
    else:
        signal = "NONE"

    return {
        "score": round(score, 1),
        "signal": signal,
        "direction": direction,
        "strength": strength,
        "buy_ratio": buy_ratio,
        "options_conflict": options_conflict,
        "num_prints": num_prints,
        "sustained_days": sustained + 1 if sustained > 0 else 0,
        "recent_direction": recent_dir,
        "recent_strength": recent_strength,
    }
