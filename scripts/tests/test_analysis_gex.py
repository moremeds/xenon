from datetime import date
from xenon.analysis.gex import (
    detect_flip_point,
    rank_walls,
    detect_pinning,
    is_opex_week,
    extract_call_wall,
    extract_put_wall,
    compute_gamma_per_1pct,
)


def test_detect_flip_point_finds_sign_change():
    strikes = [
        {"strike": 90, "gamma": -1.0},
        {"strike": 95, "gamma": -0.5},
        {"strike": 100, "gamma": 0.3},
        {"strike": 105, "gamma": 1.2},
    ]
    flip = detect_flip_point(strikes)
    assert flip is not None
    assert 95 <= flip <= 100


def test_detect_flip_point_none_when_all_positive():
    strikes = [{"strike": 100, "gamma": 1.0}, {"strike": 105, "gamma": 2.0}]
    assert detect_flip_point(strikes) is None


def test_rank_walls_returns_top_absolute_gamma():
    strikes = [
        {"strike": 100, "gamma": 0.5},
        {"strike": 105, "gamma": -2.0},
        {"strike": 110, "gamma": 1.8},
        {"strike": 115, "gamma": 0.1},
    ]
    walls = rank_walls(strikes, top_n=2)
    assert len(walls) == 2
    assert walls[0]["strike"] == 105
    assert walls[1]["strike"] == 110


def test_detect_pinning_flags_near_wall_in_opex_week():
    strikes = [{"strike": 100, "gamma": 5.0}, {"strike": 105, "gamma": 0.1}]
    result = detect_pinning(strikes, price=100.5, opex_week=True, min_gamma=1.0)
    assert result is not None
    assert result["pin_strike"] == 100


def test_detect_pinning_none_outside_opex_week():
    strikes = [{"strike": 100, "gamma": 5.0}]
    assert detect_pinning(strikes, price=100.5, opex_week=False, min_gamma=1.0) is None


def test_extract_call_wall_picks_top_positive_gamma():
    strikes = [
        {"strike": 100, "gamma": 0.5},
        {"strike": 105, "gamma": -2.0},  # bigger |gamma| but negative — must be ignored
        {"strike": 110, "gamma": 1.8},
        {"strike": 115, "gamma": 0.1},
    ]
    wall = extract_call_wall(strikes)
    assert wall == {"strike": 110.0, "gamma": 1.8}


def test_extract_call_wall_none_when_no_positive():
    strikes = [{"strike": 100, "gamma": -0.5}, {"strike": 105, "gamma": -1.0}]
    assert extract_call_wall(strikes) is None


def test_extract_put_wall_picks_top_negative_gamma():
    strikes = [
        {"strike": 100, "gamma": 0.5},
        {"strike": 95, "gamma": -2.0},
        {"strike": 90, "gamma": -1.5},
    ]
    wall = extract_put_wall(strikes)
    assert wall == {"strike": 95.0, "gamma": -2.0}


def test_extract_put_wall_none_when_no_negative():
    strikes = [{"strike": 100, "gamma": 0.5}]
    assert extract_put_wall(strikes) is None


def test_compute_gamma_per_1pct_sums_within_band():
    strikes = [
        {"strike": 99.5, "gamma": 1.0},   # within ±1% of 100
        {"strike": 100.5, "gamma": -2.0}, # within band, abs counts
        {"strike": 102.0, "gamma": 5.0},  # outside (>1% away)
    ]
    assert compute_gamma_per_1pct(strikes, price=100.0) == 3.0


def test_compute_gamma_per_1pct_none_when_price_missing_or_no_strikes_in_band():
    assert compute_gamma_per_1pct([{"strike": 100, "gamma": 1.0}], price=None) is None
    assert compute_gamma_per_1pct([{"strike": 100, "gamma": 1.0}], price=0) is None
    far = [{"strike": 200, "gamma": 5.0}]
    assert compute_gamma_per_1pct(far, price=100.0) is None


def test_is_opex_week_third_friday_and_three_days_before():
    assert is_opex_week(date(2026, 4, 17)) is True
    assert is_opex_week(date(2026, 4, 15)) is True
    assert is_opex_week(date(2026, 4, 14)) is True
    assert is_opex_week(date(2026, 4, 13)) is False
    assert is_opex_week(date(2026, 4, 20)) is False
