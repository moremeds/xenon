# Sub-Plan 3: Stages B + C + Ranking

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Stage B (options structure + volatility scoring), Stage C (flow confirmation), trade type suggestion, and final composite ranking with min threshold gates. (v1: bullish trade suggestions only.)

**Architecture:** Three independent stage scorers feed into a ranking module that computes weighted composite scores and enforces minimum threshold gates. Each stage scorer takes ticker data from UWClient and returns a 0-1 score.

**Tech Stack:** Python 3.14, pytest, UWClient

**Spec:** `docs/superpowers/specs/2026-04-10-trend-scanner-design.md` (Stages B, C, Final Ranking)

**Depends on:** Sub-Plan 1 (scanner_lib) and Sub-Plan 2 (models, config) must be complete.

---

## File Structure

```
scripts/trend_scan_lib/
├── stages/
│   ├── options_structure.py     # CREATE — Stage B: dealer/gamma structure (25%)
│   ├── volatility.py            # CREATE — Stage B addon: IV state (20%)
│   └── flow_confirmation.py     # CREATE — Stage C: flow confirmation (20%)
└── ranking.py                   # CREATE — composite score + gates + trade suggestion

scripts/tests/
├── test_options_structure.py    # CREATE
├── test_volatility.py           # CREATE
├── test_flow_confirmation.py    # CREATE
└── test_trend_ranking.py        # CREATE
```

---

### Task 1: Options Structure Scorer (`stages/options_structure.py`)

**Files:**

- Create: `scripts/trend_scan_lib/stages/options_structure.py`
- Test: `scripts/tests/test_options_structure.py`

- [ ] **Step 1: Write failing tests**

