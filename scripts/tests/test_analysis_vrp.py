from datetime import datetime
from scripts.analysis.models import TickerData
from scripts.analysis.vrp import build_vrp_state, classify_regime


def _td(**kwargs):
    defaults = dict(
        ticker="X", price=100.0, fetched_at=datetime.now(),
        gex=None, gex_by_strike=None,
        iv=None, rv=None, iv_percentile=None, term_structure=None,
        rr_skew_25d=None, vrp_history=None,
        flow_alerts=None, net_premium=None, pcr=None, darkpool=None,
        oi_changes=None, short_interest=None,
        earnings_date=None, earnings_within_14d=False,
    )
    defaults.update(kwargs)
    return TickerData(**defaults)


def test_vrp_raw_when_iv_and_rv_present():
    td = _td(iv=30.0, rv=22.0)
    s = build_vrp_state(td)
    assert s.vrp_raw == 8.0


def test_vrp_raw_none_when_iv_missing():
    td = _td(iv=None, rv=22.0)
    s = build_vrp_state(td)
    assert s.vrp_raw is None


def test_vrp_zscore_none_when_history_missing():
    td = _td(iv=30.0, rv=22.0, vrp_history=None)
    s = build_vrp_state(td)
    assert s.vrp_zscore is None


def test_vrp_zscore_computed_from_history():
    history = [0.0] * 250 + [8.0]
    td = _td(iv=30.0, rv=22.0, vrp_history=history)
    s = build_vrp_state(td)
    assert s.vrp_zscore is not None
    assert s.vrp_zscore > 5


def test_ts_ratio_from_term_structure():
    term = [
        {"dte": 14, "iv": "0.30"},
        {"dte": 60, "iv": "0.28"},
        {"dte": 90, "iv": "0.27"},
    ]
    td = _td(iv=30.0, rv=22.0, term_structure=term)
    s = build_vrp_state(td)
    assert s.ts_ratio is not None
    assert abs(s.ts_ratio - (0.30 / 0.27)) < 1e-6
    assert s.ts_inverted is True


def test_ts_ratio_none_with_single_expiry():
    td = _td(iv=30.0, rv=22.0, term_structure=[{"dte": 30, "iv": "0.3"}])
    s = build_vrp_state(td)
    assert s.ts_ratio is None
    assert s.ts_inverted is None


def test_regime_r2_when_ts_inverted_and_vrp_negative():
    td = _td(iv=30.0, rv=35.0, gex={"net": -1e9},
             term_structure=[{"dte": 14, "iv": "0.35"}, {"dte": 90, "iv": "0.30"}])
    vrp = build_vrp_state(td)
    from dataclasses import replace
    vrp = replace(vrp, vrp_zscore=-1.0)
    r = classify_regime(td, vrp)
    assert r.regime == "R2"


def test_regime_r1_default_when_signals_mixed():
    td = _td(iv=30.0, rv=22.0, gex={"net": 1e9},
             term_structure=[{"dte": 14, "iv": "0.30"}, {"dte": 90, "iv": "0.29"}])
    vrp = build_vrp_state(td)
    r = classify_regime(td, vrp)
    assert r.regime in ("R0", "R1")


def test_regime_r0_requires_positive_gex_and_elevated_vrp():
    td = _td(iv=30.0, rv=22.0, price=100.0,
             gex={"net": 1e9, "flip": 95.0},
             term_structure=[{"dte": 14, "iv": "0.30"}, {"dte": 90, "iv": "0.31"}])
    vrp = build_vrp_state(td)
    from dataclasses import replace
    vrp = replace(vrp, vrp_zscore=1.2)
    r = classify_regime(td, vrp)
    assert r.regime == "R0"
    assert r.gex_flip_relative == "below_price"
    assert r.flip_distance_pct == 5.0


def test_regime_flip_distance_is_magnitude_not_signed():
    td_above = _td(iv=30.0, rv=22.0, price=100.0, gex={"net": -1e9, "flip": 103.0})
    td_below = _td(iv=30.0, rv=22.0, price=100.0, gex={"net": -1e9, "flip": 97.0})
    vrp_above = build_vrp_state(td_above)
    vrp_below = build_vrp_state(td_below)
    r_above = classify_regime(td_above, vrp_above)
    r_below = classify_regime(td_below, vrp_below)
    assert r_above.flip_distance_pct == 3.0
    assert r_below.flip_distance_pct == 3.0
    assert r_above.gex_flip_relative == "above_price"
    assert r_below.gex_flip_relative == "below_price"


def test_regime_r2_on_negative_gex_with_flip_below_price_beyond_2pct():
    td = _td(iv=30.0, rv=22.0, price=100.0,
             gex={"net": -5e9, "flip": 93.0},
             term_structure=None)
    vrp = build_vrp_state(td)
    r = classify_regime(td, vrp)
    assert r.regime == "R2"


def test_regime_biases_toward_r1_when_vrp_unknown():
    td = _td(iv=30.0, rv=22.0, price=100.0,
             gex={"net": 1e9, "flip": 95.0},
             term_structure=[{"dte": 14, "iv": "0.30"}, {"dte": 90, "iv": "0.31"}])
    vrp = build_vrp_state(td)
    r = classify_regime(td, vrp)
    assert r.regime != "R0"
