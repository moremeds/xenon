"""Risk guard account ratios (spec 2026-11-08 §5.2). Payload values are synthetic;
the payload shape follows ib_sync's collapsed positions + account_summary."""

from datetime import date, datetime, timedelta, timezone

import pytest

from xenon.api.services.risk_guard.account import account_view, drawdown, flow_adjusted_peak

NOW = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)


def _leg(direction, right, strike, contracts=1):
    return {"direction": direction, "type": "Put" if right == "P" else "Call", "strike": strike, "contracts": contracts}


def _spread(short, long, credit, contracts=1, ticker="SPX", right="P"):
    width = abs(short - long) * 100 * contracts
    return {
        "ticker": ticker,
        "structure_type": "Bull Put Spread",
        "risk_profile": "defined",
        "direction": "CREDIT",
        "expiry": "2026-11-20",
        "contracts": contracts,
        "entry_cost": -credit,
        "max_risk": width - credit,
        "legs": [_leg("LONG", right, long, contracts), _leg("SHORT", right, short, contracts)],
    }


def _payload(positions, nlv=100_000.0, excess=20_000.0):
    return {"positions": positions, "account_summary": {"net_liquidation": nlv, "excess_liquidity": excess}}


def test_leverage_width_and_cushion():
    p = _payload([_spread(7500, 7200, 6_000.0), _spread(7400, 7100, 5_000.0, contracts=2)])
    v = account_view(p, NOW - timedelta(seconds=40), NOW, {"SPX": 7600.0})
    assert v.max_loss == pytest.approx(24_000 + 55_000)
    assert v.leverage == pytest.approx(0.79)
    assert v.width == pytest.approx(30_000 + 60_000)
    assert v.width_ratio == pytest.approx(0.9)
    assert v.cushion == pytest.approx(0.20)
    assert v.snapshot_age_s == pytest.approx(40.0)


def test_undefined_risk_excluded_and_counted():
    naked = {"ticker": "TSLA", "risk_profile": "undefined", "max_risk": None, "direction": "SHORT", "legs": []}
    v = account_view(_payload([_spread(7500, 7200, 6_000.0), naked]), NOW, NOW, {})
    assert v.max_loss == pytest.approx(24_000) and v.undefined_positions == 1


def test_short_and_long_strike_distances():
    p = _payload([_spread(7500, 7200, 6_000.0), _spread(7650, 7350, 6_000.0)])
    v = account_view(p, NOW, NOW, {"SPX": 7600.0})
    assert v.min_short_distance == pytest.approx((7600 - 7650) / 7600)
    assert v.shorts_crossed == 1
    assert v.long_itm is False
    v2 = account_view(p, NOW, NOW, {"SPX": 7300.0})
    assert v2.shorts_crossed == 2 and v2.long_itm is True


def test_call_side_distance_sign():
    p = _payload([_spread(7800, 8000, 4_000.0, right="C")])
    v = account_view(p, NOW, NOW, {"SPX": 7700.0})
    assert v.min_short_distance == pytest.approx((7800 - 7700) / 7700)


def test_no_spot_no_distances():
    v = account_view(_payload([_spread(7500, 7200, 6_000.0)]), NOW, NOW, {})
    assert v.min_short_distance is None and v.shorts_crossed == 0 and v.long_itm is False


def test_long_itm_only_counts_credit_structures():
    debit = _spread(7500, 7600, 3_000.0)
    debit["direction"] = "DEBIT"
    v = account_view(_payload([debit]), NOW, NOW, {"SPX": 7000.0})
    assert v.long_itm is False


def test_missing_nlv_gives_none_ratios():
    p = {"positions": [], "account_summary": {"net_liquidation": None, "excess_liquidity": 5.0}}
    v = account_view(p, NOW, NOW, {})
    assert v.leverage is None and v.cushion is None and v.width_ratio is None


def test_flow_adjusted_peak():
    navs = [(date(2026, 10, 1), 100.0), (date(2026, 10, 2), 120.0), (date(2026, 10, 3), 110.0)]
    assert flow_adjusted_peak(navs, []) == 120.0
    # A 50 deposit after the 120 peak raises the bar to 170 (the deposit is not a gain).
    assert flow_adjusted_peak(navs, [(date(2026, 10, 3), 50.0)]) == 170.0
    # A withdrawal after the peak lowers it.
    assert flow_adjusted_peak(navs, [(date(2026, 10, 3), -30.0)]) == 110.0
    # A flow on date t is inside the close of t, so it lifts only earlier closes:
    # 100 on 10-01 plus the 50 deposited on 10-02 = 150 (the 120 close then holds a loss).
    assert flow_adjusted_peak(navs, [(date(2026, 10, 2), 50.0)]) == 150.0
    assert flow_adjusted_peak([], []) is None


def test_drawdown():
    assert drawdown(90.0, 100.0) == pytest.approx(-0.10)
    assert drawdown(None, 100.0) is None
    assert drawdown(90.0, None) is None
    assert drawdown(90.0, 0.0) is None