```python
# scripts/tests/test_options_structure.py
"""Tests for Stage B options structure scoring."""
from __future__ import annotations

import pytest


# --- Individual component scorers ---

def test_score_gamma_flip_above():
    from scripts.trend_scan_lib.stages.options_structure import score_gamma_flip

    assert score_gamma_flip(spot=150, gamma_flip=145) == 1.0


def test_score_gamma_flip_at():
    from scripts.trend_scan_lib.stages.options_structure import score_gamma_flip

    assert score_gamma_flip(spot=145, gamma_flip=145) == 0.5


def test_score_gamma_flip_below():
    from scripts.trend_scan_lib.stages.options_structure import score_gamma_flip

    assert score_gamma_flip(spot=140, gamma_flip=145) == 0.2


def test_score_gamma_flip_zero():
    from scripts.trend_scan_lib.stages.options_structure import score_gamma_flip

    # No gamma flip data
    assert score_gamma_flip(spot=150, gamma_flip=0) == 0.5


def test_score_call_wall_far():
    from scripts.trend_scan_lib.stages.options_structure import score_call_wall_distance

    # Call wall 10% above spot → plenty of room
    assert score_call_wall_distance(spot=100, call_wall=110) == 1.0


def test_score_call_wall_close():
    from scripts.trend_scan_lib.stages.options_structure import score_call_wall_distance

    # Call wall 1% above spot → capped
    assert score_call_wall_distance(spot=100, call_wall=101) < 0.4


def test_score_call_wall_zero():
    from scripts.trend_scan_lib.stages.options_structure import score_call_wall_distance

    assert score_call_wall_distance(spot=100, call_wall=0) == 0.5


def test_score_put_wall_nearby():
    from scripts.trend_scan_lib.stages.options_structure import score_put_wall_support

    # Put wall 2% below → strong support floor
    assert score_put_wall_support(spot=100, put_wall=98) > 0.7


def test_score_put_wall_far():
    from scripts.trend_scan_lib.stages.options_structure import score_put_wall_support

    # Put wall 10% below → weak support
    assert score_put_wall_support(spot=100, put_wall=90) < 0.4


def test_score_max_pain_above():
    from scripts.trend_scan_lib.stages.options_structure import score_max_pain

    # Spot above max pain → favorable
    assert score_max_pain(spot=150, max_pain=145) > 0.7


def test_score_max_pain_pinned():
    from scripts.trend_scan_lib.stages.options_structure import score_max_pain

    # Spot at max pain → pinning penalty
    result = score_max_pain(spot=145, max_pain=145)
    assert 0.3 <= result <= 0.5


def test_score_oi_change_bullish():
    from scripts.trend_scan_lib.stages.options_structure import score_oi_change

    assert score_oi_change(net_call_oi_change=5000, net_put_oi_change=-2000) == 1.0


def test_score_oi_change_bearish():
    from scripts.trend_scan_lib.stages.options_structure import score_oi_change

    assert score_oi_change(net_call_oi_change=-3000, net_put_oi_change=5000) < 0.3


def test_score_net_gex_positive():
    from scripts.trend_scan_lib.stages.options_structure import score_net_gex

    assert score_net_gex(net_gex=500_000) > 0.7


def test_score_net_gex_negative():
    from scripts.trend_scan_lib.stages.options_structure import score_net_gex

    assert score_net_gex(net_gex=-500_000) < 0.3


# --- Pinning reject ---

def test_pinning_reject_severe():
    from scripts.trend_scan_lib.stages.options_structure import is_severely_pinned

    assert is_severely_pinned(spot=100, max_pain=100.3, gex_at_spot=1_000_000, spot_pct_threshold=0.005) is True


def test_pinning_reject_not_pinned():
    from scripts.trend_scan_lib.stages.options_structure import is_severely_pinned

    assert is_severely_pinned(spot=105, max_pain=100, gex_at_spot=100_000, spot_pct_threshold=0.005) is False


# --- Composite structure score ---

def test_compute_structure_score_bullish():
    from scripts.trend_scan_lib.stages.options_structure import compute_structure_score

    data = {
        "spot": 150, "gamma_flip": 145, "call_wall": 165,
        "put_wall": 146, "max_pain": 148, "net_gex": 200_000,
        "net_call_oi_change": 3000, "net_put_oi_change": -1000,
        "gex_at_spot": 50_000,
    }
    score, rejected = compute_structure_score(data)
    assert not rejected
    assert score > 0.6


def test_compute_structure_score_rejected_pinning():
    from scripts.trend_scan_lib.stages.options_structure import compute_structure_score

    data = {
        "spot": 100, "gamma_flip": 95, "call_wall": 110,
        "put_wall": 95, "max_pain": 100.2, "net_gex": 100_000,
        "net_call_oi_change": 0, "net_put_oi_change": 0,
        "gex_at_spot": 2_000_000,  # massive GEX at spot → pinned
    }
    score, rejected = compute_structure_score(data)
    assert rejected
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_options_structure.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement options structure scorer**

```python
# scripts/trend_scan_lib/stages/options_structure.py
"""Stage B: Options structure scoring — dealer positioning and gamma context."""
from __future__ import annotations

from scripts.scanner_lib.scoring import normalize_score

# Component weights within structure score
STRUCTURE_WEIGHTS = {
    "gamma_flip": 0.25,
    "net_gex": 0.15,
    "call_wall": 0.15,
    "put_wall": 0.10,
    "max_pain": 0.15,
    "oi_change": 0.20,
}

# Pinning thresholds
PINNING_GEX_THRESHOLD = 1_000_000
PINNING_SPOT_PCT = 0.005


def score_gamma_flip(*, spot: float, gamma_flip: float) -> float:
    """Score spot position relative to gamma flip level."""
    if gamma_flip == 0:
        return 0.5
    if spot > gamma_flip:
        return 1.0
    if spot == gamma_flip:
        return 0.5
    return 0.2


def score_call_wall_distance(*, spot: float, call_wall: float) -> float:
    """Score distance to call wall. More room = better."""
    if call_wall == 0 or spot == 0:
        return 0.5
    pct_away = (call_wall - spot) / spot
    if pct_away >= 0.05:
        return 1.0
    if pct_away >= 0.03:
        return 0.7
    if pct_away >= 0.02:
        return 0.5
    return normalize_score(pct_away * 20)  # linear 0-1 for <2%


