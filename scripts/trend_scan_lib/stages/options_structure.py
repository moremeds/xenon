"""Stage B: Options structure scoring — dealer positioning and gamma context."""

from __future__ import annotations

from scripts.scanner_lib.scoring import normalize_score

STRUCTURE_WEIGHTS = {
    "gamma_flip": 0.25,
    "net_gex": 0.15,
    "call_wall": 0.15,
    "put_wall": 0.10,
    "max_pain": 0.15,
    "oi_change": 0.20,
}
PINNING_GEX_THRESHOLD = 1_000_000
PINNING_SPOT_PCT = 0.005
OVERHEAD_WALL_PCT_ABOVE = 0.02  # call wall within 2% above spot
SUPPORTIVE_PUT_PCT_BELOW = 0.03  # put wall within 3% below spot counts as support


def score_gamma_flip(*, spot: float, gamma_flip: float, direction: str = "bullish") -> float:
    """Bullish: reward spot ABOVE flip (short-gamma = reflexive uptrend).
    Bearish: reward spot BELOW flip (short-gamma = reflexive downtrend)."""
    if gamma_flip == 0:
        return 0.5
    if direction == "bearish":
        if spot < gamma_flip:
            return 1.0
        if spot == gamma_flip:
            return 0.5
        return 0.2
    if spot > gamma_flip:
        return 1.0
    if spot == gamma_flip:
        return 0.5
    return 0.2


def score_call_wall_distance(*, spot: float, call_wall: float) -> float:
    if call_wall == 0 or spot == 0:
        return 0.5
    pct_away = (call_wall - spot) / spot
    if pct_away >= 0.05:
        return 1.0
    if pct_away >= 0.03:
        return 0.7
    if pct_away >= 0.02:
        return 0.5
    return normalize_score(pct_away * 20)


def score_put_wall_support(*, spot: float, put_wall: float) -> float:
    if put_wall == 0 or spot == 0:
        return 0.5
    pct_below = (spot - put_wall) / spot
    if pct_below <= 0.03:
        return 1.0
    if pct_below <= 0.05:
        return 0.7
    if pct_below <= 0.08:
        return 0.4
    return 0.2


def score_max_pain(*, spot: float, max_pain: float) -> float:
    if max_pain == 0 or spot == 0:
        return 0.5
    pct_diff = (spot - max_pain) / spot
    if pct_diff > 0.03:
        return 1.0
    if pct_diff > 0.01:
        return 0.7
    if abs(pct_diff) <= 0.01:
        return 0.4
    return 0.2


def score_oi_change(
    *,
    net_call_oi_change: float,
    net_put_oi_change: float,
    direction: str = "bullish",
) -> float:
    """Bullish: reward call OI additions + put OI removals.
    Bearish: reward put OI additions + call OI removals.

    Preserves the original 4-tier step function; direction swaps the
    role of the two OI inputs so the bearish mirror scores the flipped
    signal identically."""
    if direction == "bearish":
        net_call_oi_change, net_put_oi_change = net_put_oi_change, net_call_oi_change
    if net_call_oi_change > 0 and net_put_oi_change <= 0:
        return 1.0
    if net_call_oi_change > 0 and net_put_oi_change > 0:
        return 0.6
    if net_call_oi_change <= 0 and net_put_oi_change <= 0:
        return 0.5
    return 0.2


def score_net_gex(*, net_gex: float) -> float:
    if net_gex >= 500_000:
        return 1.0
    if net_gex > 100_000:
        return 0.7
    if net_gex > 0:
        return 0.5
    if net_gex > -100_000:
        return 0.3
    return 0.1


def is_severely_pinned(
    *,
    spot: float,
    max_pain: float,
    gex_at_spot: float,
    spot_pct_threshold: float = PINNING_SPOT_PCT,
) -> bool:
    if max_pain == 0 or spot == 0:
        return False
    within_range = abs(spot - max_pain) / spot <= spot_pct_threshold
    high_gex = gex_at_spot >= PINNING_GEX_THRESHOLD
    return within_range and high_gex


def has_unsupported_overhead_wall(
    *,
    spot: float,
    call_wall: float,
    put_wall: float,
) -> bool:
    """True iff a call wall sits close above spot with no meaningful put
    wall below. This is the second hard-fail case in Stage B structure
    (the first being severe pinning)."""
    if spot <= 0 or call_wall <= 0:
        return False
    call_overhead = (call_wall - spot) / spot
    if not (0 < call_overhead <= OVERHEAD_WALL_PCT_ABOVE):
        return False
    # Check for supportive put wall
    if put_wall > 0:
        put_support = (spot - put_wall) / spot
        if 0 < put_support <= SUPPORTIVE_PUT_PCT_BELOW:
            return False  # supported — not a hard reject
    return True


def has_unsupported_underhead_wall(*, spot: float, call_wall: float, put_wall: float) -> bool:
    """Bearish mirror: put wall within 2% below spot with no meaningful
    call resistance above means breakdown has no clean path."""
    if spot <= 0 or put_wall <= 0:
        return False
    put_underhead = (spot - put_wall) / spot
    if not (0 < put_underhead <= OVERHEAD_WALL_PCT_ABOVE):
        return False
    if call_wall > 0:
        call_resistance = (call_wall - spot) / spot
        if 0 < call_resistance <= SUPPORTIVE_PUT_PCT_BELOW:
            return False
    return True


def compute_structure_score(data: dict, *, direction: str = "bullish") -> tuple[float, bool]:
    spot = data.get("spot", 0)
    max_pain = data.get("max_pain", 0)
    gex_at_spot = data.get("gex_at_spot", 0)

    if is_severely_pinned(spot=spot, max_pain=max_pain, gex_at_spot=gex_at_spot):
        return 0.0, True

    if direction == "bullish":
        if has_unsupported_overhead_wall(
            spot=spot,
            call_wall=data.get("call_wall", 0),
            put_wall=data.get("put_wall", 0),
        ):
            return 0.0, True
    else:
        if has_unsupported_underhead_wall(
            spot=spot,
            call_wall=data.get("call_wall", 0),
            put_wall=data.get("put_wall", 0),
        ):
            return 0.0, True

    scores = {
        "gamma_flip": score_gamma_flip(spot=spot, gamma_flip=data.get("gamma_flip", 0), direction=direction),
        "net_gex": score_net_gex(net_gex=data.get("net_gex", 0)),
        "call_wall": score_call_wall_distance(spot=spot, call_wall=data.get("call_wall", 0)),
        "put_wall": score_put_wall_support(spot=spot, put_wall=data.get("put_wall", 0)),
        "max_pain": score_max_pain(spot=spot, max_pain=max_pain),
        "oi_change": score_oi_change(
            net_call_oi_change=data.get("net_call_oi_change", 0),
            net_put_oi_change=data.get("net_put_oi_change", 0),
            direction=direction,
        ),
    }
    composite = sum(scores[k] * w for k, w in STRUCTURE_WEIGHTS.items())
    return normalize_score(composite), False
