"""
Regression tests for collapse_positions() grouping behavior.

Bug (2026-04-27, user report): two SHORT puts on QQQ at the same expiry but
different strikes ($585 + $595) were collapsed into a fake "Combo (2 legs)"
row labelled "Other" by the frontend catalog. The user lost row-level order
entry on the legs.

Root cause: collapse_positions() groups by (symbol, expiry) only, then calls
detect_structure_type(). Two short puts skip every recognized branch
(vertical wants 1 long + 1 short, all-long wants every leg long) and fall
through to the default "Combo (N legs)" / "complex" label. The frontend
catalog has no entry for that label → "other" bucket.

Fix: when len(legs) == 2 AND detect_structure_type returns the fall-through
label, split the group back into single-leg positions. Recognized structures
(verticals, straddles, synthetics, covered calls) still collapse as today.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from xenon.execution.ib_sync import collapse_positions


def _short_put(symbol: str, expiry: str, strike: float, entry_cost: float, market_value: float):
    return {
        "symbol": symbol,
        "expiry": expiry,
        "secType": "OPT",
        "right": "P",
        "strike": strike,
        "position": -1,
        "entry_cost": entry_cost,
        "avgCost": abs(entry_cost),
        "marketValue": market_value,
        "marketPrice": abs(market_value) / 100.0,
        "marketPriceIsCalculated": False,
        "structure": f"Short Put ${strike}",
    }


def test_two_short_puts_same_expiry_stay_separate():
    """QQQ SHORT 1x Put $585 + SHORT 1x Put $595 (same expiry) must NOT collapse.

    They are not a recognized structure (no vertical: both are short, not 1L+1S;
    no all-long combo: both are short). Without the fix, detect_structure_type
    falls through to "Combo (2 legs)" and the user loses per-leg order entry.
    """
    positions = [
        _short_put("QQQ", "20260522", 585.0, -858.93, -164.0),
        _short_put("QQQ", "20260522", 595.0, -1268.28, -211.0),
    ]

    out = collapse_positions(positions)

    assert len(out) == 2, f"expected 2 separate rows, got {len(out)}: {out}"
    for p in out:
        assert "Combo" not in p["structure_type"], f"position should not be wrapped in a Combo: {p['structure_type']}"
    structure_types = sorted(p["structure_type"] for p in out)
    assert structure_types == ["Short Put", "Short Put"], (
        f"each leg should be labelled 'Short Put', got {structure_types}"
    )


def test_recognized_combos_still_collapse():
    """Guardrail: a vertical (1 long + 1 short, same type, opposite directions)
    must STILL collapse to one row. Proves the fix doesn't widen the predicate
    too far.
    """
    positions = [
        {
            "symbol": "AAPL",
            "expiry": "20260620",
            "secType": "OPT",
            "right": "C",
            "strike": 200.0,
            "position": 1,
            "entry_cost": 470.0,
            "avgCost": 470.0,
            "marketValue": 480.0,
            "marketPrice": 4.80,
            "marketPriceIsCalculated": False,
            "structure": "Long Call $200",
        },
        {
            "symbol": "AAPL",
            "expiry": "20260620",
            "secType": "OPT",
            "right": "C",
            "strike": 210.0,
            "position": -1,
            "entry_cost": -220.0,
            "avgCost": 220.0,
            "marketValue": -210.0,
            "marketPrice": 2.10,
            "marketPriceIsCalculated": False,
            "structure": "Short Call $210",
        },
    ]

    out = collapse_positions(positions)

    assert len(out) == 1, f"vertical must collapse to one combo row, got {out}"
    assert "Bull Call Spread" in out[0]["structure_type"], f"expected Bull Call Spread, got {out[0]['structure_type']}"