def score_put_wall_support(*, spot: float, put_wall: float) -> float:
    """Score put wall as support. Nearby put wall = support floor."""
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
    """Score spot vs max pain. Above = favorable, at = pinning penalty."""
    if max_pain == 0 or spot == 0:
        return 0.5
    pct_diff = (spot - max_pain) / spot
    if pct_diff > 0.03:
        return 1.0
    if pct_diff > 0.01:
        return 0.7
    if abs(pct_diff) <= 0.01:
        return 0.4  # pinning zone
    return 0.2  # below max pain


def score_oi_change(*, net_call_oi_change: float, net_put_oi_change: float) -> float:
    """Score OI change direction. Rising calls on higher strikes = bullish."""
    if net_call_oi_change > 0 and net_put_oi_change <= 0:
        return 1.0
    if net_call_oi_change > 0 and net_put_oi_change > 0:
        return 0.6
    if net_call_oi_change <= 0 and net_put_oi_change <= 0:
        return 0.5
    return 0.2  # puts rising, calls falling


def score_net_gex(*, net_gex: float) -> float:
    """Score net GEX. Positive = supportive gamma."""
    if net_gex > 500_000:
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
    """Detect severe pinning: spot within threshold of max pain AND high GEX at spot."""
    if max_pain == 0 or spot == 0:
        return False
    within_range = abs(spot - max_pain) / spot <= spot_pct_threshold
    high_gex = gex_at_spot >= PINNING_GEX_THRESHOLD
    return within_range and high_gex


def compute_structure_score(data: dict) -> tuple[float, bool]:
    """Compute composite structure score. Returns (score, rejected)."""
    spot = data.get("spot", 0)
    max_pain = data.get("max_pain", 0)
    gex_at_spot = data.get("gex_at_spot", 0)

    # Check reject condition first
    if is_severely_pinned(spot=spot, max_pain=max_pain, gex_at_spot=gex_at_spot):
        return 0.0, True

    scores = {
        "gamma_flip": score_gamma_flip(spot=spot, gamma_flip=data.get("gamma_flip", 0)),
        "net_gex": score_net_gex(net_gex=data.get("net_gex", 0)),
        "call_wall": score_call_wall_distance(spot=spot, call_wall=data.get("call_wall", 0)),
        "put_wall": score_put_wall_support(spot=spot, put_wall=data.get("put_wall", 0)),
        "max_pain": score_max_pain(spot=spot, max_pain=max_pain),
        "oi_change": score_oi_change(
            net_call_oi_change=data.get("net_call_oi_change", 0),
            net_put_oi_change=data.get("net_put_oi_change", 0),
        ),
    }

    composite = sum(scores[k] * w for k, w in STRUCTURE_WEIGHTS.items())
    return normalize_score(composite), False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_options_structure.py -v`
Expected: 18 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/trend_scan_lib/stages/options_structure.py scripts/tests/test_options_structure.py
git commit -m "feat(trend_scan_lib): add Stage B options structure scorer"
```

---

### Task 2: Volatility Scorer (`stages/volatility.py`)

**Files:**

- Create: `scripts/trend_scan_lib/stages/volatility.py`
- Test: `scripts/tests/test_volatility.py`

- [ ] **Step 1: Write failing tests**

