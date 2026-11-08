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


def test_unbounded_risk_excluded_and_counted():
    naked_call = {
        "ticker": "TSLA",
        "risk_profile": "undefined",
        "max_risk": None,
        "direction": "SHORT",
        "entry_cost": -500.0,
        "legs": [_leg("SHORT", "C", 500)],
    }
    v = account_view(_payload([_spread(7500, 7200, 6_000.0), naked_call]), NOW, NOW, {})
    assert v.max_loss == pytest.approx(24_000) and v.undefined_positions == 1


def test_same_expiry_spreads_collapsed_to_one_combo():
    # ib_sync groups by (ticker, expiry): two put spreads in one expiry arrive as one
    # "Combo (4 legs)" / complex row with max_risk None (review finding 2026-11-08).
    combo = {
        "ticker": "SPX",
        "structure_type": "Combo (4 legs)",
        "risk_profile": "complex",
        "direction": "SHORT",
        "expiry": "2026-11-27",
        "contracts": 1,
        "entry_cost": -11_000.0,
        "max_risk": None,
        "legs": [_leg("LONG", "P", 7300), _leg("SHORT", "P", 7580), _leg("LONG", "P", 7140), _leg("SHORT", "P", 7430)],
    }
    v = account_view(_payload([combo]), NOW, NOW, {"SPX": 7200.0})
    assert v.max_loss == pytest.approx(28_000 + 29_000 - 11_000)
    assert v.width == pytest.approx(57_000)
    assert v.undefined_positions == 0
    assert v.long_itm is True  # 7300 long put ITM at 7200, net-credit structure


def test_cash_secured_short_put_counts_full_downside():
    short_put = {
        "ticker": "SPY",
        "risk_profile": "undefined",
        "max_risk": None,
        "direction": "SHORT",
        "entry_cost": -300.0,
        "legs": [_leg("SHORT", "P", 600)],
    }
    v = account_view(_payload([short_put]), NOW, NOW, {})
    assert v.max_loss == pytest.approx(60_000 - 300) and v.undefined_positions == 0


def test_long_stock_max_loss_is_cost():
    stock = {
        "ticker": "NVDA",
        "risk_profile": "equity",
        "direction": "LONG",
        "entry_cost": 18_000.0,
        "legs": [{"direction": "LONG", "type": "Stock", "strike": None, "contracts": 100}],
    }
    v = account_view(_payload([stock]), NOW, NOW, {})
    assert v.max_loss == pytest.approx(18_000) and v.width == 0


def test_non_usd_position_converted_with_payload_fx_rates():
    """A TSEJ stock: entry_cost in JPY (shape of the 2026-11-06 live snapshot; values synthetic)."""
    jp = {
        "ticker": "6981",
        "currency": "JPY",
        "entry_cost": 1_000_000.0,
        "legs": [{"direction": "LONG", "type": "Stock", "strike": 0.0, "contracts": 100}],
    }
    v = account_view({**_payload([jp]), "fx_rates": {"JPY": 0.0063, "USD": 1.0}}, NOW, NOW, {})
    assert v.max_loss == pytest.approx(6_300) and v.undefined_positions == 0
    v = account_view(_payload([jp]), NOW, NOW, {})  # no rate: not measured, never 1:1
    assert v.max_loss == 0 and v.undefined_positions == 1


def test_covered_call_net_credit_not_clamped_away():
    cc = {
        "ticker": "AAPL",
        "risk_profile": "defined",
        "direction": "LONG",
        "entry_cost": 20_000.0 - 500.0,
        "legs": [{"direction": "LONG", "type": "Stock", "strike": None, "contracts": 100}, _leg("SHORT", "C", 220)],
    }
    v = account_view(_payload([cc]), NOW, NOW, {})
    assert v.max_loss == pytest.approx(19_500) and v.undefined_positions == 0


def test_unreadable_position_counted_not_summed():
    bad = {"ticker": "X", "entry_cost": None, "legs": [_leg("SHORT", "P", None)]}
    v = account_view(_payload([bad]), NOW, NOW, {})
    assert v.max_loss == 0 and v.undefined_positions == 1


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


def test_no_spot_no_distances_but_counted():
    v = account_view(_payload([_spread(7500, 7200, 6_000.0)]), NOW, NOW, {})
    assert v.min_short_distance is None and v.shorts_crossed == 0 and v.long_itm is False
    assert v.positions_without_spot == 1


def test_long_itm_only_counts_credit_structures():
    debit = _spread(7500, 7600, 3_000.0)
    debit["direction"] = "DEBIT"
    debit["entry_cost"] = 3_000.0
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