```python
# scripts/tests/test_volatility.py
"""Tests for Stage B volatility state scoring."""
from __future__ import annotations

import pytest


def test_score_iv_rank_low():
    from scripts.trend_scan_lib.stages.volatility import score_iv_rank

    assert score_iv_rank(20) == 1.0


def test_score_iv_rank_moderate():
    from scripts.trend_scan_lib.stages.volatility import score_iv_rank

    assert score_iv_rank(40) == 0.7


def test_score_iv_rank_high():
    from scripts.trend_scan_lib.stages.volatility import score_iv_rank

    assert score_iv_rank(60) == 0.4


def test_score_iv_rank_extreme():
    from scripts.trend_scan_lib.stages.volatility import score_iv_rank

    assert score_iv_rank(85) == 0.2


def test_score_term_structure_normal():
    from scripts.trend_scan_lib.stages.volatility import score_term_structure

    assert score_term_structure("normal") == 1.0


def test_score_term_structure_flat():
    from scripts.trend_scan_lib.stages.volatility import score_term_structure

    assert score_term_structure("flat") == 0.6


def test_score_term_structure_inverted():
    from scripts.trend_scan_lib.stages.volatility import score_term_structure

    assert score_term_structure("inverted") == 0.3


def test_score_iv_rv_ratio_cheap():
    from scripts.trend_scan_lib.stages.volatility import score_iv_rv_ratio

    # IV below RV → options cheap
    assert score_iv_rv_ratio(0.9) > 0.7


def test_score_iv_rv_ratio_expensive():
    from scripts.trend_scan_lib.stages.volatility import score_iv_rv_ratio

    assert score_iv_rv_ratio(1.5) < 0.4


def test_score_iv_rv_ratio_zero_rv():
    from scripts.trend_scan_lib.stages.volatility import score_iv_rv_ratio

    assert score_iv_rv_ratio(0) == 0.5


def test_compute_vol_score():
    from scripts.trend_scan_lib.stages.volatility import compute_vol_score

    data = {
        "iv_rank": 22,
        "term_structure": "normal",
        "iv_rv_ratio": 0.94,
    }
    score, flags = compute_vol_score(data)
    assert score > 0.7
    assert flags == []


def test_compute_vol_score_event_flag():
    from scripts.trend_scan_lib.stages.volatility import compute_vol_score

    data = {
        "iv_rank": 65,
        "term_structure": "inverted",
        "iv_rv_ratio": 1.3,
        "earnings_days": 5,
    }
    score, flags = compute_vol_score(data)
    assert score < 0.5
    assert "event_premium" in flags


def test_suggest_trade_type_cheap():
    from scripts.trend_scan_lib.stages.volatility import suggest_trade_type

    assert suggest_trade_type(iv_rank=20, term_structure="normal", capped=False) == "debit_call"


def test_suggest_trade_type_moderate():
    from scripts.trend_scan_lib.stages.volatility import suggest_trade_type

    assert suggest_trade_type(iv_rank=45, term_structure="normal", capped=True) == "call_spread"


def test_suggest_trade_type_expensive():
    from scripts.trend_scan_lib.stages.volatility import suggest_trade_type

    assert suggest_trade_type(iv_rank=70, term_structure="inverted", capped=True) == "premium_sell"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_volatility.py -v`
Expected: FAIL

- [ ] **Step 3: Implement volatility scorer**

```python
# scripts/trend_scan_lib/stages/volatility.py
"""Stage B addon: Volatility state scoring and trade type suggestion."""
from __future__ import annotations

from scripts.scanner_lib.scoring import normalize_score

VOL_WEIGHTS = {
    "iv_rank": 0.40,
    "term_structure": 0.30,
    "iv_rv_ratio": 0.30,
}


def score_iv_rank(iv_rank: float) -> float:
    """Score IV rank. Lower = cheaper options = better for debit trades."""
    if iv_rank < 30:
        return 1.0
    if iv_rank < 50:
        return 0.7
    if iv_rank < 75:
        return 0.4
    return 0.2


def score_term_structure(shape: str | None) -> float:
    """Score term structure shape. Normal = best for swing trades."""
    if not shape or not isinstance(shape, str):
        return 0.5
    shapes = {"normal": 1.0, "flat": 0.6, "inverted": 0.3}
    return shapes.get(shape.lower(), 0.5)


def score_iv_rv_ratio(ratio: float) -> float:
    """Score IV/RV ratio. Below 1.0 = options underpriced relative to realized."""
    if ratio == 0:
        return 0.5
    if ratio <= 0.8:
        return 1.0
    if ratio <= 1.0:
        return 0.8
    if ratio <= 1.2:
        return 0.5
    if ratio <= 1.5:
        return 0.3
    return 0.1


def compute_vol_score(data: dict) -> tuple[float, list[str]]:
    """Compute composite volatility score. Returns (score, flags)."""
    flags: list[str] = []

    scores = {
        "iv_rank": score_iv_rank(data.get("iv_rank", 50)),
        "term_structure": score_term_structure(data.get("term_structure", "flat")),
        "iv_rv_ratio": score_iv_rv_ratio(data.get("iv_rv_ratio", 1.0)),
    }

    # Event premium detection
    earnings_days = data.get("earnings_days")
    if earnings_days is not None and earnings_days <= 14:
        if data.get("iv_rank", 0) >= 50 or data.get("term_structure") == "inverted":
            flags.append("event_premium")

    composite = sum(scores[k] * w for k, w in VOL_WEIGHTS.items())
    return normalize_score(composite), flags


def suggest_trade_type(
    *,
    iv_rank: float,
    term_structure: str,
    capped: bool,
) -> str:
    """Suggest trade expression based on volatility state and structure."""
    ts = term_structure.lower() if isinstance(term_structure, str) else "flat"
    if iv_rank >= 60 and capped:
        return "premium_sell"
    if iv_rank >= 30 and capped:
        return "call_spread"
    if iv_rank < 30 and ts == "normal":
        return "debit_call"
    if iv_rank < 30:
        return "debit_call"
    return "call_spread"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_volatility.py -v`
Expected: 15 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/trend_scan_lib/stages/volatility.py scripts/tests/test_volatility.py
git commit -m "feat(trend_scan_lib): add volatility scorer and trade type suggestion"
```

---

### Task 3: Flow Confirmation Scorer (`stages/flow_confirmation.py`)

**Files:**

- Create: `scripts/trend_scan_lib/stages/flow_confirmation.py`
- Test: `scripts/tests/test_flow_confirmation.py`

- [ ] **Step 1: Write failing tests**

```python
# scripts/tests/test_flow_confirmation.py
"""Tests for Stage C flow confirmation scoring."""
from __future__ import annotations

import pytest


def test_score_ask_dominance_high():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_ask_dominance

    assert score_ask_dominance(0.85) == 1.0


def test_score_ask_dominance_moderate():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_ask_dominance

    assert score_ask_dominance(0.65) == 0.7


def test_score_ask_dominance_low():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_ask_dominance

    assert score_ask_dominance(0.45) == 0.2


def test_score_flow_repetition_multiple():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_flow_repetition

    assert score_flow_repetition(5) == 1.0


def test_score_flow_repetition_single():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_flow_repetition

    assert score_flow_repetition(1) == 0.2


def test_score_flow_repetition_zero():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_flow_repetition

    assert score_flow_repetition(0) == 0.0


def test_score_expiry_clustering_tight():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_expiry_clustering

    # Most flow in 1-4 week window
    assert score_expiry_clustering(cluster_ratio=0.8) == 1.0


def test_score_expiry_clustering_scattered():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_expiry_clustering

    assert score_expiry_clustering(cluster_ratio=0.3) == 0.4


def test_score_strike_reasonableness_near():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_strike_reasonableness

    # Avg strike within 5% of spot
    assert score_strike_reasonableness(avg_strike_pct_otm=0.05) == 1.0


def test_score_strike_reasonableness_far():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_strike_reasonableness

    assert score_strike_reasonableness(avg_strike_pct_otm=0.20) == 0.2


def test_score_delta_vega_positive():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_delta_vega_flow

    assert score_delta_vega_flow(net_delta=50_000, net_vega=30_000) == 1.0


def test_score_delta_vega_contradictory():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_delta_vega_flow

    assert score_delta_vega_flow(net_delta=-20_000, net_vega=-10_000) == 0.1


def test_score_dark_pool_bullish_bonus():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_dark_pool_alignment

    assert score_dark_pool_alignment(dp_direction="bullish") == 0.15


def test_score_dark_pool_none():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_dark_pool_alignment

    assert score_dark_pool_alignment(dp_direction="neutral") == 0.0


def test_compute_flow_score_strong():
    from scripts.trend_scan_lib.stages.flow_confirmation import compute_flow_score

    data = {
        "ask_dominance": 0.85,
        "flow_count": 5,
        "expiry_cluster_ratio": 0.8,
        "avg_strike_pct_otm": 0.04,
        "net_delta": 40_000,
        "net_vega": 20_000,
        "dp_direction": "bullish",
    }
    score = compute_flow_score(data)
    assert score > 0.8


def test_compute_flow_score_weak():
    from scripts.trend_scan_lib.stages.flow_confirmation import compute_flow_score

    data = {
        "ask_dominance": 0.40,
        "flow_count": 1,
        "expiry_cluster_ratio": 0.2,
        "avg_strike_pct_otm": 0.25,
        "net_delta": -5_000,
        "net_vega": -3_000,
        "dp_direction": "neutral",
    }
    score = compute_flow_score(data)
    assert score < 0.3


def test_compute_flow_score_no_data():
    from scripts.trend_scan_lib.stages.flow_confirmation import compute_flow_score

    score = compute_flow_score({})
    assert 0.0 <= score <= 0.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_flow_confirmation.py -v`
Expected: FAIL

- [ ] **Step 3: Implement flow confirmation scorer**

```python
# scripts/trend_scan_lib/stages/flow_confirmation.py
"""Stage C: Flow confirmation scoring — institutional participation alignment."""
from __future__ import annotations

from scripts.scanner_lib.scoring import normalize_score

FLOW_WEIGHTS = {
    "ask_dominance": 0.20,
    "flow_repetition": 0.25,
    "expiry_clustering": 0.15,
    "strike_reasonableness": 0.15,
    "delta_vega": 0.25,
}


def score_ask_dominance(ratio: float) -> float:
    """Score ask-side flow dominance. >80% = strong directional."""
    if ratio >= 0.80:
        return 1.0
    if ratio >= 0.60:
        return 0.7
    if ratio >= 0.50:
        return 0.5
    return 0.2


def score_flow_repetition(count: int) -> float:
    """Score number of distinct flow prints. Multiple = conviction."""
    if count >= 3:
        return 1.0
    if count == 2:
        return 0.6
    if count == 1:
        return 0.2
    return 0.0


def score_expiry_clustering(*, cluster_ratio: float) -> float:
    """Score flow clustering in 1-4 week expiry window."""
    if cluster_ratio >= 0.7:
        return 1.0
    if cluster_ratio >= 0.5:
        return 0.7
    if cluster_ratio >= 0.3:
        return 0.4
    return 0.2


def score_strike_reasonableness(*, avg_strike_pct_otm: float) -> float:
    """Score average strike distance from spot. Near money = better."""
    if avg_strike_pct_otm <= 0.05:
        return 1.0
    if avg_strike_pct_otm <= 0.10:
        return 0.7
    if avg_strike_pct_otm <= 0.15:
        return 0.4
    return 0.2


def score_delta_vega_flow(*, net_delta: float, net_vega: float) -> float:
    """Score net directional greek flow."""
    if net_delta > 0 and net_vega > 0:
        return 1.0
    if net_delta > 0:
        return 0.7
    if net_delta == 0 and net_vega == 0:
        return 0.5
    return 0.1


def score_dark_pool_alignment(*, dp_direction: str | None) -> float:
    """Dark pool alignment bonus. Returns 0 or 0.15."""
    if not dp_direction or not isinstance(dp_direction, str):
        return 0.0
    if dp_direction.lower() == "bullish":
        return 0.15
    if dp_direction.lower() == "bearish":
        return -0.05  # slight penalty for contradictory DP
    return 0.0


def compute_flow_score(data: dict) -> float:
    """Compute composite flow confirmation score."""
    scores = {
        "ask_dominance": score_ask_dominance(data.get("ask_dominance", 0.5)),
        "flow_repetition": score_flow_repetition(data.get("flow_count", 0)),
        "expiry_clustering": score_expiry_clustering(
            cluster_ratio=data.get("expiry_cluster_ratio", 0.5),
        ),
        "strike_reasonableness": score_strike_reasonableness(
            avg_strike_pct_otm=data.get("avg_strike_pct_otm", 0.10),
        ),
        "delta_vega": score_delta_vega_flow(
            net_delta=data.get("net_delta", 0),
            net_vega=data.get("net_vega", 0),
        ),
    }

    composite = sum(scores[k] * w for k, w in FLOW_WEIGHTS.items())
    dp_bonus = score_dark_pool_alignment(dp_direction=data.get("dp_direction", "neutral"))
    return normalize_score(composite + dp_bonus)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_flow_confirmation.py -v`
Expected: 17 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/trend_scan_lib/stages/flow_confirmation.py scripts/tests/test_flow_confirmation.py
git commit -m "feat(trend_scan_lib): add Stage C flow confirmation scorer"
```

---

### Task 4: Ranking Module (`trend_scan_lib/ranking.py`)

**Files:**

- Create: `scripts/trend_scan_lib/ranking.py`
- Test: `scripts/tests/test_trend_ranking.py`

- [ ] **Step 1: Write failing tests**

```python
# scripts/tests/test_trend_ranking.py
"""Tests for trend scanner ranking module."""
from __future__ import annotations

import pytest


def test_rank_by_final_score():
    from scripts.trend_scan_lib.models import TrendCandidate
    from scripts.trend_scan_lib.ranking import rank_candidates

    candidates = [
        TrendCandidate(ticker="AAPL", direction="bullish", final_score=0.7, scores={"trend": 0.8, "structure": 0.6}, spot_price=185),
        TrendCandidate(ticker="NVDA", direction="bullish", final_score=0.9, scores={"trend": 0.95, "structure": 0.8}, spot_price=148),
        TrendCandidate(ticker="GOOG", direction="bullish", final_score=0.5, scores={"trend": 0.6, "structure": 0.4}, spot_price=155),
    ]

    ranked = rank_candidates(candidates, top_n=3)
    assert [c.ticker for c in ranked] == ["NVDA", "AAPL", "GOOG"]


def test_rank_top_n_limit():
    from scripts.trend_scan_lib.models import TrendCandidate
    from scripts.trend_scan_lib.ranking import rank_candidates

    candidates = [
        TrendCandidate(ticker=f"T{i}", direction="bullish", final_score=i * 0.1, scores={"trend": 0.5}, spot_price=100)
        for i in range(10)
    ]
    ranked = rank_candidates(candidates, top_n=3)
    assert len(ranked) == 3
    assert ranked[0].ticker == "T9"


def test_apply_min_thresholds_filters():
    from scripts.trend_scan_lib.models import TrendCandidate
    from scripts.trend_scan_lib.ranking import apply_min_thresholds

    candidates = [
        TrendCandidate(ticker="GOOD", direction="bullish", final_score=0.8, scores={"trend": 0.6, "structure": 0.5}, spot_price=100),
        TrendCandidate(ticker="BAD_TREND", direction="bullish", final_score=0.7, scores={"trend": 0.35, "structure": 0.5}, spot_price=100),
        TrendCandidate(ticker="BAD_STRUCT", direction="bullish", final_score=0.6, scores={"trend": 0.5, "structure": 0.2}, spot_price=100),
    ]
    thresholds = {"trend": 0.4, "structure": 0.3}

    filtered = apply_min_thresholds(candidates, thresholds)
    assert len(filtered) == 1
    assert filtered[0].ticker == "GOOD"


def test_apply_min_thresholds_missing_score():
    from scripts.trend_scan_lib.models import TrendCandidate
    from scripts.trend_scan_lib.ranking import apply_min_thresholds

    # Candidate missing "structure" score entirely
    candidates = [
        TrendCandidate(ticker="MISSING", direction="bullish", final_score=0.8, scores={"trend": 0.8}, spot_price=100),
    ]
    thresholds = {"trend": 0.4, "structure": 0.3}

    filtered = apply_min_thresholds(candidates, thresholds)
    assert len(filtered) == 0


def test_compute_final_scores():
    from scripts.trend_scan_lib.ranking import compute_final_score

    scores = {"trend": 0.9, "structure": 0.7, "volatility": 0.6, "flow": 0.8}
    weights = {"trend": 0.35, "structure": 0.25, "volatility": 0.20, "flow": 0.20}

    result = compute_final_score(scores, weights)
    expected = (0.9 * 0.35) + (0.7 * 0.25) + (0.6 * 0.20) + (0.8 * 0.20)
    assert abs(result - expected) < 1e-9


def test_mixed_directions_ranked_together():
    from scripts.trend_scan_lib.models import TrendCandidate
    from scripts.trend_scan_lib.ranking import rank_candidates

    candidates = [
        TrendCandidate(ticker="BULL", direction="bullish", final_score=0.8, scores={"trend": 0.8}, spot_price=100),
        TrendCandidate(ticker="BEAR", direction="bearish", final_score=0.9, scores={"trend": 0.9}, spot_price=100),
    ]

    ranked = rank_candidates(candidates, top_n=10)
    assert ranked[0].ticker == "BEAR"
    assert ranked[0].direction == "bearish"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_trend_ranking.py -v`
Expected: FAIL

- [ ] **Step 3: Implement ranking module**

```python
# scripts/trend_scan_lib/ranking.py
"""Trend scanner ranking — composite scoring, min thresholds, final sort."""
from __future__ import annotations

from scripts.scanner_lib.scoring import passes_min_thresholds, weighted_composite
from scripts.trend_scan_lib.models import TrendCandidate


def compute_final_score(scores: dict[str, float], weights: dict[str, float]) -> float:
    """Compute weighted composite final score."""
    return weighted_composite(scores, weights)


def apply_min_thresholds(
    candidates: list[TrendCandidate],
    thresholds: dict[str, float],
) -> list[TrendCandidate]:
    """Filter candidates that don't meet minimum score thresholds."""
    return [c for c in candidates if passes_min_thresholds(c.scores, thresholds)]


def rank_candidates(
    candidates: list[TrendCandidate],
    *,
    top_n: int = 25,
) -> list[TrendCandidate]:
    """Sort candidates by final_score descending, return top N."""
    sorted_candidates = sorted(candidates, key=lambda c: (-c.final_score, c.ticker))
    return sorted_candidates[:top_n]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_trend_ranking.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/trend_scan_lib/ranking.py scripts/tests/test_trend_ranking.py
git commit -m "feat(trend_scan_lib): add ranking module with composite scoring and threshold gates"
```

---

### Task 5: Run Full Test Suite

- [ ] **Step 1: Run all Stage B/C/ranking tests**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_options_structure.py scripts/tests/test_volatility.py scripts/tests/test_flow_confirmation.py scripts/tests/test_trend_ranking.py -v`
Expected: 56 passed (18 + 15 + 17 + 6)

- [ ] **Step 2: Run all trend_scan_lib tests**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_trend_*.py scripts/tests/test_ta_*.py scripts/tests/test_options_*.py scripts/tests/test_volatility.py scripts/tests/test_flow_*.py -v`
Expected: All pass

- [ ] **Step 3: Run scanner_lib + uw_scan regression**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_scanner_lib*.py scripts/tests/test_uw_scan*.py -v`
Expected: All pass, no regressions
